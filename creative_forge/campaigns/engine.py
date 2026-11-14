"""Create, activate, pause and sync: the steps of ``forge campaign`` and of the UI's Kampanya page.

Every step writes to the audit log; every network call goes through the adapters' audited client. The rules:

* ``create`` refuses the whole plan when any network has a problem, checks each account's currency (and, in sandbox
  mode, that it is a sandbox) before the first write, executes ops in order with the idempotency journal, and after
  the last op reads the campaign back: one that is not PAUSED is paused at once and reported as an error.
* ``activate`` needs the typed phrase ``YAYINA AL <daily> <currency>`` (or ``ACTIVATE ...``), re-checks the daily and
  total caps against the config, re-reads budget, currency and spend from the network, then switches ads, ad sets and
  the campaign on (the campaign last) and locks the test cell's rule.
* ``pause`` switches the campaign (the master switch) off. It needs no phrase: pausing is always allowed.
* ``sync`` reads status, budget and spend, imports per-creative delivery into the performance store, judges the test
  cell and produces recommendations; with ``auto_pause`` it pauses killed ads and a campaign at its cap, nothing else.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import requests

from creative_forge.campaigns.audit import AuditLog
from creative_forge.campaigns.config import CampaignConfig
from creative_forge.campaigns.http import AmbiguousWrite, NetworkError, WriteClient
from creative_forge.campaigns.networks import SafetyError, adapter_class, write_packs
from creative_forge.campaigns.networks.base import Adapter
from creative_forge.campaigns.plan import Plan, build_plan, payload_sha, resolve
from creative_forge.campaigns.recommend import PAUSE_AD, PAUSE_CAMPAIGN, Recommendation, recommend
from creative_forge.campaigns.state import (
    ACTIVE,
    DONE,
    PAUSED,
    PENDING,
    UNKNOWN,
    CampaignRecord,
    CampaignState,
    now,
)
from creative_forge.perf.cells import CellError, CellResult, CellStore, Rule, evaluate
from creative_forge.perf.http import Redactor
from creative_forge.perf.store import ImportSummary, PerfStore, default_db
from creative_forge.retry import RetryPolicy


def confirmation_phrase(rec: CampaignRecord, lang: str = "tr") -> str:
    word = "YAYINA AL" if lang == "tr" else "ACTIVATE"
    return f"{word} {rec.daily:.2f} {rec.currency}"


def _normal(phrase: str) -> str:
    return " ".join(phrase.replace(",", ".").split()).upper()


@dataclass
class NetworkResult:
    network: str
    created: int = 0
    existing: int = 0
    reconciled: int = 0
    stopped: str | None = None
    campaign: CampaignRecord | None = None


@dataclass
class CreateReport:
    plan: Plan
    networks: list[NetworkResult]
    packs: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(n.stopped is None for n in self.networks)


@dataclass
class SyncResult:
    campaign: CampaignRecord
    info: dict[str, Any]
    rows: int
    cell: CellResult | None
    recommendations: list[Recommendation]


class CampaignEngine:
    def __init__(
        self,
        state: CampaignState | None = None,
        *,
        mock_url: str | None = None,
        allow_live: bool = False,
        perf_db: Path | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
        today: date | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.state = state or CampaignState()
        self.audit = AuditLog(self.state.audit_path)
        self._mock_url = mock_url
        self._mock_server: Any = None
        self.allow_live = allow_live
        self.perf_db = Path(perf_db) if perf_db else default_db()
        self.session = session or requests.Session()
        self.sleep = sleep
        self.log = log or (lambda _m: None)
        self.today = today
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------------------------------------- plumbing
    def __enter__(self) -> CampaignEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._mock_server is not None:
            self._mock_server.stop()
            self._mock_server = None

    def day(self) -> date:
        return self.today or date.today()

    @property
    def mock_path(self) -> Path:
        return self.state.dir / "mock_network.json"

    def mock_url(self) -> str:
        if self._mock_url is None:
            from creative_forge.campaigns.mock_server import MockNetwork, MockServer

            self._mock_server = MockServer(MockNetwork(self.mock_path))
            self._mock_url = self._mock_server.start()
        return self._mock_url

    def adapter(
        self, network: str, mode: str, account: str, settings: dict[str, Any], *, safe_direction: bool = False
    ) -> Adapter:
        """``safe_direction``: reading, syncing and pausing need no --live; creating and activating do."""
        if mode == "live" and not (self.allow_live or safe_direction):
            raise SafetyError(
                f"{network} is in live mode: real money. Pass --live (CLI) or tick the live box (UI) to write to it"
            )
        cls = adapter_class(network)
        secrets = cls.load_secrets(mode)
        client = WriteClient(
            network,
            mode,
            self.audit,
            Redactor(*secrets.values()),
            session=self.session,
            classify=cls.classify,
            timeout_s=self.timeout_s,
            retry=RetryPolicy(attempts=5, base_delay_s=1.0, max_delay_s=60.0),
            sleep=self.sleep,
            log=self.log,
        )
        base = cls.base_for(mode, self.mock_url() if mode == "mock" else None)
        return cls(
            mode=mode,
            account=account,
            settings=settings,
            client=client,
            base_url=base,
            secrets=secrets,
            sleep=self.sleep,
        )

    def _adapter_for(self, rec: CampaignRecord, *, safe_direction: bool = False) -> Adapter:
        return self.adapter(rec.network, rec.mode, rec.account, rec.settings, safe_direction=safe_direction)

    def _require(self, ref: str) -> CampaignRecord:
        rec = self.state.campaign(ref)
        if rec is None:
            raise SafetyError(f"no campaign {ref!r} in {self.state.path} (forge campaign status lists them)")
        return rec

    # ----------------------------------------------------------------------------------------------------- plan
    def plan(self, cfg: CampaignConfig) -> Plan:
        return build_plan(cfg, self.day())

    # --------------------------------------------------------------------------------------------------- create
    def create(self, cfg: CampaignConfig, networks: list[str] | None = None) -> CreateReport:
        plan = self.plan(cfg)
        if plan.problems:
            raise SafetyError("plan has problems, nothing was created: " + "; ".join(plan.problems))
        chosen = [n for n in plan.networks if networks is None or n.network in networks]
        for n in chosen:  # refuse live before the first write anywhere
            if n.mode == "live" and not self.allow_live:
                raise SafetyError(f"{n.network} is in live mode: real money. Pass --live to write to it")
        self.audit.append(
            "create_requested",
            plan=plan.plan_id,
            config=cfg.name,
            networks=[n.network for n in chosen],
            daily=cfg.budget.daily,
            total_cap=cfg.budget.total_cap,
            currency=cfg.currency,
            store_url=plan.listing.url,
        )
        results = []
        for n in chosen:
            net_cfg = getattr(cfg.networks, n.network)
            cls = adapter_class(n.network)
            adapter = self.adapter(n.network, n.mode, n.account, cls.settings_of(net_cfg))
            res = NetworkResult(n.network)
            results.append(res)
            try:
                adapter.preflight(cfg.currency)
            except (SafetyError, NetworkError) as e:
                res.stopped = str(e)
                self.audit.append("create_stopped", plan=plan.plan_id, network=n.network, reason=str(e))
                continue
            ids: dict[str, str] = {}
            for op in n.ops:
                rec = self.state.op(op.key)
                if rec and rec.state == DONE and rec.remote_id:
                    ids[op.key] = rec.remote_id
                    res.existing += 1
                    continue
                if rec and rec.state in (PENDING, UNKNOWN):
                    found = adapter.find(op, ids)
                    self.audit.append(
                        "reconcile", plan=plan.plan_id, network=n.network, op=op.key, object_kind=op.kind, found=found
                    )
                    if found:
                        self.state.mark(op.key, plan.plan_id, n.network, op.kind, op.name, DONE, found)
                        ids[op.key] = found
                        res.reconciled += 1
                        continue
                payload = resolve(copy.deepcopy(op.payload), ids)
                self.state.mark(
                    op.key, plan.plan_id, n.network, op.kind, op.name, PENDING, request_sha=payload_sha(op.payload)
                )
                try:
                    remote = adapter.execute(op, payload, ids)
                except AmbiguousWrite as e:
                    self.state.mark(op.key, plan.plan_id, n.network, op.kind, op.name, UNKNOWN)
                    res.stopped = f"{e}. Run create again: it looks the object up by its key before creating."
                    break
                except (NetworkError, SafetyError) as e:
                    res.stopped = str(e)
                    break
                self.state.mark(op.key, plan.plan_id, n.network, op.kind, op.name, DONE, remote)
                ids[op.key] = remote
                res.created += 1
            if res.stopped:
                self.audit.append("create_stopped", plan=plan.plan_id, network=n.network, reason=res.stopped)
                continue
            res.campaign = self._record(cfg, plan, n, ids, adapter)
        if plan.manual:
            packs = write_packs(plan, self.state.dir / "manual")
            self.audit.append("manual_packs", plan=plan.plan_id, folders=[str(p) for p in packs])
        else:
            packs = []
        return CreateReport(plan, results, packs)

    def _record(self, cfg: CampaignConfig, plan: Plan, n: Any, ids: dict[str, str], adapter: Adapter) -> CampaignRecord:
        by_kind: dict[str, list[str]] = {}
        ads: dict[str, str] = {}
        ad_kind = "video_asset" if n.network == "google" else "ad"
        for op in n.ops:
            by_kind.setdefault(op.kind, []).append(ids[op.key])
            if op.kind == ad_kind and op.creative:
                ads[op.creative] = ids[op.key]
        camp = n.campaign_op
        existing = self.state.campaign(f"{plan.plan_id}:{n.network}")
        rec = existing or CampaignRecord(
            plan_id=plan.plan_id,
            network=n.network,
            name=camp.name,
            config_name=cfg.name,
            mode=n.mode,
            account=n.account,
            campaign_id=ids[camp.key],
            currency=cfg.currency,
            daily=cfg.budget.daily,
            total_cap=cfg.budget.total_cap,
            max_daily=cfg.limits.max_daily,
            max_total=cfg.limits.max_total,
            start=plan.start.isoformat(),
            end_day=plan.end.isoformat(),
            store_url=plan.listing.url,
            status=PAUSED,
            auto_pause=cfg.automation.auto_pause,
            settings=adapter_class(n.network).settings_of(getattr(cfg.networks, n.network)),
            created_at=now(),
        )
        rec.children = {k: v for k, v in by_kind.items() if k in ("adset", "adgroup", "ad")}
        rec.ads = ads
        if existing is not None:  # recorded by an earlier run (and maybe activated since): never touched by create
            self.state.save_campaign(rec)
            return rec
        info = adapter.read(rec)
        if info.get("status") != PAUSED:
            adapter.set_status(rec, False)
            self.audit.append("safety_pause", campaign=rec.ref, reason="read back ACTIVE right after create")
            raise SafetyError(f"{n.network}: campaign {rec.campaign_id} read back as ACTIVE after create; paused it")
        if rec.cell_id is None and len(n.creatives) >= 2:
            rec.cell_id = self._cell(cfg, n.network, n.creatives)
        self.state.save_campaign(rec)
        self.audit.append(
            "campaign_created",
            campaign=rec.ref,
            network=rec.network,
            campaign_id=rec.campaign_id,
            status=PAUSED,
            daily=rec.daily,
            total_cap=rec.total_cap,
            start=rec.start,
            end=rec.end_day,
            cell=rec.cell_id,
        )
        return rec

    def _cell(self, cfg: CampaignConfig, network: str, creatives: list[Any]) -> int | None:
        ctl = next((c for c in creatives if cfg.control in (c.creative_id, c.ad_name, c.concept)), creatives[0])
        rule = Rule(**(cfg.test_rule or {}))
        try:
            cell = CellStore(self.perf_db).create(
                f"{cfg.name} {network}",
                network,
                ctl.creative_id,
                [c.creative_id for c in creatives if c is not ctl],
                rule,
            )
        except CellError as e:
            self.log(f"{network}: no test cell ({e})")
            return None
        return cell.id

    # -------------------------------------------------------------------------------------------------- activate
    def activate(self, ref: str, phrase: str, cfg: CampaignConfig | None = None) -> CampaignRecord:
        rec = self._require(ref)
        if rec.status == ACTIVE:
            raise SafetyError(f"{rec.ref} is already active")
        expected = {_normal(confirmation_phrase(rec, "tr")), _normal(confirmation_phrase(rec, "en"))}
        if _normal(phrase) not in expected:
            raise SafetyError(f"confirmation does not match; type exactly: {confirmation_phrase(rec)}")
        max_daily, max_total = rec.max_daily, rec.max_total
        if cfg is not None:
            max_daily, max_total = min(max_daily, cfg.limits.max_daily), min(max_total, cfg.limits.max_total)
        if rec.daily > max_daily or rec.total_cap > max_total:
            raise SafetyError(
                f"{rec.ref}: daily {rec.daily:.2f} / total {rec.total_cap:.2f} is above the limits "
                f"{max_daily:.2f} / {max_total:.2f}"
            )
        if self.day().isoformat() > rec.end_day:
            raise SafetyError(f"{rec.ref} ended on {rec.end_day}; create a new campaign instead")
        adapter = self._adapter_for(rec)
        info = adapter.read(rec)
        problems = []
        if info.get("currency") != rec.currency:
            problems.append(f"account currency is {info.get('currency')}, the campaign was planned in {rec.currency}")
        if float(info.get("daily") or 0) > rec.daily + 0.005:
            problems.append(
                f"the network's daily budget {float(info['daily']):.2f} is above the {rec.daily:.2f} forge "
                "created (changed outside forge)"
            )
        if float(info.get("daily") or 0) > max_daily:
            problems.append(f"the network's daily budget is above limits.max_daily {max_daily:.2f}")
        if float(info.get("spend") or 0) >= rec.total_cap:
            problems.append(f"spend {float(info['spend']):.2f} already reached the total cap {rec.total_cap:.2f}")
        if problems:
            self.audit.append("activate_refused", campaign=rec.ref, problems=problems)
            raise SafetyError(f"{rec.ref}: " + "; ".join(problems))
        self.audit.append(
            "activate_confirmed",
            campaign=rec.ref,
            phrase=_normal(phrase),
            daily=rec.daily,
            total_cap=rec.total_cap,
            currency=rec.currency,
            max_daily=max_daily,
            max_total=max_total,
            network_daily=info.get("daily"),
            spend=info.get("spend"),
        )
        adapter.set_status(rec, True)
        after = adapter.read(rec)
        if after.get("status") != ACTIVE:
            raise NetworkError(f"{rec.ref}: the network still reports {after.get('status')} after activation")
        rec.status, rec.activated_at = ACTIVE, now()
        self.state.save_campaign(rec)
        if rec.cell_id:
            store = CellStore(self.perf_db)
            cell = store.get(rec.cell_id)
            if cell is not None and not cell.locked:
                store.start(rec.cell_id, max(self.day(), date.fromisoformat(rec.start)))
        self.audit.append("campaign_activated", campaign=rec.ref, campaign_id=rec.campaign_id)
        return rec

    def pause(self, ref: str, reason: str = "manual") -> CampaignRecord:
        rec = self._require(ref)
        adapter = self._adapter_for(rec, safe_direction=True)
        adapter.set_status(rec, False)
        after = adapter.read(rec)
        if after.get("status") != PAUSED:
            raise NetworkError(f"{rec.ref}: the network still reports {after.get('status')} after pausing")
        rec.status, rec.paused_at = PAUSED, now()
        self.state.save_campaign(rec)
        self.audit.append("campaign_paused", campaign=rec.ref, campaign_id=rec.campaign_id, reason=reason)
        return rec

    # ------------------------------------------------------------------------------------------------------ sync
    def sync(self, ref: str | None = None, cfg: CampaignConfig | None = None) -> list[SyncResult]:
        recs = [self._require(ref)] if ref else self.state.campaigns()
        out = []
        for rec in recs:
            adapter = self._adapter_for(rec, safe_direction=True)
            info = adapter.read(rec)
            start = date.fromisoformat(rec.start)
            until = min(self.day(), date.fromisoformat(rec.end_day))
            rows = adapter.insights(rec, start, until) if until >= start else []
            if rows:
                level = rows[0].level
                PerfStore(self.perf_db).upsert(
                    rows,
                    ImportSummary(rec.network, rec.account, level),
                    since=start.isoformat(),
                    until=until.isoformat(),
                )
            result = None
            if rec.cell_id:
                cell = CellStore(self.perf_db).get(rec.cell_id)
                if cell is not None:
                    result = evaluate(cell, self.perf_db, self.day())
            auto = cfg.automation.auto_pause if cfg is not None else rec.auto_pause
            recs_ = recommend(rec, info, result, auto)
            for r in recs_:
                if not r.auto:
                    continue
                try:
                    if r.action == PAUSE_AD and r.ad_id:
                        adapter.set_ad_status(rec, r.ad_id, False)
                    elif r.action == PAUSE_CAMPAIGN:
                        adapter.set_status(rec, False)
                        rec.status, rec.paused_at = PAUSED, now()
                        info["status"] = PAUSED
                    r.applied = True
                    self.audit.append("auto_pause", campaign=rec.ref, action=r.action, target=r.target, reason=r.reason)
                except SafetyError as e:
                    r.reason += f" (not applied: {e})"
            if info.get("status") in (ACTIVE, PAUSED) and rec.status != info["status"]:
                self.audit.append("status_drift", campaign=rec.ref, forge=rec.status, network=info["status"])
            rec.synced_at = now()
            rec.last_sync = {
                "at": rec.synced_at,
                "day": self.day().isoformat(),
                "status": info.get("status"),
                "daily": info.get("daily"),
                "spend": info.get("spend"),
                "currency": info.get("currency"),
                "rows": len(rows),
                "impressions": sum(r.impressions or 0 for r in rows),
                "installs": sum(r.installs or 0 for r in rows),
                "recommendations": [r.to_dict() for r in recs_],
            }
            self.state.save_campaign(rec)
            self.audit.append(
                "sync",
                campaign=rec.ref,
                status=info.get("status"),
                spend=info.get("spend"),
                rows=len(rows),
                recommendations=[r.action for r in recs_],
            )
            out.append(SyncResult(rec, info, len(rows), result, recs_))
        return out
