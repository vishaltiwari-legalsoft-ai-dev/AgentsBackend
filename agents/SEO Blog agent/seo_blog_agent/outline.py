"""Stage 2 — team steps 5-8: competitor outlines + feature audit, meta drafting,
word/link targets, our outline + the honest evaluator loop."""
from __future__ import annotations

import re

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules
from .research import tokens


def _external_links(html: str, own_domain: str) -> int:
    hosts = set(re.findall(r'href="https?://([^/">]+)', html))
    cleaned = {h[4:] if h.startswith("www.") else h for h in (x.lower() for x in hosts)}
    return len({h for h in cleaned if own_domain not in h})


def competitor_profile(url: str, fetch=None, fetch_raw=None, llm=None) -> dict:
    fetch = fetch or sources.fetch_page
    fetch_raw = fetch_raw or sources.fetch_text
    llm = llm or sources.llm_json
    f = fetch(url)
    raw = fetch_raw(url)
    profile = {
        "url": url, "title": f.title, "meta_description": f.meta_description,
        "h1": f.h1, "h2": f.h2, "h3": f.h3, "faqs": f.questions,
        "word_count": f.word_count, "external_links": _external_links(raw["text"], sources.domain_of(url)),
        "schema_types": f.schema_types, "available": f.status == 200, "degraded": [],
    }
    try:
        audit = llm(
            'JSON only: {"eeat": bool, "key_takeaways": bool, "tables": bool, "tools": bool, '
            '"lacks": [str]}.',
            f"Feature-audit this ranking page (team step 5). Headings: {f.h2 + f.h3}. "
            f"First 2000 chars: {f.text[:2000]}. eeat = visible author credentials, citations, "
            "first-hand expertise. lacks = up to 4 concrete things missing that would help readers.",
        )
        profile["features"] = {k: bool(audit.get(k)) for k in ("eeat", "key_takeaways", "tables", "tools")}
        profile["features"]["lacks"] = [str(s)[:120] for s in audit.get("lacks", []) if s][:4]
    except CredentialMissing as exc:
        profile["features"] = {"eeat": False, "key_takeaways": False,
                               "tables": "<table" in raw["text"].lower(), "tools": False, "lacks": []}
        profile["degraded"].append(f"feature audit skipped ({exc}) — structural facts only")
    return profile


def _slug(keyword: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")


def _meta(sheet: dict, profiles: list[dict], llm) -> tuple[dict, list[str]]:
    """Team step 6: study competitor metas, draft a better unique one."""
    try:
        raw = llm(
            'JSON only: {"title": str, "description": str, "slug": str}.',
            f"Competitor metas for '{sheet['keyword']}': "
            + "; ".join(f"title={p['title']!r} desc={p['meta_description']!r}" for p in profiles)
            + ". Write a unique, better meta title (<=60 chars), description (<=155 chars) and a "
            "short hyphenated url slug for our article.",
        )
        return ({"title": str(raw.get("title", ""))[:70], "description": str(raw.get("description", ""))[:170],
                 "slug": _slug(str(raw.get("slug", "")) or sheet["keyword"])}, [])
    except CredentialMissing as exc:
        return ({"title": sheet["keyword"].title(), "description": f"A practical guide to {sheet['keyword']}.",
                 "slug": _slug(sheet["keyword"])}, [f"meta drafting skipped ({exc}) — keyword-derived meta"])


def _clean(items) -> list[dict]:
    out = []
    for o in items or []:
        if isinstance(o, dict) and o.get("heading"):
            out.append({"heading": str(o["heading"])[:120],
                        "level": int(o.get("level", 2)) if str(o.get("level", 2)).isdigit() else 2,
                        "note": str(o.get("note", ""))[:300],
                        "keywords": [str(k)[:60] for k in o.get("keywords", [])][:6]})
    return out


def _fallback_outline(sheet: dict, profiles: list[dict]) -> list[dict]:
    """Structural outline from competitor headings shared by 2+ pages (a2 briefs pattern)."""
    seen: dict[str, int] = {}
    order: list[str] = []
    for p in profiles:
        for h in p["h2"]:
            key = " ".join(sorted(tokens(h))) or h.lower()
            if key not in seen:
                order.append(h)
            seen[key] = seen.get(key, 0) + 1
    shared = [h for h in order if seen[" ".join(sorted(tokens(h))) or h.lower()] >= 2] or order[:8]
    items = [{"heading": f"What is {sheet['keyword']}?", "level": 2,
              "note": "Answer the query in the first 2 sentences — AI Overviews quote this.",
              "keywords": [sheet["keyword"]]}]
    items += [{"heading": h, "level": 2, "note": "Covered by the top-ranking pages — match and beat it.",
               "keywords": []} for h in shared[:8]]
    if sheet["serp"]["paa"]:
        items.append({"heading": "FAQ", "level": 2,
                      "note": "Answer each: " + "; ".join(sheet["serp"]["paa"][:6]), "keywords": []})
    return items


def _generate(sheet: dict, profiles: list[dict], targets: dict, llm) -> tuple[list[dict], list[str]]:
    """Team step 8: cover what competitors have, add what they lack, weave gap keywords."""
    lacks = [l for p in profiles for l in p["features"]["lacks"]]
    kw_by_tag = {t: [g["keyword"] for g in sheet["gap"] if g["tag"] == t]
                 for t in ("secondary", "long_tail", "aio")}
    try:
        raw = llm(
            'JSON only: {"outline": [{"heading": str, "level": int, "note": str, "keywords": [str]}]}.',
            f"Build the section outline for an article targeting '{sheet['keyword']}' "
            f"(~{targets['word_count']} words). Competitor outlines: "
            + "; ".join(f"{p['url']}: {p['h2'][:10]}" for p in profiles)
            + f". Things they lack (add these): {lacks[:8]}. Weave in secondary keywords "
            f"{kw_by_tag['secondary'][:8]}, long-tail {kw_by_tag['long_tail'][:6]}, and answer these "
            f"AI-overview questions {kw_by_tag['aio'][:6] + sheet['serp']['paa'][:4]}. Include a "
            "key-takeaways section near the top and an FAQ section. 8-12 headings, each with a "
            "one-line writer note and its target keywords.",
        )
        items = _clean(raw.get("outline"))
        if items:
            return items, []
        return _fallback_outline(sheet, profiles), ["LLM returned no outline — structural fallback used"]
    except CredentialMissing as exc:
        return _fallback_outline(sheet, profiles), [f"outline LLM skipped ({exc}) — structural fallback used"]


def _evaluate(sheet: dict, profiles: list[dict], items: list[dict], llm) -> tuple[dict, list[dict]]:
    """Team step 8 evaluator: our outline vs competitors, revise up to 3 rounds, never lie."""
    scores: dict = {}
    for rnd in range(1, rules.EVALUATOR_MAX_ROUNDS + 1):
        try:
            verdict = llm(
                'JSON only: {"our_score": int, "competitor_scores": [int], "beats_all": bool, '
                '"weaknesses": [str]}. Scores 0-100 for coverage, intent match, differentiation.',
                f"Keyword: '{sheet['keyword']}'. Our outline: {[o['heading'] for o in items]}. "
                f"Competitor outlines: {[{'url': p['url'], 'h2': p['h2'][:12]} for p in profiles]}.",
            )
        except CredentialMissing as exc:
            return {"rounds": rnd - 1, "beats_all": None, "scores": scores,
                    "note": f"evaluator skipped ({exc})"}, items
        scores = {"our_score": verdict.get("our_score"),
                  "competitor_scores": verdict.get("competitor_scores", [])}
        if verdict.get("beats_all"):
            return {"rounds": rnd, "beats_all": True, "scores": scores, "note": ""}, items
        try:
            revised = llm(
                'JSON only: {"outline": [{"heading": str, "level": int, "note": str, "keywords": [str]}]}.',
                f"Revise this outline to fix these weaknesses: {verdict.get('weaknesses', [])}. "
                f"Keep the strong sections. Outline: {items}",
            )
            items = _clean(revised.get("outline")) or items
        except CredentialMissing:
            break
    return {"rounds": rules.EVALUATOR_MAX_ROUNDS, "beats_all": False, "scores": scores,
            "note": "did not beat all competitors in "
                    f"{rules.EVALUATOR_MAX_ROUNDS} rounds — best version shown with honest scores"}, items


def build_outline(sheet: dict, profiles: list[dict], llm=None) -> dict:
    llm = llm or sources.llm_json
    degraded: list[str] = [n for p in profiles for n in p["degraded"]]
    avail = [p for p in profiles if p["available"]]
    wc = [p["word_count"] for p in avail if p["word_count"]]
    links = [p["external_links"] for p in avail]
    targets = {
        "word_count": round((sum(wc) / len(wc)) * (1 + rules.TARGET_UPLIFT)) if wc else 1500,
        "links": max(rules.MIN_LINKS,
                     round((sum(links) / len(links)) * (1 + rules.TARGET_UPLIFT)) if links else rules.MIN_LINKS),
    }
    meta, notes = _meta(sheet, profiles, llm)
    degraded += notes
    items, notes = _generate(sheet, profiles, targets, llm)
    degraded += notes
    evaluator, items = _evaluate(sheet, profiles, items, llm)
    return {"competitor_outlines": profiles, "meta": meta, "targets": targets,
            "outline": items, "evaluator": evaluator, "degraded": degraded}
