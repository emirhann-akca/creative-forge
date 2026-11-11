"""``forge perf``: import, register and report. Kept out of ``cli.py`` so the parser stays readable."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from creative_forge.config import ConfigError
from creative_forge.naming import AdNameError, CreativeIds, parse
from creative_forge.perf.http import ENV_APPLOVIN_KEY, ENV_META_TOKEN, PerfApiError, require_env
from creative_forge.perf.store import ImportSummary, PerfStore


def _store(a: argparse.Namespace) -> PerfStore:
    return PerfStore(Path(a.db) if a.db else None)


def creatives_from(path: Path) -> list[dict[str, Any]]:
    """Creative IDs and tags from a publish ``manifest.json``, a ``report.json`` or a run folder holding one."""
    path = Path(path)
    if path.is_dir():
        for name in ("manifest.json", "report.json"):
            if (path / name).is_file():
                path = path / name
                break
        else:
            raise ValueError(f"{path}: no manifest.json or report.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    exports = data.get("exports") or [e for c in data.get("concepts", []) for e in c.get("exports", [])]
    out = []
    for e in exports:
        name = e.get("ad_name")
        if not name:
            continue
        try:
            ids: CreativeIds = parse(name)
        except AdNameError:
            continue
        out.append({"ids": ids, "tags": e.get("tags") or {}})
    return out


def cmd_import(a: argparse.Namespace) -> int:
    store = _store(a)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    try:
        if a.source == "meta":
            from creative_forge.perf.meta import MetaInsights

            token = require_env(ENV_META_TOKEN, "forge perf import meta")
            meta = MetaInsights(token, a.account, version=a.api_version, log=log)
            summary = ImportSummary("meta", meta.account, "ad")
            store.upsert(meta.rows(a.since, a.until, by_country=a.by_country), summary, since=a.since, until=a.until)
            summary.pages = meta.pages
        elif a.source == "applovin":
            from creative_forge.perf.applovin import AppLovinReports

            key = require_env(ENV_APPLOVIN_KEY, "forge perf import applovin")
            al = AppLovinReports(key, account=a.account, log=log)
            summary = ImportSummary("applovin", a.account, "creative_set")
            store.upsert(al.creative_sets(a.since, a.until), summary, since=a.since, until=a.until)
            if a.assets:
                assets = ImportSummary("applovin", a.account, "asset")
                store.upsert(al.assets(a.since, a.until), assets, since=a.since, until=a.until)
                linked = store.link_assets()
                print(f"assets: {assets.rows} row(s), {assets.matched} matched by name, {linked} linked to a set")
            summary.pages = al.pages
        else:
            from creative_forge.perf.csvimport import CsvImportError, read

            overrides = {}
            for item in a.column or []:
                field, sep, header = item.partition("=")
                if not sep:
                    raise CsvImportError(f"--column needs FIELD=HEADER, got {item!r}")
                overrides[field.strip()] = header.strip()
            summary = ImportSummary(a.network, a.account, "csv", pages=1)
            store.upsert(read(Path(a.file), a.network, a.account, overrides), summary)
    except (PerfApiError, ConfigError, ValueError) as e:
        print(f"perf import failed: {e}", file=sys.stderr)
        return 1
    print(summary.text())
    print(f"store: {store.path} ({store.count()} row(s) in total)")
    return 0


def cmd_register(a: argparse.Namespace) -> int:
    store = _store(a)
    total = 0
    for p in a.paths:
        try:
            creatives = creatives_from(Path(p))
        except (OSError, ValueError) as e:
            print(f"{p}: {e}", file=sys.stderr)
            return 1
        total += store.register(creatives, str(p))
        print(f"{p}: {len(creatives)} creative(s)")
    print(f"registered {total} creative(s) in {store.path}")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from creative_forge.perf.lessons import write_lessons
    from creative_forge.perf.report import build_report, text_summary, write_html

    store = _store(a)
    try:
        rep = build_report(
            store,
            network=a.network,
            since=a.since,
            until=a.until,
            metric=a.metric,
            min_impressions=a.min_impressions,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(text_summary(rep))
    html_path = write_html(rep, Path(a.out))
    print(f"\nreport: {html_path} (+ {html_path.with_suffix('.json').name})")
    if a.write_lessons:
        path, n = write_lessons(rep)
        print(f"lessons: {n} written to {path}; `forge ideate` retrieves them")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    sp = sub.add_parser("perf", help="ad performance: import from networks, register creatives, report, lessons")
    psub = sp.add_subparsers(dest="perf_cmd", required=True)

    def db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", help="SQLite store (default: perf.sqlite in the user data folder)")

    pi = psub.add_parser("import", help="pull creative-level metrics into the local store (idempotent upserts)")
    isub = pi.add_subparsers(dest="source", required=True)
    m = isub.add_parser("meta", help=f"Meta Insights, level=ad, daily; token from ${ENV_META_TOKEN}")
    m.add_argument("--account", required=True, help="ad account id (act_123 or 123)")
    m.add_argument("--since", required=True, help="YYYY-MM-DD")
    m.add_argument("--until", required=True, help="YYYY-MM-DD")
    m.add_argument("--by-country", action="store_true", help="one row per country (breakdowns=country)")
    m.add_argument("--api-version", default="v27.0")
    db_arg(m)
    al = isub.add_parser("applovin", help=f"AppLovin creative-set report; Report Key from ${ENV_APPLOVIN_KEY}")
    al.add_argument("--since", required=True)
    al.add_argument("--until", required=True)
    al.add_argument("--assets", action="store_true", help="also the Asset Reporting API (no installs per asset)")
    al.add_argument("--account", default="default", help="a label for this AppLovin account")
    db_arg(al)
    c = isub.add_parser("csv", help="a CSV export (TikTok, Unity, Google Ads, Mintegral, ...)")
    c.add_argument("file")
    c.add_argument("--network", required=True, help="tiktok, unity, google, mintegral, moloco, liftoff, other")
    c.add_argument("--account", default="csv")
    c.add_argument("--column", action="append", metavar="FIELD=HEADER", help="map a field to a header (repeatable)")
    db_arg(c)
    pi.set_defaults(fn=cmd_import)

    pr = psub.add_parser("register", help="read creative IDs and tags from publish manifests or run folders")
    pr.add_argument("paths", nargs="+")
    db_arg(pr)
    pr.set_defaults(fn=cmd_register)

    rp = psub.add_parser("report", help="per creative and per tag, with intervals; HTML + JSON; lessons for ideate")
    rp.add_argument("--network", help="only this network (meta, applovin, tiktok, ...)")
    rp.add_argument("--since", help="first day (YYYY-MM-DD)")
    rp.add_argument("--until", help="last day (YYYY-MM-DD)")
    rp.add_argument("--metric", default="ipm", choices=["ipm", "ctr", "hook_rate", "hook_rate_2s", "completion"])
    rp.add_argument(
        "--min-impressions", type=float, default=10000, help="no verdict below this many impressions (default 10000)"
    )
    rp.add_argument("--out", default="out/perf_report.html")
    rp.add_argument("--write-lessons", action="store_true", help="write tag winners/losers into the ideate library")
    db_arg(rp)
    rp.set_defaults(fn=cmd_report)
