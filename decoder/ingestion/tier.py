from __future__ import annotations

from decoder.config import TIER_LOC_BOUNDS, TIER_TEAM_PLAN, Tier
from decoder.schemas import RepoMetrics, TierDecision


def decide_tier(metrics: RepoMetrics) -> TierDecision:
    loc = metrics.total_lines
    tier = Tier.NANO
    for candidate in (Tier.NANO, Tier.SMALL, Tier.MEDIUM, Tier.LARGE, Tier.HUGE):
        lo, hi = TIER_LOC_BOUNDS[candidate]
        if lo <= loc < hi:
            tier = candidate
            break
    else:
        tier = Tier.HUGE

    plan = TIER_TEAM_PLAN[tier]
    reasoning = (
        f"{loc:,} LOC places this repo in the {tier.value} tier "
        f"(bounds {TIER_LOC_BOUNDS[tier][0]:,}-{TIER_LOC_BOUNDS[tier][1]:,}). "
        f"Plan: {plan['teams']} team(s) x {plan['workers_per_team']} worker(s)."
    )
    return TierDecision(
        tier=tier,
        reasoning=reasoning,
        teams=plan["teams"],
        workers_per_team=plan["workers_per_team"],
    )
