"""Meta Marketing API Insights at ad level, one row per ad per day (optionally per country).

``GET https://graph.facebook.com/{version}/act_{account}/insights`` with ``level=ad``, ``time_increment=1`` and the
fields below. What is stored, per Meta's documentation:

* ``actions[video_view]`` is officially "3-Second Video Views" (Ads Action Stats reference), the numerator of hook
  rate; ``video_p25/50/75/95/100_watched_actions`` and ``video_thruplay_watched_actions`` are lists of
  ``{action_type: video_view, value}``.
* Installs: the first of ``mobile_app_install``, ``app_install``, ``omni_app_install`` found in ``actions`` (they
  overlap, so they are never summed).

The token is sent as an ``Authorization: Bearer`` header, never as a query parameter, and the ``access_token``
Meta embeds in ``paging.next`` links is stripped before the next page is requested. Throttling (error codes 4, 17,
32, 613 and 80000-80014, HTTP 429/5xx) is retried with backoff; when ``x-fb-ads-insights-throttle`` reports 75 %
or more of a quota used, the next page waits.

Sources: https://developers.facebook.com/docs/marketing-api/insights/breakdowns,
https://developers.facebook.com/docs/marketing-api/reference/ads-action-stats/ (``video_view`` label checked
2026-11-11). Tested only against recorded-shape fixtures: no token was available, no live call was made.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests

from creative_forge.perf.http import ApiClient, PerfApiError, Redactor, num, strip_query_param
from creative_forge.perf.store import PerfRow
from creative_forge.retry import RetryPolicy, TransientError

HOST = "graph.facebook.com"
DEFAULT_VERSION = "v27.0"
FIELDS = (
    "ad_id,ad_name,adset_name,campaign_name,account_currency,impressions,clicks,spend,actions,"
    "video_p25_watched_actions,video_p50_watched_actions,video_p75_watched_actions,video_p95_watched_actions,"
    "video_p100_watched_actions,video_thruplay_watched_actions"
)
INSTALL_ACTIONS = ("mobile_app_install", "app_install", "omni_app_install")
THROTTLE_CODES = {4, 17, 32, 613} | set(range(80000, 80015))
TRANSIENT_CODES = {1, 2}


def _action(actions: list[dict[str, Any]] | None, kind: str) -> float | None:
    for a in actions or []:
        if a.get("action_type") == kind:
            return num(a.get("value"))
    return None


def _video(actions: list[dict[str, Any]] | None) -> float | None:
    """The ``video_pXX_watched_actions`` lists hold one ``video_view`` entry."""
    return _action(actions, "video_view")


def to_row(d: dict[str, Any], account: str, source: str = "meta-insights") -> PerfRow:
    installs = next((v for k in INSTALL_ACTIONS if (v := _action(d.get("actions"), k)) is not None), None)
    return PerfRow(
        network="meta",
        account=account,
        level="ad",
        date=d["date_start"],
        date_end=d.get("date_stop") or d["date_start"],
        ad_key=str(d["ad_id"]),
        ad_name=d.get("ad_name"),
        country=d.get("country", "") or "",
        impressions=num(d.get("impressions")),
        clicks=num(d.get("clicks")),
        spend=num(d.get("spend")),
        installs=installs,
        video_3s=_action(d.get("actions"), "video_view"),
        p25=_video(d.get("video_p25_watched_actions")),
        p50=_video(d.get("video_p50_watched_actions")),
        p75=_video(d.get("video_p75_watched_actions")),
        p95=_video(d.get("video_p95_watched_actions")),
        p100=_video(d.get("video_p100_watched_actions")),
        thruplay=_video(d.get("video_thruplay_watched_actions")),
        currency=d.get("account_currency"),
        source=source,
    )


class MetaClient(ApiClient):
    def classify(self, r: requests.Response, body: Any) -> None:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = err.get("code")
        if code in THROTTLE_CODES or code in TRANSIENT_CODES or r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"HTTP {r.status_code}, error code {code}: {err.get('message', '')[:200]}")
        msg = err.get("message") or str(body)[:300]
        raise PerfApiError(
            f"{HOST}: HTTP {r.status_code}, error code {code} ({err.get('type', '')}): {self.redactor.clean(msg)}"
        )


class MetaInsights:
    def __init__(
        self,
        token: str,
        account: str,
        *,
        version: str = DEFAULT_VERSION,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy | None = None,
        log: Callable[[str], None] | None = None,
        page_size: int = 500,
    ) -> None:
        self.account = account if account.startswith("act_") else f"act_{account}"
        if not self.account[4:].isdigit():
            raise ValueError(f"Meta ad account id must be digits (act_123...), got {account!r}")
        self.version = version
        self.page_size = page_size
        self._token = token
        self.client = MetaClient(HOST, Redactor(token), session, sleep=sleep, retry=retry, log=log)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def fetch(self, since: str, until: str, *, by_country: bool = False) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] | None = {
            "level": "ad",
            "fields": FIELDS,
            "time_range": json.dumps({"since": since, "until": until}, separators=(",", ":")),
            "time_increment": 1,
            "limit": self.page_size,
        }
        if by_country:
            params["breakdowns"] = "country"
        url = f"https://{HOST}/{self.version}/{self.account}/insights"
        self.pages = 0
        while url:
            r, body = self.client.get(url, params, self._headers())
            self.pages += 1
            yield from body.get("data", [])
            nxt = (body.get("paging") or {}).get("next")
            url = strip_query_param(nxt, "access_token") if nxt else None
            params = None  # the next link carries every parameter (and the cursor)
            if url:
                self._pace(r)

    def _pace(self, r: requests.Response) -> None:
        raw = r.headers.get("x-fb-ads-insights-throttle") if hasattr(r, "headers") else None
        try:
            t = json.loads(raw) if raw else {}
        except ValueError:
            return
        used = max(float(t.get("app_id_util_pct", 0) or 0), float(t.get("acc_id_util_pct", 0) or 0))
        if used >= 75:
            wait = 5.0 if used < 90 else 30.0
            self.client.log(f"{HOST}: insights quota {used:.0f}% used; pausing {wait:.0f} s before the next page")
            self.client.sleep(wait)

    def rows(self, since: str, until: str, *, by_country: bool = False) -> Iterator[PerfRow]:
        for d in self.fetch(since, until, by_country=by_country):
            yield to_row(d, self.account)
