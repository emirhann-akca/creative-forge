"""Generic CSV import for exports from TikTok Ads Manager, Unity, Google Ads, Mintegral or anything else.

Headers are matched case-insensitively against the candidate names below; ``--column field=Header`` adds or
replaces a mapping when an export names a column differently. Required: a date, an ad (or creative) name and
impressions. Everything else is optional and stays NULL when absent.

TikTok's 2- and 6-second views land in their own columns (``video_2s``, ``video_6s``): they are never mixed with
Meta's 3-second ``video_view``, so hook rates from different definitions are not compared.

The file may be UTF-8 (with or without BOM) or UTF-16 (what Excel writes for "Unicode text"); the delimiter
(comma, semicolon or tab) is detected.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from creative_forge.perf.http import num
from creative_forge.perf.store import PerfRow

CANDIDATES: dict[str, tuple[str, ...]] = {
    "date": ("date", "day", "stat_time_day", "reporting starts", "by day", "timestamp"),
    "ad_name": (
        "ad name",
        "ad_name",
        "creative name",
        "creative_name",
        "creative pack name",
        "creative pack",
        "creative set",
        "creative_set",
        "asset name",
        "asset",
        "ad",
        "creative",
    ),
    "ad_key": ("ad id", "ad_id", "creative id", "creative_id", "creative pack id", "asset id", "creative set id"),
    "country": ("country", "country code", "country_code", "region", "geo"),
    "impressions": ("impressions", "impr.", "impr", "impression", "starts"),
    "clicks": ("clicks", "click", "clicks (all)", "clicks (destination)"),
    "spend": ("spend", "cost", "amount spent", "amount spent (usd)", "total cost", "cost (usd)", "spend (usd)"),
    "installs": ("installs", "install", "conversions", "conversion", "app installs", "app install"),
    "video_3s": ("3-second video plays", "3-second video views", "video_view", "3s video views"),
    "video_2s": ("2-second video views", "video_watched_2s", "2s video views", "2-second views"),
    "video_6s": ("6-second video views", "video_watched_6s", "6s video views", "6-second views"),
    "p25": ("video views at 25%", "video_views_p25", "video played to 25%", "p25"),
    "p50": ("video views at 50%", "video_views_p50", "video played to 50%", "p50"),
    "p75": ("video views at 75%", "video_views_p75", "video played to 75%", "p75"),
    "p100": ("video views at 100%", "video_views_p100", "video played to 100%", "p100", "completed views"),
}
REQUIRED = ("date", "ad_name", "impressions")
NETWORKS = ("tiktok", "unity", "google", "mintegral", "moloco", "liftoff", "applovin", "meta", "other")


class CsvImportError(ValueError):
    pass


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.replace("﻿", "").strip().lower())


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvImportError(f"{path.name}: not UTF-8 or UTF-16; export the report as CSV (UTF-8)") from None


DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%d.%m.%Y")


def _date(v: str, line: int) -> str:
    s = (v or "").strip()
    for cand in dict.fromkeys((s, s[:10], s[:19])):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    raise CsvImportError(f"line {line}: cannot read date {s!r} (use YYYY-MM-DD)")


def mapping(headers: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """canonical field -> header in this file."""
    by_norm = {_norm(h): h for h in headers}
    out: dict[str, str] = {}
    for field, names in CANDIDATES.items():
        for n in names:
            if n in by_norm:
                out[field] = by_norm[n]
                break
    for field, header in (overrides or {}).items():
        if field not in CANDIDATES:
            raise CsvImportError(f"--column {field}=...: unknown field (known: {', '.join(CANDIDATES)})")
        if _norm(header) not in by_norm:
            raise CsvImportError(f"--column {field}={header!r}: no such column (have: {', '.join(headers)})")
        out[field] = by_norm[_norm(header)]
    missing = [f for f in REQUIRED if f not in out]
    if missing:
        raise CsvImportError(
            f"no column for {', '.join(missing)} (have: {', '.join(headers)}); map one with --column FIELD=HEADER"
        )
    return out


def read(path: Path, network: str, account: str = "csv", overrides: dict[str, str] | None = None) -> Iterator[PerfRow]:
    if network not in NETWORKS:
        raise CsvImportError(f"--network must be one of {', '.join(NETWORKS)}")
    text = _read_text(Path(path))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = mapping(list(reader.fieldnames or []), overrides)
    for i, rec in enumerate(reader, start=2):
        name = (rec.get(cols["ad_name"]) or "").strip()
        if not name or name.lower() in ("total", "totals", "grand total"):
            continue
        key = (rec.get(cols["ad_key"]) or "").strip() if "ad_key" in cols else ""
        values = {f: num(rec.get(h)) for f, h in cols.items() if f not in ("date", "ad_name", "ad_key", "country")}
        yield PerfRow(
            network=network,
            account=account,
            level="csv",
            date=_date(rec.get(cols["date"]) or "", i),
            ad_key=key or name,
            ad_name=name,
            country=(rec.get(cols["country"]) or "").strip().upper() if "country" in cols else "",
            source=f"csv:{Path(path).name}",
            **values,
        )
