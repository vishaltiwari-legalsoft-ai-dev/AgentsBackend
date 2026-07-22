"""Ask the SEO expert — grounded chat over everything the agent knows about a brand.

The answer is only as good as the stored data, so the context builder gathers
every doc the agent has persisted (runs, keyword tiers, ranks, audit, briefs)
and the system prompt forbids inventing numbers that aren't in it.
"""
from __future__ import annotations

import json

from . import audit, briefs, competitors, keywords, insights, sources, state


def _context(brand: dict) -> str:
    """Compact, honest snapshot of what we actually know about this brand."""
    run = insights.latest_run(brand["id"]) or {}
    lab = keywords.latest(brand["id"]) or {}
    report = audit.latest_audit(brand["id"]) or {}
    ranks = state.load(f"ranks-{brand['id']}") or {}
    sitemaps = state.load(f"sitemaps-{brand['id']}") or {}

    ctx = {
        "brand": {"name": brand["name"], "domain": brand["domain"], "seeds": brand.get("seeds", []),
                  "tracked_competitors": brand.get("competitors", [])},
        "latest_run": {
            "at": run.get("at"), "summary": run.get("summary"), "data_gaps": run.get("degraded"),
            "top_fixes": [
                {k: t.get(k) for k in ("action", "why", "est_monthly_clicks", "position", "status")}
                for t in run.get("todos", [])[:8]
            ],
            "top_blog_topics": [
                {k: t.get(k) for k in ("keyword", "priority", "trend", "difficulty", "impact")}
                for t in run.get("topics", [])[:8]
            ],
        },
        "keyword_map": {
            "at": lab.get("at"), "keyword_count": lab.get("keyword_count"),
            "clusters": [
                {k: c.get(k) for k in ("name", "tier", "intent", "coverage", "best_position",
                                       "recommendation", "owned_by")}
                for c in lab.get("clusters", [])[:12]
            ],
            "content_gaps": lab.get("gaps", [])[:10],
        },
        "rank_movement": competitors.rank_shifts(brand["id"])[:10],
        "suggested_competitors": ranks.get("suggested_competitors", []),
        "competitor_new_content": {
            d: {"new_count": e.get("new_count"), "new_urls": e.get("new_urls", [])[:5]}
            for d, e in (sitemaps.get("last_feed") or {}).items()
        },
        "tech_audit": {
            "at": report.get("at"), "health_score": report.get("health_score"),
            "site_checks": report.get("site_checks"),
            "issues": [
                {k: i.get(k) for k in ("issue", "severity", "count", "fix")}
                for i in report.get("issues", [])
            ],
        },
        "existing_briefs": [b["keyword"] for b in briefs.list_briefs(brand["id"])],
    }
    return json.dumps(ctx, ensure_ascii=False)[:9000]


SYSTEM = (
    "You are the dedicated SEO strategist for the brand in DATA. Answer the owner's question "
    "directly and concretely, like a senior consultant on a call.\n"
    "Rules:\n"
    "- Ground every claim in DATA. Never invent traffic numbers, rankings, or facts not present.\n"
    "- If DATA is missing what the question needs, say exactly which dashboard action fills it "
    "(Refresh data / Map keywords / Check now / Run audit / Build brief).\n"
    "- Always end with 1-3 specific next actions, most valuable first.\n"
    "- Mirror the user's language (English or Hinglish). Keep it under 250 words, no headers, "
    "short paragraphs or dashes."
)


def ask(brand: dict, question: str) -> dict:
    answer = sources.llm_text(SYSTEM, f"DATA:\n{_context(brand)}\n\nQUESTION: {question}")
    return {"question": question, "answer": answer}
