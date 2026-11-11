"""``forge perf report``: per creative and per tag, with intervals, and lessons for ``forge ideate``.

Everything is computed per network: audiences, auctions and even metric definitions differ (Meta's hook rate counts
3-second views, TikTok's counts 2-second views), so a creative is only compared with other creatives on the same
network. AppLovin asset rows are left out of the creative totals because their creative set's row already counts the
same impressions.

Definitions (rates are per impression):

* IPM = installs / impressions x 1000; CTR = clicks / impressions; CPI = spend / installs
* hook rate = 3-second views / impressions. Meta labels ``video_view`` "3-Second Video Views"; dividing it by
  impressions is an industry convention, not a Meta definition.
* hook rate (2 s) = TikTok's 2-second views / impressions, reported apart from the 3-second one
* completion = 100 % views / impressions

A rate's denominator only counts impressions from rows that report its numerator, so a network without 3-second views
does not dilute the hook rate.

A creative (or a tag value) is a winner or a loser only when it and the rest of its pool both have at least
``min_impressions`` and their 95 % Wilson intervals do not overlap; the Bayesian P(better than the rest) is reported
next to it. Below the threshold the verdict is "insufficient data", whatever the point estimate says.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creative_forge.perf.stats import INSUFFICIENT, LOSER, WINNER, compare
from creative_forge.perf.store import PerfStore
from creative_forge.tags import flatten


@dataclass(frozen=True)
class Metric:
    name: str
    numerator: str
    scale: float
    unit: str
    label: str


METRICS = {
    "ipm": Metric("ipm", "installs", 1000.0, "", "IPM (installs per 1000 impressions)"),
    "ctr": Metric("ctr", "clicks", 100.0, "%", "CTR (clicks per 100 impressions)"),
    "hook_rate": Metric("hook_rate", "video_3s", 100.0, "%", "hook rate (3-second views per 100 impressions)"),
    "hook_rate_2s": Metric("hook_rate_2s", "video_2s", 100.0, "%", "hook rate, 2 s (TikTok 2-second views per 100)"),
    "completion": Metric("completion", "p100", 100.0, "%", "completion (100 % views per 100 impressions)"),
}
NUMERATORS = ("installs", "clicks", "video_3s", "video_2s", "p100", "thruplay")


def _aggregate(store: PerfStore, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cols = ", ".join(
        f"SUM({m}) AS {m}, SUM(CASE WHEN {m} IS NOT NULL THEN impressions END) AS n_{m}" for m in NUMERATORS
    )
    sql = (
        f"SELECT network, creative_id, MIN(ad_name) AS ad_name, COUNT(DISTINCT date) AS days, MIN(date) AS first, "
        f"MAX(date_end) AS last, SUM(impressions) AS impressions, SUM(spend) AS spend, {cols} "
        f"FROM perf_rows WHERE level != 'asset' AND creative_id IS NOT NULL{where} GROUP BY network, creative_id"
    )
    return [dict(r) for r in store.rows(sql, params)]


def _rate(row: dict[str, Any], m: Metric) -> tuple[float, float]:
    return float(row.get(m.numerator) or 0.0), float(row.get(f"n_{m.numerator}") or 0.0)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    def r(num: str, scale: float) -> float | None:
        x, n = float(row.get(num) or 0), float(row.get(f"n_{num}") or 0)
        return round(x / n * scale, 4) if n else None

    installs = float(row.get("installs") or 0)
    return {
        "impressions": int(row.get("impressions") or 0),
        "installs": installs,
        "spend": round(float(row.get("spend") or 0), 2),
        "ipm": r("installs", 1000),
        "ctr": r("clicks", 100),
        "cpi": round(float(row.get("spend") or 0) / installs, 4) if installs else None,
        "hook_rate": r("video_3s", 100),
        "hook_rate_2s": r("video_2s", 100),
        "completion": r("p100", 100),
        "hold": r("thruplay", 100),
    }


def _sum(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = defaultdict(float)
    for row in rows:
        for k, v in row.items():
            if isinstance(v, int | float) and k not in ("days",):
                out[k] += v
    return dict(out)


def _cmp(group: dict[str, Any], rest: dict[str, Any], m: Metric, min_n: float) -> dict[str, Any]:
    x, n = _rate(group, m)
    rx, rn = _rate(rest, m)
    c = compare(x, n, rx, rn, min_n)
    s = m.scale
    return {
        "metric": m.name,
        "value": None if c.rate is None else round(c.rate * s, 4),
        "ci": [round(c.ci[0] * s, 4), round(c.ci[1] * s, 4)],
        "rest": None if c.rest_rate is None else round(c.rest_rate * s, 4),
        "rest_ci": [round(c.rest_ci[0] * s, 4), round(c.rest_ci[1] * s, 4)],
        "n": n,
        "rest_n": rn,
        "p_better": None if c.p_better is None else round(c.p_better, 4),
        "verdict": c.verdict,
    }


def build_report(
    store: PerfStore,
    *,
    network: str | None = None,
    since: str | None = None,
    until: str | None = None,
    metric: str = "ipm",
    min_impressions: float = 10000,
) -> dict[str, Any]:
    if metric not in METRICS:
        raise ValueError(f"--metric must be one of {', '.join(METRICS)}")
    m = METRICS[metric]
    where, params = "", []
    if network:
        where += " AND network = ?"
        params.append(network)
    if since:
        where += " AND date >= ?"
        params.append(since)
    if until:
        where += " AND date_end <= ?"
        params.append(until)
    rows = _aggregate(store, where, tuple(params))
    tags = {r["creative_id"]: json.loads(r["tags"]) for r in store.rows("SELECT creative_id, tags FROM creatives")}
    unmatched_rows = store.rows(
        "SELECT network, COUNT(DISTINCT ad_name) AS names, SUM(impressions) AS impressions FROM perf_rows "
        f"WHERE creative_id IS NULL AND level != 'asset'{where} GROUP BY network",
        tuple(params),
    )
    unmatched = {r["network"]: {"names": r["names"], "impressions": r["impressions"] or 0} for r in unmatched_rows}

    networks: dict[str, Any] = {}
    for net in sorted({r["network"] for r in rows} | set(unmatched)):
        mine = [r for r in rows if r["network"] == net]
        total = _sum(mine)
        creatives = []
        for r in sorted(mine, key=lambda r: -(r["impressions"] or 0)):
            rest = _sum([o for o in mine if o is not r])
            creatives.append(
                {
                    "creative_id": r["creative_id"],
                    "ad_name": r["ad_name"],
                    "days": r["days"],
                    "first": r["first"],
                    "last": r["last"],
                    "registered": r["creative_id"] in tags,
                    "tags": tags.get(r["creative_id"], {}),
                    **_summary(r),
                    "comparison": _cmp(r, rest, m, min_impressions),
                }
            )
        by_value: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        dims: dict[str, set[str]] = defaultdict(set)
        for r in mine:
            for dim, value in flatten(tags.get(r["creative_id"], {})):
                by_value[(dim, value)].append(r)
                dims[dim].add(r["creative_id"])
        tag_rows = []
        for (dim, value), group in sorted(by_value.items()):
            ids = {g["creative_id"] for g in group}
            rest = [r for r in mine if r["creative_id"] in dims[dim] and r["creative_id"] not in ids]
            g = _sum(group)
            tag_rows.append(
                {
                    "dimension": dim,
                    "value": value,
                    "creatives": len(ids),
                    **_summary(g),
                    "comparison": _cmp(g, _sum(rest), m, min_impressions),
                    "rest_creatives": len({r["creative_id"] for r in rest}),
                }
            )
        networks[net] = {
            "totals": {"creatives": len(mine), **_summary(total)} if mine else {"creatives": 0},
            "first": min((r["first"] for r in mine), default=None),
            "last": max((r["last"] for r in mine), default=None),
            "creatives": creatives,
            "tags": tag_rows,
            "unmatched": unmatched.get(net, {"names": 0, "impressions": 0}),
        }
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "store": str(store.path),
        "filters": {"network": network, "since": since, "until": until},
        "metric": metric,
        "metric_label": m.label,
        "min_impressions": min_impressions,
        "networks": networks,
    }


# ------------------------------------------------------------------------------------------------------------- text
def _fmt(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"


def text_summary(rep: dict[str, Any]) -> str:
    lines = [f"{rep['metric_label']}; verdicts need >= {rep['min_impressions']:,.0f} impressions on both sides"]
    for net, d in rep["networks"].items():
        t = d["totals"]
        lines.append(
            f"\n{net}: {t.get('creatives', 0)} creative(s), {t.get('impressions', 0):,} impressions, "
            f"{d['first']} .. {d['last']}; unmatched names: {d['unmatched']['names']}"
        )
        for c in d["creatives"]:
            cmp_ = c["comparison"]
            lines.append(
                f"  {c['ad_name'][:70]:70}  {c['impressions']:>10,}  {rep['metric']} {_fmt(cmp_['value'])} "
                f"[{_fmt(cmp_['ci'][0])}, {_fmt(cmp_['ci'][1])}]  {cmp_['verdict']}"
            )
        for tg in d["tags"]:
            cmp_ = tg["comparison"]
            if cmp_["verdict"] in (WINNER, LOSER):
                lines.append(
                    f"  tag {tg['dimension']}={tg['value']}: {rep['metric']} {_fmt(cmp_['value'])} vs "
                    f"{_fmt(cmp_['rest'])} for the rest -> {cmp_['verdict']} (P(better) {cmp_['p_better']:.2f})"
                )
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------- HTML
def _verdict_class(v: str) -> str:
    return {WINNER: "pass", LOSER: "fail", INSUFFICIENT: "skip"}.get(v, "warn")


def render_tables(rep: dict[str, Any]) -> str:
    """The report body (no page chrome); also embedded by ``forge ui``."""
    e = html.escape
    out = []
    unit = METRICS[rep["metric"]].unit
    for net, d in rep["networks"].items():
        t = d["totals"]
        out.append(
            f"<h2>{e(net)}</h2><p class='muted'>{t.get('creatives', 0)} creative(s), "
            f"{t.get('impressions', 0):,} impressions, {e(str(d['first']))} to {e(str(d['last']))}. "
            f"Network ad names that are not creative-forge names: {d['unmatched']['names']} "
            f"({d['unmatched']['impressions']:,.0f} impressions, left out).</p>"
        )
        rows = []
        for c in d["creatives"]:
            cm = c["comparison"]
            p = "-" if cm["p_better"] is None else f"{cm['p_better']:.2f}"
            note = "" if c["registered"] else " <span class=muted>(not registered)</span>"
            rows.append(
                f"<tr><td><code>{e(c['ad_name'])}</code>{note}</td>"
                f"<td>{c['impressions']:,}</td><td>{_fmt(c['installs'], 0)}</td><td>{_fmt(c['ipm'])}</td>"
                f"<td>{_fmt(c['ctr'])}</td><td>{_fmt(c['cpi'])}</td><td>{_fmt(c['hook_rate'])}</td>"
                f"<td>{_fmt(c['hook_rate_2s'])}</td><td>{_fmt(c['completion'])}</td>"
                f"<td>{_fmt(cm['ci'][0])}&ndash;{_fmt(cm['ci'][1])}{unit}</td><td>{p}</td>"
                f"<td><span class='badge {_verdict_class(cm['verdict'])}'>{e(cm['verdict'])}</span></td></tr>"
            )
        out.append(
            "<h3>Creatives</h3><div class='scroll'><table><tr><th>ad name</th><th>impressions</th><th>installs</th>"
            "<th>IPM</th><th>CTR %</th><th>CPI</th><th>hook rate % (3 s)</th><th>hook rate % (2 s)</th>"
            f"<th>completion %</th><th>95% CI ({e(rep['metric'])})</th><th>P(better)</th><th>verdict</th></tr>"
            + "".join(rows)
            + "</table></div>"
        )
        trows = []
        for tg in d["tags"]:
            cm = tg["comparison"]
            p = "-" if cm["p_better"] is None else f"{cm['p_better']:.2f}"
            trows.append(
                f"<tr><td>{e(tg['dimension'])}</td><td>{e(tg['value'])}</td><td>{tg['creatives']}</td>"
                f"<td>{tg['impressions']:,}</td><td>{_fmt(cm['value'])}</td>"
                f"<td>{_fmt(cm['ci'][0])}&ndash;{_fmt(cm['ci'][1])}</td><td>{_fmt(cm['rest'])} "
                f"({tg['rest_creatives']} creative(s))</td><td>{p}</td>"
                f"<td><span class='badge {_verdict_class(cm['verdict'])}'>{e(cm['verdict'])}</span></td></tr>"
            )
        out.append(
            f"<h3>Tags</h3><div class='scroll'><table><tr><th>dimension</th><th>value</th><th>creatives</th>"
            f"<th>impressions</th><th>{e(rep['metric'])}</th><th>95% CI</th><th>rest of the dimension</th>"
            "<th>P(better)</th><th>verdict</th></tr>" + "".join(trows) + "</table></div>"
            if trows
            else "<p class='muted'>No tags: run <code>forge perf register deliver/manifest.json</code>.</p>"
        )
    if not rep["networks"]:
        out.append("<p>No performance data yet. Import some with <code>forge perf import</code>.</p>")
    return "".join(out)


def write_html(rep: dict[str, Any], path: Path) -> Path:
    from creative_forge.report import CSS

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Creative Performance</title><style>{CSS}</style></head>
<body><main><h1>Creative performance</h1>
<p class="muted">{html.escape(rep["metric_label"])}. Winner or loser only when both sides have at least
{rep["min_impressions"]:,.0f} impressions and the 95% Wilson intervals do not overlap; P(better) is the Bayesian
probability (Beta posteriors, uniform priors) that the row beats the rest of its pool. Hook rate = 3-second views
(Meta <code>video_view</code>) per impression, an industry convention; TikTok's 2-second views are a separate column.
Generated {html.escape(rep["generated_at"])} from <code>{html.escape(rep["store"])}</code>.</p>
{render_tables(rep)}</main></body></html>"""
    path.write_text(body, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
