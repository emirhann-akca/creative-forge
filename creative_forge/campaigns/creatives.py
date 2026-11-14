"""Which exports a campaign may use: only what ``forge publish`` delivered, with a clean QA record and a reviewer.

The source is a ``forge publish`` folder. Its ``manifest.json`` lists the exports a person approved, by hash. Each
export is checked again here, because a manifest is a file anyone can edit:

* its ad name parses and its creative id matches (``naming.parse``);
* the file exists and its SHA-256 is still the one the reviewer approved;
* QA passed (``pass`` or ``warn``), no check failed and nothing was published with ``--override``;
* a reviewer is recorded.

Everything else is listed as excluded with the reason, so the preview says why a creative is missing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from creative_forge.cache import file_sha256
from creative_forge.naming import AdNameError, format_ratio, parse

MANIFEST = "manifest.json"


class CreativeGateError(ValueError):
    """No usable creative: nothing approved, or the folder is not a ``forge publish`` output."""


@dataclass(frozen=True)
class Creative:
    path: Path
    ad_name: str
    creative_id: str
    format: str  # "9:16" or "4:5"
    concept: str
    sha256: str
    reviewer: str
    qa_status: str

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass(frozen=True)
class Excluded:
    file: str
    reason: str


def load_creatives(folder: Path, formats: list[str] | None = None) -> tuple[list[Creative], list[Excluded]]:
    folder = Path(folder)
    manifest = folder / MANIFEST
    if not manifest.is_file():
        raise CreativeGateError(
            f"{manifest} not found: a campaign uses only a `forge publish` folder (approved, QA-clean exports)"
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as e:
        raise CreativeGateError(f"{manifest}: not valid JSON ({e})") from None
    ok: list[Creative] = []
    out: list[Excluded] = []
    for e in data.get("exports") or []:
        name = str(e.get("file", ""))
        if Path(name).name != name or not name:
            out.append(Excluded(name or "?", "unsafe file name in manifest"))
            continue
        try:
            ids = parse(str(e.get("ad_name") or ""))
        except AdNameError:
            out.append(Excluded(name, "no valid creative-forge ad name"))
            continue
        fmt = format_ratio(ids.format)
        path = folder / name
        reason = None
        if e.get("overridden") or e.get("failed_checks"):
            reason = "published over failed QA checks (--override); a campaign never uses it"
        elif str(e.get("qa_status")) not in ("pass", "warn"):
            reason = f"QA status is {e.get('qa_status')!r}, not pass or warn"
        elif not str(e.get("reviewer") or "").strip():
            reason = "no reviewer recorded"
        elif not path.is_file():
            reason = "file missing"
        elif file_sha256(path) != e.get("sha256"):
            reason = "file changed after it was approved (SHA-256 differs); review and publish it again"
        elif formats and fmt not in formats:
            reason = f"format {fmt} is not used by this campaign"
        if reason:
            out.append(Excluded(name, reason))
            continue
        ok.append(
            Creative(
                path=path,
                ad_name=ids.ad_name,
                creative_id=ids.creative_id,
                format=fmt,
                concept=ids.concept,
                sha256=str(e["sha256"]),
                reviewer=str(e["reviewer"]),
                qa_status=str(e["qa_status"]),
            )
        )
    ok.sort(key=lambda c: c.ad_name)
    return ok, out
