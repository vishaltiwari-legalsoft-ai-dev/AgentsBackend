"""Renderers: one block structure → markdown, standalone HTML, plain text.

Citation numbers are stable across all three formats: n is the 1-based order
(ledger order) of the ledger items the draft actually cites, and the Sources
section lists exactly those items — nothing the reader can't trace.
"""
from __future__ import annotations

import html as _html
import re

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _html_inline(text: str) -> str:
    """Escape text while converting [anchor](url) markdown links to <a>."""
    parts: list[str] = []
    last = 0
    for m in _MD_LINK.finditer(text):
        parts.append(_html.escape(text[last:m.start()]))
        parts.append(f'<a href="{_html.escape(m.group(2))}">{_html.escape(m.group(1))}</a>')
        last = m.end()
    parts.append(_html.escape(text[last:]))
    return "".join(parts)


def _plain_inline(text: str) -> str:
    return _MD_LINK.sub(r"\1 (\2)", text)


def _draft(run: dict) -> dict:
    draft = run.get("draft")
    if not draft:
        raise ValueError("no draft to export yet")
    return draft


def _visuals(run: dict) -> dict:
    visuals = run.get("visuals")
    if not visuals or not visuals.get("items"):
        raise ValueError("no visual plan to export yet")
    return visuals


def _citation_numbers(run: dict) -> tuple[dict[str, int], list[dict]]:
    """Map cited ledger ids → [n], in ledger order; return the cited items too."""
    cited = {c for b in _draft(run)["blocks"] for c in b.get("cites", [])}
    numbered, items = {}, []
    for item in run.get("ledger", []):
        if item["id"] in cited:
            items.append(item)
            numbered[item["id"]] = len(items)
    return numbered, items


def _marks(block: dict, numbers: dict[str, int]) -> str:
    return "".join(f"[{numbers[c]}]" for c in block.get("cites", []) if c in numbers)


def to_markdown(run: dict) -> str:
    draft = _draft(run)
    numbers, sources = _citation_numbers(run)
    lines = [f"# {draft['meta'].get('title', run['topic'])}", ""]
    for block in draft["blocks"]:
        if block.get("heading"):
            lines += [f"## {block['heading']}", ""]
        marks = _marks(block, numbers)
        lines += [block["text"] + (f" {marks}" if marks else ""), ""]
    if sources:
        lines += ["## Sources", ""]
        lines += [f"{n}. {i.get('source_name', i['url'])} — {i['url']}" for n, i in enumerate(sources, 1)]
        lines.append("")
    return "\n".join(lines)


def to_html(run: dict) -> str:
    draft = _draft(run)
    numbers, sources = _citation_numbers(run)
    meta = draft["meta"]
    title = _html.escape(meta.get("title", run["topic"]))
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{title}</title>",
        f'<meta name="description" content="{_html.escape(meta.get("description", ""))}">',
        "<style>body{max-width:720px;margin:2rem auto;padding:0 1rem;"
        "font-family:Georgia,serif;line-height:1.6}sup{font-size:.75em}</style>",
        "</head><body><article>",
        f"<h1>{title}</h1>",
    ]
    for block in draft["blocks"]:
        if block.get("heading"):
            parts.append(f"<h2>{_html.escape(block['heading'])}</h2>")
        sups = "".join(f"<sup>[{numbers[c]}]</sup>" for c in block.get("cites", []) if c in numbers)
        paragraphs = []
        for line in block["text"].split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("### "):
                paragraphs.append(f"<h3>{_html.escape(line[4:])}</h3>")
            else:
                paragraphs.append(f"<p>{_html_inline(line)}</p>")
        if paragraphs and paragraphs[-1].endswith("</p>"):
            paragraphs[-1] = paragraphs[-1][: -len("</p>")] + sups + "</p>"
        elif sups:
            paragraphs.append(f"<p>{sups}</p>")
        parts.extend(paragraphs)
    if sources:
        parts.append("<h2>Sources</h2><ol>")
        parts += [
            f'<li>{_html.escape(i.get("source_name", i["url"]))} — '
            f'<a href="{_html.escape(i["url"])}">{_html.escape(i["url"])}</a></li>'
            for i in sources
        ]
        parts.append("</ol>")
    parts.append("</article></body></html>")
    return "\n".join(parts)


def to_text(run: dict) -> str:
    draft = _draft(run)
    numbers, sources = _citation_numbers(run)
    lines = [draft["meta"].get("title", run["topic"]).upper(), ""]
    for block in draft["blocks"]:
        if block.get("heading"):
            lines += [block["heading"].upper(), ""]
        marks = _marks(block, numbers)
        text = _plain_inline(block["text"]).replace("### ", "")
        lines += [text + (f" {marks}" if marks else ""), ""]
    if sources:
        lines += ["Sources", ""]
        lines += [f"{n}. {i.get('source_name', i['url'])} — {i['url']}" for n, i in enumerate(sources, 1)]
        lines.append("")
    return "\n".join(lines)


def visuals_markdown(run: dict) -> str:
    visuals = _visuals(run)
    title = (run.get("draft") or {}).get("meta", {}).get("title", run["topic"])
    lines = [f"# Visual prompts — {title}", ""]
    for v in visuals["items"]:
        lines += [
            f"## Visual {v['n']} — {v['type']}",
            "",
            f"- **Placement:** {v['section']}",
            f"- **Theme:** {v['theme']}",
            f"- **Why:** {v['rationale']}",
            "",
            f"**Prompt:** {v['prompt']}",
            "",
        ]
    return "\n".join(lines)


def visuals_text(run: dict) -> str:
    visuals = _visuals(run)
    title = (run.get("draft") or {}).get("meta", {}).get("title", run["topic"])
    lines = [f"VISUAL PROMPTS — {title.upper()}", ""]
    for v in visuals["items"]:
        lines += [
            f"Visual {v['n']} ({v['type']})",
            f"  Placement: {v['section']}",
            f"  Theme: {v['theme']}",
            f"  Why: {v['rationale']}",
            f"  Prompt: {v['prompt']}",
            "",
        ]
    return "\n".join(lines)
