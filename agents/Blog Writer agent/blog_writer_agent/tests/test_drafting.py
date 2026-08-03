"""Drafting: ledger-grounded blocks; per-block comments rewrite or re-research."""
from __future__ import annotations

import pytest

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
