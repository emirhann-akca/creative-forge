"""Pause / scale recommendations from the test cell's locked rule and the campaign's caps.

The verdicts come from ``perf.cells.judge`` (the rule was locked when the campaign was activated). A recommendation is
only an action when ``automation.auto_pause`` is true, and then only in the safe direction: pausing an ad that the
rule killed, or pausing a campaign that reached its total cap. Scaling is never applied: the most it does is name a
daily budget 20 % higher, capped at ``limits.max_daily``, for a person to set.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from creative_forge.campaigns.state import ACTIVE, PAUSED, CampaignRecord
from creative_forge.perf.cells import KEEP, KILL, KILL_EARLY, SCALE, CellResult

PAUSE_AD, SCALE_UP, PAUSE_CAMPAIGN, HOLD, WAIT, ALERT = "pause_ad", "scale", "pause_campaign", "keep", "wait", "alert"
SCALE_STEP = 0.20


@dataclass
class Recommendation:
    action: str
    target: str
    reason: str
    auto: bool = False  # applied automatically (auto_pause on, safe direction)
    applied: bool = False
    ad_id: str | None = None
    suggested_daily: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_lift(lift: float | None) -> str:
    return "n/a" if lift is None else f"{lift * 100:+.0f} %"


def recommend(
    rec: CampaignRecord, info: dict[str, Any], result: CellResult | None, auto_pause: bool
) -> list[Recommendation]:
    out: list[Recommendation] = []
    spend, cap, cur = float(info.get("spend") or 0), rec.total_cap, rec.currency
    status = info.get("status")
    if spend >= cap - 0.005:
        out.append(
            Recommendation(
                PAUSE_CAMPAIGN,
                rec.ref,
                f"spend {spend:.2f} {cur} reached the total cap {cap:.2f}",
                auto=auto_pause and status == ACTIVE,
            )
        )
    elif spend >= 0.8 * cap:
        out.append(Recommendation(ALERT, rec.ref, f"spend {spend:.2f} {cur} is {spend / cap:.0%} of the total cap"))
    if float(info.get("daily") or 0) > rec.daily + 0.005:
        out.append(
            Recommendation(
                ALERT,
                rec.ref,
                f"the network's daily budget {float(info['daily']):.2f} is above the "
                f"{rec.daily:.2f} forge created: changed outside forge",
            )
        )
    if status == ACTIVE and rec.status == PAUSED:
        out.append(Recommendation(ALERT, rec.ref, "active on the network although forge last paused it"))
    if result is None:
        return out
    by_creative = {}
    for ad_name, ad_id in rec.ads.items():
        by_creative[ad_name.split(".")[-1][:10] if ad_name.startswith("cf1.") else ad_name] = (ad_name, ad_id)
    ctl = result.control
    for o in result.outcomes:
        name, ad_id = by_creative.get(o.arm.creative_id, (o.arm.ad_name or o.arm.creative_id, None))
        detail = (
            f"{result.cell.metric} lift {_fmt_lift(o.lift)} vs control, P(better) "
            f"{'n/a' if o.p_better is None else f'{o.p_better:.2f}'}, installs {o.arm.installs:.0f} / "
            f"{ctl.installs:.0f}"
        )
        if o.verdict in (KILL, KILL_EARLY):
            out.append(
                Recommendation(
                    PAUSE_AD,
                    name,
                    f"rule says kill ({o.verdict}): {detail}",
                    auto=auto_pause and status == ACTIVE and ad_id is not None,
                    ad_id=ad_id,
                )
            )
        elif o.verdict == SCALE:
            target = round(min(rec.daily * (1 + SCALE_STEP), rec.max_daily), 2)
            reason = f"rule says scale: {detail}; raise the daily budget by hand to {target:.2f} {cur}"
            if target <= rec.daily:
                reason = f"rule says scale: {detail}; the daily budget is already at limits.max_daily"
            out.append(Recommendation(SCALE_UP, name, reason, auto=False, ad_id=ad_id, suggested_daily=target))
        elif o.verdict == KEEP:
            out.append(Recommendation(HOLD, name, f"no clear difference under the rule: {detail}", ad_id=ad_id))
        else:
            out.append(
                Recommendation(
                    WAIT,
                    name,
                    f"no decision yet: {o.installs_missing} more installs per arm (to {o.next_threshold})",
                    ad_id=ad_id,
                )
            )
    return out
