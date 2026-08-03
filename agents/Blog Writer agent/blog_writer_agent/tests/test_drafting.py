"""Drafting: ledger-grounded blocks, the house guideline pass, block revision."""
from __future__ import annotations

import pytest
from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import drafting, research

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com"}

INVENTORY = {
    "domain": "legalsoft.com",
    "posts": [{"url": "https://legalsoft.com/blog/existing-post/", "title": "Existing Post"}],
    "counts": {}, "notes": [],
}


def _run_with_ledger():
    run = research.new_run(BRAND, "virtual receptionists for law firms")
    run["ledger"] = [
        {"id": "ev-1", "claim": "Firms miss 40% of calls", "quote": "q", "url": "https://s.example/a",
         "source_name": "Study", "source_class": "studies", "date": "", "credibility": "report"},
        {"id": "ev-2", "claim": "Reddit users report faster intake", "quote": "q2", "url": "https://s.example/b",
         "source_name": "Reddit", "source_class": "anecdotes", "date": "", "credibility": "forum anecdote"},
    ]
    research.save_run(run)
    return run


_DRAFT_PAYLOAD = {
    "meta": {"title": "The Real Cost of Missed Calls", "description": "desc", "slug": "missed-calls"},
    "blocks": [
        {"kind": "intro", "heading": "", "text": "Opening grounded in data.", "cites": ["ev-1"]},
        {"kind": "section", "heading": "What firms actually lose", "text": "Body text.", "cites": ["ev-1", "ev-99"]},
        {"kind": "conclusion", "heading": "Where to go next", "text": "Close.", "cites": ["ev-2"]},
    ],
    "internal_links": [{"url": "https://legalsoft.com/blog/existing-post/", "title": "Existing Post"}],
}


def test_build_draft_filters_bogus_cites_and_notes_them():
    run = _run_with_ledger()
    prompts: list[str] = []

    def fake_llm(system, prompt, **kw):
        prompts.append(prompt)
        return _DRAFT_PAYLOAD

    run = drafting.build_draft(run, INVENTORY, llm=fake_llm)
    blocks = run["draft"]["blocks"]
    assert [b["id"] for b in blocks] == ["b1", "b2", "b3"]
    assert blocks[1]["cites"] == ["ev-1"]  # ev-99 dropped
    assert any("ev-99" in n for n in run["draft"]["notes"])
    assert all(b["history"] == [] for b in blocks)
    # Existing posts reached the prompt (overlap avoidance + internal links).
    assert "Existing Post" in prompts[0]
    assert research.load_run(run["id"])["draft"] is not None


def test_build_draft_requires_evidence():
    run = research.new_run(BRAND, "topic with no research")
    with pytest.raises(ValueError):
        drafting.build_draft(run, None, llm=lambda s, p, **kw: _DRAFT_PAYLOAD)


def test_revise_block_rewrite_path_keeps_history_and_skips_search():
    run = _run_with_ledger()
    run = drafting.build_draft(run, INVENTORY, llm=lambda s, p, **kw: _DRAFT_PAYLOAD)

    def fake_llm(system, prompt, **kw):
        if "classif" in system.lower():
            return {"action": "rewrite", "queries": []}
        return {"text": "Tighter opening.", "cites": ["ev-2"]}

    def no_search(q, client=None):
        raise AssertionError("rewrite path must not search")

    run = drafting.revise_block(run, "b1", "make it punchier", llm=fake_llm, search=no_search)
    b1 = run["draft"]["blocks"][0]
    assert b1["text"] == "Tighter opening."
    assert b1["cites"] == ["ev-2"]
    assert b1["history"] == ["Opening grounded in data."]
    assert b1["last_comment"] == "make it punchier"


def test_revise_block_research_path_banks_new_evidence():
    run = _run_with_ledger()
    run = drafting.build_draft(run, INVENTORY, llm=lambda s, p, **kw: _DRAFT_PAYLOAD)

    def fake_llm(system, prompt, **kw):
        if "classif" in system.lower():
            return {"action": "research", "queries": ["missed call statistics 2026"]}
        if "evidence" in system.lower():
            return {"evidence": [{
                "claim": "New stat", "quote": "vq", "url": "https://fresh.example/s",
                "source_name": "Fresh", "source_class": "studies", "date": "", "credibility": "study",
            }]}
        if "gap" in system.lower():
            return {"gaps": []}
        return {"text": "Rewritten with fresh stat.", "cites": ["ev-3"]}

    run = drafting.revise_block(
        run, "b2", "back this with real numbers",
        llm=fake_llm,
        search=lambda q, client=None: {"organic": [{"link": "https://fresh.example/s"}]},
        fetch=lambda u, client=None: {"url": u, "title": "t", "text": "body", "status": 200},
    )
    assert any(i["id"] == "ev-3" for i in run["ledger"])
    b2 = run["draft"]["blocks"][1]
    assert b2["text"] == "Rewritten with fresh stat."
    assert b2["cites"] == ["ev-3"]


def test_revise_unknown_block_raises_keyerror():
    run = _run_with_ledger()
    run = drafting.build_draft(run, INVENTORY, llm=lambda s, p, **kw: _DRAFT_PAYLOAD)
    with pytest.raises(KeyError):
        drafting.revise_block(run, "b99", "hi", llm=lambda s, p, **kw: {"action": "rewrite"})


# ------------------------------------------------------------ guideline pass


def test_guideline_pass_rewrites_blocks_and_keeps_history():
    run = _run_with_ledger()

    def fake_llm(system, prompt, **kw):
        s = system.lower()
        if "editorial writer" in s:
            return _DRAFT_PAYLOAD
        if "house line editor" in s:
            assert "HOUSE WRITING RULES" in prompt and "BLUF" in prompt
            return {
                "meta": {"title": "Polished Title"},
                "blocks": [
                    {"id": "b1", "heading": "", "text": "Polished opening.", "cites": ["ev-1"]},
                    {"id": "b2", "heading": "What firms actually lose", "text": "Polished body.", "cites": ["ev-1"]},
                    {"id": "b3", "heading": "Where to go next", "text": "Polished close.", "cites": ["ev-2"]},
                ],
            }
        raise AssertionError(f"unexpected: {system[:40]}")

    run = drafting.build_draft(run, INVENTORY, llm=fake_llm)
    draft = run["draft"]
    assert draft["guidelines_applied"] is True
    assert draft["meta"]["title"] == "Polished Title"
    assert draft["meta"]["slug"] == "missed-calls"  # unedited meta keys survive
    b1 = draft["blocks"][0]
    assert b1["text"] == "Polished opening."
    assert b1["history"] == ["Opening grounded in data."]  # raw version kept


def test_style_violations_catches_hard_bans():
    dirty = "In today's world we delve into synergy — furthermore, it's a game-changer."
    found = drafting.style_violations(dirty)
    assert 'banned word "delve"' in found
    assert "em dash" in found
    assert any("in today's" in v for v in found)
    assert any("furthermore" in v for v in found)
    assert drafting.style_violations("Firms miss 40% of calls. Fix the intake first.") == []


def test_polish_failure_keeps_raw_draft_and_retry_only_polishes():
    run = _run_with_ledger()
    calls = {"draft": 0}

    def failing_polish(system, prompt, **kw):
        s = system.lower()
        if "editorial writer" in s:
            calls["draft"] += 1
            return _DRAFT_PAYLOAD
        raise CredentialMissing("LLM unavailable: boom")

    with pytest.raises(CredentialMissing):
        drafting.build_draft(run, INVENTORY, llm=failing_polish)
    run = research.load_run(run["id"])
    assert run["draft"] is not None and run["draft"]["guidelines_applied"] is False
    assert calls["draft"] == 1

    def working_polish(system, prompt, **kw):
        s = system.lower()
        if "editorial writer" in s:
            calls["draft"] += 1
            return _DRAFT_PAYLOAD
        return {"blocks": [{"id": "b1", "text": "Polished after retry.", "cites": ["ev-1"]}]}

    run = drafting.build_draft(run, INVENTORY, llm=working_polish)
    assert calls["draft"] == 1  # no redraft — polish only
    assert run["draft"]["guidelines_applied"] is True
    assert run["draft"]["blocks"][0]["text"] == "Polished after retry."


def test_leftover_style_violations_are_noted_honestly():
    run = _run_with_ledger()
    fix_calls = {"n": 0}

    def fake_llm(system, prompt, **kw):
        s = system.lower()
        if "editorial writer" in s:
            return _DRAFT_PAYLOAD
        if "still break" in s:  # the fix loop keeps failing to fix it
            fix_calls["n"] += 1
            return {"blocks": []}
        return {"blocks": [{"id": "b1", "text": "We delve into intake here.", "cites": ["ev-1"]}]}

    run = drafting.build_draft(run, INVENTORY, llm=fake_llm)
    notes = run["draft"]["notes"]
    assert fix_calls["n"] == drafting.MAX_FIX_ROUNDS  # kept trying before giving up
    assert any("style check still flags" in n and "delve" in n for n in notes)


def test_fix_loop_runs_until_clean_without_leftover_note():
    run = _run_with_ledger()

    def fake_llm(system, prompt, **kw):
        s = system.lower()
        if "editorial writer" in s:
            return _DRAFT_PAYLOAD
        if "still break" in s:
            return {"blocks": [{"id": "b1", "text": "Intake data, stated plainly.", "cites": ["ev-1"]}]}
        return {"blocks": [{"id": "b1", "text": "We leverage synergy here.", "cites": ["ev-1"]}]}

    run = drafting.build_draft(run, INVENTORY, llm=fake_llm)
    assert run["draft"]["blocks"][0]["text"] == "Intake data, stated plainly."
    assert not any("style check still flags" in n for n in run["draft"]["notes"])


def test_style_scan_catches_reframes_and_analogy_setups():
    found = drafting.style_violations(
        "This isn't about speed. It's not about tools either. Think of it as a bridge between teams."
    )
    assert any(v.startswith("reframe") for v in found)
    assert any("think of it as" in v for v in found)


def test_revise_rewrite_goes_through_style_gate():
    run = _run_with_ledger()
    run = drafting.build_draft(run, INVENTORY, llm=lambda s, p, **kw: _DRAFT_PAYLOAD)

    def fake_llm(system, prompt, **kw):
        s = system.lower()
        if "classif" in s:
            return {"action": "rewrite", "queries": []}
        if "still break" in s:
            return {"blocks": [{"id": "b1", "text": "Plain rewrite, no hype.", "cites": ["ev-1"]}]}
        return {"text": "We unlock seamless synergy.", "cites": ["ev-1"]}

    run = drafting.revise_block(run, "b1", "tighten", llm=fake_llm)
    assert run["draft"]["blocks"][0]["text"] == "Plain rewrite, no hype."


def test_voice_profile_reaches_draft_and_polish_prompts():
    run = _run_with_ledger()
    voice_doc = {"profile": {"tone": "plainspoken operator voice", "summary": "s"}}
    prompts: list[str] = []

    def fake_llm(system, prompt, **kw):
        prompts.append(prompt)
        return _DRAFT_PAYLOAD

    drafting.build_draft(run, INVENTORY, voice=voice_doc, llm=fake_llm)
    assert all("plainspoken operator voice" in p for p in prompts[:2])
