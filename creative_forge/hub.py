"""One read-only snapshot of what creative-forge holds, for another local tool to show (``forge snapshot --json``).

studio-pulse's ad centre (``/reklam``) runs ``forge snapshot --json`` as a subprocess and reads this, so the two tools
share no database and no process: creative-forge stays the only writer of its runs, reviews, campaign state and
audit log, and every change still goes through its own commands (``forge make``, ``forge review``, ``forge publish``,
``forge campaign create|activate|pause``) with their gates.

Schema ``creative-forge/snapshot/v1`` (keys are only ever added):

* ``creatives``: every export under ``--root`` with its run, creative ID, ad name, format, QA status and failed
  checks, the review decision (``pending`` / ``approve`` / ``reject`` / ``stale`` when the file changed since), the
  game it was made for (``app``: the ``app.json`` that ``forge make`` writes next to a run) and the absolute paths
  of the file and of a poster frame (cached JPEG; ``null`` when ffmpeg cannot read the file).
* ``runs``, ``published`` (``forge publish`` folders with their manifest), ``configs`` (campaign YAML files under
  ``<root>/campaigns``), ``campaigns`` (what ``forge campaign create`` recorded, with status, caps and last sync),
  ``packs`` (manual upload packs for networks without a paused-create API), ``playables`` (sources under
  ``<root>/playables/src``, packed builds and saved check reports).
* ``timeline``: the audit log read as changes a person would name (campaign created paused, switched on, paused,
  paused automatically, budget changed outside forge, new creatives, manual pack written, a stop or a refusal), each
  with its audit record's hash, so a marker on a chart can be traced to the line that recorded it. ``audit`` says
  whether the hash chain is intact.

A store URL is reduced to its store id (Android package or App Store id) in ``store_id`` so the reader can match a
campaign to a game without parsing URLs itself.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creative_forge import __version__

SCHEMA = "creative-forge/snapshot/v1"
APP_FILE = "app.json"  # written by `forge make` next to a run's report.json: which game the run is for


def store_id(url: str | None) -> str | None:
    """The package name or App Store id of a store URL, ``None`` when it is not one."""
    from creative_forge.campaigns.store_url import StoreUrlError, parse_store_url

    try:
        return parse_store_url(str(url or "")).app_id
    except StoreUrlError:
        return None


def read_app(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(folder) / APP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None
    out = {k: data.get(k) for k in ("id", "name", "store_url") if isinstance(data.get(k), str)}
    out["store_id"] = store_id(out.get("store_url"))
    return out


def write_app(folder: Path, app_id: str, name: str | None = None, store_url: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = {"id": app_id, "name": name or app_id, "store_url": store_url or None}
    p = folder / APP_FILE
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------------------------------------- runs
def creatives(root: Path, *, posters: bool = True, cache: Path | None = None) -> tuple[list[dict], list[dict]]:
    from creative_forge.paths import cache_dir
    from creative_forge.ui import catalog
    from creative_forge.ui.app import find_runs

    cache = cache or cache_dir() / "posters"
    out, runs = [], []
    for run in find_runs(root):
        app = read_app(run.path)
        runs.append(
            {
                "rid": run.rid,
                "path": str(run.path),
                "status": run.status,
                "exports": len(run.exports),
                "approved": run.approved,
                "modified": datetime.fromtimestamp(run.mtime, UTC).isoformat(timespec="seconds"),
                "app": app,
            }
        )
        for c in catalog.creatives_of(run):
            poster = catalog.poster(c.path, c.poster_time, cache) if posters else None
            out.append(
                {
                    "key": c.key,
                    "rid": c.rid,
                    "file": c.file,
                    "path": str(c.path),
                    "poster": str(poster) if poster else None,
                    "creative_id": c.rec.creative_id,
                    "ad_name": c.rec.ad_name,
                    "spec": c.spec,
                    "concept": c.rec.concept,
                    "variant": c.short_label,
                    "format": c.rec.format,
                    "kind": "video",
                    "qa_status": c.rec.status,
                    "failed": list(c.rec.failed),
                    "warned": list(c.rec.warned),
                    "review": c.review,
                    "decision": {k: c.decision.get(k) for k in ("decision", "note", "reviewer", "at")}
                    if c.decision
                    else None,
                    "hypothesis": c.hypothesis,
                    "tags": c.hand_tags,
                    "seconds": c.timeline.get("total"),
                    "app": app,
                    "modified": datetime.fromtimestamp(c.run_mtime, UTC).isoformat(timespec="seconds"),
                }
            )
    return out, runs


def published(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    out = []
    for pattern in ("*/manifest.json", "*/*/manifest.json", "*/*/*/manifest.json"):
        for m in root.glob(pattern):
            try:
                data = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            exports = data.get("exports") if isinstance(data, dict) else None
            if not isinstance(exports, list):
                continue
            out.append(
                {
                    "folder": str(m.parent),
                    "rel": m.parent.relative_to(root).as_posix(),
                    "exports": len(exports),
                    "ad_names": [e.get("ad_name") for e in exports if isinstance(e, dict) and e.get("ad_name")],
                    "app": read_app(m.parent),
                }
            )
    return sorted(out, key=lambda p: p["rel"])


# ------------------------------------------------------------------------------------------------------ campaigns
def configs(root: Path) -> list[dict[str, Any]]:
    from creative_forge.campaigns.config import CampaignConfigError, load_config

    folder = Path(root) / "campaigns"
    out = []
    for p in sorted(folder.glob("*.y*ml")) if folder.is_dir() else []:
        item: dict[str, Any] = {"name": p.name, "path": str(p)}
        try:
            cfg = load_config(p)
        except (CampaignConfigError, ValueError) as e:
            item["error"] = str(e)
            out.append(item)
            continue
        nets = {n: getattr(cfg.networks, n).mode for n in ("meta", "tiktok", "google", "applovin", "unity", "mintegral")
                if getattr(cfg.networks, n) is not None}  # fmt: skip
        item.update(
            config=cfg.name,
            app=cfg.app,
            store_url=cfg.store_url,
            store_id=store_id(cfg.store_url),
            daily=cfg.budget.daily,
            total_cap=cfg.budget.total_cap,
            currency=cfg.currency,
            countries=cfg.countries,
            networks=nets,
            creatives=str(cfg.creatives_dir),
        )
        out.append(item)
    return out


def campaigns(state: Any) -> list[dict[str, Any]]:
    from creative_forge.campaigns.engine import confirmation_phrase

    out = []
    for r in state.campaigns():
        d = {k: v for k, v in r.__dict__.items() if k not in ("settings",)}
        d["ref"] = r.ref
        d["store_id"] = store_id(r.store_url)
        d["phrase"] = {"tr": confirmation_phrase(r, "tr"), "en": confirmation_phrase(r, "en")}
        out.append(d)
    return out


def packs(state: Any) -> list[dict[str, Any]]:
    folder = Path(state.dir) / "manual"
    out = []
    for p in sorted(folder.glob("*/*/pack.json")) if folder.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["folder"] = str(p.parent)
            data["plan_id"] = p.parent.parent.name
            data["store_id"] = store_id(data.get("store_url"))
            out.append(data)
    return out


# Audit record kinds -> the change a person would name. Records that are plumbing (api_call, reconcile, sync without a
# change) are not changes and stay out of the timeline.
KINDS = {
    "campaign_created": "created_paused",
    "campaign_activated": "activated",
    "campaign_paused": "paused",
    "auto_pause": "auto_paused",
    "safety_pause": "safety_paused",
    "manual_packs": "manual_pack",
    "create_stopped": "stopped",
    "activate_refused": "refused",
    "status_drift": "status_drift",
}


def timeline(state: Any, records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The audit log as dated changes, oldest first. ``budget_changed`` is derived from two syncs that read a different
    daily budget from the network (forge itself never changes a budget)."""
    from creative_forge.campaigns.audit import AuditLog

    recs = AuditLog(state.audit_path).records() if records is None else records
    by_ref = {r.ref: r for r in state.campaigns()}
    by_plan: dict[str, list[Any]] = {}
    for r in by_ref.values():
        by_plan.setdefault(r.plan_id, []).append(r)
    activated: dict[str, dict[str, Any]] = {}
    for rec in recs:
        if rec.get("kind") == "activate_confirmed":
            activated[str(rec.get("campaign"))] = rec
    last_daily: dict[str, float] = {}
    out = []
    for rec in recs:
        kind = rec.get("kind")
        ref = str(rec.get("campaign") or "")
        camp = by_ref.get(ref)
        detail: dict[str, Any] = {}
        event = KINDS.get(str(kind))
        if kind == "sync":
            daily = rec.get("daily")
            if isinstance(daily, (int, float)):
                before = last_daily.get(ref)
                last_daily[ref] = float(daily)
                if before is not None and abs(before - float(daily)) > 0.005:
                    event, detail = "budget_changed", {"before": before, "after": float(daily)}
            if event is None:
                continue
        if event is None:
            continue
        if kind == "campaign_created":
            detail = {k: rec.get(k) for k in ("daily", "total_cap", "start", "end")}
            if camp is not None:
                detail["creatives"] = sorted(camp.ads)
        elif kind == "campaign_activated":
            conf = activated.get(ref) or {}
            detail = {k: conf.get(k) for k in ("daily", "total_cap", "currency")}
            if camp is not None:
                detail["creatives"] = sorted(camp.ads)
        elif kind in ("campaign_paused", "auto_pause", "safety_pause", "create_stopped"):
            detail = {"reason": rec.get("reason"), "target": rec.get("target")}
        elif kind == "activate_refused":
            detail = {"problems": rec.get("problems")}
        elif kind == "manual_packs":
            detail = {"folders": rec.get("folders")}
        elif kind == "status_drift":
            detail = {"forge": rec.get("forge"), "network": rec.get("network")}
        plan = str(rec.get("plan") or (ref.split(":", 1)[0] if ref else ""))
        net = (camp.network if camp else None) or rec.get("network") or (ref.split(":", 1)[1] if ":" in ref else None)
        siblings = by_plan.get(plan) or []
        store = (camp.store_url if camp else None) or (siblings[0].store_url if siblings else None)
        out.append(
            {
                "at": rec.get("at"),
                "kind": event,
                "campaign": ref or None,
                "campaign_name": camp.name if camp else None,
                "network": net,
                "plan": plan or None,
                "mode": camp.mode if camp else None,
                "store_id": store_id(store),
                "detail": {k: v for k, v in detail.items() if v is not None},
                "hash": rec.get("hash"),
                "seq": rec.get("seq"),
            }
        )
    return out


# ------------------------------------------------------------------------------------------------------ playables
def playables(root: Path) -> list[dict[str, Any]]:
    """Sources under ``<root>/playables/src`` (an .html file, or a folder with index.html), packed builds under
    ``<root>/playables/packed/<network>/`` and the newest saved check per file (``<root>/playables/*.json``)."""
    base = Path(root) / "playables"
    src = base / "src"
    out: list[dict[str, Any]] = []
    checks: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(base.glob("*.json"), key=lambda q: q.stat().st_mtime) if base.is_dir() else []:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for r in data:
                    if isinstance(r, dict) and "network" in r and "file" in r:
                        checks.setdefault(Path(str(r["file"])).name, []).append(
                            {"network": r["network"], "status": r.get("status"), "report": p.name}
                        )
    entries = []
    if src.is_dir():
        for p in sorted(src.iterdir()):
            if p.suffix.lower() == ".html" and p.is_file():
                entries.append((p.stem, p, p))
            elif p.is_dir() and (p / "index.html").is_file():
                entries.append((p.name, p, p / "index.html"))
    for name, path, html in entries:
        builds = []
        for b in sorted((base / "packed").glob(f"*/{name}.*")) if (base / "packed").is_dir() else []:
            if b.name.endswith(".check.json"):
                continue
            net = b.parent.name
            own = b.parent / f"{name}.{net}.check.json"  # written by `forge playable pack --json`
            found = list(checks.get(b.name, []))
            with contextlib.suppress(OSError, ValueError):
                for r in json.loads(own.read_text(encoding="utf-8")):
                    found.append({"network": r.get("network"), "status": r.get("status"), "report": own.name,
                                  "problems": [f"{f.get('status')}: {f.get('rule')}" for f in r.get("findings") or []
                                               if f.get("status") in ("fail", "uncertain", "warn")][:4]})  # fmt: skip
            builds.append({"network": net, "path": str(b), "size": _size(b), "checks": found})
        out.append(
            {
                "name": name,
                "path": str(path),
                "html": str(html),
                "size": _size(path),
                "app": read_app(path if path.is_dir() else path.parent),
                "builds": builds,
                "checks": checks.get(path.name, []),
            }
        )
    return out


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


# ------------------------------------------------------------------------------------------------------- snapshot
def snapshot(
    root: Path, *, state_dir: Path | None = None, perf_db: Path | None = None, posters: bool = True
) -> dict[str, Any]:
    from creative_forge.campaigns.audit import AuditLog
    from creative_forge.campaigns.state import CampaignState
    from creative_forge.paths import data_dir

    root = Path(root).resolve()
    state = CampaignState(Path(state_dir).resolve() if state_dir else None)
    items, runs = creatives(root, posters=posters)
    audit = AuditLog(state.audit_path)
    check = audit.verify()
    return {
        "schema": SCHEMA,
        "version": __version__,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "root": str(root),
        "data_dir": str(data_dir()),
        "campaign_state": str(state.dir),
        "perf_db": str(perf_db) if perf_db else None,
        "creatives": items,
        "runs": runs,
        "published": published(root),
        "configs": configs(root),
        "campaigns": campaigns(state),
        "packs": packs(state),
        "playables": playables(root),
        "timeline": timeline(state),
        "audit": {"ok": check.ok, "records": check.records, "problem": check.problem, "path": str(audit.path)},
    }
