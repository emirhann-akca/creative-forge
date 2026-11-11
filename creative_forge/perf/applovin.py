"""AppLovin Reporting API (creative sets) and Asset Reporting API (assets inside a creative set).

* ``GET https://r.applovin.com/report`` with ``report_type=advertiser``, ``format=json``, ``start``/``end`` and the
  columns below; ``conversions`` is installs. Paginated with ``limit`` and ``offset``. Dates must lie within the
  last 45 days.
* ``GET https://r.applovin.com/assetAnalyticsReport`` with ``start``/``end``: per asset impressions, clicks, ctr and
  cost over the window. Assets have **no installs**; each row carries its ``creative_set_id``, which is how
  ``PerfStore.link_assets()`` joins it to the creative set's creative ID.

The Report Key can only be sent as the ``api_key`` query parameter, so it is part of every request URL. It is kept
out of logs and exceptions by the ``Redactor`` and by re-raising transport errors without their URL.

Sources (read 2026-11-11): https://support.applovin.com/en/app-discovery/api/reporting-api and
https://support.applovin.com/en/app-discovery/api/asset-reporting-api. Neither page documents the JSON response
shape; this client accepts ``{"results": [...]}`` and a bare list. Tested only against fixtures: no Report Key was
available, no live call was made.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any

import requests

from creative_forge.perf.http import ApiClient, PerfApiError, Redactor, num
from creative_forge.perf.store import PerfRow
from creative_forge.retry import RetryPolicy

HOST = "r.applovin.com"
REPORT_COLUMNS = "day,campaign,creative_set,creative_set_id,country,impressions,clicks,conversions,cost"
ASSET_COLUMNS = "asset_id,asset_name,creative_set,creative_set_id,campaign,impressions,clicks,cost"
WINDOW_DAYS = 45


def _results(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        rows = body.get("results")
        if rows is None:
            rows = body.get("data")
        if isinstance(rows, list):
            return rows
    shape = list(body)[:8] if isinstance(body, dict) else type(body).__name__
    raise PerfApiError(f"{HOST}: unexpected response shape ({shape})")


class AppLovinReports:
    def __init__(
        self,
        api_key: str,
        *,
        account: str = "default",
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy | None = None,
        log: Callable[[str], None] | None = None,
        page_size: int = 1000,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.account = account
        self.page_size = page_size
        self._key = api_key
        self.today = today
        self.client = ApiClient(HOST, Redactor(api_key), session, sleep=sleep, retry=retry, log=log)
        self.pages = 0

    def _check_window(self, since: str, until: str) -> None:
        s, u = date.fromisoformat(since), date.fromisoformat(until)
        if u < s:
            raise ValueError(f"--until {until} is before --since {since}")
        oldest = self.today() - timedelta(days=WINDOW_DAYS)
        if s < oldest:
            raise ValueError(f"AppLovin reports only the last {WINDOW_DAYS} days: --since must be {oldest} or later")

    def _pages(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            q = {**params, "api_key": self._key, "format": "json", "limit": self.page_size, "offset": offset}
            _r, body = self.client.get(f"https://{HOST}{path}", q)
            self.pages += 1
            rows = _results(body)
            yield from rows
            if len(rows) < self.page_size:
                return
            offset += self.page_size

    def creative_sets(self, since: str, until: str) -> Iterator[PerfRow]:
        self._check_window(since, until)
        params = {"report_type": "advertiser", "start": since, "end": until, "columns": REPORT_COLUMNS}
        for d in self._pages("/report", params):
            name = d.get("creative_set") or ""
            yield PerfRow(
                network="applovin",
                account=self.account,
                level="creative_set",
                date=str(d.get("day") or since)[:10],
                ad_key=str(d.get("creative_set_id") or name),
                ad_name=name or None,
                country=(d.get("country") or "").upper(),
                impressions=num(d.get("impressions")),
                clicks=num(d.get("clicks")),
                spend=num(d.get("cost")),
                installs=num(d.get("conversions")),
                source="applovin-report",
            )

    def assets(self, since: str, until: str) -> Iterator[PerfRow]:
        self._check_window(since, until)
        params = {"start": since, "end": until, "columns": ASSET_COLUMNS}
        for d in self._pages("/assetAnalyticsReport", params):
            yield PerfRow(
                network="applovin",
                account=self.account,
                level="asset",
                date=since,
                date_end=until,
                ad_key=str(d.get("asset_id") or d.get("asset_name")),
                ad_name=d.get("asset_name"),
                parent_key=str(d["creative_set_id"]) if d.get("creative_set_id") else None,
                impressions=num(d.get("impressions")),
                clicks=num(d.get("clicks")),
                spend=num(d.get("cost")),
                installs=None,  # the Asset Reporting API has no installs
                source="applovin-asset-report",
            )
