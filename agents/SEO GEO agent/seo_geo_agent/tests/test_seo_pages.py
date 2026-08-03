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


def test_ga_fetch_pages_skips_malformed_rows():
    """A row missing metrics must not raise KeyError/IndexError — that's not a
    CredentialMissing, so it would otherwise escape run_brand's degrade guard."""
    svc = FakePagesData({"rows": [
        {"dimensionValues": [{"value": "/good"}],
         "metricValues": [{"value": "10"}, {"value": "5"}, {"value": "0.5"}]},
        {"dimensionValues": [{"value": "/bad"}], "metricValues": [{"value": "10"}]},
    ]})
    pages = ga_fetch_pages("properties/2", date(2026, 7, 6), date(2026, 8, 3), service=svc)
    assert pages == [{"path": "/good", "views": 10, "sessions": 5, "engagement_rate": 0.5}]


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


def test_meta_long_only_gets_heuristic_rec():
    """A page whose only flag is meta-long must not fall through to the
    generic 'Healthy' fallback — it has a real, actionable issue."""
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page(meta_description="d" * 200)],
        ga_pages=[], gsc_rows=[],
    )
    page = doc["pages"][0]
    assert page["flags"] == ["meta-long"]
    assert page["recommendation"] == "Tighten the meta description under 160 characters — long snippets get truncated."


def test_data_notes_merge_into_persisted_doc():
    """Upstream GA/GSC degradation notes passed in by the caller (router or
    run_brand) must land in the persisted doc, not be silently dropped."""
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page()],
        ga_pages=[], gsc_rows=[],
        data_notes=["Search Console: offline mode", "Google Analytics: no property shared"],
    )
    assert "Search Console: offline mode" in doc["notes"]
    assert "Google Analytics: no property shared" in doc["notes"]


def test_offline_recs_are_honest_heuristics():
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     corpus_pages=[_corpus_page(meta_description="")],
                                     ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False  # offline → llm_text raises → heuristic path
    assert "meta description" in doc["pages"][0]["recommendation"].lower()
    assert pages_mod.latest("b")["pages"]  # persisted


def test_page_known_only_from_ga_is_flagged_not_crawled():
    """A page GA/GSC know about but the site crawl never fetched has no on-page
    facts to audit — it must not silently pass as 'healthy'."""
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[],
        ga_pages=[{"path": "/mystery", "views": 50, "sessions": 40, "engagement_rate": 0.3}],
        gsc_rows=[],
    )
    p = doc["pages"][0]
    assert p["flags"] == ["not-crawled"]
    assert p["recommendation"] == "Not in the site crawl — re-run the site analysis to audit this page."
    # no crawled pages at all -> ai_candidates is empty -> the AI pass never ran;
    # ai must reflect that honestly rather than defaulting to True
    assert doc["ai"] is False


def test_ai_recs_success_used_for_top_traffic_only(monkeypatch):
    """When the LLM call succeeds, its recommendation wins for the page it names;
    pages outside the top-MAX_AI_PAGES traffic slice still get honest heuristics."""
    n = pages_mod.MAX_AI_PAGES + 1
    corpus_pages = [_corpus_page(url=f"https://x.com/p{i}") for i in range(n)]
    ga_pages = [{"path": f"/p{i}", "views": n - i, "sessions": 5, "engagement_rate": 0.4}
                for i in range(n)]

    def fake_llm_text(system, prompt, **kw):
        return '```json\n[{"path": "/p0", "recommendation": "Ship the pricing table above the fold."}]\n```'

    monkeypatch.setattr(pages_mod, "llm_text", fake_llm_text)
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, corpus_pages, ga_pages, [])
    assert doc["ai"] is True
    by_path = {p["path"]: p for p in doc["pages"]}
    assert by_path["/p0"]["recommendation"] == "Ship the pricing table above the fold."
    assert by_path["/p0"]["ai"] is True
    # lowest-traffic page (index n-1) falls outside the top MAX_AI_PAGES slice sent to the LLM
    beyond = by_path[f"/p{n - 1}"]
    assert beyond["recommendation"] == "Healthy — keep it fresh and add internal links to weaker pages."
    assert beyond["ai"] is False


def test_ai_recs_malformed_json_falls_back_to_heuristics(monkeypatch):
    monkeypatch.setattr(pages_mod, "llm_text", lambda system, prompt, **kw: "not json at all")
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     corpus_pages=[_corpus_page()], ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False
    assert pages_mod.NO_REC_NOTE in doc["notes"]
    assert all(p["recommendation"] for p in doc["pages"])
