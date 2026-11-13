"""What the pages show, read from the runs under ``--root`` and from the performance store. Nothing here writes.

* A **creative** is one export of one run (a file with its creative ID), with its QA checks, its review decision and,
  when the performance store has rows for its creative ID, its numbers per network from ``perf.report``.
* A **concept** groups the renders (hook x segment x end card x language) of one concept of one spec across runs.
* A **poster** is one frame of an export, cached by the file's size and modification time in the cache folder.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creative_forge.cache import file_sha256
from creative_forge.naming import AdNameError, parse
from creative_forge.review import ExportRecord

DERIVED = ("format", "length_bucket", "ai_generated")
REVIEW_STATES = ("pending", "approve", "reject", "stale")


@dataclass
class Creative:
    rid: str
    run_path: Path
    rec: ExportRecord
    decision: dict[str, Any]
    checks: list[dict[str, Any]]
    pre_checks: list[dict[str, Any]]
    variant: dict[str, str]
    hypothesis: str | None
    timeline: dict[str, float]
    spec: str
    run_mtime: float
    perf: list[dict[str, Any]] = field(default_factory=list)  # one entry per network, most impressions first
    stale: bool = False  # approved or rejected, but the file changed since: the decision no longer applies

    @property
    def file(self) -> str:
        return self.rec.file

    @property
    def path(self) -> Path:
        return self.run_path / self.rec.file

    @property
    def key(self) -> str:
        return f"{self.rid}/{self.rec.file}"

    @property
    def review(self) -> str:
        if self.stale:
            return "stale"
        return self.decision.get("decision") or "pending"

    @property
    def hook_type(self) -> str | None:
        v = self.rec.tags.get("hook_type")
        return v if isinstance(v, str) else None

    @property
    def mechanics(self) -> list[str]:
        v = self.rec.tags.get("mechanic_shown") or []
        return list(v) if isinstance(v, list) else [str(v)]

    @property
    def emotions(self) -> list[str]:
        v = self.rec.tags.get("emotion") or []
        return list(v) if isinstance(v, list) else [str(v)]

    @property
    def format_token(self) -> str:
        return self.rec.format.replace(":", "x")

    @property
    def hand_tags(self) -> dict[str, Any]:
        return {k: v for k, v in self.rec.tags.items() if k not in DERIVED}

    @property
    def variant_label(self) -> str:
        v = self.variant or {}
        parts = [v.get("hook", "main"), v.get("segment", "main"), v.get("end_card", "main")]
        return " / ".join(parts) + (f" · {v['language']}" if v.get("language") else "")

    @property
    def short_label(self) -> str:
        """``oops · tr``: the variant parts that are not ``main``, then the language."""
        v = self.variant or {}
        parts = [v[k] for k in ("hook", "segment", "end_card") if v.get(k) and v.get(k) != "main"] or ["main"]
        return " / ".join(parts) + (f" · {v['language']}" if v.get("language") else "")

    @property
    def primary(self) -> dict[str, Any] | None:
        return self.perf[0] if self.perf else None

    @property
    def poster_time(self) -> float:
        total = float(self.timeline.get("total") or 0)
        return min(0.5, total / 4) if total else 0.5


@dataclass
class Concept:
    spec: str
    concept: str
    hypothesis: str | None
    creatives: list[Creative]

    @property
    def key(self) -> str:
        return f"{self.spec}.{self.concept}"

    @property
    def runs(self) -> list[str]:
        return sorted({c.rid for c in self.creatives})

    @property
    def formats(self) -> list[str]:
        return sorted({c.rec.format for c in self.creatives}, key=lambda f: (f != "9:16", f))

    @property
    def hook_types(self) -> list[str]:
        return sorted({c.hook_type for c in self.creatives if c.hook_type})

    @property
    def mechanics(self) -> list[str]:
        return sorted({m for c in self.creatives for m in c.mechanics})

    @property
    def emotions(self) -> list[str]:
        return sorted({m for c in self.creatives for m in c.emotions})

    @property
    def variants(self) -> list[tuple[str, dict[str, str], list[Creative]]]:
        """(run, variant, exports) per render, newest run first."""
        groups: dict[tuple[str, str], list[Creative]] = defaultdict(list)
        for c in self.creatives:
            groups[(c.rid, json.dumps(c.variant, sort_keys=True))].append(c)
        out = []
        for (rid, v), items in groups.items():
            items.sort(key=lambda c: (c.rec.format != "9:16", c.rec.format))
            out.append((rid, json.loads(v), items))
        out.sort(key=lambda g: (-g[2][0].run_mtime, json.dumps(g[1], sort_keys=True)))
        return out

    def count(self, attr: str, value: str) -> int:
        return sum(1 for c in self.creatives if getattr(c, attr) == value)


def ad_parts(name: str | None) -> dict[str, str] | None:
    """A creative-forge ad name as concept, short variant label, format and ID (for tables); ``None`` otherwise."""
    if not name:
        return None
    try:
        ids = parse(name)
    except AdNameError:
        return None
    label = " / ".join(x for x in (ids.hook, ids.segment, ids.end_card) if x != "main") or "main"
    return {"spec": ids.spec, "concept": ids.concept, "label": f"{label} · {ids.language}",
            "format": ids.format.replace("x", ":"), "id": ids.creative_id}  # fmt: skip


# ---------------------------------------------------------------------------------------------------------- hashes
_SHA_LOCK = threading.Lock()
_SHA: dict[tuple[str, int, int], str] = {}


def cached_sha256(path: Path) -> str | None:
    """The file's SHA-256, remembered per (path, size, mtime) so a gallery of large exports hashes each one once."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _SHA_LOCK:
        if key in _SHA:
            return _SHA[key]
    digest = file_sha256(path)
    with _SHA_LOCK:
        if len(_SHA) > 4096:
            _SHA.clear()
        _SHA[key] = digest
    return digest


# ------------------------------------------------------------------------------------------------------------ runs
def _report(run_path: Path) -> dict[str, Any]:
    try:
        return json.loads((run_path / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def creatives_of(run: Any) -> list[Creative]:
    """Every export of one ``ui.app.RunInfo``, in report order, with checks and lineage from its report.json."""
    data = _report(run.path)
    raw: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for c in data.get("concepts", []):
        for e in c.get("exports", []):
            raw[Path(str(e.get("file", ""))).name] = (c, e)
    out = []
    for rec in run.exports:
        c, e = raw.get(rec.file, ({}, {}))
        spec = data.get("name") or ""
        if rec.ad_name:
            with contextlib.suppress(AdNameError):
                spec = parse(rec.ad_name).spec
        decision = run.decisions.get(rec.file, {})
        cr = Creative(
            rid=run.rid,
            run_path=run.path,
            rec=rec,
            decision=decision,
            checks=list(e.get("checks") or []),
            pre_checks=list(c.get("pre_checks") or []),
            variant=dict(c.get("variant") or {}),
            hypothesis=c.get("hypothesis"),
            timeline=dict(c.get("timeline") or {}),
            spec=spec,
            run_mtime=run.mtime,
        )
        if decision.get("sha256"):
            current = cached_sha256(cr.path)
            cr.stale = current is not None and current != decision["sha256"]
        out.append(cr)
    return out


def all_creatives(runs: list[Any], perf: dict[str, list[dict[str, Any]]] | None = None) -> list[Creative]:
    out = [c for r in runs for c in creatives_of(r)]
    for c in out:
        if perf and c.rec.creative_id:
            c.perf = perf.get(c.rec.creative_id, [])
    return out


def concepts_of(creatives: list[Creative]) -> list[Concept]:
    groups: dict[tuple[str, str], list[Creative]] = defaultdict(list)
    for c in creatives:
        groups[(c.spec, c.rec.concept)].append(c)
    out = []
    for (spec, concept), items in groups.items():
        newest = max(items, key=lambda c: c.run_mtime)
        hypothesis = newest.hypothesis or next((c.hypothesis for c in items if c.hypothesis), None)
        out.append(Concept(spec, concept, hypothesis, items))
    out.sort(key=lambda k: -max(c.run_mtime for c in k.creatives))
    return out


# ----------------------------------------------------------------------------------------------------- performance
def perf_index(db: Path | None, *, min_impressions: float = 10000) -> tuple[dict[str, Any] | None, dict[str, list]]:
    """``perf.report.build_report`` (IPM) and, per creative ID, its row on each network, most impressions first.
    Read only: a missing store gives ``(None, {})``."""
    if db is None or not Path(db).is_file():
        return None, {}
    from creative_forge.perf.report import build_report
    from creative_forge.perf.store import PerfStore

    rep = build_report(PerfStore(Path(db)), metric="ipm", min_impressions=min_impressions)
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for net, d in rep["networks"].items():
        for c in d["creatives"]:
            by_id[c["creative_id"]].append({"network": net, **c})
    for rows in by_id.values():
        rows.sort(key=lambda r: -(r.get("impressions") or 0))
    return rep, dict(by_id)


def import_status(db: Path | None) -> dict[str, dict[str, Any]]:
    """The last import per network from the store's ``imports`` table."""
    if db is None or not Path(db).is_file():
        return {}
    import sqlite3

    out: dict[str, dict[str, Any]] = {}
    try:
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT network, level, rows, matched, unmatched, at FROM imports ORDER BY at ASC, id ASC"
        ).fetchall()
        totals = {
            r["network"]: r
            for r in con.execute(
                "SELECT network, COUNT(*) AS n, SUM(creative_id IS NOT NULL) AS matched, MIN(date) AS first, "
                "MAX(date_end) AS last FROM perf_rows GROUP BY network"
            )
        }
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    for r in rows:
        try:
            unmatched = len(json.loads(r["unmatched"] or "[]"))
        except ValueError:
            unmatched = 0
        out[r["network"]] = {
            "level": r["level"],
            "rows": r["rows"],
            "matched": r["matched"],
            "unmatched": unmatched,
            "at": r["at"],
        }
    for net, t in totals.items():
        out.setdefault(net, {"level": "", "rows": 0, "matched": 0, "unmatched": 0, "at": None})
        out[net] |= {"stored": t["n"], "stored_matched": t["matched"] or 0, "first": t["first"], "last": t["last"]}
    return out


def known_creatives(creatives: list[Creative], db: Path | None) -> list[dict[str, Any]]:
    """Creative IDs a test can use: every export with an ID, plus IDs the performance store knows (registered from a
    manifest, or matched in a network's ad names) that are not in the runs under the root."""
    out: dict[str, dict[str, Any]] = {}
    for c in creatives:
        cid = c.rec.creative_id
        if cid and cid not in out:
            out[cid] = {
                "creative_id": cid,
                "ad_name": c.rec.ad_name,
                "concept": c.rec.concept,
                "variant": c.short_label,
                "format": c.rec.format,
                "key": c.key,
                "rid": c.rid,
                "file": c.file,
            }
    if db is not None and Path(db).is_file():
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT creative_id, MIN(ad_name) FROM perf_rows WHERE creative_id IS NOT NULL "
                    "GROUP BY creative_id UNION SELECT creative_id, ad_name FROM creatives"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            rows = []
        for cid, name in rows:
            if cid in out:
                continue
            ids = None
            with contextlib.suppress(AdNameError, AttributeError):
                ids = parse(name)
            out[cid] = {
                "creative_id": cid,
                "ad_name": name,
                "concept": ids.concept if ids else "",
                "variant": (" / ".join(x for x in (ids.hook, ids.segment, ids.end_card) if x != "main") or "main")
                + f" · {ids.language}"
                if ids
                else "",
                "format": ids.format.replace("x", ":") if ids else "",
                "key": None,
                "rid": None,
                "file": None,
            }
    return list(out.values())


# --------------------------------------------------------------------------------------------------------- posters
def poster(video: Path, t: float, cache: Path, width: int = 360) -> Path | None:
    """One frame of ``video`` as a JPEG, made once per (path, size, mtime) and kept under ``cache``. ``None`` when the
    file does not decode (ffmpeg missing, not a video)."""
    try:
        st = video.stat()
    except OSError:
        return None
    key = hashlib.sha256(f"{video.resolve()}|{st.st_size}|{st.st_mtime_ns}|{t:.2f}|{width}".encode()).hexdigest()[:32]
    out = cache / f"{key}.jpg"
    if out.is_file():
        return out
    from creative_forge.ffmpeg import extract_frame

    try:
        im = extract_frame(video, t)
    except Exception:  # ffmpeg missing, a broken or partial file: the card shows its placeholder instead
        return None
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))))
    cache.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{threading.get_ident()}.tmp")
    im.save(tmp, format="JPEG", quality=82)
    tmp.replace(out)
    return out
