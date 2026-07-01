import os
from datetime import date

from marketing_research_agent.schemas import DateRange
from marketing_research_agent.sources.csv_source import CsvSource

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
RANGE = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def test_google_ads_metrics_parse():
    src = CsvSource(os.path.join(FIX, "google_ads.csv"), platform="google_ads")
    metrics, gaps = src.fetch_campaign_metrics(RANGE)
    assert gaps == []
    assert len(metrics) == 2
    pi = next(m for m in metrics if m.campaign == "PI-Search")
    assert pi.spend == 1200.0 and pi.demos_booked == 4 and pi.channel == "Google"


def test_hubspot_leads_parse():
    src = CsvSource(os.path.join(FIX, "hubspot_leads.csv"), platform="hubspot")
    leads, gaps = src.fetch_leads(RANGE)
    assert gaps == []
    assert len(leads) == 3
    assert {l.practice_area for l in leads} == {"PI", "Immigration"}


def test_missing_column_produces_datagap_not_crash(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Campaign,Cost\nX,100\n")  # missing required columns
    src = CsvSource(str(p), platform="google_ads")
    metrics, gaps = src.fetch_campaign_metrics(RANGE)
    assert metrics == []
    assert len(gaps) >= 1
    assert any("missing" in g.message.lower() for g in gaps)
