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
                                     corpus_pages=[_corpus_page(meta_description="", word_count=120)],
                                     ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False  # offline → llm_text raises → heuristic path
    page = doc["pages"][0]
    assert "no-meta" in page["flags"]                       # the audit-grouped finding: a chip
    assert page["recommendation"] == pages_mod.FLAG_TEXT["thin"]  # page-specific rule speaks
    assert page["rec_source"] == "rule"
    assert pages_mod.latest("b")["pages"]  # persisted


def test_audit_grouped_flags_are_chips_not_repeated_sentences():
    """Missing meta / long title / missing H1 / missing alt are ONE grouped
    finding in the audit; repeating 'Add a meta description' on forty rows is
    the repetition the tester reported."""
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page(url=f"https://x.com/p{i}", meta_description="",
                                   title="T" * 70, images_no_alt=2) for i in range(3)],
        ga_pages=[], gsc_rows=[])
    for page in doc["pages"]:
        assert {"no-meta", "title-long", "images-no-alt"} <= set(page["flags"])
        assert page["recommendation"] == "" and page["rec_source"] == "flags"


def test_template_image_is_not_a_chip_on_every_row():
    """A header logo missing alt on every page is ONE template finding in the
    audit — not an images-no-alt chip repeated on each page row."""
    logo, chart = "https://x.com/logo.svg", "https://x.com/chart.png"
    corpus = [_corpus_page(url=f"https://x.com/p{i}", images_no_alt=1, images_no_alt_srcs=[logo])
              for i in range(3)]
    corpus[2].update(images_no_alt=2, images_no_alt_srcs=[logo, chart])
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, corpus, [], [])
    flagged = {p["path"]: "images-no-alt" in p["flags"] for p in doc["pages"]}
    assert flagged == {"/p0": False, "/p1": False, "/p2": True}   # only the page's own chart


# ------------------------------- page parser -------------------------------

from seo_geo_agent.sources import is_question_heading, parse_page  # noqa: E402


def _page(body: str, head: str = "<title>Legal VA | Acme</title>", url="https://x.com/a") -> PageFacts:
    return parse_page(url, f"<html><head>{head}</head><body>{body}</body></html>")


def test_webflow_split_h1_counts_once():
    """Verified on live legalsoft.com (Webflow): one <h1> per page, but its text
    is split by <br> and <span> — per-chunk collection counted it 2-3 times."""
    facts = _page('<h1 class="heading">Legal <br>Virtual <span class="hl">Assistants</span>'
                  '<br/>for <strong>Law Firms</strong></h1><h2>Plans <em>and</em> pricing</h2>')
    assert facts.h1 == ["Legal Virtual Assistants for Law Firms"]
    assert facts.h2 == ["Plans and pricing"]


def test_unclosed_heading_ends_where_the_next_begins():
    facts = _page("<h2>First section<h2>Second section</h2>")
    assert facts.h2 == ["First section", "Second section"]


def test_svg_title_is_never_the_page_title_or_heading_text():
    facts = parse_page("https://x.com/a", "<html><body><svg><title>Menu icon</title></svg>"
                                          "<h1><svg><title>Arrow</title></svg>Pricing</h1></body></html>")
    assert facts.title == ""  # an icon's accessible name is not the page title
    assert facts.h1 == ["Pricing"]
    facts = _page("<svg><title>Logo</title></svg><p>Body</p>")
    assert facts.title == "Legal VA | Acme"


def test_missing_alt_is_distinct_from_decorative_alt():
    facts = _page('<img src="/a.png"><img src="/b.png" alt=""><img src="/c.png" alt>'
                  '<img src="/d.png" alt="Our team">')
    assert facts.images_total == 4
    assert facts.images_no_alt == 1          # no alt attribute: the real defect
    assert facts.images_decorative == 2      # alt="" and bare alt: declared decorative
    assert facts.images_no_alt_srcs == ["https://x.com/a.png"]


def test_noscript_duplicates_and_presentational_images_are_skipped():
    facts = _page('<img src="/hero.jpg" alt="Hero"><noscript><img src="/hero.jpg"></noscript>'
                  '<noscript><img src="https://px.ads.example/t.gif" height="1"></noscript>'
                  '<img src="/sp.gif" role="presentation"><img src="/i.svg" aria-hidden="true">')
    assert (facts.images_total, facts.images_no_alt, facts.images_decorative) == (1, 0, 0)


def test_offending_srcs_are_capped_deduped_and_resolved():
    imgs = "".join(f'<img src="img/{i}.png">' for i in range(25)) + '<img src="img/0.png">'
    facts = _page(imgs, url="https://x.com/blog/post")
    assert facts.images_no_alt == 26
    assert len(facts.images_no_alt_srcs) == 20
    assert facts.images_no_alt_srcs[0] == "https://x.com/blog/img/0.png"


def test_microdata_and_rdfa_types_are_captured():
    facts = _page('<div itemscope itemtype="https://schema.org/LocalBusiness">'
                  '<div vocab="https://schema.org/" typeof="Service Product"></div></div>')
    assert facts.schema_types == ["LocalBusiness", "Service", "Product"]


def test_jsonld_walked_recursively():
    head = ('<script type="application/ld+json">{"@context": "https://schema.org", "@graph": ['
            '{"@type": "WebSite"}, {"@type": "WebPage", "mainEntity": {"@type": "FAQPage",'
            ' "mainEntity": [{"@type": "Question", "acceptedAnswer": {"@type": "Answer"}}]}}]}'
            '</script><script type="application/ld+json; charset=utf-8">'
            '[{"@type": ["Service", "https://schema.org/Product"]}]</script>')
    facts = _page("<p>x</p>", head=head)
    assert facts.schema_types == ["WebSite", "WebPage", "FAQPage", "Question", "Answer",
                                  "Service", "Product"]
    assert facts.schema_errors == []


def test_malformed_jsonld_is_recorded_not_skipped():
    head = ('<script type="application/ld+json">{"@type": "Organization",, }</script>'
            '<script type="application/ld+json">{"@type": "WebSite"}</script>'
            '<script type="application/ld+json">   </script>')
    facts = _page("<p>x</p>", head=head)
    assert facts.schema_types == ["WebSite"]
    assert len(facts.schema_errors) == 1 and facts.schema_errors[0].startswith("JSON-LD block 1:")


@pytest.mark.parametrize(("heading", "is_q"), [
    ("How much does it cost?", True),
    ("Is it worth it", True),
    ("What is a legal virtual assistant", True),
    ("Can I cancel anytime", True),
    ("Pricing?", True),
    ("How it works", False),       # a section label, not a question
    ("What we do", False),
    ("Do more with less", False),
    ("Why choose us", False),
])
def test_question_style_headings(heading, is_q):
    assert is_question_heading(heading) is is_q


def test_question_headings_counted_across_levels():
    facts = _page("<h2>How much does it cost?</h2><h3>How it works</h3>"
                  "<h4>Is it worth it</h4><h5>What is legal intake</h5>")
    assert facts.q_headings == 3


def test_phone_and_address_detected():
    assert _page('<a href="tel:+15551234567">Call</a>').has_phone is True
    assert _page("<p>Call (555) 123-4567 today</p>").has_phone is True
    assert _page("<p>Visit 120 Main Street, Austin</p>").has_address is True
    plain = _page("<p>We answer calls for law firms in 2024.</p>")
    assert (plain.has_phone, plain.has_address) == (False, False)


def test_corpus_entry_stores_schema_question_and_image_fields():
    facts = parse_page("https://x.com/faq", (
        '<html><head><title>FAQ</title><script type="application/ld+json">{"@type": "FAQPage"}'
        '</script></head><body><h1>FAQ</h1><h2>How much does it cost?</h2><h2>Is it worth it</h2>'
        '<img src="/a.png"><img src="/b.png" alt=""><p>' + "word " * 50 + "</p></body></html>"))
    corpus = site_brain.build_corpus(
        {"id": "b", "domain": "x.com"},
        fetch=lambda url, client=None: facts,
        sitemap=lambda domain, client=None: ["https://x.com/faq"],
    )
    entry = corpus["pages"][0]
    assert entry["schema_types"] == ["FAQPage"] and entry["schema_errors"] == []
    assert entry["q_headings"] == 2
    assert (entry["images_total"], entry["images_no_alt"], entry["images_decorative"]) == (2, 1, 1)
    assert entry["images_no_alt_srcs"] == ["https://x.com/a.png"]
    assert entry["has_phone"] is False and entry["has_address"] is False


# ----------------------- per-page AI advice grounding -----------------------

def _ai_corpus():
    return [
        _corpus_page(url="https://x.com/pricing", title="P" * 70, schema_types=[], q_headings=0),
        _corpus_page(url="https://x.com/help", title="Help", schema_types=[], q_headings=3),
        _corpus_page(url="https://x.com/team", title="Team", schema_types=[], q_headings=0),
    ]


def test_ai_advice_that_rewords_the_flags_is_dropped(monkeypatch):
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: (
        '[{"path": "/pricing", "recommendation": "Shorten the title tag to under 60 characters."},'
        ' {"path": "/team", "recommendation": "Add each paralegal\'s bar admission and years in practice."}]'))
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, _ai_corpus(), [], [])
    by_path = {p["path"]: p for p in doc["pages"]}
    assert by_path["/pricing"]["ai"] is False                      # the chip says it already
    assert (by_path["/pricing"]["recommendation"], by_path["/pricing"]["rec_source"]) == ("", "flags")
    assert by_path["/team"]["rec_source"] == "ai"
    assert by_path["/team"]["recommendation"].startswith("Add each paralegal")
    assert doc["grounding"]["kept"] == 1
    assert [(d["path"], d["reason"]) for d in doc["grounding"]["dropped"]] == [("/pricing", "repeats-flags")]


def test_ai_advice_adding_a_named_query_to_a_flag_is_kept():
    assert pages_mod.rewords_flags("Shorten the title to under 60 characters.", ["title-long"])
    assert not pages_mod.rewords_flags(
        "Lead the title with 'legal virtual assistant pricing' and cut the brand suffix.", ["title-long"])
    assert pages_mod.rewords_flags("Write descriptive alt text for all images", ["images-no-alt"])
    assert not pages_mod.rewords_flags("Write descriptive alt text for all images", [])


def test_ai_schema_advice_must_fit_the_page(monkeypatch):
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: (
        '[{"path": "/team", "recommendation": "Add FAQPage schema to win rich results."},'
        ' {"path": "/help", "recommendation": "Mark up the three questions with FAQPage JSON-LD."},'
        ' {"path": "/nowhere", "recommendation": "Publish a pricing calculator."}]'))
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, _ai_corpus(), [], [])
    by_path = {p["path"]: p for p in doc["pages"]}
    assert by_path["/help"]["rec_source"] == "ai"     # 3 question headings: FAQPage fits
    assert by_path["/team"]["rec_source"] != "ai"     # no questions: the FAQ claim is ungrounded
    reasons = {d["path"]: d["reason"] for d in doc["grounding"]["dropped"]}
    assert reasons == {"/team": "schema-not-fit", "/nowhere": "unknown-page"}


def test_schema_advice_on_a_corpus_without_schema_facts_is_dropped(monkeypatch):
    """A corpus crawled before schema was stored cannot verify fit — unknown is not 'fits'."""
    old = [_corpus_page(url="https://x.com/help")]  # no schema_types / q_headings keys
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: (
        '[{"path": "/help", "recommendation": "Add FAQPage structured data here."}]'))
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, old, [], [])
    assert doc["pages"][0]["rec_source"] == "rule"
    assert doc["grounding"]["dropped"][0]["reason"] == "schema-not-fit"


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
