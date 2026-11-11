"""`forge perf import`: Meta, AppLovin and CSV into SQLite, from recorded-shape fixtures. No live calls, no tokens."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from creative_forge.cli import main
from creative_forge.perf.applovin import AppLovinReports
from creative_forge.perf.csvimport import CsvImportError, read
from creative_forge.perf.http import ENV_META_TOKEN, PerfApiError, Redactor, strip_query_param
from creative_forge.perf.meta import MetaInsights
from creative_forge.perf.store import ImportSummary, PerfStore
from creative_forge.retry import RetryPolicy

FIX = Path(__file__).parent / "fixtures" / "perf"
TOKEN = "EAAtestTOKEN0123456789abcdefSECRET"
KEY = "al-report-key-7f3c9e-SECRET"
A = "cf1.puzzle-ads.dust-blower.blower.lvl7.tryit.9x16.en.e252faa195"
C = "cf1.puzzle-ads.oops.wrong-move.lvl7.tryit.9x16.en.fc972ce8b2"
FAST = RetryPolicy(attempts=4, base_delay_s=0.0, max_delay_s=0.0, jitter=0.0)


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8").replace("__TOKEN__", TOKEN))


class Resp:
    def __init__(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._payload, self.status_code, self.headers = payload, status, headers or {}
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class Session:
    """Answers GETs from a script of responses (or exceptions) and records every request."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params=None, headers=None, timeout=None) -> Resp:
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def meta(session: Session, logs: list[str] | None = None, sleeps: list[float] | None = None) -> MetaInsights:
    return MetaInsights(
        TOKEN,
        "1234567890",
        session=session,  # type: ignore[arg-type]
        retry=FAST,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        log=(logs.append if logs is not None else None),
    )


def test_meta_insights_paginates_and_maps_the_documented_fields(tmp_path):
    s = Session([Resp(fixture("meta_insights_page1.json")), Resp(fixture("meta_insights_page2.json"))])
    m = meta(s)
    store = PerfStore(tmp_path / "perf.sqlite")
    summary = store.upsert(m.rows("2026-10-20", "2026-10-21"), ImportSummary("meta", m.account, "ad"))
    assert m.pages == 2 and summary.rows == 12 and summary.matched == 10
    assert summary.unmatched == ["Old static banner (manual)"]

    first, second = s.calls
    assert first["url"] == "https://graph.facebook.com/v27.0/act_1234567890/insights"
    assert first["params"]["level"] == "ad" and first["params"]["time_increment"] == 1
    assert json.loads(first["params"]["time_range"]) == {"since": "2026-10-20", "until": "2026-10-21"}
    assert "video_p100_watched_actions" in first["params"]["fields"]
    # the token travels only in the header; the one Meta embeds in paging.next is stripped
    assert first["headers"]["Authorization"] == f"Bearer {TOKEN}" and "access_token" not in first["params"]
    assert TOKEN not in second["url"] and "after" in parse_qs(urlsplit(second["url"]).query)

    (a,) = store.rows(
        "SELECT SUM(impressions) i, SUM(clicks) c, SUM(spend) s, SUM(installs) n, SUM(video_3s) v, SUM(p100) p "
        "FROM perf_rows WHERE creative_id = ?",
        ("e252faa195",),
    )
    # hand-computed from the fixture: 2 days x (60,000 impressions, 900 clicks, $300, 360 installs, 15,000 3 s views)
    assert (a["i"], a["c"], a["s"], a["n"], a["v"], a["p"]) == (120000, 1800, 600.0, 720, 30000, 6000)
    ipm, ctr, cpi, hook = a["n"] / a["i"] * 1000, a["c"] / a["i"], a["s"] / a["n"], a["v"] / a["i"]
    assert (ipm, ctr, round(cpi, 4), hook) == (6.0, 0.015, 0.8333, 0.25)
    # installs come from mobile_app_install, never summed with the overlapping omni_app_install
    (c,) = store.rows(
        "SELECT installs, ad_name FROM perf_rows WHERE creative_id = 'fc972ce8b2' AND date = '2026-10-20'"
    )
    assert c["installs"] == 690 and c["ad_name"].endswith(" - Copy")


def test_reimporting_the_same_range_adds_no_duplicates(tmp_path):
    store = PerfStore(tmp_path / "perf.sqlite")
    for _ in range(2):
        s = Session([Resp(fixture("meta_insights_page1.json")), Resp(fixture("meta_insights_page2.json"))])
        m = meta(s)
        store.upsert(m.rows("2026-10-20", "2026-10-21"), ImportSummary("meta", m.account, "ad"))
    assert store.count() == 12
    assert len(store.rows("SELECT * FROM imports")) == 2  # both imports are logged


def test_meta_rate_limit_is_retried_and_quota_header_slows_paging(tmp_path):
    throttle = {"x-fb-ads-insights-throttle": json.dumps({"app_id_util_pct": 12, "acc_id_util_pct": 81})}
    s = Session(
        [
            Resp(fixture("meta_error_rate_limit.json"), 400),
            requests.ConnectionError(f"Max retries exceeded with url: /insights?access_token={TOKEN}"),
            Resp(fixture("meta_insights_page1.json"), headers=throttle),
            Resp({"error": {"code": 2, "message": "Service temporarily unavailable"}}, 500),
            Resp(fixture("meta_insights_page2.json")),
        ]
    )
    logs: list[str] = []
    sleeps: list[float] = []
    rows = list(meta(s, logs, sleeps).rows("2026-10-20", "2026-10-21"))
    assert len(rows) == 12 and len(s.calls) == 5
    assert 5.0 in sleeps  # 81 % of the account quota used: pause before the next page
    assert any("insights quota 81% used" in line for line in logs)
    assert all(TOKEN not in line for line in logs)


def test_meta_permanent_error_names_the_code_without_the_token():
    s = Session([Resp(fixture("meta_error_token.json"), 400)])
    with pytest.raises(PerfApiError) as e:
        list(meta(s).rows("2026-10-20", "2026-10-21"))
    assert "error code 190" in str(e.value) and TOKEN not in str(e.value) and "***" in str(e.value)
    assert e.value.__cause__ is None
    with pytest.raises(ValueError, match="digits"):
        MetaInsights(TOKEN, "act_12x")


def applovin(session: Session, logs: list[str] | None = None) -> AppLovinReports:
    return AppLovinReports(
        KEY,
        session=session,  # type: ignore[arg-type]
        retry=FAST,
        sleep=lambda _s: None,
        log=(logs.append if logs is not None else None),
        page_size=2,
        today=lambda: date(2026, 11, 1),
    )


def test_applovin_creative_sets_paginate_and_assets_join_their_set(tmp_path):
    pages = [Resp(fixture(f"applovin_report_page{i}.json")) for i in (1, 2, 3)]
    empty = Resp(fixture("applovin_report_page3.json"))
    s = Session([*pages, Resp(fixture("applovin_assets.json")), empty])  # 2 assets = a full page of 2: one more
    al = applovin(s)
    store = PerfStore(tmp_path / "perf.sqlite")
    sets = store.upsert(
        al.creative_sets("2026-10-20", "2026-10-21"), ImportSummary("applovin", "default", "creative_set")
    )
    assert sets.rows == 4 and sets.matched == 4 and al.pages == 3
    assert [c["params"]["offset"] for c in s.calls] == [0, 2, 4]
    q = s.calls[0]["params"]
    assert q["report_type"] == "advertiser" and q["format"] == "json" and "creative_set_id" in q["columns"]
    assets = store.upsert(al.assets("2026-10-20", "2026-10-21"), ImportSummary("applovin", "default", "asset"))
    assert assets.matched == 0  # asset file names are not creative-forge names ...
    assert store.link_assets() == 2  # ... but each joins its creative set's creative ID
    rows = store.rows("SELECT ad_key, creative_id, installs, date, date_end FROM perf_rows WHERE level = 'asset'")
    assert {(r["ad_key"], r["creative_id"]) for r in rows} == {("a-771", "36d3704762"), ("a-772", "1d4e68cf56")}
    assert all(r["installs"] is None and (r["date"], r["date_end"]) == ("2026-10-20", "2026-10-21") for r in rows)
    (t,) = store.rows(
        "SELECT SUM(installs) n, SUM(spend) s FROM perf_rows "
        "WHERE creative_id = '36d3704762' AND level = 'creative_set'"
    )
    assert (t["n"], round(t["s"], 2)) == (415, 310.25)


def test_applovin_key_never_reaches_logs_exceptions_or_the_store(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    leak = requests.ConnectionError(f"HTTPSConnectionPool(host='r.applovin.com'): url: /report?api_key={KEY}&start=x")
    s = Session([leak, Resp({"code": 429}, 429, {"Retry-After": "3"}), leak, leak])
    logs: list[str] = []
    with pytest.raises(PerfApiError) as e:
        list(applovin(s, logs).creative_sets("2026-10-20", "2026-10-21"))
    text = str(e.value) + repr(e.value) + "".join(logs) + caplog.text
    assert KEY not in text and "ConnectionError contacting r.applovin.com" in str(e.value)
    chain, x = [], e.value  # nothing in the exception chain (cause or context) still holds the URL with the key
    while x is not None:
        chain.append(repr(x))
        x = x.__cause__ or x.__context__
    assert len(chain) == 1 and KEY not in "".join(chain)
    assert any("waiting 3 s" in line for line in logs)
    # a successful import stores rows, never the key
    store = PerfStore(tmp_path / "perf.sqlite")
    pages = [Resp(fixture(f"applovin_report_page{i}.json")) for i in (1, 2, 3)]
    store.upsert(
        applovin(Session(pages)).creative_sets("2026-10-20", "2026-10-21"), ImportSummary("applovin", "d", "x")
    )
    assert KEY.encode() not in (tmp_path / "perf.sqlite").read_bytes()


def test_applovin_window_is_checked_before_any_call():
    s = Session([])
    with pytest.raises(ValueError, match="last 45 days"):
        list(applovin(s).creative_sets("2026-09-01", "2026-09-02"))
    assert s.calls == []


def test_redactor_and_query_stripping():
    r = Redactor("a b/c+d")
    assert r.clean("x a b/c+d y a%20b%2Fc%2Bd z a+b%2Fc%2Bd") == "x *** y *** z ***"
    assert (
        strip_query_param("https://h/p?access_token=T&after=X&limit=5", "access_token") == "https://h/p?after=X&limit=5"
    )


def test_csv_tiktok_export_with_bom_and_semicolons(tmp_path):
    rows = list(read(FIX / "tiktok_export.csv", "tiktok"))
    assert len(rows) == 3  # the "Total" line is dropped
    a = next(r for r in rows if r.creative_id == "e252faa195")
    assert (a.date, a.impressions, a.clicks, a.spend, a.installs) == ("2026-10-20", 30000, 300, 150.0, 100)
    assert (a.video_2s, a.video_6s, a.video_3s, a.p25, a.p100) == (9000, 4000, None, 5000, 1200)  # 2 s is not 3 s
    assert rows[2].creative_id is None and rows[2].ad_name == "Spark ad - creator 12"


def test_csv_google_export_and_column_overrides(tmp_path):
    rows = list(read(FIX / "google_ads_export.csv", "google"))
    assert [(r.date, r.impressions, r.spend, r.installs) for r in rows] == [
        ("2026-10-20", 12500, 61.2, 48),
        ("2026-10-21", 13100, 63.0, 51),
    ]
    odd = tmp_path / "odd.csv"
    odd.write_text("When,Creative label,Views served,Downloads\n2026/10/20,x,100,2\n", encoding="utf-8")
    with pytest.raises(CsvImportError, match="no column for date, ad_name, impressions"):
        list(read(odd, "other"))
    mapped = list(
        read(
            odd,
            "other",
            overrides={
                "date": "When",
                "ad_name": "Creative label",
                "impressions": "Views served",
                "installs": "Downloads",
            },
        )
    )
    assert (mapped[0].date, mapped[0].impressions, mapped[0].installs) == ("2026-10-20", 100, 2)
    with pytest.raises(CsvImportError, match="no such column"):
        list(read(odd, "other", overrides={"date": "Nope"}))


def test_cli_import_needs_the_env_token_and_never_prints_it(tmp_path, monkeypatch, capsys):
    db = tmp_path / "perf.sqlite"
    monkeypatch.delenv(ENV_META_TOKEN, raising=False)
    args = ["perf", "import", "meta", "--account", "1234567890", "--since", "2026-10-20", "--until", "2026-10-21"]
    assert main([*args, "--db", str(db)]) == 1
    assert ENV_META_TOKEN in capsys.readouterr().err

    monkeypatch.setenv(ENV_META_TOKEN, TOKEN)
    script = [Resp(fixture("meta_insights_page1.json")), Resp(fixture("meta_insights_page2.json"))]
    monkeypatch.setattr("creative_forge.perf.http.requests.Session", lambda: Session(script))
    assert main([*args, "--db", str(db)]) == 0
    out = capsys.readouterr()
    assert "12 row(s) upserted" in out.out and "Old static banner (manual)" in out.out
    assert TOKEN not in out.out + out.err

    assert main(["perf", "import", "csv", str(FIX / "tiktok_export.csv"), "--network", "tiktok", "--db", str(db)]) == 0
    assert main(["perf", "register", str(FIX / "manifest.json"), "--db", str(db)]) == 0
    assert "7 creative(s)" in capsys.readouterr().out
    store = PerfStore(db)
    assert store.count() == 15
    tags = json.loads(store.rows("SELECT tags FROM creatives WHERE creative_id = 'fc972ce8b2'")[0]["tags"])
    assert tags["hook_type"] == "fail-state"
