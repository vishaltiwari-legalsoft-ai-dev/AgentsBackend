"""Content briefs a writer can execute — built from live SERP evidence, not
generic AI prose. Also builds "update plans" for decaying pages: what the
pages that outrank us cover that our page doesn't.
"""
from __future__ import annotations

import hashlib
from datetime import date

from . import competitors, keywords as kw_lab, sources, state
from .sources import CredentialMissing, QueryStat, fetch_page
from .topics import _tokens

MAX_BRIEFS = 30


def _brief_id(brand_id: str, keyword: str) -> str:
    return hashlib.sha1(f"{brand_id}|{keyword.lower()}".encode()).hexdigest()[:10]


def _cluster_for(brand_id: str, keyword: str) -> dict | None:
    lab = kw_lab.latest(brand_id)
    toks = _tokens(keyword)
    for c in (lab or {}).get("clusters", []):
        if keyword.lower() in [k.lower() for k in c["keywords"]] or len(toks & _tokens(c["name"])) >= 2:
            return c
    return None


def _internal_links(rows: list[QueryStat], keyword: str) -> list[str]:
    toks = _tokens(keyword)
    scored: dict[str, int] = {}
    for r in rows:
        if toks & _tokens(r.query):
            scored[r.page] = scored.get(r.page, 0) + r.clicks
    return [p for p, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:3]]


def _fallback_outline(keyword: str, deep: dict) -> list[dict]:
    outline = [{
        "heading": f"What is {keyword}? (intro)",
        "note": "Answer the query in the first 2 sentences — AI Overviews and featured snippets quote this.",
    }]
    for theme in deep["common_themes"][:8]:
        outline.append({"heading": theme, "note": "Covered by 2+ of the top-ranking pages — match and beat it."})
    if deep["questions"]:
        outline.append({"heading": "FAQ", "note": "Answer each question in 2-3 sentences: "
                        + "; ".join(deep["questions"][:6])})
    outline.append({"heading": "Next step / CTA", "note": "One clear action for the reader."})
    return outline


def build_brief(brand: dict, keyword: str, rows: list[QueryStat], search=None, fetch=fetch_page) -> dict:
    """SERP-grounded brief: outline, target keywords, entities, length, links."""
    deep = competitors.serp_deep_dive(brand, keyword, search=search, fetch=fetch)
    cluster = _cluster_for(brand["id"], keyword)
    notes: list[str] = []
    try:
        raw = sources.llm_json(
            'You are a content strategist. Answer with JSON only: {"outline": [{"heading": str, "note": str}]}.',
            f"Write a content-brief outline for an article targeting '{keyword}' for {brand['name']} "
            f"({brand['domain']}). Ground it in this SERP evidence — themes the top pages share: "
            f"{deep['common_themes']}; questions searchers ask: {deep['questions']}; entities that keep "
            f"appearing: {deep['entities']}. 6-10 headings, each with a one-line writer note.",
        )
        outline = [
            {"heading": str(o.get("heading", ""))[:120], "note": str(o.get("note", ""))[:300]}
            for o in raw.get("outline", []) if isinstance(o, dict) and o.get("heading")
        ] or _fallback_outline(keyword, deep)
    except CredentialMissing as exc:
        notes.append(f"LLM outline skipped ({exc}) — structural outline from SERP data")
        outline = _fallback_outline(keyword, deep)

    brief = {
        "id": _brief_id(brand["id"], keyword),
        "keyword": keyword,
        "at": date.today().isoformat(),
        "intent": cluster["intent"] if cluster else kw_lab.intent_of(keyword),
        "target_keywords": (cluster["keywords"][:10] if cluster else [keyword]),
        "outline": outline,
        "questions": deep["questions"],
        "entities": deep["entities"],
        "target_word_count": deep["target_word_count"] or 1200,
        "schema_recommended": deep["schema_seen"][:4] or ["Article", "FAQPage"],
        "internal_links": _internal_links(rows, keyword),
        "who_ranks": deep["who_ranks"][:5],
        "our_position": deep["our_position"],
        "aio_present": deep["aio_present"],
        "degraded": notes,
    }
    doc = state.load(f"briefs-{brand['id']}") or {"briefs": []}
    doc["briefs"] = ([brief] + [b for b in doc["briefs"] if b["id"] != brief["id"]])[:MAX_BRIEFS]
    state.save(f"briefs-{brand['id']}", doc)
    return brief


def list_briefs(brand_id: str) -> list[dict]:
    return (state.load(f"briefs-{brand_id}") or {}).get("briefs", [])


def update_plan(brand: dict, page_url: str, rows: list[QueryStat], search=None, fetch=fetch_page) -> dict:
    """Decay rescue plan: compare our decaying page against what outranks it."""
    page_rows = [r for r in rows if r.page == page_url]
    if not page_rows:
        raise ValueError("No search data for that page — run a data refresh first")
    top_query = max(page_rows, key=lambda r: r.impressions).query
    deep = competitors.serp_deep_dive(brand, top_query, search=search, fetch=fetch)
    ours = fetch(page_url)
    our_heading_tokens = [_tokens(h) for h in ours.h2 + ours.h3]

    def missing(items: list[str]) -> list[str]:
        out = []
        for item in items:
            toks = _tokens(item)
            if toks and not any(len(toks & ht) >= 2 for ht in our_heading_tokens):
                out.append(item)
        return out

    suggestions = []
    for theme in missing(deep["common_themes"])[:5]:
        suggestions.append(f"Add a section on “{theme}” — the pages outranking us cover it, ours doesn't.")
    for q in missing(deep["questions"])[:4]:
        suggestions.append(f"Answer “{q}” directly on the page.")
    if deep["schema_seen"] and not ours.schema_types:
        suggestions.append(f"Add structured data ({', '.join(deep['schema_seen'][:3])}) — top pages carry it.")
    if deep["target_word_count"] and ours.word_count and ours.word_count < 0.6 * deep["target_word_count"]:
        suggestions.append(
            f"Deepen the page: ours is ~{ours.word_count} words, the top pages average ~{deep['target_word_count']}."
        )
    suggestions.append("Refresh dates, statistics, and screenshots — decayed pages usually read stale.")
    return {
        "page": page_url,
        "at": date.today().isoformat(),
        "query": top_query,
        "our_position": deep["our_position"],
        "suggestions": suggestions,
    }
