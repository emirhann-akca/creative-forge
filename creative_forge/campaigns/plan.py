"""The plan: every object a ``create`` would make, with its exact payload and idempotency key, and nothing sent.

Keys are deterministic. A plan's id is a hash of the campaign name and the promoted app; an object's key is a hash of
(plan id, network, kind, logical name). Re-running the same config therefore produces the same keys, so the journal
(``state.ops``) and the network (the key is in every object's name as ``[cf:<10 hex>]``) both recognise objects that
already exist. A payload that differs from what was created is shown as changed and is NOT updated: forge creates and
switches status, it does not edit live objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from creative_forge.campaigns.config import CampaignConfig
from creative_forge.campaigns.creatives import Creative, Excluded, load_creatives
from creative_forge.campaigns.state import DONE, CampaignState
from creative_forge.campaigns.store_url import StoreListing

TAG = "cf:"
TAG_RE = re.compile(r"\[cf:([0-9a-f]{10})\]")


def plan_id_of(cfg: CampaignConfig, listing: StoreListing) -> str:
    return hashlib.sha256(f"{cfg.name}|{listing.platform}|{listing.app_id}".encode()).hexdigest()[:10]


def op_key(plan_id: str, network: str, kind: str, logical: str) -> str:
    return hashlib.sha256(f"{plan_id}|{network}|{kind}|{logical}".encode()).hexdigest()[:16]


def tagged(name: str, key: str) -> str:
    return f"{name} [{TAG}{key[:10]}]"


def tagged_short(name: str, key: str, limit: int = 100) -> str:
    """For fields with a length limit (YouTube titles, TikTok file names): the key first, so truncation keeps it."""
    return f"[{TAG}{key[:10]}] {name}"[:limit]


def tag_of(name: str) -> str:
    """``cf:<10 hex>`` from an object name, wherever the key sits in it."""
    m = TAG_RE.search(name)
    if not m:
        raise ValueError(f"no idempotency key in {name!r}")
    return f"{TAG}{m.group(1)}"


def ref(key: str) -> dict[str, str]:
    """A payload placeholder for the remote id an earlier op produced."""
    return {"$ref": key}


def payload_sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Op:
    key: str
    network: str
    kind: str  # campaign, adset, video, cover, creative, ad, budget, criteria, adgroup, video_upload, video_asset
    name: str  # the object's name on the network (carries the key)
    method: str
    path: str  # relative to the network's base URL
    payload: dict[str, Any]
    summary: str
    upload: Path | None = None
    creative: str | None = None  # the ad name of the creative this op belongs to

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ("key", "kind", "name", "method", "path", "payload", "summary")}
        if self.upload:
            d["upload"] = {"file": self.upload.name, "bytes": self.upload.stat().st_size}
        return d


@dataclass
class PlanContext:
    cfg: CampaignConfig
    listing: StoreListing
    plan_id: str
    network: str
    creatives: list[Creative]
    start: date
    end: date

    def key(self, kind: str, logical: str = "") -> str:
        return op_key(self.plan_id, self.network, kind, logical)

    @property
    def base_name(self) -> str:
        return f"{self.cfg.name} {self.network}"


@dataclass
class NetworkPlan:
    network: str
    mode: str
    account: str
    ops: list[Op]
    creatives: list[Creative]
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def campaign_op(self) -> Op | None:
        return next((o for o in self.ops if o.kind == "campaign"), None)


@dataclass
class ManualPlan:
    network: str
    creatives: list[Creative]
    doc: str
    reason: str


@dataclass
class Plan:
    cfg: CampaignConfig
    plan_id: str
    listing: StoreListing
    start: date
    end: date
    networks: list[NetworkPlan]
    manual: list[ManualPlan]
    excluded: list[Excluded]
    all_creatives: list[Creative]

    @property
    def problems(self) -> list[str]:
        return [f"{n.network}: {p}" for n in self.networks for p in n.problems]

    def status_of(self, state: CampaignState, net: NetworkPlan) -> list[tuple[Op, str, str | None]]:
        """(op, "create" | "exists" | "changed" | "reconcile", remote id) for the preview."""
        known = state.ops_of(self.plan_id, net.network)
        out = []
        for op in net.ops:
            rec = known.get(op.key)
            if rec is None:
                out.append((op, "create", None))
            elif rec.state == DONE and rec.request_sha and rec.request_sha != payload_sha(op.payload):
                out.append((op, "changed", rec.remote_id))
            elif rec.state == DONE:
                out.append((op, "exists", rec.remote_id))
            else:
                out.append((op, "reconcile", rec.remote_id))
        return out

    def to_dict(self, state: CampaignState | None = None) -> dict[str, Any]:
        cfg = self.cfg
        nets = []
        for n in self.networks:
            rows = self.status_of(state, n) if state else [(o, "create", None) for o in n.ops]
            nets.append(
                {
                    "network": n.network,
                    "mode": n.mode,
                    "account": n.account,
                    "notes": n.notes,
                    "problems": n.problems,
                    "creatives": [c.ad_name for c in n.creatives],
                    "ops": [o.to_dict() | {"state": s, "remote_id": rid} for o, s, rid in rows],
                }
            )
        return {
            "plan_id": self.plan_id,
            "name": cfg.name,
            "store_url": self.listing.url,
            "platform": self.listing.platform,
            "app_id": self.listing.app_id,
            "currency": cfg.currency,
            "daily": cfg.budget.daily,
            "total_cap": cfg.budget.total_cap,
            "limits": {"max_daily": cfg.limits.max_daily, "max_total": cfg.limits.max_total},
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": cfg.days,
            "auto_pause": cfg.automation.auto_pause,
            "networks": nets,
            "manual": [
                {"network": m.network, "doc": m.doc, "reason": m.reason, "creatives": [c.ad_name for c in m.creatives]}
                for m in self.manual
            ],
            "excluded": [{"file": e.file, "reason": e.reason} for e in self.excluded],
        }


def build_plan(cfg: CampaignConfig, today: date | None = None) -> Plan:
    """Validate everything that can be validated offline and build every network's ops. Raises on a bad store URL
    or when no approved creative is usable; per-network problems are collected on the network plan."""
    from creative_forge.campaigns.networks import adapter_class, manual_plan

    listing = cfg.listing()
    pid = plan_id_of(cfg, listing)
    start, end = cfg.start_date(today), cfg.end_date(today)
    if today is not None and start < today:
        raise ValueError(f"budget.start {start} is in the past")
    every, excluded = load_creatives(cfg.creatives_dir)
    if not every:
        raise ValueError(
            "no approved, QA-clean creative in "
            f"{cfg.creatives_dir}: " + ("; ".join(f"{e.file}: {e.reason}" for e in excluded) or "the manifest is empty")
        )
    nets: list[NetworkPlan] = []
    for name, net in cfg.api_networks():
        formats = cfg.formats_for(name)
        chosen = [c for c in every if c.format in formats]
        ctx = PlanContext(cfg, listing, pid, name, chosen, start, end)
        cls = adapter_class(name)
        np = NetworkPlan(name, net.mode, cls.account_of(net), [], chosen)
        if not chosen:
            np.problems.append(f"no approved creative in {', '.join(formats)}")
        elif len(chosen) > 5:
            np.problems.append(f"{len(chosen)} creatives; a test cell holds a control and at most 4 variants")
        else:
            np.ops, np.notes = cls.build(ctx, net)
        nets.append(np)
    manual = [manual_plan(n, [c for c in every if c.format in cfg.formats_for(n)]) for n in cfg.manual_networks()]
    return Plan(cfg, pid, listing, start, end, nets, manual, excluded, every)


def resolve(payload: Any, ids: dict[str, str]) -> Any:
    """Replace ``{"$ref": key}`` with the remote id of that op (KeyError when it does not exist yet)."""
    if isinstance(payload, dict):
        if set(payload) == {"$ref"}:
            return ids[payload["$ref"]]
        return {k: resolve(v, ids) for k, v in payload.items()}
    if isinstance(payload, list):
        return [resolve(v, ids) for v in payload]
    return payload


def human_size(path: Path) -> str:
    n = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def preview_text(plan: Plan, state: CampaignState) -> str:
    """The dry-run diff: what would be created, what already exists, what changed and would NOT be updated."""
    cfg = plan.cfg
    lines = [
        f"plan {plan.plan_id}  {cfg.name}  ({plan.listing.platform}: {plan.listing.app_id})",
        f"store URL  {plan.listing.url}  (format checked; not a placeholder)",
        f"budget     {cfg.budget.daily:.2f} {cfg.currency}/day, total cap {cfg.budget.total_cap:.2f} {cfg.currency} "
        f"-> {cfg.days} day(s): {plan.start} .. {plan.end}",
        f"limits     max {cfg.limits.max_daily:.2f}/day, max {cfg.limits.max_total:.2f} total; "
        f"auto-pause {'ON' if cfg.automation.auto_pause else 'off (recommendations only)'}; auto-scale never",
        f"creatives  {len(plan.all_creatives)} approved and QA-clean, {len(plan.excluded)} excluded",
    ]
    for e in plan.excluded:
        lines.append(f"  - {e.file}: {e.reason}")
    for n in plan.networks:
        lines.append("")
        lines.append(f"{n.network}  mode={n.mode}  account={n.account}")
        for note in n.notes:
            lines.append(f"  note: {note}")
        for p in n.problems:
            lines.append(f"  PROBLEM: {p}")
        for op, st, rid in plan.status_of(state, n):
            mark = {"create": "+", "exists": "=", "changed": "~", "reconcile": "?"}[st]
            extra = f"  -> {rid}" if rid else ""
            if st == "changed":
                extra += "  (differs from what was created; NOT updated: rename the campaign to create anew)"
            if st == "reconcile":
                extra += "  (an earlier call ended without an answer; looked up by key before any create)"
            lines.append(f"  {mark} {op.kind:<12} {op.summary}{extra}")
    for m in plan.manual:
        lines.append("")
        lines.append(f"{m.network}  manual  ({len(m.creatives)} file(s) in the upload pack)")
        lines.append(f"  {m.reason}")
        lines.append(f"  {m.doc}")
    counts = {"create": 0, "exists": 0, "changed": 0, "reconcile": 0}
    for n in plan.networks:
        for _, st, _ in plan.status_of(state, n):
            counts[st] += 1
    lines.append("")
    lines.append(
        f"{counts['create']} to create, {counts['exists'] + counts['changed']} already exist "
        f"({counts['changed']} changed, not updated), {counts['reconcile']} to look up. "
        "Everything is created PAUSED; nothing can spend before `forge campaign activate`."
    )
    return "\n".join(lines)
