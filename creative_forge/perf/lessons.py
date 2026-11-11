"""Performance lessons for ``forge ideate``: clear tag winners and losers, written where the library loader looks.

Only tag values with a verdict (``winner`` or ``loser``, which already require the minimum impressions on both sides
and non-overlapping 95 % intervals) become lessons. Each lesson states the network, the dates, the metric with its
interval, the sample and the comparison group, so the model that reads it (and the person who reviews its concepts)
can weigh it. Lessons live in ``<data dir>/library/perf_lessons.yaml``; a new report replaces the lessons of the same
network and metric and leaves the rest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from creative_forge.paths import data_dir
from creative_forge.perf.report import METRICS
from creative_forge.perf.stats import LOSER, WINNER

NAMES = {
    "hook_type": "hook type",
    "mechanic_shown": "mechanic shown",
    "emotion": "emotion",
    "format": "format",
    "length_bucket": "length",
    "ai_generated": "AI-generated",
}


def lessons_path() -> Path:
    return data_dir() / "library" / "perf_lessons.yaml"


def lessons_from(rep: dict[str, Any]) -> list[dict[str, Any]]:
    metric = METRICS[rep["metric"]]
    out = []
    for net, d in rep["networks"].items():
        for tg in d["tags"]:
            cm = tg["comparison"]
            if cm["verdict"] not in (WINNER, LOSER):
                continue
            dim = NAMES.get(tg["dimension"], tg["dimension"])
            verb = "wins" if cm["verdict"] == WINNER else "loses"
            advice = (
                f"Prefer {tg['value']} for this {dim} in new concepts, and keep a smaller share of tests on the others."
                if cm["verdict"] == WINNER
                else f"Use {tg['value']} for this {dim} less, or only with a new angle that addresses why it lost."
            )
            out.append(
                {
                    "id": f"perf-{net}-{metric.name}-{tg['dimension']}-{tg['value']}",
                    "title": f"{net}: {dim} {tg['value']} {verb} on {metric.name.upper()} "
                    f"({cm['value']:.2f} vs {cm['rest']:.2f})",
                    "text": (
                        f"Performance lesson from imported ad data ({net}, {d['first']} to {d['last']}). Creatives "
                        f"tagged {tg['dimension']}={tg['value']} ({dim} {tg['value']}) had {metric.label} "
                        f"{cm['value']:.2f} (95% interval {cm['ci'][0]:.2f}-{cm['ci'][1]:.2f}) over "
                        f"{cm['n']:,.0f} impressions from {tg['creatives']} creative(s), against {cm['rest']:.2f} "
                        f"({cm['rest_ci'][0]:.2f}-{cm['rest_ci'][1]:.2f}) for other {dim} values over "
                        f"{cm['rest_n']:,.0f} impressions from {tg['rest_creatives']} creative(s). "
                        f"P(better than the rest) = {cm['p_better']:.2f}. The {dim} {tg['value']} {verb} on installs "
                        f"and {metric.name}. {advice}"
                    ),
                    "network": net,
                    "metric": metric.name,
                    "verdict": cm["verdict"],
                    "generated_at": rep["generated_at"],
                }
            )
    return out


def write_lessons(rep: dict[str, Any], path: Path | None = None) -> tuple[Path, int]:
    path = Path(path or lessons_path())
    new = lessons_from(rep)
    replaced = {(net, rep["metric"]) for net in rep["networks"]}
    keep = []
    if path.is_file():
        old = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        keep = [x for x in old.get("lessons", []) if (x.get("network"), x.get("metric")) not in replaced]
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "lessons": keep + new,
    }
    header = (
        "# Written by `forge perf report --write-lessons`. Read by `forge ideate` (search_creative_library).\n"
        "# Each lesson is a tag value with a clear winner/loser verdict on imported ad data.\n"
    )
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return path, len(new)
