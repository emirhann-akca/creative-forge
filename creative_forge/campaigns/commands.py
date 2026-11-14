"""``forge campaign``: plan, create, status, activate, pause, sync, demo, audit, mock-server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from creative_forge.config import ConfigError


def _state(a: argparse.Namespace):  # type: ignore[no-untyped-def]
    from creative_forge.campaigns.state import CampaignState

    return CampaignState(Path(a.state) if a.state else None)


def _engine(a: argparse.Namespace, *, live: bool = False):  # type: ignore[no-untyped-def]
    from creative_forge.campaigns.engine import CampaignEngine

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    return CampaignEngine(_state(a), mock_url=a.mock_url, allow_live=live, perf_db=Path(a.db) if a.db else None,
                          log=log)  # fmt: skip


def _config(path: str | None):  # type: ignore[no-untyped-def]
    from creative_forge.campaigns.config import load_config

    return load_config(path) if path else None


def _fail(msg: str, code: int = 2) -> int:
    print(f"campaign: {msg}", file=sys.stderr)
    return code


def cmd_plan(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.plan import preview_text
    from creative_forge.campaigns.store_url import verify_online

    with _engine(a) as eng:
        cfg = _config(a.config)
        plan = eng.plan(cfg)
        if a.json:
            print(json.dumps(plan.to_dict(eng.state), indent=2, ensure_ascii=False, default=str))
        else:
            print(preview_text(plan, eng.state))
            print("dry run: nothing was sent to any network")
        if a.verify_store:
            problem = verify_online(plan.listing)
            print(f"store listing online: {problem or 'OK'}")
            if problem:
                return 1
    return 1 if plan.problems else 0


def cmd_create(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.plan import preview_text

    with _engine(a, live=a.live) as eng:
        cfg = _config(a.config)
        plan = eng.plan(cfg)
        print(preview_text(plan, eng.state))
        print()
        rep = eng.create(cfg, a.network or None)
        for n in rep.networks:
            if n.stopped:
                print(f"{n.network}: STOPPED after {n.created} new object(s): {n.stopped}")
            else:
                c = n.campaign
                print(
                    f"{n.network}: {n.created} created, {n.existing} existed, {n.reconciled} found by key; campaign "
                    f"{c.campaign_id if c else '-'} is PAUSED (ref {c.ref if c else '-'})"
                )
        for p in rep.packs:
            print(f"manual upload pack: {p}")
        print(f"audit log: {eng.state.audit_path}")
        print("nothing is active; `forge campaign activate <ref>` is the separate step that can spend")
        return 0 if rep.ok else 1


def cmd_status(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.engine import confirmation_phrase

    st = _state(a)
    recs = [st.campaign(a.ref)] if a.ref else st.campaigns()
    recs = [r for r in recs if r is not None]
    if a.json:
        print(json.dumps([r.__dict__ for r in recs], indent=2, ensure_ascii=False, default=str))
        return 0
    if not recs:
        print(f"no campaigns in {st.path}")
        return 0 if not a.ref else 1
    for r in recs:
        last = r.last_sync or {}
        print(f"{r.ref}  {r.network:<7} {r.mode:<8} {r.status:<7} {r.name}")
        print(f"    {r.daily:.2f} {r.currency}/day, cap {r.total_cap:.2f}, {r.start} .. {r.end_day}")
        print(f"    campaign {r.campaign_id}")
        if last:
            print(f"    last sync {last.get('at')}: network {last.get('status')}, spend {last.get('spend')}, "
                  f"{last.get('installs', 0):.0f} installs")  # fmt: skip
            for rec in last.get("recommendations", []):
                flag = " [applied]" if rec.get("applied") else ""
                print(f"      {rec['action']:<14} {rec['target'][:60]}: {rec['reason']}{flag}")
        if r.status == "PAUSED":
            print(f'    to activate: forge campaign activate {r.ref} --confirm "{confirmation_phrase(r)}"')
    return 0


def cmd_activate(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.engine import confirmation_phrase

    st = _state(a)
    rec = st.campaign(a.ref)
    if rec is None:
        return _fail(f"no campaign {a.ref!r}")
    phrase = a.confirm
    if phrase is None:
        if not sys.stdin.isatty():
            return _fail(f'activation needs the typed phrase: --confirm "{confirmation_phrase(rec)}"')
        print(f"{rec.network} {rec.name}: {rec.daily:.2f} {rec.currency}/day, total cap {rec.total_cap:.2f} "
              f"{rec.currency}, {rec.start} .. {rec.end_day}, mode {rec.mode}")  # fmt: skip
        phrase = input(f'type "{confirmation_phrase(rec)}" to switch it on: ')
    with _engine(a, live=a.live) as eng:
        rec = eng.activate(a.ref, phrase, _config(a.config))
    print(f"{rec.ref}: ACTIVE ({rec.daily:.2f} {rec.currency}/day, cap {rec.total_cap:.2f}, ends {rec.end_day})")
    return 0


def cmd_pause(a: argparse.Namespace) -> int:
    with _engine(a) as eng:  # pausing needs no --live: it is always allowed
        rec = eng.pause(a.ref, a.reason)
    print(f"{rec.ref}: PAUSED")
    return 0


def cmd_sync(a: argparse.Namespace) -> int:
    with _engine(a) as eng:  # reads, and pauses at most: no --live needed
        results = eng.sync(a.ref, _config(a.config))
    for s in results:
        r = s.campaign
        print(f"{r.ref}  network {s.info.get('status')}, spend {float(s.info.get('spend') or 0):.2f} {r.currency}, "
              f"{s.rows} delivery row(s) imported")  # fmt: skip
        for rec in s.recommendations:
            flag = " [applied: auto_pause]" if rec.applied else ""
            print(f"    {rec.action:<14} {rec.target[:60]}: {rec.reason}{flag}")
    if not results:
        print("no campaigns to sync")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.demo import run_campaign_demo

    res = run_campaign_demo(Path(a.out), ocr=a.ocr, log=lambda m: print(m, flush=True))
    ok = res.audit_ok and res.paused_at_creation and res.paused_day_rows == 0 and not any(res.created_again.values())
    return 0 if ok else 1


def cmd_audit(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.audit import AuditLog

    log = AuditLog(_state(a).audit_path)
    check = log.verify()
    if a.tail:
        for rec in log.records()[-a.tail :]:
            print(json.dumps(rec, ensure_ascii=False))
    print(f"{log.path}: {check.records} record(s), chain {'intact' if check.ok else 'BROKEN: ' + check.problem}")
    return 0 if check.ok else 1


def cmd_mock(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.mock_server import MockNetwork, MockServer

    st = _state(a)
    server = MockServer(MockNetwork(st.dir / "mock_network.json"), port=a.port)
    print(f"mock ad networks on {server.url} (/meta, /tiktok, /google); state {st.dir / 'mock_network.json'}")
    print("Ctrl+C to stop; point forge at it with --mock-url")
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


def run(a: argparse.Namespace) -> int:
    from creative_forge.campaigns.config import CampaignConfigError
    from creative_forge.campaigns.creatives import CreativeGateError
    from creative_forge.campaigns.http import NetworkError
    from creative_forge.campaigns.networks import SafetyError
    from creative_forge.campaigns.store_url import StoreUrlError

    try:
        return int(a.campaign_fn(a))
    except (StoreUrlError, CreativeGateError, CampaignConfigError) as e:
        return _fail(f"refused: {e}")
    except SafetyError as e:
        return _fail(f"refused by a safety rule: {e}")
    except NetworkError as e:
        return _fail(f"network error: {e}", 1)
    except (ConfigError, ValueError) as e:
        return _fail(str(e))


def add_parser(sub: argparse._SubParsersAction) -> None:
    sp = sub.add_parser("campaign", help="paused app-install campaigns on Meta, TikTok, Google Ads; activate apart")
    csub = sp.add_subparsers(dest="campaign_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state", help="campaign state folder (default: campaigns/ in the user data folder)")
        p.add_argument("--db", help="performance store (default: perf.sqlite in the user data folder)")
        p.add_argument("--mock-url", help="use a running `forge campaign mock-server` instead of an in-process one")
        p.set_defaults(fn=run)

    p = csub.add_parser("plan", help="dry run: every object and payload that create would send; nothing is sent")
    p.add_argument("config")
    p.add_argument("--json", action="store_true", help="the full payloads as JSON")
    p.add_argument("--verify-store", action="store_true", help="also fetch the public store listing page")
    common(p)
    p.set_defaults(campaign_fn=cmd_plan)

    c = csub.add_parser("create", help="create everything PAUSED (idempotent: a re-run creates nothing twice)")
    c.add_argument("config")
    c.add_argument("--network", action="append", choices=["meta", "tiktok", "google"], help="only these (repeatable)")
    c.add_argument("--live", action="store_true", help="allow networks in live mode (real ad accounts)")
    common(c)
    c.set_defaults(campaign_fn=cmd_create)

    s = csub.add_parser("status", help="campaigns forge created, their caps and the last sync")
    s.add_argument("ref", nargs="?", help="<plan id>:<network> (default: all)")
    s.add_argument("--json", action="store_true")
    common(s)
    s.set_defaults(campaign_fn=cmd_status)

    ac = csub.add_parser("activate", help="switch one campaign on: typed confirmation, caps checked again")
    ac.add_argument("ref")
    ac.add_argument("--confirm", metavar="PHRASE", help='the typed phrase, e.g. "YAYINA AL 40.00 USD"')
    ac.add_argument("--config", help="re-check the caps against this config's limits too")
    ac.add_argument("--live", action="store_true")
    common(ac)
    ac.set_defaults(campaign_fn=cmd_activate)

    pa = csub.add_parser("pause", help="switch one campaign off (always allowed)")
    pa.add_argument("ref")
    pa.add_argument("--reason", default="manual")
    common(pa)
    pa.set_defaults(campaign_fn=cmd_pause)

    sy = csub.add_parser("sync", help="status, spend and delivery from the networks; kill / scale recommendations")
    sy.add_argument("ref", nargs="?")
    sy.add_argument("--config", help="take automation.auto_pause from this config")
    common(sy)
    sy.set_defaults(campaign_fn=cmd_sync)

    d = csub.add_parser("demo", help="the whole loop offline on the mock network (render, approve, create, deliver)")
    d.add_argument("--out", default="out/campaign-demo")
    d.add_argument("--ocr", choices=["auto", "vision", "rapidocr", "tesseract", "off"], default="auto")
    d.set_defaults(fn=run, campaign_fn=cmd_demo)

    au = csub.add_parser("audit", help="verify the hash chain of the audit log")
    au.add_argument("--tail", type=int, default=0, help="also print the last N records")
    common(au)
    au.set_defaults(campaign_fn=cmd_audit)

    m = csub.add_parser("mock-server", help="serve the mock Meta / TikTok / Google Ads APIs on 127.0.0.1")
    m.add_argument("--port", type=int, default=8799)
    common(m)
    m.set_defaults(campaign_fn=cmd_mock)
