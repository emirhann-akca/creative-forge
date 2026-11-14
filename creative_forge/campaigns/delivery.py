"""Simulated delivery for the mock network: what an active campaign would buy in a day, with realistic noise.

Nothing here is a forecast. It exists so the whole loop (create, activate, deliver, import, judge) can run offline:

* spend paces at 86-100 % of the daily budget and never past the remaining total cap;
* CPM varies day to day (log-normal, sigma 0.12) around a per-network base in USD;
* the network concentrates impressions on the ads it likes: share ~ IPM^1.5 with log-normal noise, floored at 8 %;
* installs, clicks and 3-second views are binomial draws on those impressions from each ad's true rates, whose daily
  IPM also wobbles (sigma 0.10).

Each ad's true rates come from a stable hash of its name (IPM 4-9, CTR 0.8-1.6 %, hook rate 20-35 %) unless a
``truth`` entry whose ``contains`` is a substring of the name overrides them. Seeds are fixed per (seed, day,
campaign), so a run reproduces.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

CPM_USD = {"meta": 1.6, "tiktok": 1.2, "google": 1.0}


@dataclass(frozen=True)
class Truth:
    ipm: float
    ctr: float
    hook: float


def stable(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def truth_for(name: str, overrides: list[dict[str, Any]]) -> Truth:
    for t in overrides:
        if str(t.get("contains", "")) and str(t["contains"]) in name:
            return Truth(float(t.get("ipm", 6.0)), float(t.get("ctr", 0.012)), float(t.get("hook", 0.28)))
    h = stable(name)
    return Truth(
        4 + (h % 1000) / 1000 * 5, 0.008 + ((h >> 10) % 1000) / 1000 * 0.008, 0.2 + ((h >> 20) % 1000) / 1000 * 0.15
    )


def simulate_day(
    network: str,
    campaign_key: str,
    day: date,
    units: list[tuple[str, str]],  # (unit id, unit name)
    daily: float,
    remaining: float | None,
    overrides: list[dict[str, Any]],
    seed: int = 7,
) -> list[dict[str, Any]]:
    if not units or daily <= 0 or (remaining is not None and remaining <= 0.005):
        return []
    rng = np.random.default_rng([seed, day.toordinal(), stable(campaign_key) % (2**31)])
    spend = daily * rng.uniform(0.86, 1.0)
    if remaining is not None:
        spend = min(spend, remaining)
    cpm = CPM_USD.get(network, 1.3) * math.exp(rng.normal(0, 0.12))
    impressions = int(spend / cpm * 1000)
    truths = [truth_for(name, overrides) for _, name in units]
    w = np.array([t.ipm**1.5 * math.exp(rng.normal(0, 0.2)) for t in truths])
    w = w / w.sum()
    w = np.maximum(w, 0.08)
    w = w / w.sum()
    shares = rng.multinomial(impressions, w)
    rows = []
    for (uid, name), t, imp in zip(units, truths, shares, strict=True):
        imp = int(imp)
        ipm = t.ipm * math.exp(rng.normal(0, 0.10))
        v3 = int(rng.binomial(imp, t.hook)) if imp else 0
        rows.append(
            {
                "network": network,
                "campaign": campaign_key,
                "unit": uid,
                "name": name,
                "date": day.isoformat(),
                "impressions": imp,
                "clicks": int(rng.binomial(imp, t.ctr)) if imp else 0,
                "installs": int(rng.binomial(imp, min(1.0, ipm / 1000))) if imp else 0,
                "spend": round(spend * imp / impressions, 4) if impressions else 0.0,
                "video_3s": v3,
                "video_2s": int(v3 * 1.25),
                "video_6s": int(v3 * 0.55),
                "p25": int(v3 * 0.62),
                "p50": int(v3 * 0.41),
                "p75": int(v3 * 0.27),
                "p100": int(v3 * 0.16),
            }
        )
    return rows
