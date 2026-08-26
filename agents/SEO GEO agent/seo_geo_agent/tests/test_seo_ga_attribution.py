"""One brand must never be handed another brand's GA4 property.

The live incident this pins: ``berry-virtual`` (berryvirtual.com) and
``legal-soft`` (legalsoft.com) shared a workspace where the service account
could see exactly one GA4 property, "Legal Soft". ``ga_discover_property``
matches the domain root against property names, finds nothing for
"berryvirtual", and then falls back to "there is only one property, take it".
``_ga_section`` pinned the result with ``upsert_brand``, so Berry Virtual's SEO
panel reported Legal Soft's 25,595 sessions — under the name "Legal Soft" —
every day, permanently, with no note saying anything was wrong.

The single-property fallback itself is deliberate and still tested in
``test_seo_ga.py``; it is genuinely useful for the first brand in a workspace.
What was missing is the check that the property is not already spoken for.
"""
from __future__ import annotations

from datetime import date

import pytest

from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

LEGAL_SOFT = {
    "id": "legal-soft",
    "name": "legal soft",
    "domain": "legalsoft.com",
    "ga4_property": "properties/357249754",
    "ga4_property_name": "Legal Soft",
}
BERRY = {"id": "berry-virtual", "name": "berry Virtual", "domain": "berryvirtual.com"}


@pytest.fixture()
def registry(monkeypatch):
    """A two-brand registry, and a record of anything pinned during the test."""
    brands = [dict(LEGAL_SOFT), dict(BERRY)]
    pinned: list[dict] = []
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(b) for b in brands])
    monkeypatch.setattr(insights, "upsert_brand", lambda b: pinned.append(b))
    return pinned


def test_a_property_another_brand_pinned_is_refused(registry, monkeypatch):
    """The regression. Berry Virtual must not inherit Legal Soft's property."""
    monkeypatch.setattr(
        insights,
        "ga_discover_property",
        lambda domain: {"property": "properties/357249754", "name": "Legal Soft"},
    )
    monkeypatch.setattr(
        insights, "ga_fetch_overview",
        lambda *a, **k: pytest.fail("must not fetch another brand's analytics"),
    )

    with pytest.raises(CredentialMissing) as exc:
        insights._ga_section(dict(BERRY), date(2026, 8, 25))

    message = str(exc.value)
    assert "already pinned" in message
    assert "legal soft" in message.lower()
    assert not registry, "a refused property must never be pinned to the brand"


def test_an_unclaimed_property_is_still_discovered_and_pinned(registry, monkeypatch):
    """The convenience the fallback exists for keeps working.

    Nothing else owns properties/999, so Berry Virtual may take it — this is the
    first-brand-in-a-workspace case, and breaking it would be an overcorrection.
    """
    monkeypatch.setattr(
        insights,
        "ga_discover_property",
        lambda domain: {"property": "properties/999", "name": "Berry Virtual"},
    )
    monkeypatch.setattr(insights, "ga_fetch_overview", lambda *a, **k: {"totals": {}})

    section = insights._ga_section(dict(BERRY), date(2026, 8, 25))

    assert section["property"] == "properties/999"
    assert len(registry) == 1
    assert registry[0]["ga4_property"] == "properties/999"


def test_a_brand_keeps_its_own_pinned_property(registry, monkeypatch):
    """An already-pinned brand never re-discovers, so it cannot be re-attributed."""
    monkeypatch.setattr(
        insights, "ga_discover_property",
        lambda domain: pytest.fail("must not rediscover an already-pinned brand"),
    )
    monkeypatch.setattr(insights, "ga_fetch_overview", lambda *a, **k: {"totals": {}})

    section = insights._ga_section(dict(LEGAL_SOFT), date(2026, 8, 25))

    assert section["property"] == "properties/357249754"
    assert section["property_name"] == "Legal Soft"
    assert not registry, "no re-pin should happen for a brand that already has one"
