"""Venue discovery — the list the Action Plan is allowed to name.

Search is faked at the ``search`` seam; nothing here spends a Serper call. The
behaviours worth pinning are the ones that decide whether a plan is executable:
that a community is identified by its subreddit rather than by reddit.com, that
a venue our own polls prove the engines cite outranks one that merely exists,
and that a failed search degrades the list instead of taking the plan down.
"""
import pytest

from final_geo_agent import geo_venues
from seo_geo_agent.sources import CredentialMissing

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"]}

REPORT = {"source_gap": [
    {"domain": "clio.com", "count": 31, "example_prompt_ids": ["p1"]},
    {"domain": "g2.com", "count": 12, "example_prompt_ids": ["p2"]},
    {"domain": "legalsoft.com", "count": 4, "example_prompt_ids": ["p3"]},  # our own site
]}


def fake_search(results_by_query: dict):
    def search(query: str) -> dict:
        for needle, organic in results_by_query.items():
            if needle in query:
                return {"organic": organic}
        return {"organic": []}
    return search


REDDIT_HITS = [
    {"link": "https://www.reddit.com/r/LawFirm/comments/1/best_intake/", "title": "Best intake?"},
    {"link": "https://www.reddit.com/r/LawFirm/comments/2/va_recs/", "title": "VA recs"},
    {"link": "https://www.reddit.com/r/Lawyertalk/comments/3/who_do_you_use/", "title": "Who do you use"},
]


def test_communities_are_identified_by_subreddit_not_by_reddit_dot_com():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({"site:reddit.com": REDDIT_HITS}))

    communities = [v for v in out["venues"] if v["kind"] == geo_venues.KIND_COMMUNITY]
    names = sorted(v["name"] for v in communities)
    # "reddit.com" would be useless in a plan — you cannot go and post on reddit.com
    assert names == ["r/LawFirm", "r/Lawyertalk"]
    assert all(v["url"].startswith("https://reddit.com/r/") for v in communities)


def test_threads_from_one_subreddit_collapse_into_that_venue_with_examples():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({"site:reddit.com": REDDIT_HITS}))

    lawfirm = geo_venues.venue_by_name(out, "r/LawFirm")
    assert len(lawfirm["examples"]) == 2       # both threads kept as evidence
    assert lawfirm["examples"][0]["url"].endswith("/best_intake/")


def test_cited_domains_from_our_own_polls_become_venues():
    out = geo_venues.discover(BRAND, [], REPORT, search=fake_search({}))

    clio = geo_venues.venue_by_name(out, "clio.com")
    assert clio["cited_where_absent"] == 31
    assert clio["found_via"] == ["engine-citations"]


def test_our_own_domain_is_never_offered_as_a_venue():
    out = geo_venues.discover(BRAND, [], REPORT, search=fake_search({
        "best legal virtual assistant": [{"link": "https://legalsoft.com/blog", "title": "us"}],
    }))

    assert geo_venues.venue_by_name(out, "legalsoft.com") is None


def test_proven_venues_outrank_merely_discovered_ones():
    out = geo_venues.discover(BRAND, [], REPORT, search=fake_search({"site:reddit.com": REDDIT_HITS}))

    # clio.com is cited 31x in answers that skipped us; a subreddit that simply
    # exists has not earned the top of the list
    assert out["venues"][0]["name"] == "clio.com"
    assert out["venues"][1]["name"] == "g2.com"


def test_review_platforms_are_listed_even_when_the_brand_is_absent():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({}))

    capterra = geo_venues.venue_by_name(out, "capterra.com")
    # a missing profile is exactly why the venue belongs in the plan
    assert capterra["brand_present"] is False
    assert capterra["kind"] == geo_venues.KIND_REVIEW


def test_a_brand_result_on_a_review_platform_marks_the_profile_present():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({
        "Legal Soft reviews": [{"link": "https://www.g2.com/products/legal-soft/reviews",
                                "title": "Legal Soft Reviews"}],
    }))

    assert geo_venues.venue_by_name(out, "g2.com")["brand_present"] is True


# --------------------------------------------------------------- degradation

def test_a_missing_search_key_yields_a_partial_list_not_an_exception():
    def no_key(query):
        raise CredentialMissing("SEO_SERPER_API_KEY not set")

    out = geo_venues.discover(BRAND, [], REPORT, search=no_key)

    # citation-derived venues still make a usable plan
    assert geo_venues.venue_by_name(out, "clio.com")
    assert out["complete"] is False
    assert out["searched"] == 0
    assert any("search unavailable" in e for e in out["errors"])


def test_one_failing_query_does_not_lose_the_others():
    def flaky(query):
        if "reddit" in query:
            raise RuntimeError("upstream 500")
        return {"organic": [{"link": "https://abovethelaw.com/best-va", "title": "Best VAs"}]}

    out = geo_venues.discover(BRAND, [], {}, search=flaky)

    assert geo_venues.venue_by_name(out, "abovethelaw.com")
    assert out["complete"] is False
    assert any("upstream 500" in e for e in out["errors"])


def test_a_fully_successful_sweep_reports_itself_complete():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({"site:reddit.com": REDDIT_HITS}))

    assert out["complete"] is True
    assert out["searched"] == 6


# ------------------------------------------------------------------ category

def test_category_comes_from_brand_seeds():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({}))
    assert out["category"] == "legal virtual assistant"


def test_category_falls_back_to_a_prompt_stripped_of_question_framing():
    seedless = {**BRAND, "seeds": []}
    prompts = [{"text": "best intake service for personal injury firms"}]

    out = geo_venues.discover(seedless, prompts, {}, search=fake_search({}))

    assert out["category"] == "intake service for personal injury firms"


def test_allowed_names_are_lower_cased_for_matching():
    out = geo_venues.discover(BRAND, [], {}, search=fake_search({"site:reddit.com": REDDIT_HITS}))

    assert "r/lawfirm" in geo_venues.allowed_names(out)


@pytest.mark.parametrize("url,expected", [
    ("https://www.reddit.com/r/LawFirm/comments/1/x/", "r/LawFirm"),
    ("https://reddit.com/r/legaladvice", "r/legaladvice"),
    ("https://reddit.com/user/someone", ""),
    ("", ""),
])
def test_subreddit_extraction(url, expected):
    assert geo_venues._subreddit_of(url) == expected
