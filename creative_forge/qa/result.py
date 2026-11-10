"""Structured QA results: every check reports status + measured value + the threshold it was judged against."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


SEVERITY = {Status.PASS: 0, Status.SKIP: 1, Status.WARN: 2, Status.FAIL: 3}


@dataclass
class CheckResult:
    check: str
    status: Status
    value: float | str | None
    threshold: str
    unit: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        if isinstance(self.value, float):
            d["value"] = round(self.value, 3)
        return d

    @property
    def display_value(self) -> str:
        if self.value is None:
            return "-"
        if isinstance(self.value, float):
            return f"{self.value:.2f}{(' ' + self.unit) if self.unit else ''}"
        return f"{self.value}{(' ' + self.unit) if self.unit else ''}"


def grade(value: float, warn: float, fail: float) -> Status:
    """Higher is worse: ``value <= warn`` passes, ``value <= fail`` warns, anything above fails."""
    if value <= warn:
        return Status.PASS
    if value <= fail:
        return Status.WARN
    return Status.FAIL


def worst(results: list[CheckResult]) -> Status:
    """Most severe status; skipped checks only decide the outcome when nothing else ran."""
    ran = [r.status for r in results if r.status is not Status.SKIP]
    if not ran:
        return Status.SKIP
    return max(ran, key=lambda s: SEVERITY[s])


def skipped(check: str, reason: str, threshold: str = "") -> CheckResult:
    return CheckResult(check, Status.SKIP, None, threshold, message=reason)
