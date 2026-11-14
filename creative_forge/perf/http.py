"""HTTP for the reporting APIs: retries, rate limits, pagination helpers, and secrets that never leave this module.

Tokens come only from environment variables. Meta's token travels in an ``Authorization`` header; AppLovin's API
takes its Report Key only as the ``api_key`` query parameter, and ``requests`` puts the full URL into its exception
messages. So every message that can reach a log, an exception or the terminal goes through ``Redactor.clean``, and
transport errors are re-raised ``from None``: the original exception, whose text holds the URL, is not chained into
the traceback.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import requests

from creative_forge.config import ConfigError
from creative_forge.retry import RetryPolicy, TransientError, with_retry

ENV_META_TOKEN = "CREATIVE_FORGE_META_TOKEN"
ENV_APPLOVIN_KEY = "CREATIVE_FORGE_APPLOVIN_KEY"


class PerfApiError(RuntimeError):
    """A reporting API refused or failed. The message never contains a token."""


def require_env(name: str, what: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{what} needs {name} in the environment (never in a file or on the command line)")
    return value


class Redactor:
    def __init__(self, *secrets: str) -> None:
        forms: set[str] = set()
        for s in secrets:
            if s:
                forms |= {s, urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s)}
        self._forms = sorted((f for f in forms if len(f) >= 4), key=len, reverse=True)

    def clean(self, text: object) -> str:
        out = str(text)
        for f in self._forms:
            out = out.replace(f, "***")
        return out


def strip_query_param(url: str, name: str) -> str:
    """Remove ``name`` from a URL's query (Meta's ``paging.next`` links embed the access token)."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != name]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class ApiClient:
    """GET JSON with retries on 429 / 5xx / connection errors, honouring ``Retry-After``."""

    def __init__(
        self,
        host: str,
        redactor: Redactor,
        session: requests.Session | None = None,
        *,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.redactor = redactor
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.retry = retry or RetryPolicy(attempts=6, base_delay_s=2.0, max_delay_s=120.0)
        self.sleep = sleep
        self._log = log
        self.requests = 0

    def log(self, msg: str) -> None:
        if self._log:
            self._log(self.redactor.clean(msg))

    def classify(self, r: requests.Response, body: Any) -> None:
        """Raise ``TransientError`` (retried) or ``PerfApiError`` for a non-success response. Subclasses add the
        network's own throttling codes."""
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"HTTP {r.status_code}")
        raise PerfApiError(f"{self.host}: HTTP {r.status_code}: {self.redactor.clean(str(body)[:500])}")

    def check_url(self, url: str) -> None:
        """Only https to this client's own host: a pagination link in a response (Meta's ``paging.next``) is data,
        and the token in the headers must never follow it anywhere else."""
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        if (
            parts.scheme != "https"
            or (parts.hostname or "").lower() != self.host.lower()
            or port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise PerfApiError(f"{self.host}: refused a request to {parts.scheme}://{parts.hostname} (not {self.host})")

    def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        """``(response, parsed JSON body)`` of a successful GET, after retries."""
        self.check_url(url)

        def once() -> Any:
            self.requests += 1
            failure: str | None = None
            try:
                r = self.session.get(url, params=params, headers=headers or {}, timeout=self.timeout_s)
            except requests.RequestException as e:
                # str(e) holds the request URL (with AppLovin's api_key); only the class name and host survive
                failure = f"{type(e).__name__} contacting {self.host}"
            if failure is not None:  # raised outside the except block: no __context__ points at the original
                raise TransientError(failure)
            try:
                body: Any = r.json()
            except ValueError:
                body = r.text[:500]
            if r.status_code == 200 and not (isinstance(body, dict) and "error" in body):
                return r, body
            wait = _retry_after(r)
            try:
                self.classify(r, body)
            except TransientError as e:
                if wait:
                    self.log(f"{self.host}: throttled ({e}); waiting {wait:.0f} s as the server asked")
                    self.sleep(wait)
                raise
            raise PerfApiError(f"{self.host}: HTTP {r.status_code}")  # classify() must raise

        def on_retry(attempt: int, e: BaseException) -> None:
            self.log(f"{self.host}: retry {attempt} after {self.redactor.clean(e)}")

        gave_up: str | None = None
        try:
            return with_retry(once, self.retry, sleep=self.sleep, on_retry=on_retry)
        except TransientError as e:
            gave_up = f"{self.host}: gave up after {self.retry.attempts} attempts: {self.redactor.clean(e)}"
        raise PerfApiError(gave_up)


def _retry_after(r: requests.Response) -> float | None:
    v = r.headers.get("Retry-After") if hasattr(r, "headers") else None
    try:
        return min(float(v), 300.0) if v else None
    except ValueError:
        return None


def num(v: Any) -> float | None:
    """Numbers as networks send them: 123, "123", "1,234.5", "$3.20", "12%", "-", ""."""
    if v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    if s in ("", "-", "--", "N/A", "n/a", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
