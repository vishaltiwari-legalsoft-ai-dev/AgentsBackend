"""Page Check — offline, over the frozen cold-brew fixture SERP.

Covers: the URL path (fake fetch → markdown → score) and the draft path,
target-query derivation (given / page title / draft heading), every
cannibalization branch including self-URL exclusion and the honest
"unknown", the verdict rules and confidence, brand isolation of the
optimizer index, the strengths mirror of ``gap_report``, and
``sources.fetch_html``'s address guard through a mock transport. The
strengths test lives here rather than beside the gap tests because
``test_optimizer_math.py`` belongs to another owner.
"""
import socket

import httpx
import pytest

from final_geo_agent import opt_pipeline, opt_score, page_check
from final_geo_agent.opt_config import load_config
from final_geo_agent.opt_serp import FixtureProvider, SerpData, SerpResult
from final_geo_agent.opt_structure import StructureBand
from final_geo_agent.tests.test_optimizer_pipeline import (
    DRAFT, FakeEmbedder, _filler, article_html, build_provider,
)
from seo_geo_agent import sources, state
from seo_geo_agent.sources import CredentialMissing

BRAND = {"id": "coffeelab", "name": "CoffeeLab", "domain": "mybrand.example.com"}
OTHER = {"id": "teahouse", "name": "TeaHouse", "domain": "teahouse.example.com"}
CHECKED_URL = "https://mybrand.example.com/blog/cold-brew"
KEYWORD = "how to make cold brew coffee"
CFG = load_config()


def page_html(title="How to Make Cold Brew Coffee | CoffeeLab", h1="How to make cold brew coffee"):
    h1_tag = f"<h1>{h1}</h1>" if h1 else ""
    return f"""<html lang="en"><head><title>{title}</title></head><body>
<nav><a href="/">Home</a> <a href="/shop">Shop All Products</a></nav>
<article>{h1_tag}
<p>Making cold brew at home is simple once the basics are in place. {_filler(7, 8)}</p>
<h2>What ratio should I use?</h2>
<p>The golden measure is a 1:8 ratio of coffee to water. Check the proportion with a scale every single batch you run.</p>
<table><tr><th>Style</th><th>Ratio</th></tr><tr><td>Concentrate</td><td>1:5</td></tr></table>
<h2>How long should it steep?</h2>
<p>Steep the coffee overnight, twelve hours is the sweet spot. Steeping much past a full day turns the batch woody.</p>
<h2>Equipment</h2>
<ul><li>A large jar with a tight lid for the whole batch</li><li>A coarse burr grinder for even grounds</li></ul>
<img src="/img/hero.jpg" alt="A jar of cold brew on the counter">
<p>Once bottled it lasts about a week in the fridge before the flavour fades.</p>
</article>
<footer><p>Copyright CoffeeLab. Privacy policy.</p></footer>
</body></html>"""


def fake_fetch(html=None):
    calls = []

    def fetch(url):
        calls.append(url)
        return url, (page_html() if html is None else html)

    fetch.calls = calls
    return fetch


def run_check(brand=BRAND, **kw):
    kw.setdefault("provider", build_provider())
    kw.setdefault("embedder", FakeEmbedder())
    return page_check.check(brand, **kw)


def own_row_provider(own_url: str, rank: int) -> FixtureProvider:
    """The 9-article fixture SERP with the brand's own page at a chosen rank."""
    base = build_provider()
    results = [r for r in base._serp.results if "mybrand" not in r.url]
    results.insert(rank - 1, SerpResult(rank=rank, url=own_url, title="Our own guide"))
    for i, r in enumerate(results):
        r.rank = i + 1
    serp = base._serp.model_copy(update={"results": results})
    return FixtureProvider(serp, base._pages)


# ------------------------------------------------------------ markdown parity

def test_html_to_markdown_yields_every_draft_feature():
    md, facts = page_check.html_to_markdown(page_html(), CFG.extract)
    assert facts["title"] == "How to Make Cold Brew Coffee | CoffeeLab"
    assert facts["h1"] == "How to make cold brew coffee"
    assert "Shop All Products" not in md and "Privacy policy" not in md   # nav/footer stripped
    f = opt_pipeline.draft_features(md)
    assert f["h2_count"] == 3 and f["question_headings"] == 3   # h1 "How to…" + two "?" h2s
    assert f["list_count"] == 1 and f["table_count"] == 1 and f["image_count"] == 1
    assert f["word_count"] > 100
    # 4 <p> + 4 headings + one list + one table + one image = 11 blocks, never one per list item
    assert f["paragraph_count"] == 11


def test_div_soup_page_falls_back_to_extracted_text_and_says_so():
    soup = "<html lang='en'><head><title>Cold brew basics | CoffeeLab</title></head><body><h1>Cold brew basics</h1>" \
           + "".join(f"<div>{_filler(i, 6)}</div>" for i in range(6)) + "</body></html>"
    doc = run_check(url=CHECKED_URL, fetch=fake_fetch(soup))
    pc = doc["page_check"]
    assert "markdown_fallback" in pc["page_flags"]
    assert pc["verdict"]["confidence"] == "low"
    assert doc["last_report"]["draft_features"]["word_count"] > 50


# ------------------------------------------------------------------ check()

def test_check_url_end_to_end_persists_block_and_index_row():
    fetch = fake_fetch()
    doc = run_check(url=CHECKED_URL, fetch=fetch)
    assert fetch.calls == [CHECKED_URL]
    pc = doc["page_check"]
    assert pc["source_url"] == CHECKED_URL
    assert pc["target_query"] == KEYWORD and pc["target_query_source"] == "page_title"
    assert pc["verdict"]["label"] in {"likely helps", "needs work", "likely cannibalizes", "cannot tell"}
    assert pc["verdict"]["confidence"] in {"high", "medium", "low"}
    assert any("winners' median" in r for r in pc["verdict"]["reasons"])
    assert pc["disclaimer"] == opt_score.DISCLAIMER
    # the page's ratio section is a covered subtopic, with the page's own words as evidence
    assert any(p["kind"] == "subtopic" and "ratio" in p["message"] and "1:8" in p["message"] for p in pc["pros"])
    assert any(p["kind"] == "term" for p in pc["pros"]) and len(pc["pros"]) <= opt_score.STRENGTHS_CAP
    assert all(c["kind"] in {"subtopic", "term", "structure", "paa"} for c in pc["cons"])
    # the page says "how long" and "lasts a week", so the PAA question is answered
    assert not any(c["kind"] == "paa" for c in pc["cons"])
    # own SERP row is the checked page itself -> not evidence; no site scan -> low, not unknown
    assert pc["cannibalization"]["risk"] == "low" and pc["cannibalization"]["evidence"] == []
    assert pc["checked_at"]

    aid = doc["meta"]["analysis_id"]
    stored = opt_pipeline.get_analysis(BRAND["id"], aid)
    assert stored["page_check"]["verdict"] == pc["verdict"]
    row = opt_pipeline.list_analyses(BRAND["id"])[0]
    assert row["id"] == aid and row["verdict"] == pc["verdict"]["label"] and row["source_url"] == CHECKED_URL


def test_check_draft_path_derives_query_from_first_heading_and_flags_paa():
    doc = run_check(draft=DRAFT)
    pc = doc["page_check"]
    assert pc["source_url"] == ""
    assert pc["target_query"] == "my easy cold brew" and pc["target_query_source"] == "draft_heading"
    paa = [c for c in pc["cons"] if c["kind"] == "paa"]
    assert len(paa) == 1 and "How long does cold brew last?" in paa[0]["message"]
    assert "not answered" in paa[0]["message"]
    assert any(g["kind"] == "subtopic" for g in pc["cons"])   # gaps carried over as cons


def test_given_keyword_wins_over_title():
    doc = run_check(url=CHECKED_URL, keyword="cold brew ratio", fetch=fake_fetch())
    assert doc["page_check"]["target_query"] == "cold brew ratio"
    assert doc["page_check"]["target_query_source"] == "given"


def test_title_used_when_page_has_no_h1():
    html = page_html(title="Cold Brew Coffee Guide | CoffeeLab", h1="")
    doc = run_check(url=CHECKED_URL, fetch=fake_fetch(html))
    assert doc["page_check"]["target_query"] == "cold brew coffee guide"


def test_exactly_one_input_required():
    with pytest.raises(ValueError):
        run_check(url=CHECKED_URL, draft=DRAFT, fetch=fake_fetch())
    with pytest.raises(ValueError):
        run_check()


def test_draft_without_heading_needs_a_keyword():
    with pytest.raises(ValueError, match="keyword"):
        run_check(draft="just a paragraph with no heading at all")


def test_title_only_brand_needs_a_keyword():
    html = page_html(title="CoffeeLab", h1="")
    with pytest.raises(ValueError, match="keyword"):
        run_check(url=CHECKED_URL, fetch=fake_fetch(html))


def test_interstitial_page_is_an_honest_error():
    wall = "<html lang='en'><body>" + "<p>We value your privacy. Accept all cookies to continue reading.</p>" * 8 + "</body></html>"
    with pytest.raises(ValueError, match="wall"):
        run_check(url=CHECKED_URL, fetch=fake_fetch(wall))


def test_no_llm_anywhere(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("page check must not call a model")
    monkeypatch.setattr(sources, "llm_text", boom)
    monkeypatch.setattr(sources, "llm_json", boom)
    run_check(url=CHECKED_URL, fetch=fake_fetch())


# --------------------------------------------------------- target_query_from_title

@pytest.mark.parametrize("title,expected", [
    ("How to Make Cold Brew Coffee | CoffeeLab", "how to make cold brew coffee"),
    ("CoffeeLab: Cold Brew Guide", "cold brew guide"),
    ("Cold brew guide - www.mybrand.example.com", "cold brew guide"),
    ("Mybrand cold brew tips", "cold brew tips"),
    ("CoffeeLab", ""),
    ("", ""),
])
def test_target_query_from_title(title, expected):
    assert page_check.target_query_from_title(title, BRAND) == expected


# -------------------------------------------------------------- cannibal_overlap

def test_cannibal_overlap():
    assert page_check.cannibal_overlap(KEYWORD, KEYWORD) == 1.0
    assert page_check.cannibal_overlap("how to make cold brew", "make cold brew") == 1.0   # stopwords ignored
    assert page_check.cannibal_overlap("cold brew coffee", "cold brew maker") == pytest.approx(0.5)
    assert page_check.cannibal_overlap(KEYWORD, "cold brew coffee recipe") == pytest.approx(0.6)
    assert page_check.cannibal_overlap("cold brew", "espresso shots") == 0.0
    assert page_check.cannibal_overlap("", KEYWORD) == 0.0
    assert page_check.cannibal_overlap("the and of", KEYWORD) == 0.0


# ------------------------------------------------------------- cannibalization

def own(url: str, rank: int) -> dict:
    return {"rank": rank, "url": url, "title": "", "page_type": "article", "excluded": "own domain", "flags": []}


def test_serp_top10_is_high_and_beyond_is_medium():
    other = "https://mybrand.example.com/guides/cold-brew-101"
    c = page_check.cannibalization(BRAND, KEYWORD, [own(other, 2)], CHECKED_URL)
    assert c["risk"] == "high" and c["evidence"][0]["kind"] == "serp" and "#2" in c["evidence"][0]["detail"]
    c = page_check.cannibalization(BRAND, KEYWORD, [own(other, 14)], CHECKED_URL)
    assert c["risk"] == "medium" and c["evidence"][0]["url"] == other


def test_checked_url_is_never_its_own_evidence():
    # trailing slash / www. / query string are the same page
    rows = [own("https://www.mybrand.example.com/blog/cold-brew/?utm=x", 1)]
    c = page_check.cannibalization(BRAND, KEYWORD, rows, CHECKED_URL)
    assert c["evidence"] == [] and c["risk"] == "low"
    # a draft has no URL: the same row is evidence
    c = page_check.cannibalization(BRAND, KEYWORD, rows, "")
    assert c["risk"] == "high"


def test_corpus_overlap_branches():
    state.save(f"corpus-{BRAND['id']}", {"pages": [
        {"url": CHECKED_URL, "title": "Cold brew", "target_query": KEYWORD},               # self: excluded
        {"url": "https://mybrand.example.com/pricing", "title": "Pricing", "target_query": "coffee subscription cost"},
        {"url": "https://mybrand.example.com/recipes/cold-brew", "title": "Cold brew coffee recipe", "target_query": ""},
    ]})
    c = page_check.cannibalization(BRAND, KEYWORD, [], CHECKED_URL)
    assert c["risk"] == "medium"
    assert [e["url"] for e in c["evidence"]] == ["https://mybrand.example.com/recipes/cold-brew"]
    assert c["evidence"][0]["kind"] == "corpus" and "60%" in c["evidence"][0]["detail"]

    state.save(f"corpus-{BRAND['id']}", {"pages": [
        {"url": "https://mybrand.example.com/guides/cold-brew", "title": "Guide", "target_query": "make cold brew coffee"},
    ]})
    c = page_check.cannibalization(BRAND, KEYWORD, [], CHECKED_URL)
    assert c["risk"] == "high" and "100%" in c["evidence"][0]["detail"]


def test_corpus_without_overlap_is_low_with_note():
    state.save(f"corpus-{BRAND['id']}", {"pages": [
        {"url": "https://mybrand.example.com/pricing", "title": "Pricing", "target_query": "coffee subscription cost"},
    ]})
    c = page_check.cannibalization(BRAND, KEYWORD, [], CHECKED_URL)
    assert c["risk"] == "low" and "No other page" in c["note"]


def test_gsc_best_query_overlap_is_high_and_path_only_rows_match_self():
    state.save(f"pages-{BRAND['id']}", {"pages": [
        {"path": "/blog/cold-brew", "url": None, "best_query": KEYWORD, "clicks": 40, "position": 3.0},   # self
        {"path": "/blog/cold-brew-2", "url": None, "best_query": "make cold brew coffee", "clicks": 12, "position": 4.2},
        {"path": "/pricing", "url": None, "best_query": "coffee subscription cost", "clicks": 5, "position": 9.0},
    ]})
    c = page_check.cannibalization(BRAND, KEYWORD, [], CHECKED_URL)
    assert c["risk"] == "high"
    assert [e["url"] for e in c["evidence"]] == ["/blog/cold-brew-2"]
    assert c["evidence"][0]["kind"] == "gsc" and "position 4.2" in c["evidence"][0]["detail"]


def test_unknown_when_site_analysis_never_ran():
    c = page_check.cannibalization(BRAND, KEYWORD, [], CHECKED_URL)
    assert c["risk"] == "unknown" and "site analysis" in c["note"].lower()


def test_check_end_to_end_cannibalizes_on_top10_own_row():
    provider = own_row_provider("https://mybrand.example.com/guides/cold-brew-101", 3)
    doc = run_check(url=CHECKED_URL, fetch=fake_fetch(), provider=provider)
    pc = doc["page_check"]
    assert pc["verdict"]["label"] == "likely cannibalizes"
    assert pc["cannibalization"]["risk"] == "high"
    assert any("cannibalization risk high" in r for r in pc["verdict"]["reasons"])


# ------------------------------------------------------------------- verdict

GOOD = {"total": 80, "winners_median": 70, "gaps": [{"kind": "term"}] * 2, "degraded": []}
BANDS = {"word_count": StructureBand(feature="word_count", n=9, lo=1400, hi=2340, median=1775)}
META = {"n_docs": 9, "warnings": [], "degraded": [], "volatility": "stable"}
NO_CANNIBAL = {"risk": "low", "evidence": [], "note": ""}


def test_verdict_rules():
    v = page_check.verdict(GOOD, NO_CANNIBAL, META, BANDS)
    assert v["label"] == "likely helps" and v["confidence"] == "high"
    assert "score 80 vs winners' median 70" in v["reasons"] and "2 gap(s) against the winners' profile" in v["reasons"]

    too_many = {**GOOD, "gaps": [{"kind": "term"}] * 4}
    assert page_check.verdict(too_many, NO_CANNIBAL, META, BANDS)["label"] == "needs work"
    below = {**GOOD, "total": 60}
    assert page_check.verdict(below, NO_CANNIBAL, META, BANDS)["label"] == "needs work"

    high = {"risk": "high", "evidence": [{"kind": "serp"}], "note": ""}
    v = page_check.verdict(GOOD, high, META, BANDS)
    assert v["label"] == "likely cannibalizes" and any("1 page(s)" in r for r in v["reasons"])

    v = page_check.verdict(None, NO_CANNIBAL, META, BANDS)
    assert v["label"] == "cannot tell" and v["confidence"] == "low"
    v = page_check.verdict({**GOOD, "winners_median": None}, NO_CANNIBAL, {**META, "n_docs": 0}, {})
    assert v["label"] == "cannot tell"


def test_verdict_confidence_ladder():
    warned = {**META, "warnings": ["Only 3 usable winners — ranges are widened, treat as hints."]}
    v = page_check.verdict(GOOD, NO_CANNIBAL, warned, BANDS)
    assert v["confidence"] == "medium" and warned["warnings"][0] in v["reasons"]
    medium_band = {"word_count": StructureBand(feature="word_count", n=9, kind="modes", modes=[1, 3], confidence="medium")}
    assert page_check.verdict(GOOD, NO_CANNIBAL, META, medium_band)["confidence"] == "medium"
    assert page_check.verdict(GOOD, NO_CANNIBAL, {**META, "degraded": ["semantic_unavailable"]}, BANDS)["confidence"] == "low"
    assert page_check.verdict({**GOOD, "degraded": ["semantic_error: X"]}, NO_CANNIBAL, META, BANDS)["confidence"] == "low"
    assert page_check.verdict(GOOD, NO_CANNIBAL, META, BANDS, ["thin"])["confidence"] == "low"
    medium_cannibal = {"risk": "medium", "evidence": [{"kind": "corpus"}], "note": ""}
    v = page_check.verdict(GOOD, medium_cannibal, META, BANDS)
    assert v["label"] == "likely helps" and any("risk medium" in r for r in v["reasons"])


# ------------------------------------------------------------ paa_unanswered

def test_paa_unanswered_is_lexical_and_names_missing_words():
    qs = ["How long does cold brew last?", "What is cold brew coffee?"]
    out = page_check.paa_unanswered(qs, KEYWORD, DRAFT)
    assert len(out) == 1 and "last, long" in out[0]["message"]        # 2nd question has no words beyond the query
    assert page_check.paa_unanswered(qs, KEYWORD, DRAFT + " It lasts about a week.") == []   # lemma: lasts -> last


# ------------------------------------------------------------ brand isolation

def test_optimizer_index_and_lookups_are_brand_scoped():
    doc = opt_pipeline.analyze(KEYWORD, "en-US", DRAFT, brand_id=BRAND["id"],
                               provider=build_provider(), embedder=FakeEmbedder(), own_domain=BRAND["domain"])
    aid = doc["meta"]["analysis_id"]
    assert doc["meta"]["brand_id"] == BRAND["id"]
    assert [e["id"] for e in opt_pipeline.list_analyses(BRAND["id"])] == [aid]
    assert opt_pipeline.list_analyses(OTHER["id"]) == []

    with pytest.raises(LookupError) as exc:
        opt_pipeline.get_analysis(OTHER["id"], aid)
    assert not isinstance(exc.value, KeyError)          # KeyError is the router's "unknown vertical"
    with pytest.raises(LookupError):
        opt_pipeline.rescore(OTHER["id"], aid, DRAFT, embedder=FakeEmbedder())
    with pytest.raises(LookupError):
        opt_pipeline.attach_page_check(OTHER["id"], aid, {"verdict": {"label": "x"}, "source_url": ""})
    assert "page_check" not in opt_pipeline.get_analysis(BRAND["id"], aid)

    # volatility compares within a brand only
    other = opt_pipeline.analyze(KEYWORD, "en-US", "", brand_id=OTHER["id"],
                                 provider=build_provider(), embedder=FakeEmbedder())
    assert other["meta"]["volatility"] == "first-analysis"
    again = opt_pipeline.analyze(KEYWORD, "en-US", "", brand_id=BRAND["id"],
                                 provider=build_provider(), embedder=FakeEmbedder())
    assert again["meta"]["volatility"] == "stable"


def test_rescore_keeps_the_page_check_block():
    doc = run_check(url=CHECKED_URL, fetch=fake_fetch())
    aid = doc["meta"]["analysis_id"]
    opt_pipeline.rescore(BRAND["id"], aid, DRAFT, embedder=FakeEmbedder())
    stored = opt_pipeline.get_analysis(BRAND["id"], aid)
    assert stored["page_check"]["source_url"] == CHECKED_URL
    assert opt_pipeline.list_analyses(BRAND["id"])[0]["source_url"] == CHECKED_URL


# ----------------------------------------------------------- strengths_report

def test_strengths_report_mirrors_gap_report():
    entries = [
        opt_score.TermEntry(term="steep time", importance=0.9, draft_count=3, lo=2, hi=4),
        opt_score.TermEntry(term="ratio", importance=0.5, draft_count=6, lo=3, hi=4),
        opt_score.TermEntry(term="fridge", importance=0.4, draft_count=0, lo=1, hi=2),
        opt_score.TermEntry(term="starbucks", importance=5, draft_count=9, lo=2, hi=3, brand_optin=True),
    ]
    bands = {
        "word_count": StructureBand(feature="word_count", n=18, lo=1400, hi=2340, median=1775),
        "h2_count": StructureBand(feature="h2_count", n=18, lo=4, hi=8, median=6),
        "table_count": StructureBand(feature="table_count", n=18, kind="modes", modes=[0, 3]),
    }
    out = opt_score.strengths_report(
        [("storage", "Keep it in the fridge for a week.")], entries, bands,
        {"word_count": 1600, "h2_count": 2, "table_count": 3}, CFG.score,
    )
    assert [s["kind"] for s in out] == ["subtopic", "term", "term", "structure"]
    assert "storage" in out[0]["message"] and "fridge for a week" in out[0]["message"]
    assert out[1]["message"].startswith("'steep time' used 3x") and "inside" in out[1]["message"]
    assert out[2]["message"].startswith("'ratio' used 6x") and "extra repetitions" in out[2]["message"]
    assert out[3]["message"] == "word_count: 1600 is inside the winners' band 1400-2340"
    assert not any(s["message"].startswith(("'fridge'", "'starbucks'")) for s in out)

    many = [opt_score.TermEntry(term=f"t{i}", importance=1, draft_count=2, lo=1, hi=3) for i in range(20)]
    assert len(opt_score.strengths_report([], many, {}, {}, CFG.score)) == opt_score.STRENGTHS_CAP


def test_strengths_and_gaps_partition_the_terms():
    doc = opt_pipeline.analyze(KEYWORD, "en-US", DRAFT, brand_id=BRAND["id"],
                               provider=build_provider(), embedder=FakeEmbedder(), own_domain=BRAND["domain"])
    report = doc["last_report"]
    pro_terms = {s["message"].split("'")[1] for s in report["strengths"] if s["kind"] == "term"}
    con_terms = {g["message"].split("'")[1] for g in report["gaps"] if g["kind"] == "term"}
    assert pro_terms and con_terms and not (pro_terms & con_terms)


# ----------------------------------------------------------------- fetch_html

def _fake_getaddrinfo(host, port, *args, **kwargs):
    ip = {"public.example": "93.184.216.34", "internal.example": "10.0.0.1"}[host]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def _handler(request):
    if request.url.host == "internal.example":
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>secret</html>")
    path = request.url.path
    if path == "/redir":
        return httpx.Response(302, headers={"location": "http://internal.example/"})
    if path == "/hop":
        return httpx.Response(301, headers={"location": "/final"})
    if path == "/missing":
        return httpx.Response(404, text="nope")
    if path == "/pdf":
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          text="<html><body><p>hi there</p></body></html>")


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(state, "use_cloud", lambda: True)
    monkeypatch.setattr(sources.socket, "getaddrinfo", _fake_getaddrinfo)
    return httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)


def test_fetch_html_returns_final_url_and_body(online):
    final, html = sources.fetch_html("https://public.example/page", client=online)
    assert final == "https://public.example/page" and "<p>hi there</p>" in html
    final, _ = sources.fetch_html("https://public.example/hop", client=online)
    assert final == "https://public.example/final"


def test_fetch_html_rechecks_every_redirect_hop(online):
    with pytest.raises(ValueError, match="non-public"):
        sources.fetch_html("https://public.example/redir", client=online)


def test_fetch_html_refuses_non_http_and_bad_responses(online):
    with pytest.raises(ValueError, match="http"):
        sources.fetch_html("ftp://public.example/file", client=online)
    with pytest.raises(ValueError, match="HTTP 404"):
        sources.fetch_html("https://public.example/missing", client=online)
    with pytest.raises(ValueError, match="not an HTML page"):
        sources.fetch_html("https://public.example/pdf", client=online)
    with pytest.raises(ValueError, match="larger than"):
        sources.fetch_html("https://public.example/page", max_bytes=5, client=online)


def test_fetch_html_offline_is_credential_missing():
    with pytest.raises(CredentialMissing):
        sources.fetch_html("https://public.example/page")


def test_check_default_fetcher_is_the_guarded_one(monkeypatch):
    seen = []

    def guarded(url):
        seen.append(url)
        return url, page_html()
    monkeypatch.setattr(sources, "fetch_html", guarded)
    run_check(url=CHECKED_URL)
    assert seen == [CHECKED_URL]
