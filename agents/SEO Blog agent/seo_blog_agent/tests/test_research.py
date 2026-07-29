import pytest

from seo_geo_agent.sources import CredentialMissing, PageFacts
from seo_blog_agent import research

SERP = {
    "organic": [
        {"link": "https://a.com/post", "title": "Best Legal VAs", "position": 1},
        {"link": "https://b.com/post", "title": "Legal VA Guide", "position": 2},
        {"link": "https://c.com/post", "title": "What is a Legal VA", "position": 3},
    ],
    "related": ["legal virtual assistant cost", "what does a legal virtual assistant do"],
    "paa": ["What does a legal virtual assistant do?"],
    "aio_present": True,
}


def fake_search(query):
    return dict(SERP)


def fake_fetch(url):
    text = "legal virtual assistant " * 4 + "paralegal intake staffing law firm " * 30
    return PageFacts(url=url, status=200, title="T", h1=["H1"], h2=["Costs", "Hiring"],
                     word_count=1400, text=text)


def fake_llm(system, prompt):
    if '"pages"' in system:
        return {"pages": [{"url": "https://a.com/post", "intent": "commercial",
                           "page_type": "Best [Category]", "audience": "law firm owners"}]}
    return {"lsi": [{"term": "virtual paralegal", "fit_note": "hiring section"}]}


def no_llm(system, prompt):
    raise CredentialMissing("no key")


def test_sheet_core_fields():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    assert [t["url"] for t in sheet["serp"]["top3"]] == ["https://a.com/post", "https://b.com/post", "https://c.com/post"]
    assert sheet["serp"]["aio_present"] is True
    assert sheet["competitors"][0]["intent"] == "commercial"
    assert sheet["usage"]["main_count_top1"] == 4
    assert sheet["usage"]["target_min"] == 4 and sheet["usage"]["target_max"] == 6
    assert sheet["lsi"][0]["term"] == "virtual paralegal"
    assert sheet["data_source"] == "serp_estimated"


def test_gap_from_pasted_ahrefs_rows():
    ck = {"https://a.com/post": [
        {"keyword": "legal virtual assistant", "volume": 5400, "position": 3, "url": ""},
        {"keyword": "virtual paralegal services pricing guide", "volume": 880, "position": 7, "url": ""},
        {"keyword": "what does a legal va do", "volume": 300, "position": 5, "url": ""},
    ]}
    sheet = research.build_research("legal virtual assistant", {"metrics": {"volume": 5400}, "competitor_keywords": ck},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    tags = {g["keyword"]: g["tag"] for g in sheet["gap"]}
    assert tags["legal virtual assistant"] == "main"
    assert tags["virtual paralegal services pricing guide"] == "long_tail"
    assert tags["what does a legal va do"] == "aio"
    assert all(g["source"] == "ahrefs_pasted" for g in sheet["gap"])
    assert sheet["data_source"] == "ahrefs_pasted"


def test_gap_fallback_is_honestly_labeled():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    assert sheet["gap"] and all(g["source"] == "serp_estimated" for g in sheet["gap"])
    assert any("SERP-estimated" in n for n in sheet["degraded"])


def test_llm_down_degrades_not_crashes():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=no_llm)
    assert sheet["competitors"][0]["intent"] == ""
    assert len(sheet["lsi"]) >= 1  # SERP-derived fallback
    assert sheet["degraded"]


def test_serper_down_propagates():
    def down(q):
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
    with pytest.raises(CredentialMissing):
        research.build_research("x", {"metrics": {}, "competitor_keywords": {}},
                                search=down, fetch=fake_fetch, llm=fake_llm)
