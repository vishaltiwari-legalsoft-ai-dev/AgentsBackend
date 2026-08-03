"""GA4 adapter tests: property discovery, overview parsing, degradation isolation."""
from datetime import date

import pytest

from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing, ga_discover_property, ga_fetch_overview


# ------------------------- fake Google API clients -------------------------

class _Exec:
    def __init__(self, payload, fail=False):
        self._payload = payload
        self._fail = fail

    def execute(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._payload


class FakeAdmin:
    """Mirrors the chained accountSummaries().list().execute() call shape."""

    def __init__(self, summaries):
        self._summaries = summaries

    def accountSummaries(self):
        return self

    def list(self, **kw):
        return _Exec({"accountSummaries": self._summaries})


class FakeData:
    """Mirrors properties().batchRunReports()/runReport() call shapes."""

    def __init__(self, batch, key_events=None, key_events_fail=False):
        self._batch = batch
        self._key_events = key_events or {"rows": []}
        self._key_events_fail = key_events_fail

    def properties(self):
        return self

    def batchRunReports(self, property=None, body=None):
        return _Exec(self._batch)

    def runReport(self, property=None, body=None):
        return _Exec(self._key_events, fail=self._key_events_fail)


# ------------------------------- discovery -------------------------------

def test_discovery_matches_brand_domain_root():
    svc = FakeAdmin([
        {"propertySummaries": [{"property": "properties/1", "displayName": "Acme Inc"}]},
        {"propertySummaries": [{"property": "properties/2", "displayName": "Legal Soft - Website"}]},
    ])
    found = ga_discover_property("legalsoft.com", service=svc)
    assert found == {"property": "properties/2", "name": "Legal Soft - Website"}


def test_discovery_single_property_wins_without_name_match():
    svc = FakeAdmin([{"propertySummaries": [{"property": "properties/9", "displayName": "Main site"}]}])
    assert ga_discover_property("legalsoft.com", service=svc)["property"] == "properties/9"


def test_discovery_no_access_is_credential_missing():
    with pytest.raises(CredentialMissing):
        ga_discover_property("legalsoft.com", service=FakeAdmin([]))


def test_discovery_ambiguous_properties_raise():
    svc = FakeAdmin([
        {"propertySummaries": [{"property": "properties/1", "displayName": "Alpha"}]},
        {"propertySummaries": [{"property": "properties/2", "displayName": "Beta"}]},
    ])
    with pytest.raises(CredentialMissing):
        ga_discover_property("legalsoft.com", service=svc)


# ------------------------------- overview fetch -------------------------------

# Metric order: sessions, totalUsers, newUsers, engagementRate, averageSessionDuration, screenPageViews.
# With two named dateRanges the API appends the range name as the LAST dimension value.
BATCH = {"reports": [
    {"rows": [
        {"dimensionValues": [{"value": "current"}],
         "metricValues": [{"value": "4800"}, {"value": "3900"}, {"value": "2100"},
                          {"value": "0.61"}, {"value": "95.5"}, {"value": "9100"}]},
        {"dimensionValues": [{"value": "previous"}],
         "metricValues": [{"value": "4100"}, {"value": "3400"}, {"value": "1800"},
                          {"value": "0.58"}, {"value": "88.0"}, {"value": "8000"}]},
    ]},
    {"rows": [
        {"dimensionValues": [{"value": "/pricing"}], "metricValues": [{"value": "1200"}, {"value": "900"}]},
    ]},
    {"rows": [
        {"dimensionValues": [{"value": "Organic Search"}, {"value": "current"}], "metricValues": [{"value": "2400"}]},
        {"dimensionValues": [{"value": "Organic Search"}, {"value": "previous"}], "metricValues": [{"value": "2000"}]},
        {"dimensionValues": [{"value": "Direct"}, {"value": "current"}], "metricValues": [{"value": "1100"}]},
    ]},
]}

WINDOW = (date(2026, 7, 6), date(2026, 8, 3), date(2026, 6, 8), date(2026, 7, 5))


def test_overview_parses_totals_pages_channels_and_key_events():
    svc = FakeData(BATCH, key_events={"rows": [
        {"dimensionValues": [{"value": "generate_lead"}], "metricValues": [{"value": "31"}]},
        {"dimensionValues": [{"value": "scroll"}], "metricValues": [{"value": "0"}]},
    ]})
    ga = ga_fetch_overview("properties/2", *WINDOW, service=svc)
    assert ga["totals"] == {"sessions": 4800, "users": 3900, "new_users": 2100,
                            "engagement_rate": 0.61, "avg_session_sec": 95.5, "pageviews": 9100}
    assert ga["prev_totals"]["sessions"] == 4100
    assert ga["top_pages"] == [{"path": "/pricing", "views": 1200, "sessions": 900}]
    organic = next(c for c in ga["channels"] if c["channel"] == "Organic Search")
    assert (organic["sessions"], organic["prev_sessions"]) == (2400, 2000)
    direct = next(c for c in ga["channels"] if c["channel"] == "Direct")
    assert direct["prev_sessions"] == 0
    assert ga["key_events"] == [{"event": "generate_lead", "count": 31}]  # zero-count rows dropped


def test_key_events_failure_never_sinks_overview():
    svc = FakeData(BATCH, key_events_fail=True)
    ga = ga_fetch_overview("properties/2", *WINDOW, service=svc)
    assert ga["key_events"] == []
    assert ga["totals"]["sessions"] == 4800


# ------------------------------- run integration -------------------------------

def _fake_overview(sessions=100, prev_sessions=80):
    zero = {"sessions": prev_sessions, "users": 60, "new_users": 20,
            "engagement_rate": 0.5, "avg_session_sec": 70.0, "pageviews": 150}
    now = dict(zero, sessions=sessions, users=75)
    return {"totals": now, "prev_totals": zero, "top_pages": [], "channels": [], "key_events": []}


def test_run_brand_attaches_ga_and_persists_discovered_property(monkeypatch):
    brand = insights.list_brands()[0]
    monkeypatch.setattr(insights, "ga_discover_property",
                        lambda domain: {"property": "properties/7", "name": "Legal Soft"})
    monkeypatch.setattr(insights, "ga_fetch_overview", lambda *a, **k: _fake_overview())
    run = insights.run_brand(brand, trigger="test", today=date(2026, 8, 3))
    assert run["ga"]["property"] == "properties/7"
    assert run["ga"]["totals"]["sessions"] == 100
    saved = next(b for b in insights.list_brands() if b["id"] == brand["id"])
    assert saved["ga4_property"] == "properties/7"  # discovery persisted, runs once
    assert any("website sessions" in b.lower() for b in run["insights"])


def test_run_brand_skips_discovery_when_property_known(monkeypatch):
    brand = dict(insights.list_brands()[0], ga4_property="properties/7", ga4_property_name="Legal Soft")
    monkeypatch.setattr(insights, "ga_discover_property",
                        lambda domain: (_ for _ in ()).throw(AssertionError("must not rediscover")))
    monkeypatch.setattr(insights, "ga_fetch_overview", lambda *a, **k: _fake_overview())
    run = insights.run_brand(brand, trigger="test", today=date(2026, 8, 3))
    assert run["ga"]["property"] == "properties/7"


def test_run_brand_degrades_when_ga_unavailable():
    brand = insights.list_brands()[0]
    run = insights.run_brand(brand, trigger="test", today=date(2026, 8, 3))  # offline mode
    assert run["ga"] is None
    assert any(n.startswith("Google Analytics") for n in run["degraded"])
