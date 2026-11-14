"""Self-contained HTML + JSON run report (QA table, cost ledger, contact sheets, master loop history)."""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import Any

from creative_forge.pipeline import ConceptResult, RunResult
from creative_forge.qa.result import CheckResult, Status

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--card:#fff;
--pass:#1f7a3a;--warn:#9a6200;--fail:#b3261e;--skip:#6b6b70}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--muted:#9a9aa2;--line:#2c2c30;--card:#1c1c1f;
--pass:#5cc47e;--warn:#e0a640;--fail:#ff7b70;--skip:#9a9aa2}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 8px}h3{font-size:15px;margin:20px 0 6px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.scroll{overflow-x:auto}.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;
font-weight:600;border:1px solid currentColor}.pass{color:var(--pass)}.warn{color:var(--warn)}
.fail{color:var(--fail)}.skip{color:var(--skip)}img{max-width:100%;height:auto;border-radius:6px}
code{font-size:12px}
"""


def _badge(s: Status | str) -> str:
    v = s.value if isinstance(s, Status) else s
    v = html.escape(str(v), quote=True)
    return f'<span class="badge {v}">{v}</span>'


def _checks_table(checks: list[CheckResult]) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(c.check)}</td><td>{_badge(c.status)}</td><td>{html.escape(c.display_value)}</td>"
        f"<td>{html.escape(c.threshold)}</td><td>{html.escape(c.message)}</td></tr>"
        for c in checks
    )
    return (
        '<div class="scroll"><table><tr><th>check</th><th>status</th><th>measured</th><th>threshold</th>'
        f"<th>note</th></tr>{rows}</table></div>"
    )


def _img(path: Path | None) -> str:
    if path is None or not Path(path).exists():
        return ""
    data = base64.b64encode(Path(path).read_bytes()).decode()
    alt = "contact sheet: hook start, before cut, after cut, gameplay, end card"
    return f'<img alt="{alt}" src="data:image/jpeg;base64,{data}">'


def _concept(cr: ConceptResult) -> str:
    parts = [
        f'<div class="card"><h3>{html.escape(cr.label)} {_badge(cr.status)} '
        f'<span class="muted">{cr.seconds:.0f} s</span></h3>'
    ]
    if cr.error:
        parts.append(f"<p class='fail'>{html.escape(cr.error)}</p>")
        if cr.work_dir:
            parts.append(f"<p class='muted'>work dir kept: <code>{html.escape(str(cr.work_dir))}</code></p>")
    parts.append(_img(cr.contact_sheet))
    if cr.pre_checks:
        parts.append("<p class='muted'>Generation checks</p>" + _checks_table(cr.pre_checks))
    for ex in cr.exports:
        parts.append(
            f"<p><b>{html.escape(str(ex.format))}</b> <code>{html.escape(ex.path.name)}</code> {_badge(ex.status)}</p>"
        )
        parts.append(_checks_table(ex.checks))
    if cr.master:
        it = cr.master.get("iterations", [])
        b = cr.master.get("before", {})
        rows = "".join(
            f"<tr><td>{i + 1}</td><td>{x['gain_db']:+.2f}</td><td>{x['hook_offset_db']:+.2f}</td>"
            f"<td>{x['limiter']:.3f}</td>"
            f"<td>{x['integrated_lufs']:.2f}</td><td>{x['true_peak_dbtp']:.2f}</td>"
            f"<td>{x['hook_lufs']:.2f}</td><td>{x['gameplay_lufs']:.2f}</td></tr>"
            for i, x in enumerate(it)
        )
        parts.append(
            f"<p class='muted'>Master loop ({html.escape(str(cr.master.get('encoder')))}, "
            f"converged: {html.escape(str(cr.master.get('converged')))}); "
            f"before: hook {html.escape(str(b.get('hook_lufs')))} / gameplay "
            f"{html.escape(str(b.get('gameplay_lufs')))} LUFS</p>"
            "<div class='scroll'><table><tr><th>round</th><th>gain dB</th><th>hook offset dB</th><th>limiter</th>"
            "<th>LUFS</th><th>true peak</th><th>hook</th><th>gameplay</th></tr>" + rows + "</table></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def report_dict(rr: RunResult) -> dict[str, Any]:
    return {
        "name": rr.name,
        "status": rr.status.value,
        "seconds": rr.seconds,
        "plan": {
            "total_upper_bound_usd": rr.plan.total_priced,
            "unpriced_calls": len(rr.plan.unpriced),
            "pricing_as_of": rr.plan.pricing.as_of if rr.plan.pricing else None,
        },
        "lint": [i.__dict__ for i in rr.lint],
        "licenses": rr.licenses,
        "cost": rr.ledger.to_dict(),
        "judge_key": rr.judge_key,
        "concepts": [
            {
                "concept": c.concept,
                "variant": c.variant,
                "hypothesis": c.hypothesis,
                "status": c.status.value,
                "error": c.error,
                "timeline": c.timeline,
                "master": c.master,
                "pre_checks": [x.to_dict() for x in c.pre_checks],
                "exports": [
                    {
                        "format": e.format,
                        "file": e.path.name,
                        "creative_id": e.creative.creative_id if e.creative else None,
                        "ad_name": e.creative.ad_name if e.creative else None,
                        "tags": e.tags,
                        "status": e.status.value,
                        "checks": [x.to_dict() for x in e.checks],
                    }
                    for e in c.exports
                ],
            }
            for c in rr.concepts
        ],
    }


def write_report(rr: RunResult, out_dir: Path | None = None) -> tuple[Path, Path]:
    out_dir = Path(out_dir or rr.out_dir)
    data = report_dict(rr)
    jp = out_dir / "report.json"
    jp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    led = rr.ledger
    ledger_rows = "".join(
        f"<tr><td>{html.escape(e.concept)}</td><td>{html.escape(e.stage)}</td><td>{html.escape(e.provider)}</td>"
        f"<td>{html.escape(e.model)}</td><td>{html.escape(e.detail)}</td><td>${e.usd:.2f}</td>"
        f"<td>{'-' if e.list_usd is None else f'${e.list_usd:.2f}'}</td></tr>"
        for e in led.entries
    )
    lint_rows = "".join(
        f"<li><b>{html.escape(i.concept)}</b> [{html.escape(i.rule)}] {html.escape(i.message)}</li>" for i in rr.lint
    )
    n_exports = sum(len(c.exports) for c in rr.concepts)
    n_concepts = len({c.concept for c in rr.concepts})
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge QA Report</title><style>{CSS}</style></head>
<body><main><h1>{html.escape(rr.name)} {_badge(rr.status)}</h1>
<p class="muted">{n_concepts} concept(s), {len(rr.concepts)} render(s), {n_exports} export(s), {rr.seconds:.0f} s.
Spent ${led.spent:.2f}; cache saved ${led.saved_by_cache:.2f}. Upper-bound plan ${rr.plan.total_priced:.2f}
(prices as of {rr.plan.pricing.as_of if rr.plan.pricing else "?"}).</p>
<h2>Concepts</h2>{"".join(_concept(c) for c in rr.concepts)}
<h2>Cost ledger</h2><div class="card scroll"><table><tr><th>concept</th><th>stage</th><th>provider</th><th>model</th>
<th>detail</th><th>charged</th><th>list price</th></tr>{ledger_rows}</table>
<p class="muted">Fake providers are charged $0; the list price shows what the same call costs on Vertex AI.</p></div>
<h2>Spec lint</h2><div class="card">{"<ul>" + lint_rows + "</ul>" if lint_rows else "<p>No issues.</p>"}</div>
</main></body></html>"""
    hp = out_dir / "report.html"
    hp.write_text(body, encoding="utf-8")
    return hp, jp
