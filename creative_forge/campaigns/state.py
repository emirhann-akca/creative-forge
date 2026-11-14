"""Local campaign state: one SQLite file next to the audit log.

``ops`` is the idempotency journal. Every planned object has a deterministic key (``plan.op_key``). Before a create
call the key is written as ``pending``; after the network answers it becomes ``done`` with the remote id. A call that
ended without a clear answer (a read timeout, a 5xx) is left ``unknown``: the next run first asks the network for an
object carrying that key in its name and only creates one when there is none. So a retry or a re-run never creates
an object twice.

``campaigns`` holds one row per (plan, network): ids, the budget and caps that were created, the test cell, and the
status forge set last. ``events`` keeps what ``sync`` read (status, budget, spend) for the UI.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creative_forge.paths import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    key TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    network TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    remote_id TEXT,
    request_sha TEXT,
    started_at TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (
    plan_id TEXT NOT NULL,
    network TEXT NOT NULL,
    name TEXT NOT NULL,
    config_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    account TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    daily REAL NOT NULL,
    total_cap REAL NOT NULL,
    max_daily REAL NOT NULL,
    max_total REAL NOT NULL,
    start TEXT NOT NULL,
    end_day TEXT NOT NULL,
    store_url TEXT NOT NULL,
    children TEXT NOT NULL DEFAULT '{}',
    ads TEXT NOT NULL DEFAULT '{}',
    settings TEXT NOT NULL DEFAULT '{}',
    cell_id INTEGER,
    status TEXT NOT NULL,
    auto_pause INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    paused_at TEXT,
    synced_at TEXT,
    last_sync TEXT,
    PRIMARY KEY (plan_id, network)
);
"""
PENDING, DONE, UNKNOWN = "pending", "done", "unknown"
PAUSED, ACTIVE = "PAUSED", "ACTIVE"


def default_state_dir() -> Path:
    return data_dir() / "campaigns"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class OpRecord:
    key: str
    plan_id: str
    network: str
    kind: str
    name: str
    state: str
    remote_id: str | None = None
    request_sha: str | None = None


@dataclass
class CampaignRecord:
    plan_id: str
    network: str
    name: str
    config_name: str
    mode: str
    account: str
    campaign_id: str
    currency: str
    daily: float
    total_cap: float
    max_daily: float
    max_total: float
    start: str
    end_day: str
    store_url: str
    status: str
    children: dict[str, list[str]] = field(default_factory=dict)  # kind -> remote ids (ad sets / ad groups)
    ads: dict[str, str] = field(default_factory=dict)  # ad name -> remote ad id (Google: the video asset)
    settings: dict[str, Any] = field(default_factory=dict)  # what an adapter needs besides the account
    cell_id: int | None = None
    auto_pause: bool = False
    created_at: str = ""
    activated_at: str | None = None
    paused_at: str | None = None
    synced_at: str | None = None
    last_sync: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """How a person names this campaign on the command line: ``<plan id>:<network>``."""
        return f"{self.plan_id}:{self.network}"


class CampaignState:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.dir = Path(state_dir or default_state_dir())
        self.path = self.dir / "campaigns.sqlite"

    @property
    def audit_path(self) -> Path:
        return self.dir / "audit.jsonl"

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(SCHEMA)
            yield db
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------------------------------------------ ops
    def op(self, key: str) -> OpRecord | None:
        if not self.path.is_file():
            return None
        with self._db() as db:
            r = db.execute("SELECT * FROM ops WHERE key = ?", (key,)).fetchone()
        if r is None:
            return None
        return OpRecord(
            r["key"], r["plan_id"], r["network"], r["kind"], r["name"], r["state"], r["remote_id"], r["request_sha"]
        )

    def ops_of(self, plan_id: str, network: str) -> dict[str, OpRecord]:
        if not self.path.is_file():
            return {}
        with self._db() as db:
            rows = db.execute("SELECT * FROM ops WHERE plan_id = ? AND network = ?", (plan_id, network)).fetchall()
        return {
            r["key"]: OpRecord(
                r["key"], r["plan_id"], r["network"], r["kind"], r["name"], r["state"], r["remote_id"], r["request_sha"]
            )
            for r in rows
        }

    def mark(
        self,
        key: str,
        plan_id: str,
        network: str,
        kind: str,
        name: str,
        state: str,
        remote_id: str | None = None,
        request_sha: str | None = None,
    ) -> None:
        stamp = now()
        with self._db() as db:
            db.execute(
                "INSERT INTO ops (key, plan_id, network, kind, name, state, remote_id, request_sha, started_at, "
                "done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET state = excluded.state, "
                "remote_id = COALESCE(excluded.remote_id, ops.remote_id), "
                "request_sha = COALESCE(excluded.request_sha, ops.request_sha), done_at = excluded.done_at",
                (
                    key,
                    plan_id,
                    network,
                    kind,
                    name,
                    state,
                    remote_id,
                    request_sha,
                    stamp,
                    stamp if state == DONE else None,
                ),
            )

    # ------------------------------------------------------------------------------------------------ campaigns
    @staticmethod
    def _rec(r: sqlite3.Row) -> CampaignRecord:
        return CampaignRecord(
            plan_id=r["plan_id"],
            network=r["network"],
            name=r["name"],
            config_name=r["config_name"],
            mode=r["mode"],
            account=r["account"],
            campaign_id=r["campaign_id"],
            currency=r["currency"],
            daily=float(r["daily"]),
            total_cap=float(r["total_cap"]),
            max_daily=float(r["max_daily"]),
            max_total=float(r["max_total"]),
            start=r["start"],
            end_day=r["end_day"],
            store_url=r["store_url"],
            status=r["status"],
            children=json.loads(r["children"] or "{}"),
            ads=json.loads(r["ads"] or "{}"),
            settings=json.loads(r["settings"] or "{}"),
            cell_id=r["cell_id"],
            auto_pause=bool(r["auto_pause"]),
            created_at=r["created_at"],
            activated_at=r["activated_at"],
            paused_at=r["paused_at"],
            synced_at=r["synced_at"],
            last_sync=json.loads(r["last_sync"] or "{}"),
        )

    def campaigns(self) -> list[CampaignRecord]:
        if not self.path.is_file():
            return []
        with self._db() as db:
            rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC, network").fetchall()
        return [self._rec(r) for r in rows]

    def campaign(self, ref: str) -> CampaignRecord | None:
        """``<plan id>:<network>``, a network campaign id, or a unique prefix of the plan id with the network."""
        plan, _, network = ref.partition(":")
        for c in self.campaigns():
            if (c.plan_id == plan and c.network == network) or c.campaign_id == ref:
                return c
        hits = [c for c in self.campaigns() if network and c.network == network and c.plan_id.startswith(plan)]
        return hits[0] if len(hits) == 1 else None

    def save_campaign(self, c: CampaignRecord) -> None:
        cols = [
            "plan_id", "network", "name", "config_name", "mode", "account", "campaign_id", "currency", "daily",
            "total_cap", "max_daily", "max_total", "start", "end_day", "store_url", "children", "ads", "settings",
            "cell_id", "status", "auto_pause", "created_at", "activated_at", "paused_at", "synced_at", "last_sync",
        ]  # fmt: skip
        values = {
            **{k: getattr(c, k) for k in cols if k not in ("children", "ads", "settings", "last_sync", "auto_pause")},
            "children": json.dumps(c.children, sort_keys=True),
            "ads": json.dumps(c.ads, sort_keys=True, ensure_ascii=False),
            "settings": json.dumps(c.settings, sort_keys=True, ensure_ascii=False),
            "last_sync": json.dumps(c.last_sync, sort_keys=True, ensure_ascii=False),
            "auto_pause": int(c.auto_pause),
        }
        with self._db() as db:
            db.execute(
                f"INSERT INTO campaigns ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                "ON CONFLICT (plan_id, network) DO UPDATE SET "
                + ", ".join(f"{k} = excluded.{k}" for k in cols if k not in ("plan_id", "network")),
                [values[k] for k in cols],
            )
