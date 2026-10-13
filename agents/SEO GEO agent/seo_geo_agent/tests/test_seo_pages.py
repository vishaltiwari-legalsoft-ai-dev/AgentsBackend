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
    assert (page["recommendation"], page["rec_chip"]) == ("", pages_mod.META_LONG_CHIP)


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
    assert (page["recommendation"], page["rec_source"], page["rec_chip"]) == (
        "", "flags", pages_mod.THIN_CHIP)                   # thin content: a chip too
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
        assert page["rec_chip"] is None                     # the flags ARE the chips


def test_rule_verdicts_that_repeat_down_the_table_are_chips_not_sentences():
    """'Healthy — keep it fresh…', 'Expand this page past 300 words', 'Tighten
    the meta description…' and 'Not in the site crawl…' were each the same
    sentence on every row they fit. Each is now a stable chip id, in the
    precedence the sentences had; no rule row carries a sentence any more."""
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page(url=f"https://x.com/ok{i}") for i in range(3)]
        + [_corpus_page(url=f"https://x.com/thin{i}", word_count=90 + i) for i in range(3)]
        + [_corpus_page(url="https://x.com/both", word_count=50, meta_description="d" * 200)]
        + [_corpus_page(url=f"https://x.com/long{i}", meta_description="d" * 200) for i in range(3)],
        ga_pages=[{"path": f"/ga-only{i}", "views": 5, "sessions": 4, "engagement_rate": 0.3}
                  for i in range(3)],
        gsc_rows=[])
    by_path = {p["path"]: p for p in doc["pages"]}
    verdict = {path: (p["recommendation"], p["rec_source"], p["rec_chip"]) for path, p in by_path.items()}

    for i in range(3):
        assert verdict[f"/ok{i}"] == ("", "flags", "healthy")
        assert verdict[f"/thin{i}"] == ("", "flags", "thin-content")
        assert by_path[f"/thin{i}"]["word_count"] == 90 + i       # what the chip can say
        assert verdict[f"/long{i}"] == ("", "flags", "meta-long")
        assert verdict[f"/ga-only{i}"] == ("", "flags", "not-crawled")
    assert verdict["/both"] == ("", "flags", "thin-content")      # thin keeps its precedence
    assert "meta-long" in by_path["/both"]["flags"]               # ...and meta-long is still a flag chip
    assert not [p["path"] for p in doc["pages"] if p["recommendation"]]
    assert {p["rec_chip"] for p in doc["pages"]} <= set(pages_mod.REC_CHIPS)


def test_an_ai_row_carries_no_chip(monkeypatch):
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: (
        '[{"path": "/thin", "recommendation": "Add the fee schedule PDF as an HTML table."}]'))
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        [_corpus_page(url="https://x.com/thin", word_count=90)], [], [])
    page = doc["pages"][0]
    assert (page["rec_source"], page["rec_chip"]) == ("ai", None)
    assert page["recommendation"].startswith("Add the fee schedule")


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


# ----------------------- legalsoft.com, as the crawler sees it -----------------------
# Cut down from the live Webflow HTML fetched 2026-10-14 (the staging test's
# ground truth). Every page shares the chrome: a nav that links every practice
# area, a footer with the newsletter w-form, the click-to-call link and the
# office address. /contact-us embeds HubSpot the v2 way: a src on
# js.hsforms.net plus an inline ``hbspt.forms.create``.

LS = "https://www.legalsoft.com"
LS_NAV = ('<nav class="navbar w-nav"><a href="/" class="brand w-nav-brand">Legal Soft</a>'
          '<a href="/virtual-staffing">Virtual Staffing</a><a href="/virtual-paralegal">Paralegals</a>'
          '<a href="/practice-areas/personal-injury">Personal Injury</a>'
          '<a href="/practice-areas/family">Family Law</a>'
          '<a href="/practice-areas/bankruptcy">Bankruptcy</a>'
          '<a href="/bookkeeper">Bookkeepers</a><a href="/pricing">Pricing</a>'
          '<a href="https://app.legalsoft.com" class="button w-button">Login</a>'
          '<a href="/contact-us" class="button w-button">Get Started</a></nav>')
LS_FOOTER = ('<footer class="footer"><div class="form-block w-form">'
             '<form id="wf-form-Newsletter-Sign-Up-Form" name="wf-form-Newsletter-Sign-Up-Form" '
             'data-name="Newsletter Sign Up Form" method="get" class="main-form">'
             '<input class="text-field w-input" name="Email" type="email">'
             '<input type="submit" class="button w-button" value="Subscribe"></form>'
             '<div class="success-message w-form-done"><div>Thank you!</div></div>'
             '<div class="error-message w-form-fail"><div>Oops!</div></div></div>'
             '<a href="tel:+1(424)341-4917" class="social-media-icon w-inline-block">'
             '<div class="call-icon w-embed"><svg><title>Call</title></svg></div></a>'
             '<div class="footer-address">21731 Ventura Blvd. Suite 100 Woodland Hills, CA 91364</div>'
             '</footer>')
LS_HUBSPOT_FORM = ('<div class="meeting_embed w-embed w-script"><div style="height: 25rem; width: 100%;"> '
                   '<script charset="utf-8" type="text/javascript" src="//js.hsforms.net/forms/embed/v2.js">'
                   '</script> <script>   hbspt.forms.create({     region: "na1",     portalId: "5728668",'
                   '     formId: "9ece68fa-b2f0-42ca-92af-6ed528dd9562"   }); </script>  <style> .hbspt-form,'
                   ' .hbspt-form form { width: 100% !important; } </style> </div></div>')
LS_ANSWER = ("You can outsource a wide range of tasks to your virtual paralegal, just as you would with an "
             "in-house team member in a remote setting. As a direct extension of your team, they work "
             "under your supervision.")
#: The real FAQ block (/virtual-paralegal): h3 questions, each followed by its answer.
LS_FAQ = "".join(
    f'<div class="faq-wrap main"><div class="faq-question-wrap"><h3 class="faq-q-text">{q}</h3>'
    f'<div class="faq-plus-wrap"><div class="faq-plus"></div></div></div><div class="faq-answer-block">'
    f'<p class="faq-answer-text">{LS_ANSWER}</p></div></div>'
    for q in ("What tasks can I outsource to my virtual paralegal?",
              "How do virtual paralegal services work?",
              "Are my client data and legal documents secure? "))
#: /blog: every post is a card — an <a> wrapping its question-titled <h2>.
LS_BLOG_CARDS = "".join(
    f'<a href="/blog/{slug}" class="blog-card w-inline-block"><img src="/c.png" alt="{title}">'
    f'<div class="text-size-small">October 12, 2026</div><h2 class="text-size-medium">{title}</h2>'
    f'<div class="author-name">Grace</div><p>{LS_ANSWER}</p></a>'
    for slug, title in (
        ("should-i-start-my-own-law-firm", "Should I Start My Own Law Firm or Keep Working for One?"),
        ("virtual-vs-in-house", "Virtual vs. In-House Legal Assistant: Which Is Better for Law Firms?"),
        ("how-many-hours-do-lawyers-work", "How Many Hours Do Lawyers Work?"),
        ("side-hustles-for-lawyers", "What Are the Best Side Hustles for Lawyers?")))
#: /testimonials: marketing hooks phrased as questions, each over a 29-38 word story.
LS_TESTIMONIAL_HOOKS = "".join(
    f'<div class="heading-texts-wrapper"><h2 class="heading-style-h2">{hook}</h2>'
    f'<div class="text-size-big">{story}</div></div>'
    f'<a href="#" class="yt-opener-btn w-inline-block w-lightbox">Watch</a>'
    for hook, story in (
        ('Growing in a <span class="text-color-gradient">crowded market?</span>',
         "Joseph was in the same position, launching a personal injury practice in one of the most "
         "competitive markets in the country. With Legal Soft, he got the support he needed to get "
         "his firm up and running."),
        ('<span class="text-color-gradient">Missing calls</span> from leads?',
         "Emma was too. Every missed call was a missed lead walking out the door. With a dedicated "
         "virtual intake team, she now answers every call consistently, with dependable coverage."),
        ('<span class="text-color-gradient">Buried </span>in case work?',
         "Justin needed support that understood personal injury. His Legal Soft virtual assistants "
         "came in familiar with the field and stayed on top of the details that keep cases moving.")))
#: /partners/referral-program: two CTA questions over a bullet list and a button.
LS_REFERRAL_CTAS = ('<h2 class="heading-style-h2">Why Join the <br/><span>Referral Program?</span></h2>'
                    '<ul><li>Simple, effortless referral process</li><li>Generous cash rewards</li></ul>'
                    '<a href="https://share.hsforms.com/1iiTsXme5Q36XJfPlG75Zqg3es9o" class="button">Learn More</a>'
                    '<h2 class="heading-style-h2">Ready to Start Earning?</h2>'
                    '<a href="https://share.hsforms.com/1iiTsXme5Q36XJfPlG75Zqg3es9o" class="button">Refer Now</a>')


def ls_page(path: str, body: str, title: str = "Legal Soft",
            types=("WebSite", "Organization", "ContactPoint", "WebPage"), words: int | None = None) -> PageFacts:
    """One legalsoft.com page with the shared chrome, parsed by the real parser.
    ``words`` pins the word count a test's evidence quotes (the digest's "(920w)")."""
    ld = ('<script type="application/ld+json">{"@context": "https://schema.org", "@graph": ['
          + ", ".join(f'{{"@type": "{t}"}}' for t in types) + "]}</script>") if types else ""
    facts = parse_page(LS + path, f'<html><head><title>{title}</title>{ld}</head><body>{LS_NAV}'
                                  f'<main>{body}</main>{LS_FOOTER}</body></html>')
    if words is not None:
        facts.word_count = words
    return facts


def test_contact_us_facts_hold_the_hubspot_form_the_call_link_and_the_address():
    """D1: the review said /contact-us has "no visible contact form, phone
    number, or calendar link". The page has all of that but the calendar."""
    facts = ls_page("/contact-us", '<h1>Contact Legal Soft</h1>' + LS_HUBSPOT_FORM)
    assert facts.forms == ["hubspot", "newsletter"]   # the HubSpot embed is the contact form
    assert facts.tel_links == ["tel:+1(424)341-4917"]
    assert facts.booking_links == []
    assert facts.has_address and facts.address_text == "21731 Ventura Blvd. Suite 100 Woodland Hills, CA 91364"


@pytest.mark.parametrize(("markup", "kind"), [
    ('<iframe src="https://form.typeform.com/to/abc"></iframe>', "typeform"),
    ('<div data-tf-widget="abc123"></div>', "typeform"),
    ('<div class="calendly-inline-widget" data-url="https://calendly.com/acme/intro"></div>', "calendly"),
    ('<div class="meetings-iframe-container" data-src="https://meetings.hubspot.com/acme"></div>',
     "hubspot-meetings"),
    ('<iframe src="https://form.jotform.com/2123"></iframe>', "jotform"),
    ('<div class="wpforms-container"><div class="wpforms-form"></div></div>', "wpforms"),
    ('<div class="gform_wrapper"></div>', "gravity-forms"),
    ('<script src="https://acme.formstack.com/forms/js.php/contact"></script>', "formstack"),
    ('<iframe src="https://docs.google.com/forms/d/e/1FAIpQ/viewform?embedded=true"></iframe>', "google-forms"),
    ('<script src="https://www.cognitoforms.com/f/seamless.js" data-key="k"></script>', "cognito"),
    ('<div class="contact-block w-form"><div class="w-form-done">Thanks</div></div>', "form"),
    ('<form action="/contact" method="post"><input name="name"></form>', "form"),
    ('<form role="search"><input type="search" name="q"></form>', "search"),
    ('<form action="/"><input type="text" name="s"></form>', "search"),
])
def test_form_embeds_and_form_kinds_are_recognised(markup, kind):
    assert parse_page("https://x.com/a", f"<html><body>{markup}</body></html>").forms == [kind]


def test_link_facts_are_same_site_paths_calls_mails_and_bookings():
    facts = parse_page("https://www.legalsoft.com/virtual-paralegal", (
        '<a href="/practice-areas/personal-injury/">PI</a><a href="practice-areas/family?x=1#top">Fam</a>'
        '<a href="https://legalsoft.com/pricing">Pricing</a><a href="https://www.legalsoft.com/pricing/">P</a>'
        '<a href="https://linkedin.com/company/legalsoft">In</a><a href="#faq">FAQ</a>'
        '<a href="mailto:referrals@legalsoft.com">Mail</a><a href="tel:+1(424)341-4917">Call</a>'
        '<a href="/book-a-meeting-legal-soft-sales">Book</a><a href="/bookkeeper">Bookkeepers</a>'
        '<a href="https://calendly.com/legalsoft/intro">Calendly</a>'))
    assert facts.link_paths == ["/practice-areas/personal-injury", "/practice-areas/family", "/pricing",
                                "/book-a-meeting-legal-soft-sales", "/bookkeeper"]
    assert facts.mailto_links == ["mailto:referrals@legalsoft.com"]
    assert facts.tel_links == ["tel:+1(424)341-4917"]
    assert facts.booking_links == ["https://www.legalsoft.com/book-a-meeting-legal-soft-sales",
                                   "https://calendly.com/legalsoft/intro"]       # never /bookkeeper


def test_only_real_question_and_answer_pairs_count_toward_faq():
    """D2: /blog's question headings are post titles inside links; /testimonials'
    and the partner pages' are marketing hooks. Only a question a visitor asks,
    not a link, followed by a 25+ word answer, is a Q&A pair."""
    faq = ls_page("/virtual-paralegal", "<h1>Virtual Paralegals</h1>" + LS_FAQ)
    assert (faq.q_headings, faq.faq_pairs) == (3, 3)
    blog = ls_page("/blog", "<h1>Law Firm Success Strategies</h1>" + LS_BLOG_CARDS)
    assert blog.q_headings == 4 and blog.faq_pairs == 0
    assert blog.headings_linked * 2 > blog.headings_total               # a listing
    hooks = ls_page("/testimonials", "<h1>Client Success Stories</h1>" + LS_TESTIMONIAL_HOOKS)
    assert hooks.q_headings == 3 and hooks.faq_pairs == 0
    ctas = ls_page("/partners/referral-program", "<h1>Referral Program</h1>" + LS_REFERRAL_CTAS)
    assert ctas.q_headings == 2 and ctas.faq_pairs == 0


def test_a_phone_alone_is_not_a_street_address():
    phone_only = parse_page("https://x.com/", "<html><body><p>Call (555) 123-4567</p>"
                                              "<address>hello@x.com</address></body></html>")
    assert phone_only.has_phone and not phone_only.has_address and phone_only.address_text == ""


def test_corpus_entry_stores_the_facts_absence_claims_are_checked_against():
    facts = ls_page("/contact-us", '<h1>Contact Legal Soft</h1>' + LS_HUBSPOT_FORM)
    corpus = site_brain.build_corpus({"id": "b", "domain": "legalsoft.com"},
                                     fetch=lambda url, client=None: facts,
                                     sitemap=lambda domain, client=None: [LS + "/contact-us"])
    entry = corpus["pages"][0]
    assert entry["forms"] == ["hubspot", "newsletter"] and entry["tel_links"] == ["tel:+1(424)341-4917"]
    assert "/practice-areas/family" in entry["link_paths"] and entry["booking_links"] == []
    assert entry["address_text"].startswith("21731 Ventura Blvd.")
    assert {"faq_pairs", "headings_total", "headings_linked", "mailto_links"} <= set(entry)


def test_link_targets_fit_the_one_document_corpus_and_say_when_trimmed():
    """The corpus is one Firestore document (1 MiB). Fifty pages each carrying
    a 300-link mega-menu of long paths would not fit; the links share a budget,
    and a trimmed page can no longer prove "it never links to X"."""
    menu = "".join(f'<a href="/services/category-{i:03d}/a-rather-long-descriptive-service-slug">s</a>'
                   for i in range(320))
    pages = {f"https://x.com/p{i}": parse_page(f"https://x.com/p{i}",
                                               f"<html><head><title>P{i}</title></head><body>{menu}</body></html>")
             for i in range(50)}
    assert all(f.link_paths_capped and len(f.link_paths) == 300 for f in pages.values())
    corpus = site_brain.build_corpus({"id": "b", "domain": "x.com"}, fetch=lambda u, c=None: pages[u],
                                     sitemap=lambda d: list(pages))
    total = sum(len(p) + 3 for e in corpus["pages"] for p in e["link_paths"])
    assert total <= site_brain.LINK_PATHS_BUDGET and all(e["link_paths_capped"] for e in corpus["pages"])
    entry = corpus["pages"][0]
    crawled = {"x.com/p0": entry}
    issue = {"insight": "The page has no links to /about", "evidence": "https://x.com/p0"}
    assert site_brain.absence_problem(issue, [entry], crawled)[0] == "unverifiable-absence"
    linked = {"insight": "No links to /services/category-000/a-rather-long-descriptive-service-slug",
              "evidence": "https://x.com/p0"}
    assert site_brain.absence_problem(linked, [entry], crawled)[0] == "contradicted-by-page"


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
        _corpus_page(url="https://x.com/help", title="Help", schema_types=[], q_headings=3, faq_pairs=3),
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
    assert doc["pages"][0]["rec_source"] != "ai"
    assert doc["grounding"]["dropped"][0]["reason"] == "schema-not-fit"


# ------------------- D3: categorised advice, one place per move -------------------
# The AI lines below are staging's, word for word (2026-10-14 browser test).

def _ai(monkeypatch, lines: dict[str, str]) -> None:
    import json
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: json.dumps(
        [{"path": path, "recommendation": rec} for path, rec in lines.items()]))


STAGING_TITLE_LINES = {
    "/virtual-paralegal": "Shorten the title tag to 'Virtual Paralegal Services for Law Firms | Legal Soft' "
                          "to stay within 60-character limit.",
    "/case-manager": "Reduce title tag length to 'Remote Legal Case Manager Services | Legal Soft' to prevent "
                     "truncation in search results.",
    "/remote-attorneys": "Condense title to 'Remote Attorneys for Law Firms | Legal Soft' to improve search "
                         "result appearance.",
}
STAGING_LINK_LINES = {
    "/careers": "Implement internal links from the homepage and about page to increase visibility and drive "
                "traffic to this high-performing page.",
    "/legal-assistant": "Build internal links from the homepage and virtual-staffing page to boost this "
                        "service page's authority and traffic.",
    "/intake-specialist-remote": "Create targeted blog content about intake processes that links to this page "
                                 "to increase organic traffic.",
}
STAGING_CTA_LINES = {
    "/": "Add clear call-to-action buttons above the fold to convert the 6,953 views into measurable clicks "
         "and engagement.",
    "/contact-us": "Place prominent contact form links on all service pages to convert the existing traffic "
                   "into leads.",
}


def test_title_advice_on_a_row_whose_chip_says_title_too_long_is_dropped(monkeypatch):
    """Seven rows on staging said "Shorten title to '…'" beside a "Title too
    long" chip; the new title's words got past the similarity check. The line
    is title advice on a title-flagged row — categorised, it is the chip again."""
    _ai(monkeypatch, {**STAGING_TITLE_LINES,
                      "/demand-writer": "Add sample demand letters and before/after examples to differentiate "
                                        "content and improve time-on-page metrics."})
    corpus = [_corpus_page(url=f"https://x.com{p}", title="T" * 66) for p in STAGING_TITLE_LINES]
    corpus.append(_corpus_page(url="https://x.com/demand-writer", title="Virtual Demand Writers"))
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, corpus, [], [])
    by_path = {p["path"]: p for p in doc["pages"]}
    for path in STAGING_TITLE_LINES:
        assert by_path[path]["ai"] is False and "title-long" in by_path[path]["flags"]
    reasons = {d["path"]: d["reason"] for d in doc["grounding"]["dropped"]}
    assert reasons == {p: "repeats-flags" for p in STAGING_TITLE_LINES}
    keep = by_path["/demand-writer"]                      # page-specific content advice stays
    assert (keep["rec_source"], keep["rec_category"]) == ("ai", "content")


def test_a_suggested_title_over_60_or_meta_over_160_characters_is_dropped(monkeypatch):
    """/bookkeeper's suggested title was 61 characters — still too long."""
    meta = "M" * 161
    _ai(monkeypatch, {
        "/bookkeeper": "Shorten title to 'Legal Bookkeeping Services | Virtual Bookkeepers | Legal Soft' "
                       "to optimize for search display.",
        "/pricing": f'Rewrite the meta description as "{meta}" to sell the click.',
        "/team": "Lead the title with 'Legal VA team' so the query comes first.",
    })
    corpus = [_corpus_page(url=f"https://x.com{p}", title="Short title") for p in ("/bookkeeper", "/pricing", "/team")]
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"}, corpus, [], [])
    dropped = {d["path"]: d for d in doc["grounding"]["dropped"]}
    assert dropped["/bookkeeper"]["reason"] == "suggestion-too-long"
    assert dropped["/bookkeeper"]["detail"].startswith("the suggested title is 61 characters")
    assert dropped["/pricing"]["reason"] == "suggestion-too-long"
    team = next(p for p in doc["pages"] if p["path"] == "/team")   # 13 characters, no chip: kept
    assert (team["rec_source"], team["rec_category"]) == ("ai", "title")


def test_hygiene_advice_the_audit_already_owns_is_dropped_without_a_chip(monkeypatch):
    _ai(monkeypatch, {"/team": "Give the page a unique title tag naming the paralegal team."})
    report = {"issues": [{"issue": "Duplicate titles", "severity": "medium", "pages": ["https://x.com/team"],
                          "fix": "f", "count": 1}]}
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     [_corpus_page(url="https://x.com/team", title="Team")], [], [],
                                     audit_report=report)
    assert doc["pages"][0]["ai"] is False
    assert doc["grounding"]["dropped"][0]["reason"] == "repeats-flags"
    assert "technical audit" in doc["grounding"]["dropped"][0]["detail"]


def test_internal_linking_on_three_rows_moves_to_one_fix_list_item(monkeypatch):
    """Internal linking was on six rows plus Fix #6. Three or more rows giving
    the same site-wide move collapse into ONE Fix-list item naming the pages."""
    _ai(monkeypatch, {**STAGING_LINK_LINES, **STAGING_CTA_LINES})
    paths = list(STAGING_LINK_LINES) + list(STAGING_CTA_LINES)
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        [_corpus_page(url=f"https://x.com{p}", title="T") for p in paths],
        [{"path": p, "views": 100 - i, "sessions": 1, "engagement_rate": 0.5} for i, p in enumerate(paths)], [])
    by_path = {p["path"]: p for p in doc["pages"]}
    assert all(by_path[p]["ai"] is False for p in STAGING_LINK_LINES)          # off the rows
    assert doc["fix_items"] == [{"category": "internal-links", "paths": list(STAGING_LINK_LINES),
                                 "todo_id": None}]
    moved = [d for d in doc["grounding"]["dropped"] if d["reason"] == "moved-to-fix-list"]
    assert sorted(d["path"] for d in moved) == sorted(STAGING_LINK_LINES)
    # two CTA rows and no CTA to-do on the Fix list: they stay where they are
    assert all(by_path[p]["rec_category"] == "cta" for p in STAGING_CTA_LINES)


def test_cta_advice_joins_the_site_todo_that_already_says_it(monkeypatch):
    _ai(monkeypatch, STAGING_CTA_LINES)
    site = [{"id": "t5", "kind": "site", "category": "conversion", "page": "https://x.com/contact-us",
             "action": "Embed a 3-field form above the fold, add a click-to-call number, and link to a Calendly",
             "why": "Contact page dead-ends"}]
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        [_corpus_page(url=f"https://x.com{p}", title="T") for p in STAGING_CTA_LINES], [], [], site_todos=site)
    assert doc["fix_items"] == [{"category": "cta", "paths": ["/", "/contact-us"], "todo_id": "t5"}]
    assert not [p for p in doc["pages"] if p["ai"]]
    folded = site_brain.fold_page_advice("b", site, doc["fix_items"])
    assert folded[0]["pages"] == ["/contact-us", "/"]          # its own page first, once


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
    assert (p["recommendation"], p["rec_chip"]) == ("", pages_mod.NOT_CRAWLED_CHIP)   # not "healthy"
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
    assert (beyond["recommendation"], beyond["rec_chip"]) == ("", pages_mod.HEALTHY_CHIP)
    assert beyond["ai"] is False


def test_ai_recs_malformed_json_falls_back_to_heuristics(monkeypatch):
    monkeypatch.setattr(pages_mod, "llm_text", lambda system, prompt, **kw: "not json at all")
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     corpus_pages=[_corpus_page()], ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False
    assert pages_mod.NO_REC_NOTE in doc["notes"]
    # every row still says something: a sentence, an advice chip, or its flag chips
    assert all(p["recommendation"] or p["rec_chip"] or p["flags"] for p in doc["pages"])
