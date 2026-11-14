"""``forge campaign demo``: the whole loop offline, on the mock network, in well under a minute.

spec -> render (fake providers, synthetic gameplay) -> QA -> a reviewer approves -> ``forge publish`` -> plan (dry-run
diff) -> create on Meta, TikTok and Google Ads (mock, everything PAUSED) -> create again (nothing new: idempotency)
-> a simulated day while paused (no delivery) -> activate with the typed phrase -> ten simulated days with noise ->
sync (delivery into the performance store) -> the test cells' kill / scale recommendations -> pause -> audit chain
check. No key, no network, no money: the spend it prints is simulated.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from creative_forge.campaigns.config import load_config
from creative_forge.campaigns.engine import CampaignEngine, confirmation_phrase
from creative_forge.campaigns.mock_server import MockNetwork, MockServer
from creative_forge.campaigns.plan import preview_text
from creative_forge.campaigns.state import CampaignState

STORE_URL = "https://play.google.com/store/apps/details?id=com.forgedemo.sortpuzzle"  # synthetic, never published
DAYS = 10


@dataclass
class DemoResult:
    out: Path
    seconds: float
    published: int
    excluded: list[str]
    created: dict[str, int]
    created_again: dict[str, int]
    paused_at_creation: bool
    paused_day_rows: int
    delivered_rows: int
    spend: dict[str, float]
    installs: dict[str, float]
    recommendations: dict[str, list[dict[str, Any]]]
    audit_records: int
    audit_ok: bool
    packs: list[str] = field(default_factory=list)


def demo_config(deliver: Path, start: date) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "demo-sort-tr",
        "creatives": str(deliver),
        "platform": "android",
        "store_url": STORE_URL,
        "countries": ["TR"],
        "currency": "USD",
        "budget": {"daily": 40, "total_cap": 400, "start": start.isoformat()},
        "limits": {"max_daily": 50, "max_total": 500},
        "automation": {"auto_pause": False},
        "formats": ["9:16"],
        "control": "mud-wash",
        "networks": {
            "meta": {"mode": "mock", "ad_account": "act_100200300", "page_id": "100000000000001",
                     "app_id": "200000000000002", "message": "Sort the shelves. Calm, quick, satisfying.",
                     "formats": ["9:16", "4:5"]},
            "tiktok": {"mode": "mock", "advertiser_id": "7000000000000000001", "app_id": "7100000000000000001",
                       "location_ids": ["298795"], "identity_type": "CUSTOMIZED_USER", "identity_id": "demo-identity",
                       "ad_text": "Sort the shelves. Calm, quick, satisfying."},
            "google": {"mode": "mock", "customer_id": "1234567890", "headlines": ["Sort the shelves", "Calm puzzle"],
                       "descriptions": ["A calm goods-sort puzzle you play in short breaks."],
                       "location_ids": ["2792"], "language_ids": ["1037"]},
            "applovin": {"mode": "manual"},
        },
    }  # fmt: skip


def _approve_and_publish(render: Path, deliver: Path, log: Callable[[str], None]) -> tuple[int, list[str]]:
    from creative_forge.review import decide, load_exports, publish

    excluded = []
    for r in load_exports(render):
        if r.failed:
            decide(render, r.file, "reject", f"QA failed: {', '.join(r.failed)}", "demo-reviewer")
            excluded.append(f"{r.file} (QA failed)")
        elif r.concept == "mud-wash" and r.format == "4:5":
            decide(render, r.file, "reject", "demo: rejected by the reviewer to show the gate", "demo-reviewer")
            excluded.append(f"{r.file} (rejected by the reviewer)")
        else:
            decide(render, r.file, "approve", "demo: hook reads well", "demo-reviewer")
    manifest = publish(render, deliver)
    n = len(json.loads(manifest.read_text(encoding="utf-8"))["exports"])
    log(f"review: {n} approved and published, {len(excluded)} kept out: {'; '.join(excluded) or '-'}")
    return n, excluded


def run_campaign_demo(
    out: Path, *, log: Callable[[str], None] = print, ocr: str = "auto", jobs: int = 2, today: date | None = None
) -> DemoResult:
    from creative_forge.demo import run_demo
    from creative_forge.report import write_report

    t0 = time.monotonic()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    d0 = today or date.today()
    start = d0 + timedelta(days=1)

    log("1/9 render: demo spec, fake providers, synthetic gameplay (no keys)")
    render = out / "render"
    rr = run_demo(render, jobs=jobs, ocr=ocr, log=lambda _m: None)
    write_report(rr)
    log(f"    QA: {rr.status.value} over {sum(len(c.exports) for c in rr.concepts)} export(s)")

    log("2/9 review + publish: only approved, QA-clean files reach the campaign")
    deliver = out / "deliver"
    published, excluded = _approve_and_publish(render, deliver, log)

    cfg_path = out / "campaign.yaml"
    cfg_path.write_text(yaml.safe_dump(demo_config(deliver, start), sort_keys=False, allow_unicode=True), "utf-8")
    cfg = load_config(cfg_path)
    state = CampaignState(out / "state")
    mock = MockNetwork(state.dir / "mock_network.json")
    # what the simulation treats as each creative's true rates: one better, one worse than the control
    mock.set_truth("dust-blower.main.main.main.9x16", ipm=9.5, ctr=0.014, hook=0.33)
    mock.set_truth("dust-blower.main.main.main.4x5", ipm=4.0, ctr=0.009, hook=0.22)
    mock.set_truth("mud-wash.main.main.main.9x16", ipm=6.5, ctr=0.011, hook=0.27)

    with MockServer(mock) as url:
        eng = CampaignEngine(state, mock_url=url, perf_db=out / "perf.sqlite", today=d0, sleep=lambda _s: None,
                             log=log)  # fmt: skip

        log("3/9 plan (dry run): exactly what would be created")
        plan = eng.plan(cfg)
        preview = preview_text(plan, state)
        (out / "plan.txt").write_text(preview + "\n", encoding="utf-8")
        for line in preview.splitlines()[:5]:
            log(f"    {line}")
        log(f"    ... full preview in {out / 'plan.txt'}")

        log("4/9 create on the mock Meta, TikTok and Google Ads APIs: everything PAUSED")
        rep = eng.create(cfg)
        created = {n.network: n.created for n in rep.networks}
        for n in rep.networks:
            if n.stopped:
                raise RuntimeError(f"{n.network}: {n.stopped}")
            cid = n.campaign.campaign_id if n.campaign else "-"
            log(f"    {n.network}: {n.created} object(s) created, campaign {cid}")
        again = eng.create(cfg)
        created_again = {n.network: n.created for n in again.networks}
        log(f"    create again: {sum(created_again.values())} new object(s) (idempotency keys)")
        statuses = [o.get("_created_status") for net in ("meta", "tiktok", "google") for o in mock.objects(net)]
        paused = all(s in (None, "PAUSED", "DISABLE") for s in statuses)
        log(f"    every campaign, ad set and ad was created paused: {paused}")

        log("5/9 one simulated day while paused")
        paused_rows = mock.advance(1, start)
        log(f"    delivery rows while paused: {paused_rows}")

        log("6/9 activate: typed confirmation with the daily budget, caps checked again")
        for c in state.campaigns():
            phrase = confirmation_phrase(c)
            eng.activate(c.ref, phrase, cfg)
            log(f"    {c.network}: typed '{phrase}' -> ACTIVE ({c.daily:.2f}/day, cap {c.total_cap:.2f})")

        log(f"7/9 {DAYS} simulated days of delivery (CPM, pacing and conversion noise)")
        rows = mock.advance(DAYS, start)
        eng.today = start + timedelta(days=DAYS - 1)

        log("8/9 sync: status, spend and per-creative delivery into the performance store; kill / scale verdicts")
        spend, installs, recs = {}, {}, {}
        for s in eng.sync(cfg=cfg):
            n = s.campaign.network
            spend[n] = round(float(s.info.get("spend") or 0), 2)
            installs[n] = float(s.campaign.last_sync.get("installs") or 0)
            recs[n] = [r.to_dict() for r in s.recommendations]
            log(f"    {n}: simulated spend {spend[n]:.2f} USD, {installs[n]:.0f} installs, {s.rows} row(s)")
            for r in s.recommendations:
                log(f"      {r.action:<14} {r.target[:58]}: {r.reason}")
        for c in state.campaigns():
            eng.pause(c.ref, "demo finished")

        log("9/9 audit log: append-only, hash-chained")
        check = eng.audit.verify()
        log(f"    {check.records} record(s), chain {'intact' if check.ok else 'BROKEN: ' + check.problem}")

    result = DemoResult(
        out=out,
        seconds=round(time.monotonic() - t0, 1),
        published=published,
        excluded=excluded,
        created=created,
        created_again=created_again,
        paused_at_creation=paused,
        paused_day_rows=paused_rows,
        delivered_rows=rows,
        spend=spend,
        installs=installs,
        recommendations=recs,
        audit_records=check.records,
        audit_ok=check.ok,
        packs=[str(p) for p in rep.packs],
    )
    summary = {k: (str(v) if isinstance(v, Path) else v) for k, v in result.__dict__.items()}
    (out / "campaign_report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"done in {result.seconds:.0f} s; report {out / 'campaign_report.json'}; no real money, no network calls")
    return result
