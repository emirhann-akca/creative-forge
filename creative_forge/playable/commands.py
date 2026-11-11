"""``forge playable check`` and ``forge playable pack``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creative_forge.playable.rules import FAIL, NETWORKS, PASS, SOURCES, SUNSET, Finding, worst
from creative_forge.playable.static import Bundle, PlayableError, check, load

NETWORK_CHOICES = [*NETWORKS, *SUNSET]


@dataclass
class Result:
    file: str
    network: str
    status: str
    findings: list[Finding] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)

    def reasons(self, n: int = 3) -> list[str]:
        order = {"fail": 0, "uncertain": 1, "warn": 2}
        bad = sorted((f for f in self.findings if f.status in order), key=lambda f: order[f.status])
        return [f"{f.status}: {f.rule}: {f.message}" for f in bad[:n]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "network": self.network,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "trace": self.trace,
        }


def check_file(
    path: Path,
    network: str,
    *,
    dynamic: bool = True,
    channel: str | None = None,
    render_timeout_s: float = 8.0,
    settle_s: float = 1.5,
    max_taps: int = 12,
    bundle: Bundle | None = None,
) -> Result:
    if network in SUNSET:
        msg, key = SUNSET[network]
        return Result(str(path), network, FAIL, [Finding("network", FAIL, msg, SOURCES[key])])
    net = NETWORKS[network]
    try:
        bundle = bundle or load(path)
    except PlayableError as e:
        return Result(str(path), network, FAIL, [Finding("load", FAIL, str(e))])
    findings = check(bundle, net)
    trace: dict[str, Any] = {}
    if dynamic:
        from creative_forge.playable.dynamic import run

        more, t = run(bundle, net, channel=channel, render_timeout_s=render_timeout_s, settle_s=settle_s,
                      max_taps=max_taps)  # fmt: skip
        findings = _settle(findings, more)
        trace = t.to_dict()
    return Result(str(path), network, worst(findings), findings, trace)


def _settle(static: list[Finding], dynamic: list[Finding]) -> list[Finding]:
    """A static hint the browser run answered: fetch/XMLHttpRequest that never left the bundle passes; a redirect the
    code could do but did not stays as it was, with what the run saw."""
    by = {f.rule: f for f in dynamic}
    clean = by.get("network-requests") is not None and by["network-requests"].status == PASS
    out = []
    for f in static:
        if f.rule == "xhr" and clean:
            f = Finding(f.rule, PASS, "fetch/XMLHttpRequest in the code, but no request left the bundle in the browser "
                        "run (data: URLs)", f.source, f.checked)  # fmt: skip
        elif f.rule == "redirect" and "redirect" not in by:
            f = Finding(f.rule, f.status, f.message + "; none happened in the browser run", f.source, f.checked)
        out.append(f)
    return out + dynamic


def cmd_check(a: argparse.Namespace) -> int:
    from creative_forge.playable.dynamic import BrowserUnavailable

    results: list[Result] = []
    for f in a.files:
        bundle = None
        for net in a.network:
            try:
                if net in NETWORKS and bundle is None:
                    bundle = load(Path(f))
            except PlayableError:
                bundle = None
            try:
                r = check_file(
                    Path(f),
                    net,
                    dynamic=not a.static_only,
                    channel=a.browser_channel,
                    render_timeout_s=a.render_timeout,
                    bundle=bundle,
                )
            except BrowserUnavailable as e:
                print(f"error: {e} (or use --static-only)", file=sys.stderr)
                return 2
            results.append(r)
            print(f"{r.status.upper():9} {net:9} {Path(f).name}")
            for line in r.reasons(5 if a.verbose else 3):
                print(f"          {line}")
            if a.verbose:
                for x in r.findings:
                    if x.status not in ("fail", "uncertain", "warn"):
                        print(f"          {x.status}: {x.rule}: {x.message}")
    if a.json:
        Path(a.json).write_text(
            json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"json: {a.json}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "warn", "uncertain", "fail")}
    print("summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 1 if counts["fail"] else 0


def cmd_pack(a: argparse.Namespace) -> int:
    from creative_forge.playable.pack import PackError, pack

    if a.network in SUNSET:
        print(f"error: {SUNSET[a.network][0]}", file=sys.stderr)
        return 2
    try:
        out, notes = pack(
            Path(a.source),
            NETWORKS[a.network],
            Path(a.out),
            orientation=a.orientation,
            tiktok_orientation=a.tiktok_orientation,
            cta_adapter=a.cta_adapter,
        )
    except (PackError, OSError, UnicodeDecodeError) as e:
        print(f"pack failed: {e}", file=sys.stderr)
        return 2
    print(f"packed for {a.network}: {out}")
    for n in notes:
        print(f"  {n}")
    r = check_file(out, a.network, dynamic=False)
    print(f"static check: {r.status}")
    for line in r.reasons(6):
        print(f"  {line}")
    return 1 if r.status == FAIL else 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    sp = sub.add_parser("playable", help="playable ads: per-network checks (static + headless browser) and packaging")
    psub = sp.add_subparsers(dest="playable_cmd", required=True)
    c = psub.add_parser("check", help="check an HTML file or zip against each network's published rules")
    c.add_argument("files", nargs="+")
    c.add_argument("--network", action="append", required=True, choices=NETWORK_CHOICES, help="repeatable")
    c.add_argument("--static-only", action="store_true", help="skip the headless browser run")
    c.add_argument("--browser-channel", default=None, help="e.g. chrome: drive an installed Chrome (no download)")
    c.add_argument("--render-timeout", type=float, default=8.0, help="seconds to wait for a first non-blank frame")
    c.add_argument("--json", help="write every finding (with source URL and check date) as JSON")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(fn=cmd_check)
    p = psub.add_parser("pack", help="inline assets and package a playable for one network")
    p.add_argument("source", help="an HTML file (assets next to it), a folder with index.html, or a zip")
    p.add_argument("--network", required=True, choices=NETWORK_CHOICES)
    p.add_argument("--out", default="out/playables")
    p.add_argument("--orientation", choices=["portrait", "landscape"], default="portrait", help="Google ad.orientation")
    p.add_argument("--tiktok-orientation", type=int, choices=[0, 1, 2], default=0, help="config.json value")
    p.add_argument("--cta-adapter", action="store_true", help="add window.forgeCTA(url) calling the network's API")
    p.set_defaults(fn=cmd_pack)
