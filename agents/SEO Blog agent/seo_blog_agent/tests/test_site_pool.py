from seo_geo_agent.sources import CredentialMissing, PageFacts
from seo_blog_agent import rules, site_pool

SITEMAP = ["https://legalsoft.com/", "https://legalsoft.com/services",
           "https://legalsoft.com/blog/hire-legal-va", "https://legalsoft.com/blog/intake-guide"]


def fake_sitemap(domain):
    return list(SITEMAP)


def fake_fetch(url):
    if "hire-legal-va" in url:
        return PageFacts(url=url, status=200, title="How to Hire a Legal Virtual Assistant",
                         h1=["Hire a Legal Virtual Assistant"], h2=["Costs", "Where to find one"],
                         meta_description="Hiring guide", word_count=1500)
    if "intake-guide" in url:
        return PageFacts(url=url, status=200, title="Client Intake Guide for Law Firms",
                         h1=["Client Intake Guide"], h2=["Intake forms"], word_count=1200)
    if url.endswith("/services"):
        return PageFacts(url=url, status=200, title="Legal Staffing Services",
                         h1=["Services"], h2=["Virtual paralegals", "Reception"], word_count=600)
    return PageFacts(url=url, status=200, title="Legal Soft — Legal Virtual Staffing",
                     h1=["Legal Soft"], h2=["Why us"], word_count=400)


def fake_llm(system, prompt):
    if '"themes"' in system:
        return {"themes": [
            {"name": "Legal virtual assistants", "keywords": ["legal virtual assistant cost"],
             "covered_by": ["https://legalsoft.com/blog/hire-legal-va"]},
            {"name": "Law firm billing", "keywords": ["law firm billing software"], "covered_by": []},
        ]}
    return {"topics": [{"keyword": "law firm billing software", "angle": "comparison for small firms"}]}


def no_llm(system, prompt):
    raise CredentialMissing("no key")


def _scan(llm=fake_llm):
    return site_pool.scan_site("https://www.LegalSoft.com", fetch=fake_fetch,
                               sitemap=fake_sitemap, llm=llm)


def test_scan_classifies_and_persists():
    p = _scan()
    assert p["domain"] == "legalsoft.com"
    assert p["counts"] == {"sitemap_urls": 4, "scanned": 4, "posts": 2, "pages": 2}
    assert all("fingerprint" in post for post in p["posts"])
    assert p["data_source"] == "site_scan"
    assert site_pool.load_site("legalsoft.com")["domain"] == "legalsoft.com"
    assert site_pool.list_sites()[0]["domain"] == "legalsoft.com"


def test_pool_falls_back_without_llm():
    p = _scan(llm=no_llm)
    assert p["pool"] and all(t["keywords"] for t in p["pool"])
    assert any("skipped" in n for n in p["degraded"])


def test_cannibalization_overlap():
    p = _scan()
    hits = site_pool.cannibalization(p, "hire legal virtual assistant")
    assert hits and hits[0]["url"] == "https://legalsoft.com/blog/hire-legal-va"
    assert site_pool.cannibalization(p, "maritime salvage law") == []


def test_suggest_topics_avoids_covered():
    p = _scan()
    out = site_pool.suggest_topics(p, llm=fake_llm)
    assert out["suggested"][0]["keyword"] == "law firm billing software"
    assert out["suggested"][0]["collisions"] == []
    assert any(a["covered_by"] for a in out["avoided"])


def test_suggest_topics_honest_without_llm():
    p = _scan()
    out = site_pool.suggest_topics(p, llm=no_llm)
    assert out["suggested"] and out["degraded"]


def test_internal_links_ranked_by_overlap():
    p = _scan()
    links = site_pool.internal_links(p, "legal virtual assistant")
    assert links[0] == "https://legalsoft.com/blog/hire-legal-va"
    assert len(links) <= 3
