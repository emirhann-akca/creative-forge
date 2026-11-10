"""Human review between QA and delivery.

``forge review RUN_DIR`` records a person's approve/reject decision and note for each export in
``RUN_DIR/review.json``. ``forge publish RUN_DIR --to DIR`` copies only approved exports, plus a ``manifest.json``.

Publishing refuses when:

* nothing is approved;
* an approved file is not byte-identical to the one that was reviewed (its SHA-256 is stored with the decision,
  so re-rendering a concept voids its approval);
* an approved export, or the generation checks of its concept, has a failed QA check. ``--override "reason"``
  publishes anyway and writes the reason, the reviewer and the overridden checks into the manifest.

QA measures; a person decides; the manifest records who decided what, on which bytes, and why.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creative_forge.cache import file_sha256

REVIEW_FILE = "review.json"
MANIFEST_FILE = "manifest.json"
DECISIONS = ("approve", "reject")


class ReviewError(RuntimeError):
    """The run directory, a decision or a publish request is not acceptable. Nothing was copied."""


@dataclass(frozen=True)
class ExportRecord:
    file: str
    concept: str
    format: str
    status: str
    failed: tuple[str, ...]  # failed checks of this export and of its concept's generation checks
    warned: tuple[str, ...]


def load_exports(run_dir: Path) -> list[ExportRecord]:
    report = Path(run_dir) / "report.json"
    if not report.exists():
        raise ReviewError(f"{report} not found; review works on the output directory of `forge run`")
    data = json.loads(report.read_text(encoding="utf-8"))
    out = []
    for c in data["concepts"]:
        pre = c.get("pre_checks", [])
        for e in c.get("exports", []):
            checks = pre + e["checks"]
            out.append(
                ExportRecord(
                    file=_plain_name(e["file"]),
                    concept=c["concept"],
                    format=e["format"],
                    status=e["status"],
                    failed=tuple(x["check"] for x in checks if x["status"] == "fail"),
                    warned=tuple(x["check"] for x in checks if x["status"] == "warn"),
                )
            )
    return out


def load_review(run_dir: Path) -> dict[str, Any]:
    p = Path(run_dir) / REVIEW_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"decisions": {}}


def decide(
    run_dir: Path,
    file: str,
    decision: str,
    note: str = "",
    reviewer: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one decision. Rejections need a note: "why not" is the useful part of a rejection."""
    run_dir = Path(run_dir)
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {DECISIONS}, not {decision!r}")
    if decision == "reject" and not note.strip():
        raise ReviewError("a rejection needs a note")
    records = {r.file: r for r in load_exports(run_dir)}
    if file not in records:
        raise ReviewError(f"{file!r} is not an export of this run (have: {', '.join(records)})")
    rec = records[file]
    review = load_review(run_dir)
    entry = {
        "decision": decision,
        "note": note.strip(),
        "reviewer": reviewer or getpass.getuser(),
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "sha256": file_sha256(run_dir / file),
        "qa_status": rec.status,
        "failed_checks": list(rec.failed),
    }
    review["decisions"][file] = entry
    _write_json(run_dir / REVIEW_FILE, review)
    return entry


def publish(run_dir: Path, dest: Path, override: str | None = None, now: datetime | None = None) -> Path:
    """Copy approved exports to ``dest`` with a manifest, or raise ``ReviewError`` and copy nothing."""
    run_dir, dest = Path(run_dir), Path(dest)
    if override is not None and not override.strip():
        raise ReviewError("--override needs a reason")
    records = load_exports(run_dir)
    decisions = load_review(run_dir)["decisions"]
    approved = [(r, decisions[r.file]) for r in records if decisions.get(r.file, {}).get("decision") == "approve"]
    if not approved:
        raise ReviewError("no export is approved; run `forge review` first")
    problems = []
    for r, d in approved:
        path = run_dir / r.file
        if not path.exists():
            problems.append(f"{r.file}: approved file is missing")
        elif file_sha256(path) != d["sha256"]:
            problems.append(f"{r.file}: changed since it was reviewed; review it again")
    if problems:
        raise ReviewError("; ".join(problems))
    failing = [r for r, _ in approved if r.failed]
    if failing and override is None:
        listed = "; ".join(f"{r.file} ({', '.join(r.failed)})" for r in failing)
        raise ReviewError(f"approved exports have failed QA checks: {listed}. Fix them, or --override 'reason'")

    dest.mkdir(parents=True, exist_ok=True)
    published = []
    for r, d in approved:
        shutil.copy2(run_dir / r.file, dest / r.file)
        published.append(
            {
                "file": r.file,
                "concept": r.concept,
                "format": r.format,
                "sha256": d["sha256"],
                "qa_status": r.status,
                "failed_checks": list(r.failed),
                "overridden": bool(r.failed),
                "reviewer": d["reviewer"],
                "reviewed_at": d["at"],
                "note": d["note"],
            }
        )
    manifest = {
        "run": run_dir.name,
        "published_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "override_reason": override.strip() if failing and override else None,
        "exports": published,
        "not_published": [
            {"file": r.file, "decision": decisions.get(r.file, {}).get("decision", "undecided")}
            for r in records
            if r.file not in {p["file"] for p in published}
        ],
    }
    _write_json(dest / MANIFEST_FILE, manifest)
    return dest / MANIFEST_FILE


def _plain_name(name: str) -> str:
    """review.json and report.json can be edited by hand; never let a file name leave the run directory."""
    if Path(name).name != name or name in ("", ".", ".."):
        raise ReviewError(f"unsafe file name in run report: {name!r}")
    return name


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
