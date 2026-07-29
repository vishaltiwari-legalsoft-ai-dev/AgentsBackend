from seo_geo_agent.sources import CredentialMissing
from seo_blog_agent import citations, rules

OUTLINE_DOC = {"targets": {"word_count": 2300, "links": 2},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []},
                           {"heading": "Hiring steps", "level": 2, "note": "", "keywords": []}]}

GOOD = {"claim": "78% of firms outsource intake", "source_name": "Clio 2025 Legal Trends Report",
        "url": "https://clio.com/report", "section": "Costs"}
DEAD = {"claim": "50% stat", "source_name": "Dead Source Annual Study", "url": "https://dead.com/x",
        "section": "Costs"}
OFFPAGE = {"claim": "totally different subject entirely", "source_name": "Mismatch Weekly Review",
           "url": "https://mismatch.com/y", "section": "Hiring steps"}

PAGES = {
    "https://clio.com/report": {"status": 200, "final_url": "https://clio.com/report",
                                "text": "Clio 2025 Legal Trends Report: 78% of firms outsource intake."},
    "https://dead.com/x": {"status": 404, "final_url": "https://dead.com/x", "text": ""},
    "https://mismatch.com/y": {"status": 200, "final_url": "https://mismatch.com/y",
                               "text": "unrelated page about gardening tips and tomato soil"},
    "https://aba.org/study": {"status": 200, "final_url": "https://aba.org/study",
                              "text": "ABA 2026 Tech Survey shows 61% adoption of legal automation."},
}


def fake_fetch_raw(url):
    return PAGES.get(url, {"status": 0, "final_url": url, "text": ""})


def test_only_verified_citations_enter():
    def llm(system, prompt):
        return {"citations": [GOOD, DEAD, OFFPAGE]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert [c["url"] for c in doc["items"]] == ["https://clio.com/report"]
    assert doc["items"][0]["verified"] is True
    assert doc["items"][0]["dr_status"] == "unverified"  # no DR pasted — honest flag
    assert doc["short_by"] == 1


def test_dr_enforced_when_known():
    def llm(system, prompt):
        return {"citations": [GOOD]}
    doc = citations.source_citations(OUTLINE_DOC, {"clio.com": 45}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"] == []  # DR 45 < 70 → rejected
    doc = citations.source_citations(OUTLINE_DOC, {"clio.com": 91}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"][0]["dr"] == 91 and doc["items"][0]["dr_status"] == "ok"


def test_retry_rounds_cap():
    calls = {"n": 0}

    def llm(system, prompt):
        calls["n"] += 1
        return {"citations": [DEAD]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert calls["n"] == rules.CITATION_MAX_ROUNDS
    assert doc["short_by"] == 2


def test_llm_down_is_honest():
    def llm(system, prompt):
        raise CredentialMissing("no key")
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"] == [] and doc["short_by"] == 2 and doc["degraded"]


def test_revet_applies_gate2_dr_paste():
    def llm(system, prompt):
        return {"citations": [GOOD, {"claim": "61% adoption of legal automation",
                                     "source_name": "ABA 2026 Tech Survey",
                                     "url": "https://aba.org/study", "section": "Hiring steps"}]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert len(doc["items"]) == 2
    out = citations.revet(doc, {"clio.com": 91, "aba.org": 55}, target=2)
    assert [c["domain"] for c in out["items"]] == ["clio.com"]
    assert out["items"][0]["dr_status"] == "ok"
    assert out["short_by"] == 1
