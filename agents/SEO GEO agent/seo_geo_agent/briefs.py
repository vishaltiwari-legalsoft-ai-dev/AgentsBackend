"""Content briefs a writer can execute — built from live SERP evidence, not
generic AI prose. Also builds "update plans" for decaying pages: what the
pages that outrank us cover that our page doesn't.
"""
from __future__ import annotations

import hashlib
from datetime import date

from . import audit, competitors, keywords as kw_lab, sources, state
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


def _prevalence(deep: dict, types: list[str]) -> str:
    """``Article (3 of 5 top pages), Service (2 of 5 top pages)`` — the evidence."""
    n_of = {s["type"]: s["pages"] for s in deep.get("schema_prevalence") or []}
    total = deep.get("schema_pages") or deep.get("pages_analyzed") or 0
    return ", ".join(f"{t} ({n_of[t]} of {total} top pages)" if t in n_of and total else t
                     for t in types)


def _brief_schema(deep: dict) -> tuple[list[str], str]:
    """What the brief's page should be marked up with — evidence or nothing.

    The ranking pages' own patterns first (types on 2+ of them). When they show
    none, the only rule-backed suggestion is FAQPage, and only when the brief
    carries 2+ questions the article will answer. No hard-coded default.
    """
    seen = list(deep.get("schema_seen") or [])[:4]
    if seen:
        return seen, f"Seen on the ranking pages: {_prevalence(deep, seen)}."
    planned = {"url": "", "type": "other", "schema_types": [],  # the article, not yet written
               "q_headings": len(deep.get("questions") or [])}
    faq = next((r for r in audit.schema_fit(planned) if r["type"] == "FAQPage"), None)
    if faq:
        return ["FAQPage"], (f"No schema seen on ranking pages. The brief answers "
                             f"{planned['q_headings']} questions, so FAQPage fits. "
                             + audit.FAQ_RICH_RESULT_NOTE)
    return [], "No schema seen on ranking pages."


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

    schema_recommended, schema_note = _brief_schema(deep)
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
        "schema_recommended": schema_recommended,
        "schema_note": schema_note,
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


def _missing_schema(deep: dict, ours) -> list[str]:
    """The SERP's schema patterns our page does not already carry (an Article-
    family type counts for Article; any Organization-family type for those).
    FAQPage additionally has to fit OUR page — 2+ question headings — because
    markup for Q&A the page does not show is exactly the advice not to give."""
    have = set(ours.schema_types)
    our_fit = {r["type"] for r in audit.schema_fit(ours)}
    out = []
    for typ in deep.get("schema_seen") or []:
        if typ in have:
            continue
        if typ in audit.ARTICLE_TYPES and have & audit.ARTICLE_TYPES:
            continue
        if typ in audit.ORG_TYPES and have & audit.ORG_TYPES:
            continue
        if typ == "FAQPage" and "FAQPage" not in our_fit:
            continue
        out.append(typ)
    return out[:3]


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
    missing_schema = _missing_schema(deep, ours)
    if missing_schema:
        suggestions.append(f"Add structured data the outranking pages carry and ours doesn't: "
                           f"{_prevalence(deep, missing_schema)}.")
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
