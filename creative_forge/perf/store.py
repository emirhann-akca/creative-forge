"""Local performance store: one SQLite file (Python's standard library, no server) under the user data folder.

Rows are daily (or per report window) metrics per network ad unit: a Meta ad, an AppLovin creative set or asset, or a
row of a CSV export. The primary key is (network, account, level, date, date_end, ad_key, country), and every write
is an upsert, so importing the same range twice replaces rows instead of adding them.

``creative_id`` is filled at import time by finding a creative-forge ad name (``naming.find``) in the network's ad
name; ``creatives`` holds what ``forge perf register`` read from publish manifests (the seven ids and the tags).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creative_forge.naming import find
from creative_forge.paths import data_dir

METRICS = (
    "impressions",
    "clicks",
    "spend",
    "installs",
    "video_3s",  # Meta `video_view`: "3-Second Video Views" (the hook-rate numerator)
    "video_2s",  # TikTok's 2-second views: stored apart, never compared with Meta's 3-second metric
    "video_6s",
    "p25",
    "p50",
    "p75",
    "p95",
    "p100",
    "thruplay",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS perf_rows (
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    level TEXT NOT NULL,
    date TEXT NOT NULL,
    date_end TEXT NOT NULL,
    ad_key TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    ad_name TEXT,
    parent_key TEXT,
    creative_id TEXT,
    {", ".join(f"{m} REAL" for m in METRICS)},
    currency TEXT,
    source TEXT,
    imported_at TEXT,
    PRIMARY KEY (network, account, level, date, date_end, ad_key, country)
);
CREATE INDEX IF NOT EXISTS perf_rows_creative ON perf_rows (creative_id);
CREATE TABLE IF NOT EXISTS creatives (
    creative_id TEXT PRIMARY KEY,
    ad_name TEXT NOT NULL,
    spec TEXT, concept TEXT, hook TEXT, segment TEXT, end_card TEXT, format TEXT, language TEXT,
    tags TEXT NOT NULL DEFAULT '{{}}',
    source TEXT,
    registered_at TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network TEXT, account TEXT, level TEXT, since TEXT, until TEXT,
    rows INTEGER, matched INTEGER, unmatched TEXT, source TEXT, at TEXT
);
"""


def default_db() -> Path:
    return data_dir() / "perf.sqlite"


@dataclass
class PerfRow:
    network: str
    account: str
    level: str  # "ad" (Meta), "creative_set" / "asset" (AppLovin), "csv"
    date: str  # YYYY-MM-DD; for a window report, its first day
    ad_key: str
    ad_name: str | None = None
    date_end: str = ""  # last day of the row's window; same as `date` for daily rows
    country: str = ""
    parent_key: str | None = None  # an AppLovin asset's creative_set_id
    impressions: float | None = None
    clicks: float | None = None
    spend: float | None = None
    installs: float | None = None
    video_3s: float | None = None
    video_2s: float | None = None
    video_6s: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p95: float | None = None
    p100: float | None = None
    thruplay: float | None = None
    currency: str | None = None
    source: str = ""
    creative_id: str | None = field(default=None)

    def __post_init__(self) -> None:
        self.date_end = self.date_end or self.date
        if self.creative_id is None and self.ad_name:
            ids = find(self.ad_name)
            self.creative_id = ids.creative_id if ids else None


@dataclass
class ImportSummary:
    network: str
    account: str
    level: str
    rows: int = 0
    matched: int = 0
    unmatched: list[str] = field(default_factory=list)  # distinct ad names that are not creative-forge names
    pages: int = 0

    def text(self) -> str:
        line = f"{self.network} {self.level}: {self.rows} row(s) upserted, {self.pages} page(s), {self.matched} matched"
        if self.unmatched:
            shown = ", ".join(self.unmatched[:10]) + (" ..." if len(self.unmatched) > 10 else "")
            line += f"\nunmatched ad names ({len(self.unmatched)}; stored, not in creative reports): {shown}"
        return line


class PerfStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or default_db())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def upsert(
        self, rows: Iterable[PerfRow], summary: ImportSummary, *, since: str = "", until: str = ""
    ) -> ImportSummary:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cols = [
            "network",
            "account",
            "level",
            "date",
            "date_end",
            "ad_key",
            "country",
            "ad_name",
            "parent_key",
            "creative_id",
            *METRICS,
            "currency",
            "source",
            "imported_at",
        ]
        keys = {"network", "account", "level", "date", "date_end", "ad_key", "country"}
        sql = (
            f"INSERT INTO perf_rows ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT (network, account, level, date, date_end, ad_key, country) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
        )
        unmatched: set[str] = set(summary.unmatched)
        with self.connect() as db:
            for r in rows:
                d = asdict(r) | {"imported_at": now}
                db.execute(sql, [d[c] for c in cols])
                summary.rows += 1
                if r.creative_id:
                    summary.matched += 1
                elif r.ad_name:
                    unmatched.add(r.ad_name)
            summary.unmatched = sorted(unmatched)
            db.execute(
                "INSERT INTO imports (network, account, level, since, until, rows, matched, unmatched, source, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary.network,
                    summary.account,
                    summary.level,
                    since,
                    until,
                    summary.rows,
                    summary.matched,
                    json.dumps(summary.unmatched, ensure_ascii=False),
                    "",
                    now,
                ),
            )
        return summary

    def link_assets(self) -> int:
        """AppLovin asset rows carry their creative set's id in ``parent_key``; an asset whose own name is not a
        creative-forge name inherits the creative set's creative ID."""
        with self.connect() as db:
            cur = db.execute(
                "UPDATE perf_rows SET creative_id = ("
                "  SELECT p.creative_id FROM perf_rows p WHERE p.network = perf_rows.network"
                "  AND p.level = 'creative_set' AND p.ad_key = perf_rows.parent_key AND p.creative_id IS NOT NULL"
                "  LIMIT 1)"
                " WHERE level = 'asset' AND creative_id IS NULL AND parent_key IS NOT NULL"
            )
            return cur.rowcount

    def register(self, creatives: Iterable[dict[str, Any]], source: str) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        n = 0
        with self.connect() as db:
            for c in creatives:
                ids = c["ids"]
                db.execute(
                    "INSERT INTO creatives (creative_id, ad_name, spec, concept, hook, segment, end_card, format, "
                    "language, tags, source, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (creative_id) DO UPDATE SET tags = excluded.tags, source = excluded.source, "
                    "registered_at = excluded.registered_at",
                    (
                        ids.creative_id,
                        ids.ad_name,
                        ids.spec,
                        ids.concept,
                        ids.hook,
                        ids.segment,
                        ids.end_card,
                        ids.format,
                        ids.language,
                        json.dumps(c.get("tags") or {}, sort_keys=True, ensure_ascii=False),
                        source,
                        now,
                    ),
                )
                n += 1
        return n

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(sql, params))

    def count(self) -> int:
        return int(self.rows("SELECT COUNT(*) AS n FROM perf_rows")[0]["n"])
