"""Page-level intelligence tests: GA pages, merge, health flags, AI recs."""
from datetime import date

import pytest

from seo_geo_agent.sources import CredentialMissing, ga_fetch_pages, PageFacts
from seo_geo_agent import site_brain
from seo_geo_agent import pages as pages_mod
from seo_geo_agent.sources import QueryStat


class _Exec:
    def __init__(self, payload, fail=False):
        self._payload, self._fail = payload, fail

    def execute(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._payload


class FakePagesData:
    def __init__(self, payload):
        self._payload = payload

    def properties(self):
        return self

    def runReport(self, property=None, body=None):
        return _Exec(self._payload)


def test_ga_fetch_pages_parses_rows():
    svc = FakePagesData({"rows": [
        {"dimensionValues": [{"value": "/pricing"}],
         "metricValues": [{"value": "1200"}, {"value": "900"}, {"value": "0.7"}]},
    ]})
    pages = ga_fetch_pages("properties/2", date(2026, 7, 6), date(2026, 8, 3), service=svc)
    assert pages == [{"path": "/pricing", "views": 1200, "sessions": 900, "engagement_rate": 0.7}]


def test_corpus_entries_carry_on_page_facts():
    facts = PageFacts(url="https://x.com/a", status=200, title="A", meta_description="d",
                      h1=["A"], word_count=500, text="body")
    corpus = site_brain.build_corpus(
        {"id": "b", "domain": "x.com"},
        fetch=lambda url, client=None: facts,
        sitemap=lambda domain, client=None: ["https://x.com/a"],
    )
    entry = corpus["pages"][0]
    assert (entry["meta_description"], entry["h1_count"], entry["images_no_alt"]) == ("d", 1, 0)


# ------------------------------- page intelligence -------------------------------

def _corpus_page(url="https://x.com/pricing", **kw):
    base = {"url": url, "title": "Pricing", "word_count": 800, "meta_description": "d",
            "h1_count": 1, "images_no_alt": 0}
    base.update(kw)
    return base


def test_merge_joins_three_sources_by_path():
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page()],
        ga_pages=[{"path": "/pricing", "views": 100, "sessions": 80, "engagement_rate": 0.5}],
        gsc_rows=[QueryStat(query="cost", page="https://x.com/pricing", clicks=10,
                            impressions=200, ctr=0.05, position=4.0)],
    )
    p = doc["pages"][0]
    assert (p["path"], p["views"], p["clicks"], p["best_query"]) == ("/pricing", 100, 10, "cost")


def test_health_flags_fire():
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page(title="", meta_description="", word_count=100, h1_count=0)],
        ga_pages=[], gsc_rows=[],
    )
    flags = doc["pages"][0]["flags"]
    assert {"no-title", "no-meta", "thin", "no-h1"} <= set(flags)


def test_offline_recs_are_honest_heuristics():
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     corpus_pages=[_corpus_page(meta_description="")],
                                     ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False  # offline → llm_text raises → heuristic path
    assert "meta description" in doc["pages"][0]["recommendation"].lower()
    assert pages_mod.latest("b")["pages"]  # persisted
