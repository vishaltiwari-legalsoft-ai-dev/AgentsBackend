import io

import pytest
from docx import Document

from seo_geo_agent.sources import CredentialMissing
from seo_blog_agent import drafting

SHEET = {"keyword": "legal virtual assistant",
         "usage": {"target_min": 2, "target_max": 4, "main_count_top1": 2, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}]}
OUTLINE_DOC = {"targets": {"word_count": 40, "links": 1},
               "meta": {"title": "T", "description": "D", "slug": "s"},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []}]}
CITATIONS = {"items": [{"id": "c1", "claim": "x", "source_name": "Clio 2025 Legal Trends Report",
                        "url": "https://clio.com/report", "domain": "clio.com", "dr": 91,
                        "dr_status": "ok", "section": "Costs", "verified": True}],
             "short_by": 0, "rounds": 1, "degraded": []}

GOOD_DRAFT = ("# T\n\n## Costs\n\nA legal virtual assistant helps firms. A legal virtual assistant "
              "and a virtual paralegal cut intake costs, per the [Clio 2025 Legal Trends Report]"
              "(https://clio.com/report). " + "Extra words here. " * 6)
# word budget: 47 tokens total — inside the compliance band for target 40 (36..50).


def test_compliance_all_pass():
    c = drafting.check_compliance(GOOD_DRAFT, SHEET, OUTLINE_DOC, CITATIONS)
    assert c["all_pass"] is True
    assert {x["id"] for x in c["checks"]} == {"kw_count", "lsi", "word_count", "citations", "sections", "meta"}


def test_compliance_catches_violations():
    bad = "# T\n\nshort text without the phrase or the source."
    c = drafting.check_compliance(bad, SHEET, OUTLINE_DOC, CITATIONS)
    fails = {x["id"] for x in c["checks"] if not x["pass"]}
    assert {"kw_count", "lsi", "word_count", "citations", "sections"} <= fails
    assert c["all_pass"] is False


def test_build_draft_assembles_prompt_and_checks():
    seen = {}

    def llm(system, prompt):
        seen["prompt"] = prompt
        return GOOD_DRAFT

    d = drafting.build_draft(SHEET, OUTLINE_DOC, CITATIONS, llm=llm)
    assert d["markdown"] == GOOD_DRAFT
    assert d["compliance"]["all_pass"] is True
    assert d["edited"] is False
    # the generation prompt must carry guidelines, hard rules, outline and sources
    assert "Hard rules" in seen["prompt"] and "Clio 2025 Legal Trends Report" in seen["prompt"]
    assert "legal virtual assistant" in seen["prompt"] and "Costs" in seen["prompt"]


def test_build_draft_raises_when_llm_down():
    def llm(system, prompt):
        raise CredentialMissing("no key")
    with pytest.raises(CredentialMissing):
        drafting.build_draft(SHEET, OUTLINE_DOC, CITATIONS, llm=llm)


def test_to_docx_roundtrip():
    data = drafting.to_docx("# Title\n\n## Costs\n\n- point one\n\nBody with [link](https://x.com).")
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    assert "Title" in texts and "Costs" in texts
    assert any("point one" in t for t in texts)
    assert any("link (https://x.com)" in t for t in texts)


def test_hard_rules_include_internal_links():
    sheet = dict(SHEET, internal_links=["https://legalsoft.com/services"])
    seen = {}

    def llm(system, prompt):
        seen["prompt"] = prompt
        return GOOD_DRAFT

    drafting.build_draft(sheet, OUTLINE_DOC, CITATIONS, llm=llm)
    assert "legalsoft.com/services" in seen["prompt"]
    drafting.build_draft(SHEET, OUTLINE_DOC, CITATIONS, llm=llm)  # no internal_links key
    assert "Link these pages" not in seen["prompt"]
