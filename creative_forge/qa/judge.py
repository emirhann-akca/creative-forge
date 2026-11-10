"""Blind LLM judging with variance reporting.

Production lesson: a single judge run is noise. With temperature 0, the same video scored 8 in one run and 4 in
the next. So the judge runs N >= 2 times, videos are shuffled and renamed to codes (V01, V02, ...) so the model
cannot tell which variant or round a video came from, and every criterion is reported as mean and stdev with an
"unstable" flag when runs disagree.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creative_forge.qa.result import CheckResult, Status

CRITERIA = (
    "hook_stop_power",
    "hook_to_game_transition",
    "gameplay_clarity",
    "satisfaction_payoff",
    "production_polish",
    "truthfulness_safety",
    "overall",
)

JUDGE_PROMPT = """You are a senior performance-creative strategist at a top mobile game publisher. You review a
vertical video ad for a {genre} mobile game. Judge it exactly as you would before spending real UA budget on it in
the US on Meta/TikTok. Be strict and specific; do not be polite. Score each criterion 1-10 where 10 = indistinguishable
from the best-performing ads in the genre.

Criteria:
- hook_stop_power: would the first 1-2 seconds stop a US casual-puzzle player from scrolling?
- hook_to_game_transition: is the move from the intro into gameplay seamless and satisfying (no jarring cut,
  no mismatch)?
- gameplay_clarity: within the first 5 seconds of gameplay, can a new viewer understand what to do in the game?
- satisfaction_payoff: is there a satisfying moment / reward that makes you want to play?
- production_polish: visual and audio polish, pacing, no amateur look.
- truthfulness_safety: does it avoid misleading the viewer about the actual game (10 = fully honest)?
- overall: would you put budget behind this ad?

Then list EVERY concrete flaw you can find with a timestamp (mm:ss): AI artifacts (warped hands, morphing objects,
flicker), garbled or misspelled text, UI that changes between intro and gameplay, awkward cuts, unreadable text, audio
problems, pacing problems, anything that looks cheap. If there are none, return an empty list.

Answer ONLY with JSON: {{"scores": {{"hook_stop_power": n, "hook_to_game_transition": n, "gameplay_clarity": n,
"satisfaction_payoff": n, "production_polish": n, "truthfulness_safety": n, "overall": n}},
"hook_description": "...", "strongest_point": "...", "weakest_point": "...",
"flaws": [{{"t": "mm:ss", "flaw": "..."}}]}}"""


def judge_prompt(genre: str = "goods-sort puzzle") -> str:
    return JUDGE_PROMPT.format(genre=genre)


@dataclass
class JudgeAggregate:
    runs: int
    scores: dict[str, list[float]]
    mean: dict[str, float]
    stdev: dict[str, float]
    unstable: list[str]
    flaws: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "scores": self.scores,
            "mean": {k: round(v, 2) for k, v in self.mean.items()},
            "stdev": {k: round(v, 2) for k, v in self.stdev.items()},
            "unstable": self.unstable,
            "flaws": self.flaws,
            "errors": self.errors,
        }


def aggregate(results: list[dict[str, Any]], unstable_stdev: float = 1.0) -> JudgeAggregate:
    """``results``: one parsed judge response per run (``{"scores": {...}, "flaws": [...]}`` or ``{"error": ...}``)."""
    ok = [r for r in results if isinstance(r.get("scores"), dict)]
    errors = [str(r.get("error")) for r in results if "error" in r]
    scores: dict[str, list[float]] = {c: [] for c in CRITERIA}
    for r in ok:
        for c in CRITERIA:
            v = r["scores"].get(c)
            if isinstance(v, int | float):
                scores[c].append(float(v))
    mean = {c: statistics.fmean(v) for c, v in scores.items() if v}
    stdev = {c: (statistics.stdev(v) if len(v) > 1 else 0.0) for c, v in scores.items() if v}
    unstable = [c for c, s in stdev.items() if s > unstable_stdev]
    flaws = [{"run": i, **f} for i, r in enumerate(ok) for f in (r.get("flaws") or []) if isinstance(f, dict)]
    return JudgeAggregate(len(ok), {c: v for c, v in scores.items() if v}, mean, stdev, unstable, flaws, errors)


def check_judge(
    agg: JudgeAggregate, min_runs: int = 2, unstable_stdev: float = 1.0, min_overall: float | None = None
) -> CheckResult:
    threshold = f">= {min_runs} runs, stdev <= {unstable_stdev:g}" + (
        f", overall >= {min_overall:g}" if min_overall is not None else ""
    )
    if agg.runs == 0:
        return CheckResult(
            "judge", Status.FAIL, None, threshold, message="no judge run succeeded: " + "; ".join(agg.errors[:3])
        )
    overall = agg.mean.get("overall")
    details = agg.to_dict()
    if min_overall is not None and overall is not None and overall < min_overall:
        return CheckResult(
            "judge", Status.FAIL, overall, threshold, "/10", f"mean overall below {min_overall:g}", details
        )
    if agg.runs < min_runs:
        return CheckResult(
            "judge",
            Status.WARN,
            overall,
            threshold,
            "/10",
            f"only {agg.runs} run(s): single judge runs are noise",
            details,
        )
    if agg.unstable:
        spread = ", ".join(f"{c} {agg.scores[c]}" for c in agg.unstable[:3])
        return CheckResult("judge", Status.WARN, overall, threshold, "/10", f"unstable across runs: {spread}", details)
    return CheckResult(
        "judge",
        Status.PASS,
        overall,
        threshold,
        "/10",
        f"mean of {agg.runs} runs, overall stdev {agg.stdev.get('overall', 0):.2f}",
        details,
    )


def blind_codes(videos: list[Path], seed: int) -> dict[str, Path]:
    """Shuffle and relabel videos as V01, V02, ... so the judge never sees names or order."""
    order = list(videos)
    random.Random(seed).shuffle(order)
    return {f"V{i + 1:02d}": v for i, v in enumerate(order)}
