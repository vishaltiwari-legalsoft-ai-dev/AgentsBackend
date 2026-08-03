"""Exports: md/html/txt render from the same blocks; visual docs render the plan."""
from __future__ import annotations

import pytest

from blog_writer_agent import export


def _run():
    return {
        "id": "bw-x", "topic": "virtual receptionists", "brand_name": "Legal Soft",
        "domain": "legalsoft.com",
        "ledger": [
            {"id": "ev-1", "claim": "Firms miss 40% of calls", "quote": "q",
             "url": "https://s.example/a", "source_name": "ABA Study",
             "source_class": "studies", "date": "2026", "credibility": "report"},
            {"id": "ev-2", "claim": "Faster intake reported", "quote": "q2",
             "url": "https://s.example/b", "source_name": "Reddit thread",
             "source_class": "anecdotes", "date": "", "credibility": "forum anecdote"},
        ],
        "draft": {
            "meta": {"title": "The Real Cost of Missed Calls", "description": "What 40% missed calls means", "slug": "missed-calls"},
            "blocks": [
                {"id": "b1", "kind": "intro", "heading": "", "text": "Every firm loses <calls> daily.", "cites": ["ev-1"], "history": []},
                {"id": "b2", "kind": "section", "heading": "What firms lose", "text": "The data is stark.", "cites": ["ev-1", "ev-2"], "history": []},
                {"id": "b3", "kind": "conclusion", "heading": "Next steps", "text": "Act on it.", "cites": [], "history": []},
            ],
            "internal_links": [], "notes": [],
        },
        "visuals": {"items": [
            {"n": 1, "section": "(top)", "type": "hero", "theme": "navy office", "prompt": "Wide hero of a reception desk", "rationale": "tone"},
            {"n": 2, "section": "What firms lose", "type": "chart", "theme": "brand blues", "prompt": "Bar chart 40% missed", "rationale": "key stat"},
        ], "notes": []},
    }


def test_markdown_has_title_sections_cites_and_sources():
    md = export.to_markdown(_run())
    assert md.startswith("# The Real Cost of Missed Calls")
    assert "## What firms lose" in md
    assert "[1]" in md and "[2]" in md
    assert "## Sources" in md
    assert "ABA Study — https://s.example/a" in md


def test_html_is_standalone_and_escapes():
    html = export.to_html(_run())
    assert html.startswith("<!doctype html>")
    assert "<title>The Real Cost of Missed Calls</title>" in html
    assert "&lt;calls&gt;" in html  # text is escaped
    assert "<sup>[1]</sup>" in html
    assert "<ol" in html and "https://s.example/b" in html


def test_text_is_plain():
    txt = export.to_text(_run())
    assert "THE REAL COST OF MISSED CALLS" in txt
    assert "#" not in txt and "<sup>" not in txt
    assert "Sources" in txt and "https://s.example/a" in txt


def test_visual_docs_list_every_entry():
    md = export.visuals_markdown(_run())
    txt = export.visuals_text(_run())
    for doc in (md, txt):
        assert "hero" in doc and "chart" in doc
        assert "Wide hero of a reception desk" in doc
        assert "What firms lose" in doc


def test_inline_links_and_subheadings_render_per_format():
    run = _run()
    run["draft"]["blocks"][1]["text"] = (
        "Read our guide on [legal intake](https://legalsoft.com/blog/existing/).\n"
        "### Why it matters\nMissed calls cost real money."
    )
    html = export.to_html(run)
    assert '<a href="https://legalsoft.com/blog/existing/">legal intake</a>' in html
    assert "<h3>Why it matters</h3>" in html
    assert "](" not in html
    txt = export.to_text(run)
    assert "legal intake (https://legalsoft.com/blog/existing/)" in txt
    assert "###" not in txt
    md = export.to_markdown(run)
    assert "[legal intake](https://legalsoft.com/blog/existing/)" in md
    assert "### Why it matters" in md


def test_missing_stage_raises():
    run = _run()
    run["draft"] = None
    with pytest.raises(ValueError):
        export.to_markdown(run)
    run2 = _run()
    run2["visuals"] = None
    with pytest.raises(ValueError):
        export.visuals_markdown(run2)
