from datetime import date

from marketing_research_agent.modules import funnel_analysis as fa
from marketing_research_agent.schemas import Lead


def _lead(ch, stage, pa="PI", src="google"):
    return Lead(
        id="x", channel=ch, utm_source=src, utm_medium="cpc",
        utm_campaign="c", practice_area=pa, stage=stage, created_at=date(2026, 6, 29),
    )


LEADS = [
    _lead("Google", "qualified"),
    _lead("Google", "booked"),
    _lead("Google", "form_fill"),
    _lead("Organic", "form_fill", src=""),
]


def test_attribution_pct():
    a = fa.attribution(LEADS)
    assert a["total"] == 4 and a["attributed"] == 3 and a["pct"] == 75.0


def test_conversion_by_channel():
    conv = fa.conversion_by_channel(LEADS)
    assert conv["Google"]["booked_rate"] is not None


def test_best_practice_areas_sorted():
    rows = fa.best_practice_areas(LEADS)
    assert rows[0]["qualified_rate"] >= rows[-1]["qualified_rate"]


def test_low_booking_channels_flagged():
    leads = [_lead("Email", "form_fill") for _ in range(10)]
    assert "Email" in fa.low_booking_channels(leads, min_leads=5, max_rate=0.1)
