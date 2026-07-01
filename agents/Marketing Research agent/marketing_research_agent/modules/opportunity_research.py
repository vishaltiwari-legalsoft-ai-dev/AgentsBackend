"""META/Google/Podcast/Affiliate Opportunity Research (requirements §3.4).

Scores candidate media against ICP-fit criteria, ranks/filters them, flags
stale outreach (>14 days no response), and verifies active sponsor placements.
"""

from __future__ import annotations

from datetime import date, datetime

from .. import config
from ..schemas import MediaOpportunity


def score(opp: MediaOpportunity) -> float:
    """Weighted ICP fit in [0, 1]; also stored on ``opp.icp_score``."""
    w = config.ICP["weights"]
    norm_audience = min(opp.audience_size / config.ICP["audience_size_norm"], 1.0)
    s = (
        w["audience_size"] * norm_audience
        + w["engagement_rate"] * min(opp.engagement_rate, 1.0)
        + w["host_authority"] * min(opp.host_authority, 1.0)
        + w["practice_area_fit"] * min(opp.practice_area_fit, 1.0)
    )
    opp.icp_score = round(s, 3)
    return opp.icp_score


def rank(opps: list[MediaOpportunity]) -> list[MediaOpportunity]:
    floor = config.ICP["min_score_to_surface"]
    scored = [o for o in opps if score(o) >= floor]
    scored.sort(key=lambda o: o.icp_score, reverse=True)
    return scored


def stale_outreach(opps: list[MediaOpportunity], today: date) -> list[MediaOpportunity]:
    limit = config.ICP["stale_outreach_days"]
    out = []
    for o in opps:
        if o.outreach_status == "contacted" and o.last_contact:
            days = (today - datetime.strptime(o.last_contact[:10], "%Y-%m-%d").date()).days
            if days > limit:
                out.append(o)
    return out


def placement_issues(opps: list[MediaOpportunity]) -> list[MediaOpportunity]:
    """Active sponsors whose promo code isn't live or whose UTM isn't firing."""
    return [
        o
        for o in opps
        if o.outreach_status == "active" and not (o.promo_code_live and o.utm_firing)
    ]
