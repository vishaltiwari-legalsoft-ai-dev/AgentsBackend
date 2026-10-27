"""qa.py — every check, both directions: it fires on the wrong shape and stays
quiet on the right one. The quiet half matters as much: a check that cries
wolf teaches reviewers to ignore the panel."""
from __future__ import annotations

import copy
from datetime import date

import pytest

from pr_writer_testkit import EVIDENCE, golden_press_release, load_card
from pr_writer_agent import content_types as cts
from pr_writer_agent import evidence as ev
from pr_writer_agent import qa, render

PR = cts.get("press_release")
TODAY = date(2026, 10, 27)
ALLOWED = ["https://legalsoft.com", "https://legalsoft.com/news"]
IDX = ev.index(EVIDENCE)


def run(draft, ct=PR, keyword="virtual records assistant"):
    return qa.run_qa(draft, ct, card=load_card(), evidence=EVIDENCE, primary_keyword=keyword,
                     allowed_urls=ALLOWED, today=TODAY)


def checks(report, severity=None):
    return {f.check for f in report.findings if severity is None or f.severity == severity}


def block(draft, kind):
    return next(b for b in draft["blocks"] if b["kind"] == kind)


# --------------------------------------------------------------------------- #
# The golden draft is clean — every other test is a single mutation of it
# --------------------------------------------------------------------------- #

def test_golden_press_release_passes_with_no_findings(golden):
    report = run(golden)
    assert report.findings == []
    assert report.passed
    assert report.counts["quotes_pending"] == 1
    assert report.counts["verify_open"] == 0
    assert 400 <= report.counts["body_words"] <= 600


# --------------------------------------------------------------------------- #
# Anatomy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("n,bad", [(69, True), (70, False), (100, False), (101, True)])
def test_headline_length_bounds(n, bad):
    found = qa.check_headline("x" * n, PR)
    assert bool(found) is bad
    if bad:
        assert found[0].severity == "error" and found[0].check == "headline_length"


@pytest.mark.parametrize("n,bad", [(119, True), (120, False), (160, False), (161, True)])
def test_subhead_length_bounds(n, bad):
    assert bool(qa.check_subhead("x" * n, PR)) is bad


def test_byline_has_no_headline_or_subhead_length_rule():
    tl = cts.get("thought_leadership")
    assert qa.check_headline("A short title", tl) == []
    assert qa.check_subhead("", tl) == []


def test_lede_over_35_words_is_an_error_and_under_25_a_warning(golden):
    long = copy.deepcopy(golden)
    block(long, "lede")["text"] = " ".join(["word"] * 36) + "."
    f = qa.check_lede(long, PR)
    assert f[0].severity == "error"

    short = copy.deepcopy(golden)
    block(short, "lede")["text"] = " ".join(["word"] * 20) + "."
    f = qa.check_lede(short, PR)
    assert f[0].severity == "warning"


def test_lede_word_count_ignores_cite_markers(golden):
    lede = block(golden, "lede")["text"]
    assert qa.word_count(lede) == qa.word_count(ev.strip_cites(lede))


def test_body_length_out_of_range_is_an_error(golden):
    block(golden, "detail")["text"] = "Short detail."
    report = run(golden)
    assert "body_length" in checks(report, "error")


@pytest.mark.parametrize("line,ok", [
    ("FOR IMMEDIATE RELEASE", True),
    ("EMBARGOED UNTIL 9 a.m. EST, Nov. 12, 2026", True),
    ("EMBARGOED UNTIL noon PST, March 3, 2027", True),
    ("EMBARGOED UNTIL 9am EST, November 12th 2026", False),
    ("For immediate release", False),
    ("EMBARGOED UNTIL [VERIFY: embargo time]", True),
])
def test_release_line_shapes(line, ok):
    assert (qa.check_release_line(line, PR) == []) is ok


@pytest.mark.parametrize("line,ok", [
    ("WOODLAND HILLS, Calif. — Nov. 12, 2026 —", True),
    ("LOS ANGELES — March 3, 2027 —", True),
    ("NEW YORK — Sept. 9, 2026 —", True),
    ("WOODLAND HILLS — Nov. 12, 2026 —", False),         # not a standalone city
    ("WOODLAND HILLS, CA — Nov. 12, 2026 —", False),     # postal, not AP
    ("WOODLAND HILLS, Calif. — November 12, 2026 —", False),
    ("Woodland Hills, Calif. - Nov. 12, 2026", False),
    ("WOODLAND HILLS, Calif. — [VERIFY: release date] —", True),  # counted as open VERIFY instead
])
def test_dateline_shapes(line, ok):
    assert (qa.check_dateline(line, PR) == []) is ok


def test_render_builds_ap_dateline_from_card():
    card = load_card()
    assert render.dateline(card, "2026-11-12") == "WOODLAND HILLS, Calif. — Nov. 12, 2026 —"
    assert render.dateline(card, "2027-03-03") == "WOODLAND HILLS, Calif. — March 3, 2027 —"
    assert "[VERIFY: release date]" in render.dateline(card, None)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #

def test_missing_required_part_and_wrong_kind(golden):
    golden["blocks"] = [b for b in golden["blocks"] if b["kind"] != "cta"]
    golden["blocks"].append({"id": "b9", "kind": "bio", "text": "About me.", "speaker_id": None})
    msgs = [f.message for f in qa.check_structure(golden, PR)]
    assert any("missing required part: cta" in m for m in msgs)
    assert any("'bio' does not belong" in m for m in msgs)


def test_quote_count_and_unknown_speaker(golden):
    block(golden, "quote")["speaker_id"] = "sp-9"
    found = qa.check_structure(golden, PR)
    assert any(f.check == "quote_speaker" for f in found)
    golden["blocks"] = [b for b in golden["blocks"] if b["kind"] != "quote"]
    assert any(f.check == "quote_count" for f in qa.check_structure(golden, PR))


def test_partnership_needs_a_quote_from_each_party(golden):
    pt = cts.get("partnership")
    golden["content_type"] = "partnership"
    golden["blocks"] = [
        {"id": "b1", "kind": "lede", "text": "x", "speaker_id": None},
        {"id": "b2", "kind": "partnership", "text": "x", "speaker_id": None},
        {"id": "b3", "kind": "customer_benefit", "text": "x", "speaker_id": None},
        {"id": "b4", "kind": "quote", "text": "x", "speaker_id": "sp-1"},
        {"id": "b5", "kind": "quote", "text": "y", "speaker_id": "sp-1"},
        {"id": "b6", "kind": "cta", "text": "See https://legalsoft.com", "speaker_id": None},
    ]
    assert any("each party" in f.message for f in qa.check_structure(golden, pt))
    golden["speakers"].append({"id": "sp-2", "name": "Dana Reyes", "title": "Chief Product Officer",
                               "organization": "Northwind Practice Cloud"})
    golden["blocks"][4]["speaker_id"] = "sp-2"
    assert not any("each party" in f.message for f in qa.check_structure(golden, pt))


def test_event_needs_time_zone_and_registration_url():
    et = cts.get("event")
    draft = {"blocks": [
        {"id": "b1", "kind": "lede", "text": "x"},
        {"id": "b2", "kind": "event_details", "text": "The webinar runs Dec. 9, 2026, at 1 p.m."},
        {"id": "b3", "kind": "speakers", "text": "x"},
        {"id": "b4", "kind": "registration", "text": "Register on our site."},
        {"id": "b5", "kind": "quote", "text": "x", "speaker_id": "sp-1"},
    ], "speakers": [{"id": "sp-1", "organization": "Legal Soft"}]}
    msgs = [f.message for f in qa.check_structure(draft, et)]
    assert any("time zone" in m for m in msgs)
    assert any("needs a URL" in m for m in msgs)
    draft["blocks"][1]["text"] += " EST"
    draft["blocks"][3]["text"] = "Register at https://legalsoft.com/webinars/pi-intake."
    msgs = [f.message for f in qa.check_structure(draft, et)]
    assert not any("time zone" in m or "needs a URL" in m for m in msgs)


# --------------------------------------------------------------------------- #
# Banned and restricted language
# --------------------------------------------------------------------------- #

def _lang(text, kind="detail"):
    draft = {"headline": "", "subhead": "", "blocks": [{"id": "b1", "kind": kind, "text": text}]}
    return qa.check_language(draft, IDX, ["world-class"])


@pytest.mark.parametrize("text,check", [
    ("A revolutionary platform for firms.", "language_hype"),
    ("It is a game-changing release.", "language_hype"),
    ("Cutting-edge tools for intake.", "language_hype"),
    ("Best-in-class support.", "language_hype"),
    ("The service guarantees faster settlements.", "language_legal"),
    ("Our staff help firms win cases.", "language_legal"),
    ("It ensures results for every firm.", "language_legal"),
    ("The method is clinically proven.", "language_healthcare"),
    ("The assistant diagnoses injuries.", "language_healthcare"),
    ("It cures backlog.", "language_healthcare"),
    ("A fully autonomous AI intake agent.", "language_ai"),
    ("The tool replaces paralegals at intake.", "language_ai"),
    ("It is 100% accurate.", "language_ai"),
    ("Drafts go out with no human review.", "language_ai"),
    ("A world-class team.", "language_brand_avoid"),
])
def test_restricted_language_is_an_error(text, check):
    found = _lang(text)
    assert any(f.check == check and f.severity == "error" for f in found), found


@pytest.mark.parametrize("text", [
    "Legal Soft is the leading provider of virtual staff.",
    "It is the first platform to combine intake and records.",
    "Legal Soft is the only firm that trains staff this way.",
    "Legal Soft is the No. 1 staffing partner.",
    "We offer the best support for PI firms.",
])
def test_superlative_without_third_party_cite_is_an_error(text):
    assert any(f.check == "language_superlative" for f in _lang(text))


def test_superlative_with_internal_cite_still_fails_but_third_party_cite_passes():
    assert any(f.check == "language_superlative" for f in _lang("It is the leading provider of staff [bc-1]."))
    assert not any(f.check == "language_superlative" for f in _lang("It is the leading provider of staff [ev-1]."))


@pytest.mark.parametrize("text", [
    "Results for the first quarter came in on plan.",
    "Firms follow best practices for confidentiality.",
    "The change is leading to faster intake.",
    "Staff work only when the firm assigns a matter.",
    "This is the first time the program is open to all firms.",
    "The assistants help firms prevent errors.",
])
def test_superlative_check_stays_quiet_on_ordinary_words(text):
    assert _lang(text) == []


def test_excited_to_announce_is_an_error_in_the_lede_and_a_warning_elsewhere():
    assert _lang("We are excited to announce a program.", kind="lede")[0].severity == "error"
    assert _lang("We are excited to announce a program.", kind="detail")[0].severity == "warning"


def test_language_inside_a_verify_placeholder_is_not_flagged():
    assert _lang("[VERIFY: is it the leading provider per the award body]") == []


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #

def test_uncited_number_is_an_error(golden):
    block(golden, "detail")["text"] += " The program serves 1,200 firms."
    assert "uncited_number" in checks(run(golden), "error")


@pytest.mark.parametrize("sentence", [
    "The program serves [VERIFY: firm count from ops] firms.",
    "The program serves 1,200 firms [br-1].",
    "Staff follow ABA Model Rules 5.3 when supervised.",
])
def test_cited_placeholder_or_rule_reference_numbers_pass(golden, sentence):
    block(golden, "detail")["text"] += " " + sentence
    assert "uncited_number" not in checks(run(golden))


def test_dollar_and_percent_need_cites(golden):
    block(golden, "detail")["text"] += " Plans cost $499 per month. Adoption rose 5%."
    found = [f for f in run(golden).findings if f.check == "uncited_number"]
    assert len(found) == 2


def test_unknown_citation_is_an_error(golden):
    block(golden, "detail")["text"] += " The program serves 1,200 firms [ev-99]."
    report = run(golden)
    assert "citation_unknown" in checks(report, "error")
    assert "uncited_number" in checks(report, "error")


def test_headline_number_must_be_backed_by_a_cited_body_sentence(golden):
    golden["headline"] = "Legal Soft Opens Virtual Records Assistant Program to 1,200 Personal Injury Firms Nationwide"
    assert "uncited_number" in checks(run(golden))
    block(golden, "detail")["text"] += " The program now serves 1,200 firms [br-1]."
    assert "uncited_number" not in checks(run(golden))


def test_cite_marker_after_the_period_stays_with_its_sentence():
    assert qa.sentences("Live Nov. 12, 2026. [br-2] Next one.") == ["Live Nov. 12, 2026 [br-2].", "Next one."]
    assert qa.sentences("Live Nov. 12, 2026 [br-2]. Next one.") == ["Live Nov. 12, 2026 [br-2].", "Next one."]


def test_sentence_split_respects_ap_abbreviations():
    s = qa.sentences("It opens Nov. 12 at 9 a.m. EST in Woodland Hills, Calif. The team is ready.")
    assert len(s) == 2


def test_url_not_from_brief_card_or_evidence_is_an_error(golden):
    block(golden, "cta")["text"] = "Learn more at https://evil.example.com/offer."
    assert "url_unsourced" in checks(run(golden), "error")


# --------------------------------------------------------------------------- #
# Quotes and boilerplate, checked on the rendered text
# --------------------------------------------------------------------------- #

def test_rendered_quote_has_a_pending_marker_naming_the_speaker(golden):
    md = render.render_markdown(golden, PR)
    assert "[QUOTE – PENDING APPROVAL: Hamid Kohan, Founder]" in md
    assert qa.check_quote_markers(md, PR) == []


def test_hand_edited_quote_without_marker_is_caught(golden):
    md = render.render_markdown(golden, PR).replace("[QUOTE – PENDING APPROVAL: Hamid Kohan, Founder]", "")
    assert qa.check_quote_markers(md, PR)[0].check == "quote_pending_marker"


def test_marker_for_the_wrong_speaker_is_caught(golden):
    md = render.render_markdown(golden, PR).replace("PENDING APPROVAL: Hamid Kohan", "PENDING APPROVAL: Jane Doe")
    assert "not the quoted speaker" in qa.check_quote_markers(md, PR)[0].message


def test_quote_in_a_body_paragraph_also_needs_a_marker(golden):
    block(golden, "detail")["text"] += ' "We built this for the firms that call us every day," said Hamid Kohan.'
    assert any(f.check == "quote_pending_marker" for f in run(golden).findings)


def test_partner_quote_marker_names_the_partner_organization():
    sp = {"name": "Dana Reyes", "title": "Chief Product Officer", "organization": "Northwind Practice Cloud"}
    assert render.quote_marker(sp, "Legal Soft") == (
        "[QUOTE – PENDING APPROVAL: Dana Reyes, Chief Product Officer, Northwind Practice Cloud]")


def test_boilerplate_drift_is_an_error(golden):
    golden["boilerplates"][0]["text"] = golden["boilerplates"][0]["text"].replace("growth company", "growth firm")
    md = render.render_markdown(golden, PR)
    expected = render.boilerplates(load_card(), None, 1)
    assert qa.check_boilerplate(md, expected)[0].check == "boilerplate"


def test_boilerplate_whitespace_differences_are_not_drift(golden):
    md = render.render_markdown(golden, PR).replace(". The company", ".  The company")
    assert qa.check_boilerplate(md, render.boilerplates(load_card(), None, 1)) == []


def test_missing_card_boilerplate_is_a_placeholder_and_an_error():
    card = load_card()
    card["boilerplate"] = {}
    bps = render.boilerplates(card, None, 1)
    assert "[VERIFY: approved boilerplate" in bps[0]["text"]
    assert qa.check_boilerplate("anything", bps)[0].severity == "error"


def test_old_boilerplate_approval_is_a_warning(golden):
    md = render.render_markdown(golden, PR)
    found = qa.check_boilerplate(md, golden["boilerplates"], approved_at="2024-01-01", today=TODAY)
    assert [f.check for f in found] == ["boilerplate_current"]


# --------------------------------------------------------------------------- #
# Light AP pass — fires on the wrong shape, quiet on the right one
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,check", [
    ("It opens November 12, 2026.", "ap_month"),
    ("It opens Apr. 3, 2027.", "ap_month"),
    ("It opens Sep. 9, 2026.", "ap_month"),
    ("It opens Nov. 12th.", "ap_ordinal"),
    ("Doors open at 12 p.m. EST.", "ap_time"),
    ("Doors open at 12 a.m. EST.", "ap_time"),
    ("Doors open at 9 AM EST.", "ap_time"),
    ("Doors open at 9:00 a.m. EST.", "ap_time"),
    ("Adoption rose 5 percent.", "ap_percent"),
    ("Adoption rose 5 %.", "ap_percent"),
    ("The firm opened 3 offices.", "ap_numbers"),
])
def test_ap_pass_flags(text, check):
    found = qa.ap_findings(text, "b1")
    assert [f.check for f in found] == [check]
    assert found[0].severity == "warning"


@pytest.mark.parametrize("text", [
    "It opens Nov. 12, 2026, at 9 a.m. EST.",
    "It opens March 3, 2027, at noon PST.",
    "It opens in November 2026.",
    "The 9th Circuit ruled on the appeal.",
    "Adoption rose 5% this year.",
    "Plans start at $4 million in coverage.",
    "The firm opened three offices and hired 12 staff.",
    "Read Section 7.1 of the policy.",
    "The 5 years old claim was closed.",
])
def test_ap_pass_stays_quiet_on_correct_or_ambiguous_text(text):
    assert qa.ap_findings(text, "b1") == []


# --------------------------------------------------------------------------- #
# Byline, read-aloud, keyword
# --------------------------------------------------------------------------- #

def _byline(body_text, bio_text):
    return {"content_type": "thought_leadership", "headline": "Intake is a revenue function",
            "blocks": [{"id": "b1", "kind": "thesis", "text": body_text},
                       {"id": "b2", "kind": "bio", "text": bio_text}]}


def test_byline_company_mentions_and_first_person():
    tl = cts.get("thought_leadership")
    ok = _byline("I have watched firms lose leads at the front desk.", "Hamid Kohan is the founder of Legal Soft.")
    assert qa.check_byline(ok, tl, ["Legal Soft"]) == []
    promo = _byline("I built Legal Soft to fix intake.", "Hamid Kohan is the founder of Legal Soft.")
    assert any(f.check == "byline_company" for f in qa.check_byline(promo, tl, ["Legal Soft"]))
    third = _byline("Firms lose leads at the front desk.", "Hamid Kohan is the founder of Legal Soft.")
    assert any(f.check == "byline_voice" for f in qa.check_byline(third, tl, ["Legal Soft"]))
    twice = _byline("I have seen it.", "Hamid Kohan founded Legal Soft. Legal Soft is based in California.")
    assert any("more than once in the bio" in f.message for f in qa.check_byline(twice, tl, ["Legal Soft"]))


def test_long_sentence_warning_exempts_the_lede(golden):
    block(golden, "lede")["text"] = " ".join(["word"] * 33) + "."
    assert not any(f.where == "b1" for f in qa.check_long_sentences(golden, PR))
    block(golden, "detail")["text"] += " " + " ".join(["word"] * 31) + "."
    found = [f for f in qa.check_long_sentences(golden, PR) if f.where == "b4"]
    assert found and found[0].severity == "warning"


def test_keyword_placement_and_density(golden):
    assert qa.check_keyword(golden, "virtual records assistant") == []
    found = {f.check for f in qa.check_keyword(golden, "legal intake")}
    assert found == {"keyword_placement"}
    block(golden, "detail")["text"] += " records assistant" * 30
    assert any(f.check == "keyword_density" for f in qa.check_keyword(golden, "records assistant"))


# --------------------------------------------------------------------------- #
# Package checks
# --------------------------------------------------------------------------- #

def test_seo_block_bounds():
    good = {"seo_title": "Legal Soft opens virtual records assistant program",
            "meta_description": "x" * 155, "slug": "legal-soft-records-assistants", "social_summary": "x" * 200}
    assert qa.check_seo_block(good) == []
    bad = {"seo_title": "x" * 70, "meta_description": "x" * 100, "slug": "Legal_Soft", "social_summary": "x" * 300}
    assert {f.check for f in qa.check_seo_block(bad)} == {"seo_title", "seo_meta", "seo_slug", "seo_social"}


def test_snippets_cannot_introduce_new_figures_or_hype(golden):
    md = render.render_markdown(golden, PR)
    assert qa.check_snippets({"social_summary": "62% of small firms plan to add remote staff."}, md) == []
    found = qa.check_snippets({"social_summary": "Now serving 1,200 firms with a revolutionary program."}, md)
    assert {f.check for f in found} == {"snippet_new_figure", "language_hype"}


def test_report_serializes(golden):
    d = run(golden).to_dict()
    assert d["passed"] is True and d["findings"] == [] and "counts" in d



# Live finding 2026-10-28: a placeholder speaker nests [VERIFY: …] inside the marker.
def test_pending_quote_marker_is_one_token_when_the_speaker_is_a_placeholder():
    marker = render.QUOTE_MARKER.format(who="[VERIFY: spokesperson name], [VERIFY: exact title]")
    m = qa.QUOTE_PENDING.search(marker)
    assert m is not None and m.group(0) == marker
    assert m.group(1) == "[VERIFY: spokesperson name], [VERIFY: exact title]"
    plain = render.QUOTE_MARKER.format(who="Hamid Kohan, Founder")
    assert qa.QUOTE_PENDING.search(plain).group(1) == "Hamid Kohan, Founder"


def test_the_brand_cards_spokesperson_speaks_when_the_brief_names_none():
    card = load_card() | {"spokespeople": [{"name": "Dana Whitfield", "title": "CEO",
                                            "approval_contact": "comms@legalsoft.com"}]}
    speakers, notes = ev.speakers({"spokespeople": {"value": [], "provenance": "missing"}}, card)
    assert [(s["name"], s["title"]) for s in speakers] == [("Dana Whitfield", "CEO")]
    assert speakers[0]["provenance"] == "brand_card" and "[VERIFY" not in speakers[0]["name"]


# --------------------------------------------------------------------------- #
# Live eval 2026-10-28: findings that failed accurate drafts
# --------------------------------------------------------------------------- #

def _body_words(draft):
    return sum(qa.word_count(b["text"]) for b in draft["blocks"] if b["kind"] != "bio")


def _trim_body_to(draft, target):
    """Cut the detail block until the body is ``target`` words (± a few)."""
    d = block(draft, "detail")
    words = d["text"].split()
    while _body_words(draft) > target and len(words) > 5:
        words = words[:-1]
        d["text"] = " ".join(words) + "."


def test_a_body_a_little_short_is_a_warning_not_an_error(golden):
    """An accurate 360-word release from a thin brief is short, not broken;
    an error there pushes the QA rewrite toward filler."""
    _trim_body_to(golden, 360)
    assert 300 <= _body_words(golden) < 400
    report = run(golden)
    assert "body_length" in checks(report, "warning")
    assert "body_length" not in checks(report, "error")


def test_a_body_far_short_or_too_long_is_still_an_error(golden):
    _trim_body_to(golden, 250)
    assert _body_words(golden) < 400 * qa.SHORT_BODY_ERROR_SHARE
    assert "body_length" in checks(run(golden), "error")


def test_a_figure_repeated_from_a_cited_sentence_is_grounded(golden):
    block(golden, "detail")["text"] += " The program serves 1,200 firms [br-1]. Those 1,200 firms span 30 states."
    found = [f for f in run(golden).findings if f.check == "uncited_number"]
    assert [f.excerpt for f in found] == ["Those 1,200 firms span 30 states."]  # 30 is new and uncited
    block(golden, "detail")["text"] = block(golden, "detail")["text"].replace(" span 30 states", " grew")
    assert "uncited_number" not in checks(run(golden))


def test_uncited_number_goes_to_the_qa_fix():
    assert "uncited_number" in qa.FIXABLE


@pytest.mark.parametrize("text,bad", [
    ("Interviews run Tuesday, Feb. 3, 2027 [br-1].", "Wednesday"),   # live eval: Feb. 3, 2027 is a Wednesday
    ("Interviews run Wednesday, Feb. 3, 2027 [br-1].", None),
    ("The summit opens Monday, Sept. 14, 2026 [br-1].", None),
    ("The summit opens Friday, Sept. 14, 2026 [br-1].", "Monday"),
    ("Doors open Feb. 3, 2027 [br-1].", None),                       # no weekday, nothing to check
])
def test_a_weekday_must_match_its_date(golden, text, bad):
    block(golden, "detail")["text"] += " " + text
    found = [f for f in run(golden).findings if f.check == "weekday_mismatch"]
    if bad is None:
        assert found == []
    else:
        assert len(found) == 1 and found[0].severity == "error" and bad in found[0].message
    assert "weekday_mismatch" in qa.FIXABLE
