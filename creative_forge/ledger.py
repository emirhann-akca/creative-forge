"""Cost ledger and budget guard.

Every generation goes through the ledger: paid calls, cache hits (recorded at $0 with the amount saved) and
simulated calls from fake providers (recorded at $0 with the list price, so a demo shows what a real run costs).
The guard reserves the estimate *before* a call is made, so a call that would cross the budget never starts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


class BudgetExceeded(RuntimeError):
    """A paid call would push the run over --budget-usd. Nothing was sent."""


@dataclass
class LedgerEntry:
    concept: str
    stage: str
    provider: str
    model: str
    detail: str
    usd: float
    list_usd: float | None
    cached: bool
    billable: bool
    cache_key: str
    at: float = field(default_factory=time.time)


class CostLedger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._lock = threading.Lock()
        # Refused model licences a person overrode for this run, with their reason (see licenses.py).
        self.license_overrides: list[dict] = []
        # Which provider ran each kind of generation when routing chose between a local worker and the cloud.
        self.routing: list[dict] = []

    def record(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    @property
    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    @property
    def spent(self) -> float:
        return round(sum(e.usd for e in self.entries), 4)

    @property
    def saved_by_cache(self) -> float:
        return round(sum((e.list_usd or 0.0) for e in self.entries if e.cached), 4)

    def to_dict(self) -> dict:
        return {
            "spent_usd": self.spent,
            "saved_by_cache_usd": self.saved_by_cache,
            "license_overrides": list(self.license_overrides),
            "routing": list(self.routing),
            "entries": [asdict(e) for e in self.entries],
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


class BudgetGuard:
    """Thread-safe reservation of spend against an optional USD limit."""

    def __init__(self, ledger: CostLedger, limit_usd: float | None = None) -> None:
        self.ledger = ledger
        self.limit = limit_usd
        self._reserved = 0.0
        self._lock = threading.Lock()

    def reserve(self, usd: float | None, what: str) -> float:
        """Reserve ``usd`` for a call. ``None`` means the call has no known price."""
        with self._lock:
            if self.limit is None:
                return usd or 0.0
            if usd is None:
                raise BudgetExceeded(
                    f"{what} has no price in the pricing table, so it cannot be checked against "
                    f"--budget-usd {self.limit:.2f}. Add a price (pricing.local.yaml) or run without a budget."
                )
            committed = self.ledger.spent + self._reserved
            if committed + usd > self.limit + 1e-9:
                raise BudgetExceeded(
                    f"{what} would cost ${usd:.2f}; ${committed:.2f} already committed of the "
                    f"${self.limit:.2f} budget. Nothing was sent."
                )
            self._reserved += usd
            return usd

    def release(self, usd: float) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - usd)
