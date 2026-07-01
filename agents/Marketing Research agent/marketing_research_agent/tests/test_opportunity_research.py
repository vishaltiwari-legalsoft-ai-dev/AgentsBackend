from datetime import date

from marketing_research_agent.modules import opportunity_research as orr
from marketing_research_agent.schemas import MediaOpportunity


def _opp(**kw):
    base = dict(
        name="Law Firm Podcast", type="podcast", audience_size=50000,
        engagement_rate=0.8, host_authority=0.9, practice_area_fit=1.0,
    )
    base.update(kw)
    return MediaOpportunity(**base)


def test_score_in_range_and_set():
    o = _opp()
    s = orr.score(o)
    assert 0.0 <= s <= 1.0 and o.icp_score == s


def test_rank_filters_low_scores():
    low = _opp(name="weak", audience_size=10, engagement_rate=0.0,
               host_authority=0.0, practice_area_fit=0.0)
    ranked = orr.rank([_opp(), low])
    assert all(o.name != "weak" for o in ranked)


def test_stale_outreach_flagged():
    o = _opp(outreach_status="contacted", last_contact="2026-06-01")
    assert o in orr.stale_outreach([o], today=date(2026, 6, 30))


def test_placement_issue_when_active_sponsor_missing_promo():
    o = _opp(outreach_status="active", promo_code_live=False, utm_firing=True)
    assert o in orr.placement_issues([o])
