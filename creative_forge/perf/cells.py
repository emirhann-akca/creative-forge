"""Test cells: 2-4 variants against a control creative, with a kill/scale rule written before the test starts.

A cell lives in the performance store's SQLite file (table ``test_cells``) and reads the same ``perf_rows`` as
``forge perf report``, through the same aggregation, on one network only (audiences and metric definitions differ
between networks, so a cell never mixes them).

* **The rule is pre-registered.** Thresholds and the rule text are chosen while the cell is a draft. Starting the test
  stores the SHA-256 of the canonical rule and refuses any later change, so a result cannot be argued into a winner
  by moving the bar after the data came in. A rule whose hash no longer matches (edited by hand in the SQLite file) is
  reported as tampered.
* **Sufficiency counts installs, not impressions.** A first screen at ``screen_installs`` per arm (about 50) may only
  drop a variant that is clearly worse (its 95 % Wilson interval lies entirely below the control's). A kill/scale
  verdict needs ``decide_installs`` per arm (a 20-30 % IPM difference needs roughly 200-430). Below that the answer is
  "no decision yet" and how many installs are missing, whatever the point estimate says.
* **The verdict.** At the decision threshold, with lift = variant / control - 1 on the cell's metric and P(better) from
  the Beta posteriors of ``perf.stats``: scale when lift >= +scale_pct and P(better) >= confidence; kill when
  lift <= -kill_pct and P(better) <= 1 - confidence; otherwise no clear difference, and the control stays.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from creative_forge.perf.report import METRICS, _aggregate, _rate
from creative_forge.perf.stats import prob_better, wilson
from creative_forge.perf.store import PerfStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    network TEXT NOT NULL,
    control TEXT NOT NULL,
    variants TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT 'ipm',
    rule TEXT NOT NULL,
    rule_sha256 TEXT,
    created_at TEXT NOT NULL,
    started_on TEXT,
    locked_at TEXT,
    ended_on TEXT
);
"""

CELL_METRICS = ("ipm", "ctr", "hook_rate")
MAX_VARIANTS = 4
DRAFT, RUNNING, CLOSED = "draft", "running", "closed"
# verdicts per variant
PENDING, KILL_EARLY, SCALE, KILL, KEEP = "pending", "kill_early", "scale", "kill", "keep"
# how far a variant is
WAITING, SCREENED, DECIDED = "waiting", "screened", "decided"


class CellError(ValueError):
    """A test cell or a change to one is not acceptable; nothing was written."""


class LockedError(CellError):
    """The test has started: its rule is locked."""


@dataclass(frozen=True)
class Rule:
    scale_pct: float = 20.0
    kill_pct: float = 20.0
    screen_installs: int = 50
    decide_installs: int = 200
    confidence: float = 0.95
    text: str = ""

    def problems(self) -> list[str]:
        out = []
        if not 0 < self.scale_pct <= 500:
            out.append("scale_pct must be above 0 and at most 500")
        if not 0 < self.kill_pct < 100:
            out.append("kill_pct must be between 0 and 100")
        if not 1 <= self.screen_installs <= self.decide_installs:
            out.append("screen_installs must be at least 1 and at most decide_installs")
        if self.decide_installs > 1_000_000:
            out.append("decide_installs is too large")
        if not 0.5 <= self.confidence < 1:
            out.append("confidence must be at least 0.5 and below 1")
        if len(self.text) > 2000:
            out.append("the rule text is longer than 2000 characters")
        return out

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> Rule:
        d = json.loads(text)
        return cls(
            scale_pct=float(d.get("scale_pct", 20)),
            kill_pct=float(d.get("kill_pct", 20)),
            screen_installs=int(d.get("screen_installs", 50)),
            decide_installs=int(d.get("decide_installs", 200)),
            confidence=float(d.get("confidence", 0.95)),
            text=str(d.get("text", "")),
        )


@dataclass(frozen=True)
class TestCell:
    __test__ = False  # not a pytest class

    id: int
    name: str
    network: str
    control: str
    variants: tuple[str, ...]
    metric: str
    rule: Rule
    created_at: str
    started_on: str | None = None
    locked_at: str | None = None
    ended_on: str | None = None
    rule_sha256: str | None = None

    @property
    def status(self) -> str:
        if not self.started_on:
            return DRAFT
        return CLOSED if self.ended_on else RUNNING

    @property
    def locked(self) -> bool:
        return self.locked_at is not None

    @property
    def tampered(self) -> bool:
        return self.locked and self.rule.sha256() != self.rule_sha256

    @property
    def creatives(self) -> tuple[str, ...]:
        return (self.control, *self.variants)


class CellStore:
    """``test_cells`` in the performance store. Reading never creates the file; writing does."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _db(self, create: bool) -> Iterator[sqlite3.Connection | None]:
        if not self.path.is_file() and not create:
            yield None
            return
        if create:
            PerfStore(self.path)  # the perf schema too, so `forge perf` finds a valid store
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(SCHEMA)
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _cell(r: sqlite3.Row) -> TestCell:
        return TestCell(
            id=int(r["id"]),
            name=r["name"],
            network=r["network"],
            control=r["control"],
            variants=tuple(json.loads(r["variants"])),
            metric=r["metric"],
            rule=Rule.from_json(r["rule"]),
            created_at=r["created_at"],
            started_on=r["started_on"],
            locked_at=r["locked_at"],
            ended_on=r["ended_on"],
            rule_sha256=r["rule_sha256"],
        )

    def list(self) -> list[TestCell]:
        with self._db(create=False) as db:
            if db is None:
                return []
            return [self._cell(r) for r in db.execute("SELECT * FROM test_cells ORDER BY id DESC")]

    def get(self, cell_id: int) -> TestCell | None:
        with self._db(create=False) as db:
            if db is None:
                return None
            r = db.execute("SELECT * FROM test_cells WHERE id = ?", (cell_id,)).fetchone()
            return self._cell(r) if r else None

    def create(
        self,
        name: str,
        network: str,
        control: str,
        variants: list[str],
        rule: Rule,
        metric: str = "ipm",
        now: datetime | None = None,
    ) -> TestCell:
        name = " ".join(name.split())
        variants = list(dict.fromkeys(v.strip() for v in variants if v.strip()))
        problems = list(rule.problems())
        if not name:
            problems.append("a test needs a name")
        if len(name) > 120:
            problems.append("the name is longer than 120 characters")
        if not network.strip():
            problems.append("a test runs on one network")
        if not control.strip():
            problems.append("a test needs a control creative")
        if not variants:
            problems.append("a test needs at least one variant")
        if len(variants) > MAX_VARIANTS:
            problems.append(f"at most {MAX_VARIANTS} variants per test")
        if control in variants:
            problems.append("the control cannot also be a variant")
        if metric not in CELL_METRICS:
            problems.append(f"metric must be one of {', '.join(CELL_METRICS)}")
        if problems:
            raise CellError("; ".join(problems))
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._db(create=True) as db:
            assert db is not None
            cur = db.execute(
                "INSERT INTO test_cells (name, network, control, variants, metric, rule, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, network.strip(), control.strip(), json.dumps(variants), metric, rule.canonical(), stamp),
            )
            new_id = int(cur.lastrowid or 0)
        cell = self.get(new_id)
        assert cell is not None
        return cell

    def update_rule(self, cell_id: int, rule: Rule) -> TestCell:
        cell = self._require(cell_id)
        if cell.locked:
            raise LockedError("the test has started; its rule is locked")
        if rule.problems():
            raise CellError("; ".join(rule.problems()))
        with self._db(create=True) as db:
            assert db is not None
            db.execute("UPDATE test_cells SET rule = ? WHERE id = ? AND locked_at IS NULL", (rule.canonical(), cell_id))
        return self._require(cell_id)

    def start(self, cell_id: int, on: date | None = None, now: datetime | None = None) -> TestCell:
        """Lock the rule and count rows from ``on`` (default today)."""
        cell = self._require(cell_id)
        if cell.locked:
            raise LockedError("the test has already started")
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        day = (on or date.today()).isoformat()
        with self._db(create=True) as db:
            assert db is not None
            db.execute(
                "UPDATE test_cells SET started_on = ?, locked_at = ?, rule_sha256 = ? WHERE id = ? "
                "AND locked_at IS NULL",
                (day, stamp, cell.rule.sha256(), cell_id),
            )
        return self._require(cell_id)

    def close(self, cell_id: int, on: date | None = None) -> TestCell:
        cell = self._require(cell_id)
        if cell.status != RUNNING:
            raise CellError("only a running test can be closed")
        day = (on or date.today()).isoformat()
        if cell.started_on and day < cell.started_on:
            raise CellError("a test cannot end before it started")
        with self._db(create=True) as db:
            assert db is not None
            db.execute("UPDATE test_cells SET ended_on = ? WHERE id = ?", (day, cell_id))
        return self._require(cell_id)

    def delete(self, cell_id: int) -> None:
        """Drafts only: a started test is evidence and stays."""
        cell = self._require(cell_id)
        if cell.locked:
            raise LockedError("a started test cannot be deleted")
        with self._db(create=True) as db:
            assert db is not None
            db.execute("DELETE FROM test_cells WHERE id = ? AND locked_at IS NULL", (cell_id,))

    def _require(self, cell_id: int) -> TestCell:
        cell = self.get(cell_id)
        if cell is None:
            raise CellError(f"no test cell {cell_id}")
        return cell


# ------------------------------------------------------------------------------------------------------- evaluation
@dataclass(frozen=True)
class Arm:
    creative_id: str
    ad_name: str | None
    impressions: float
    installs: float
    spend: float
    x: float  # the metric's numerator (installs, clicks, 3-second views)
    n: float  # impressions of the rows that report it
    scale: float

    @property
    def rate(self) -> float | None:
        return self.x / self.n * self.scale if self.n else None

    @property
    def ci(self) -> tuple[float, float]:
        lo, hi = wilson(self.x, self.n)
        return (lo * self.scale, hi * self.scale)

    @property
    def cpi(self) -> float | None:
        return self.spend / self.installs if self.installs else None


@dataclass(frozen=True)
class Outcome:
    arm: Arm
    stage: str  # WAITING (below the first screen), SCREENED (past it), DECIDED
    verdict: str  # PENDING, KILL_EARLY, SCALE, KILL, KEEP
    installs_missing: int  # to the next threshold, for the arm that is further behind (variant or control)
    next_threshold: int  # screen_installs or decide_installs; 0 when decided
    progress: float  # min(variant, control) installs / decide_installs, capped at 1
    lift: float | None = None  # variant / control - 1
    p_better: float | None = None


def judge(variant: Arm, control: Arm, rule: Rule) -> Outcome:
    """One variant against the control under a pre-registered rule (see the module docstring)."""
    least = min(variant.installs, control.installs)
    progress = min(1.0, least / rule.decide_installs) if rule.decide_installs else 1.0
    lift = None
    if variant.rate is not None and control.rate:
        lift = variant.rate / control.rate - 1
    if least < rule.screen_installs:
        missing = int(max(rule.screen_installs - variant.installs, rule.screen_installs - control.installs, 0) + 0.999)
        return Outcome(variant, WAITING, PENDING, missing, rule.screen_installs, progress, lift)
    if least < rule.decide_installs:
        v_ci, c_ci = variant.ci, control.ci
        if variant.n and control.n and v_ci[1] < c_ci[0]:  # clearly worse already: the first screen drops it
            p = prob_better(variant.x, variant.n, control.x, control.n)
            return Outcome(variant, DECIDED, KILL_EARLY, 0, 0, progress, lift, p)
        missing = int(max(rule.decide_installs - variant.installs, rule.decide_installs - control.installs, 0) + 0.999)
        return Outcome(variant, SCREENED, PENDING, missing, rule.decide_installs, progress, lift)
    p = prob_better(variant.x, variant.n, control.x, control.n)
    if lift is not None and lift >= rule.scale_pct / 100 and p >= rule.confidence:
        verdict = SCALE
    elif lift is not None and lift <= -rule.kill_pct / 100 and p <= 1 - rule.confidence:
        verdict = KILL
    else:
        verdict = KEEP
    return Outcome(variant, DECIDED, verdict, 0, 0, progress, lift, p)


@dataclass
class CellResult:
    cell: TestCell
    control: Arm
    outcomes: list[Outcome] = field(default_factory=list)
    since: str | None = None
    until: str | None = None
    has_data: bool = False

    @property
    def decided(self) -> int:
        return sum(1 for o in self.outcomes if o.stage == DECIDED)

    @property
    def ci_max(self) -> float:
        """A common scale for drawing the arms' intervals side by side (arms without data are left out)."""
        his = [a.ci[1] for a in (self.control, *(o.arm for o in self.outcomes)) if a.n]
        return max(his) * 1.05 if his and max(his) > 0 else 1.0

    @property
    def ready(self) -> bool:
        """At least one variant has a verdict under the rule (what the overview lists as "ready to decide")."""
        return self.cell.status == RUNNING and self.decided > 0


def _arm(creative_id: str, row: dict[str, Any] | None, metric: str) -> Arm:
    m = METRICS[metric]
    row = row or {}
    x, n = _rate(row, m)
    return Arm(
        creative_id,
        row.get("ad_name"),
        float(row.get("impressions") or 0),
        float(row.get("installs") or 0),
        float(row.get("spend") or 0),
        x,
        n,
        m.scale,
    )


def evaluate(cell: TestCell, perf_db: Path | None, today: date | None = None) -> CellResult:
    """Arms from ``perf_rows`` of the cell's network between its start and end (or today). A draft has no window
    yet, so every arm is empty and every variant is pending."""
    rows: dict[str, dict[str, Any]] = {}
    since = cell.started_on
    until = cell.ended_on or (today or date.today()).isoformat()
    if since and perf_db is not None and Path(perf_db).is_file():
        store = PerfStore(Path(perf_db))
        where = " AND network = ? AND date >= ? AND date_end <= ?"
        for r in _aggregate(store, where, (cell.network, since, until)):
            if r["creative_id"] in cell.creatives:
                rows[r["creative_id"]] = r
    control = _arm(cell.control, rows.get(cell.control), cell.metric)
    outcomes = [judge(_arm(v, rows.get(v), cell.metric), control, cell.rule) for v in cell.variants]
    return CellResult(cell, control, outcomes, since, until if since else None, bool(rows))
