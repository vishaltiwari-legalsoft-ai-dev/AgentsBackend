"""Competitor profile engine tests: resolve_top5, visibility/keywords_won math,
honest reach estimates, and persistence. Fully offline with injected fakes."""
import pytest

from seo_geo_agent import competitors, state
from seo_geo_agent.sources import CredentialMissing, PageFacts

BRAND = {"id": "acme", "domain": "acme.com", "competitors": []}


# ------------------------------- resolve_top5 -------------------------------

def test_resolve_top5_manual_first_topped_up_and_deduped():
    brand = {"competitors": ["A.com", "b.com"]}
    ranks_doc = {"suggested_competitors": ["b.com", "c.com", "d.com", "e.com", "f.com", "g.com"]}
    assert competitors.resolve_top5(brand, ranks_doc) == ["A.com", "b.com", "c.com", "d.com", "e.com"]


def test_resolve_top5_no_ranks_doc_uses_manual_only():
    brand = {"competitors": ["x.com", "y.com"]}
    assert competitors.resolve_top5(brand, None) == ["x.com", "y.com"]


def test_resolve_top5_empty_everything():
    assert competitors.resolve_top5({}, None) == []


# ------------------------------- profile math -------------------------------

def _seed_ranks(brand_id: str) -> None:
    doc = {
        "snapshots": [
            {"at": "2026-07-20", "ranks": {  # older snapshot — must be ignored by the math
                "kw1": {"position": 5, "top": ["old.com"]},
            }},
            {"at": "2026-07-27", "ranks": {  # latest — this is what the math reads
                "kw1": {"position": 5, "top": ["comp.com", "other.com"]},
                "kw2": {"position": None, "top": ["comp.com"]},
                "kw3": {"position": 2, "top": ["other.com"]},
            }},
        ],
        "suggested_competitors": ["comp.com"],
    }
    state.save(f"ranks-{brand_id}", doc)


def test_build_profiles_visibility_and_keywords_won_math():
    _seed_ranks(BRAND["id"])
    doc = competitors.build_profiles(BRAND, fetch_sitemap=lambda d: [])
    profile = doc["profiles"][0]
    assert profile["domain"] == "comp.com"
    # comp.com appears in 2 of the 3 tracked keywords in the LATEST snapshot only
    assert profile["visibility_pct"] == 67
    # both appearances are at index 0 of "top" -> 1-based position 1
    assert profile["avg_position"] == 1.0
    won = {w["keyword"]: w for w in profile["keywords_won"]}
    assert won["kw1"] == {"keyword": "kw1", "their_position": 1, "our_position": 5}
    assert won["kw2"] == {"keyword": "kw2", "their_position": 1, "our_position": None}
    assert "kw3" not in won  # comp.com isn't even in kw3's top


def test_build_profiles_raises_without_rank_snapshot():
    with pytest.raises(CredentialMissing, match="data refresh"):
        competitors.build_profiles({"id": "nodata", "domain": "nodata.com"})


def test_latest_profiles_persists():
    _seed_ranks(BRAND["id"])
    competitors.build_profiles(BRAND, fetch_sitemap=lambda d: [])
    assert competitors.latest_profiles(BRAND["id"])["profiles"][0]["domain"] == "comp.com"
    assert competitors.latest_profiles("never-run") is None


# --------------------------- content feed / reach estimates ---------------------------

def test_honest_none_estimate_when_no_volume_match():
    _seed_ranks(BRAND["id"])
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/old"]}})
    state.save(f"keywords-{BRAND['id']}", {"clusters": [
        {"name": "totally different topic", "intent": "informational",
         "keywords": ["totally different topic"], "volume_est": 999},
    ]})

    def fetch_sitemap(domain):
        return ["https://comp.com/old", "https://comp.com/new-unrelated-post"]

    def fetch(url):
        return PageFacts(url=url, status=200, title="Legal Answering Service Guide")

    def search(query):
        raise AssertionError("must not call serper when no keyword-lab volume matches the title")

    doc = competitors.build_profiles(BRAND, search=search, fetch=fetch, fetch_sitemap=fetch_sitemap)
    post = doc["profiles"][0]["recent_posts"][0]
    assert post["est_monthly_clicks"] is None
    assert post["estimate_basis"] == "no volume data — reach unknown"


def test_reach_estimate_uses_lab_volume_and_ctr_curve():
    _seed_ranks(BRAND["id"])
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/old"]}})
    state.save(f"keywords-{BRAND['id']}", {"clusters": [
        {"name": "legal answering service", "intent": "commercial",
         "keywords": ["legal answering service"], "volume_est": 500},
    ]})

    def fetch_sitemap(domain):
        return ["https://comp.com/old", "https://comp.com/blog/legal-answering-service-guide"]

    def fetch(url):
        return PageFacts(url=url, status=200, title="Legal Answering Service Guide",
                          h1=["The Ultimate Legal Answering Service Guide"])

    def search(query):
        return {"organic": [{"link": "https://comp.com/blog/legal-answering-service-guide",
                              "title": "x", "position": 3}]}

    doc = competitors.build_profiles(BRAND, search=search, fetch=fetch, fetch_sitemap=fetch_sitemap)
    post = doc["profiles"][0]["recent_posts"][0]
    assert post["topic"] == "The Ultimate Legal Answering Service Guide"
    assert post["est_monthly_clicks"] == 55  # round(500 * ctr_at(3)=0.11)
    assert post["estimate_basis"] == "lab volume × CTR curve"


def test_domain_stats_visibility_none_when_no_tracked_keywords():
    """total==0 must not read as '0% visible' — that's a real (bad) number vs
    'we have no data to compute this from' — the two must not be conflated."""
    visibility_pct, avg_position, won = competitors._domain_stats("comp.com", {})
    assert visibility_pct is None
    assert avg_position is None
    assert won == []


def test_broken_fetch_excludes_post_from_feed():
    """fetch_page returns a non-raising empty/failed PageFacts on HTTP failure —
    the loop must not render a 404'd/moved post using the raw URL as its title."""
    _seed_ranks(BRAND["id"])
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/old"]}})

    def fetch_sitemap(domain):
        return ["https://comp.com/old", "https://comp.com/moved-page"]

    def fetch(url):
        return PageFacts(url=url, status=404)

    doc = competitors.build_profiles(BRAND, fetch=fetch, fetch_sitemap=fetch_sitemap)
    assert doc["profiles"][0]["recent_posts"] == []


def test_sitemap_watch_failure_degrades_with_note_and_continues():
    _seed_ranks(BRAND["id"])

    def fetch_sitemap(domain):
        raise CredentialMissing("sitemap unreachable")

    doc = competitors.build_profiles(BRAND, fetch_sitemap=fetch_sitemap)
    assert any(n == "Sitemap watch: sitemap unreachable" for n in doc["notes"])
    profile = doc["profiles"][0]
    assert profile["domain"] == "comp.com"
    assert profile["recent_posts"] == []
    assert profile["hot_topics"] == []
    # visibility/keywords_won come from the ranks doc, unaffected by the feed failure
    assert profile["visibility_pct"] == 67


def test_build_profiles_does_not_touch_shared_sitemaps_doc():
    """The manual/track_competitors `sitemaps-{id}` doc and the profile run's
    own `profile-sitemaps-{id}` doc must be independent — no last-writer-wins."""
    _seed_ranks(BRAND["id"])
    state.save(f"sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/manual-seed"]}})

    competitors.build_profiles(
        BRAND, fetch_sitemap=lambda d: ["https://comp.com/manual-seed", "https://comp.com/new"]
    )

    assert state.load(f"sitemaps-{BRAND['id']}")["domains"]["comp.com"] == ["https://comp.com/manual-seed"]
    assert state.load(f"profile-sitemaps-{BRAND['id']}") is not None


def test_page_fetch_failure_notes_the_url_and_lets_other_domains_build():
    _seed_ranks(BRAND["id"])
    brand = {**BRAND, "competitors": ["comp2.com"]}  # manual competitor, tops up with suggested comp.com
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {
        "comp2.com": ["https://comp2.com/baseline"],
        "comp.com": ["https://comp.com/baseline"],
    }})

    def fetch_sitemap(domain):
        return {
            "comp2.com": ["https://comp2.com/baseline", "https://comp2.com/new-post"],
            "comp.com": ["https://comp.com/baseline", "https://comp.com/new-post"],
        }[domain]

    def fetch(url):
        if "comp2.com" in url:
            raise CredentialMissing("page unreachable")
        return PageFacts(url=url, status=200, title="Some Title")

    doc = competitors.build_profiles(brand, fetch=fetch, fetch_sitemap=fetch_sitemap)
    assert any(n == "Page fetch https://comp2.com/new-post: page unreachable" for n in doc["notes"])
    by_domain = {p["domain"]: p for p in doc["profiles"]}
    assert by_domain["comp2.com"]["recent_posts"] == []  # broke before appending anything
    assert len(by_domain["comp.com"]["recent_posts"]) == 1  # the next domain still builds fine


def test_serper_failure_notes_domain_and_disables_further_calls():
    _seed_ranks(BRAND["id"])
    new_urls = [f"https://comp.com/blog/legal-answering-service-{i}" for i in range(2)]
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/baseline"]}})
    state.save(f"keywords-{BRAND['id']}", {"clusters": [
        {"name": "legal answering service", "intent": "commercial",
         "keywords": ["legal answering service"], "volume_est": 500},
    ]})

    def fetch_sitemap(domain):
        return ["https://comp.com/baseline"] + new_urls

    def fetch(url):
        return PageFacts(url=url, status=200, title="Legal Answering Service Update")

    calls_made = []

    def search(query):
        calls_made.append(query)
        raise CredentialMissing("serper down")

    doc = competitors.build_profiles(BRAND, search=search, fetch=fetch, fetch_sitemap=fetch_sitemap)
    assert len(calls_made) == 1  # disabled after the first failure — post 2 never retries
    assert any(n == "Serper comp.com: serper down" for n in doc["notes"])
    posts = doc["profiles"][0]["recent_posts"]
    assert len(posts) == 2
    assert all(p["est_monthly_clicks"] is None for p in posts)
    # both titles DID match a keyword-lab volume — the failed/skipped SERP check
    # must not make the basis lie and claim there was no volume data at all
    assert all(p["estimate_basis"] == "volume matched — SERP check unavailable this run" for p in posts)


def test_serper_cap_two_calls_per_competitor():
    _seed_ranks(BRAND["id"])
    new_urls = [f"https://comp.com/blog/legal-answering-service-{i}" for i in range(3)]
    state.save(f"profile-sitemaps-{BRAND['id']}", {"domains": {"comp.com": ["https://comp.com/baseline"]}})
    state.save(f"keywords-{BRAND['id']}", {"clusters": [
        {"name": "legal answering service", "intent": "commercial",
         "keywords": ["legal answering service"], "volume_est": 500},
    ]})

    def fetch_sitemap(domain):
        return ["https://comp.com/baseline"] + new_urls

    def fetch(url):
        return PageFacts(url=url, status=200, title="Legal Answering Service Update")

    calls = []

    def search(query):
        calls.append(query)
        return {"organic": [{"link": "https://comp.com/x", "title": "x", "position": 2}]}

    doc = competitors.build_profiles(BRAND, search=search, fetch=fetch, fetch_sitemap=fetch_sitemap)
    assert len(calls) == 2  # capped even though all 3 posts matched a keyword-lab volume
    posts = doc["profiles"][0]["recent_posts"]
    assert sum(1 for p in posts if p["est_monthly_clicks"] is not None) == 2
    # the 3rd post's title DID match a lab volume — the cap just meant no SERP
    # check ran for it, which is a different (honest) claim than "no volume data"
    assert posts[2]["estimate_basis"] == "volume matched — SERP check unavailable this run"
