"""HTTP for the campaign write APIs: every call audited, and a create call never retried blindly.

A create that fails can mean two things: the network refused it (nothing exists), or the answer was lost after the
network acted (the object may exist). Only the first is retried:

* HTTP 429 and the networks' throttling codes: the request was refused, retried with backoff;
* a connection that was never established (connect timeout, connection refused): nothing was sent, retried;
* a read timeout, a dropped connection or a 5xx on a write: ``AmbiguousWrite``. The engine marks the object
  ``unknown`` and stops; the next run looks the object up by its idempotency key before creating anything.

Reads (GET, searches) are retried on 5xx and timeouts too, since repeating them changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from creative_forge.campaigns.audit import AuditLog, scrub
from creative_forge.perf.http import Redactor
from creative_forge.retry import RetryPolicy

OK, THROTTLE, SERVER, ERROR = "ok", "throttle", "server", "error"
SECRET_PARAMS = ("access_token", "api_key", "key", "token")


class NetworkError(RuntimeError):
    """The network refused the request. The message never holds a credential."""


class AmbiguousWrite(RuntimeError):
    """A write may or may not have taken effect; the object is looked up before any new attempt."""


@dataclass(frozen=True)
class Upload:
    field: str
    path: Path
    content_type: str = "application/octet-stream"

    def describe(self) -> dict[str, Any]:
        h = hashlib.sha256()
        with open(self.path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {"file": self.path.name, "bytes": self.path.stat().st_size, "sha256": h.hexdigest()}


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str]


Classifier = Callable[[int, Any], tuple[str, str]]  # (outcome, message)


def default_classify(status: int, body: Any) -> tuple[str, str]:
    if 200 <= status < 300:
        return OK, ""
    if status == 429:
        return THROTTLE, f"HTTP {status}"
    if status >= 500:
        return SERVER, f"HTTP {status}"
    return ERROR, f"HTTP {status}: {str(body)[:300]}"


def _never_sent(e: requests.RequestException) -> bool:
    """True only when the request certainly did not reach the server."""
    if isinstance(e, requests.ConnectTimeout):
        return True
    text = repr(e.args)
    return isinstance(e, requests.ConnectionError) and (
        "NewConnectionError" in text or "Connection refused" in text or "Failed to establish" in text
    )


def safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [
        (k, "***" if k.lower() in SECRET_PARAMS else v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class WriteClient:
    def __init__(
        self,
        network: str,
        mode: str,
        audit: AuditLog,
        redactor: Redactor,
        *,
        session: requests.Session | None = None,
        classify: Classifier = default_classify,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.network = network
        self.mode = mode
        self.audit = audit
        self.redactor = redactor
        self.session = session or requests.Session()
        self.classify = classify
        self.timeout_s = timeout_s
        self.retry = retry or RetryPolicy(attempts=5, base_delay_s=1.0, max_delay_s=60.0)
        self.sleep = sleep
        self._log = log
        self.calls = 0

    def log(self, msg: str) -> None:
        if self._log:
            self._log(self.redactor.clean(msg))

    def _audit(self, **fields: Any) -> None:
        self.audit.append("api_call", network=self.network, mode=self.mode, **fields)

    def call(
        self,
        method: str,
        url: str,
        *,
        write: bool,
        op: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | bytes | None = None,
        upload: Upload | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """One logical call with the retry rules of the module docstring. Every attempt is one audit record."""
        logged_request: Any = json_body if json_body is not None else (data if isinstance(data, dict) else None)
        if isinstance(data, bytes):
            logged_request = {"bytes": len(data)}
        file_info = upload.describe() if upload else None
        if file_info:
            logged_request = {**(logged_request or {}), "upload": file_info}
        logged_request = json.loads(self.redactor.clean(json.dumps(scrub(logged_request), default=str)))
        shown_url = self.redactor.clean(safe_url(url))
        logged_params = json.loads(self.redactor.clean(json.dumps(scrub(params or {}), default=str)))
        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            started = time.monotonic()
            failure: str | None = None
            sent_for_sure = False
            files = None
            fh = None
            try:
                if upload is not None:
                    fh = open(upload.path, "rb")  # noqa: SIM115 - closed in finally
                    files = {upload.field: (upload.path.name, fh, upload.content_type)}
                r = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                    headers=headers or {},
                    timeout=self.timeout_s,
                )
            except requests.RequestException as e:
                # str(e) can hold the URL; only the class survives
                failure = type(e).__name__
                sent_for_sure = not _never_sent(e)
                r = None
            finally:
                if fh is not None:
                    fh.close()
            ms = round((time.monotonic() - started) * 1000)
            if r is None:
                outcome = SERVER if sent_for_sure else THROTTLE
                self._audit(
                    op=op,
                    method=method,
                    url=shown_url,
                    params=logged_params,
                    request=logged_request,
                    attempt=attempt,
                    status=None,
                    error=failure,
                    ms=ms,
                )
                if write and sent_for_sure:
                    raise AmbiguousWrite(
                        f"{self.network}: {failure} after the request was sent; the object may exist"
                    ) from None
                message = f"{failure} contacting {urllib.parse.urlsplit(url).hostname}"
            else:
                try:
                    body: Any = r.json()
                except ValueError:
                    body = r.text[:2000]
                outcome, message = self.classify(r.status_code, body)
                trimmed = self.redactor.clean(json.dumps(scrub(body), default=str, ensure_ascii=False))[:4000]
                self._audit(
                    op=op,
                    method=method,
                    url=shown_url,
                    params=logged_params,
                    request=logged_request,
                    attempt=attempt,
                    status=r.status_code,
                    outcome=outcome,
                    response=trimmed,
                    ms=ms,
                )
                if outcome == OK:
                    return Response(r.status_code, body, {k.lower(): v for k, v in r.headers.items()})
                if outcome == ERROR:
                    raise NetworkError(f"{self.network}: {self.redactor.clean(message)}")
                if outcome == SERVER and write:
                    raise AmbiguousWrite(f"{self.network}: {self.redactor.clean(message)} on a write; it may exist")
            if attempt >= self.retry.attempts:
                raise NetworkError(f"{self.network}: gave up after {attempt} attempts: {self.redactor.clean(message)}")
            wait = self.retry.delay(attempt - 1)
            self.log(f"{self.network}: {self.redactor.clean(message)}; retry {attempt} in {wait:.1f} s")
            self.sleep(wait)
