"""Intervals for rates, so a winner is never declared from one number on a small sample.

Rates here are proportions of impressions (installs, clicks, 3-second views, completions per impression). Each gets
a Wilson score interval, which behaves at small counts and near 0 where the normal approximation does not, and a
Bayesian probability that it beats the comparison group: P(p_a > p_b) under independent Beta(1 + x, 1 + n - x)
posteriors (uniform priors), estimated with a fixed-seed Monte Carlo so reports are reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

Z95 = 1.959963984540054


def wilson(x: float, n: float, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    x = min(max(x, 0.0), n)
    p = x / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def prob_better(xa: float, na: float, xb: float, nb: float, draws: int = 20000, seed: int = 20261111) -> float:
    if na <= 0 or nb <= 0:
        return 0.5
    rng = np.random.default_rng(seed)
    a = rng.beta(1 + xa, 1 + max(na - xa, 0), draws)
    b = rng.beta(1 + xb, 1 + max(nb - xb, 0), draws)
    return float((a > b).mean())


WINNER, LOSER, NO_DIFFERENCE, INSUFFICIENT = "winner", "loser", "no clear difference", "insufficient data"


@dataclass(frozen=True)
class Comparison:
    rate: float | None
    ci: tuple[float, float]
    rest_rate: float | None
    rest_ci: tuple[float, float]
    p_better: float | None
    verdict: str


def compare(x: float, n: float, rest_x: float, rest_n: float, min_n: float) -> Comparison:
    """One group against everything else in its pool. A verdict needs ``n >= min_n`` on both sides and 95 %
    intervals that do not overlap; otherwise "insufficient data" or "no clear difference"."""
    rate = x / n if n else None
    rest = rest_x / rest_n if rest_n else None
    ci, rest_ci = wilson(x, n), wilson(rest_x, rest_n)
    if n < min_n or rest_n < min_n:
        return Comparison(rate, ci, rest, rest_ci, None, INSUFFICIENT)
    p = prob_better(x, n, rest_x, rest_n)
    if ci[0] > rest_ci[1]:
        verdict = WINNER
    elif ci[1] < rest_ci[0]:
        verdict = LOSER
    else:
        verdict = NO_DIFFERENCE
    return Comparison(rate, ci, rest, rest_ci, p, verdict)
