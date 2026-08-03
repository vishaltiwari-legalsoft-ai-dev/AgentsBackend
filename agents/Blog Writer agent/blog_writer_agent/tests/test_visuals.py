"""Visual plan: the agent decides count/type/theme — never a fabricated fallback."""
from __future__ import annotations

import pytest

from blog_writer_agent import drafting, research, visuals

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com"}

_PLAN = {
    "visuals": [
        {"section": "(top)", "type": "hero", "theme": "calm navy office scene",
         "prompt": "Wide hero image of a calm law-office reception...", "rationale": "sets tone"},
        {"section": "What firms actually lose", "type": "chart", "theme": "brand blues",
         "prompt": "Bar chart: 40% of calls missed vs answered...", "rationale": "the key stat"},
    ]
}


def _drafted_run():
    run = research.new_run(BRAND, "virtual receptionists for law firms")
    run["ledger"] = [{"id": "ev-1", "claim": "c", "quote": "q", "url": "https://s.example/a",
                      "source_name": "S", "source_class": "studies", "date": "", "credibility": "r"}]
    research.save_run(run)
    return drafting.build_draft(run, None, llm=lambda s, p, **kw: {
        "meta": {"title": "T", "description": "d", "slug": "t"},
        "blocks": [{"kind": "intro", "heading": "", "text": "x", "cites": ["ev-1"]}],
        "internal_links": [],
    })


def test_plan_visuals_validates_numbers_and_stores():
    run = _drafted_run()
    run = visuals.plan_visuals(run, BRAND, llm=lambda s, p, **kw: _PLAN)
    items = run["visuals"]["items"]
    assert [v["n"] for v in items] == [1, 2]
    assert items[0]["type"] == "hero"
    assert research.load_run(run["id"])["visuals"] is not None


def test_plan_visuals_requires_draft():
    run = research.new_run(BRAND, "no draft yet")
    with pytest.raises(ValueError):
        visuals.plan_visuals(run, BRAND, llm=lambda s, p, **kw: _PLAN)


def test_empty_or_malformed_plan_raises_instead_of_fabricating():
    run = _drafted_run()
    with pytest.raises(ValueError):
        visuals.plan_visuals(run, BRAND, llm=lambda s, p, **kw: {"visuals": []})
    with pytest.raises(ValueError):
        visuals.plan_visuals(run, BRAND, llm=lambda s, p, **kw: {"visuals": [{"type": "hero", "prompt": ""}]})
