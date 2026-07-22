"""Competitor & SERP intelligence — rank tracking, competitor discovery,
sitemap watch (new content feed), and SERP reverse-engineering.

Tracked keywords = brand seeds + top cluster heads, so the watchlist follows
the strategy instead of needing separate curation.
"""
from __future__ import annotations

import re
from datetime import date

from . import keywords as kw_lab
from . import sources, state
from .sources import CredentialMissing, fetch_page
from .topics import _tokens

MAX_TRACKED = 15
MAX_SNAPSHOTS = 12
MAX_DEEP_PAGES = 5


def _domain(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def tracked_keywords(brand: dict) -> list[str]:
    seeds = [s.strip() for s in brand.get("seeds", []) if s.strip()]
    lab = kw_lab.latest(brand["id"])
    heads = [c["name"] for c in (lab or {}).get("clusters", [])]
    out: list[str] = []
    for kw in seeds + heads:
        if kw.lower() not in [o.lower() for o in out]:
            out.append(kw)
    return out[:MAX_TRACKED]


def rank_snapshot(brand: dict, search=None) -> dict:
    """Record where we rank today for every tracked keyword, and which domains
    keep showing up above us (competitor discovery)."""
    if search is None:
        if not sources.serper_available():
            raise CredentialMissing("Serper key missing — rank tracking needs live SERPs")
        search = sources.serper_search
    ranks: dict[str, dict] = {}
    seen_domains: dict[str, int] = {}
    for kw in tracked_keywords(brand):
        serp = search(kw)
        ours = next(
            (r["position"] for r in serp["organic"] if brand["domain"] in r["link"]), None
        )
        top = [_domain(r["link"]) for r in serp["organic"]]
        for d in top:
            if d and d != brand["domain"]:
                seen_domains[d] = seen_domains.get(d, 0) + 1
        ranks[kw] = {"position": ours, "top": top[:5]}

    doc = state.load(f"ranks-{brand['id']}") or {"snapshots": []}
    doc["snapshots"] = (doc["snapshots"] + [{"at": date.today().isoformat(), "ranks": ranks}])[-MAX_SNAPSHOTS:]
    doc["suggested_competitors"] = [
        d for d, _ in sorted(seen_domains.items(), key=lambda kv: -kv[1])[:8]
    ]
    state.save(f"ranks-{brand['id']}", doc)
    return doc


def rank_shifts(brand_id: str) -> list[dict]:
    """Per-keyword movement between the two latest snapshots."""
    doc = state.load(f"ranks-{brand_id}")
    if not doc or not doc.get("snapshots"):
        return []
    latest = doc["snapshots"][-1]
    prev = doc["snapshots"][-2] if len(doc["snapshots"]) > 1 else {"ranks": {}}
    shifts = []
    for kw, entry in latest["ranks"].items():
        before = (prev["ranks"].get(kw) or {}).get("position")
        now = entry.get("position")
        shifts.append({
            "keyword": kw,
            "position": now,
            "previous": before,
            "delta": (before - now) if (before and now) else None,  # positive = we moved up
            "top": entry.get("top", []),
        })
    shifts.sort(key=lambda s: (s["position"] is None, s["position"] or 99))
    return shifts


def sitemap_watch(brand: dict, fetch_sitemap=None) -> dict:
    """Diff each tracked competitor's sitemap vs last check -> new content feed."""
    fetch = fetch_sitemap or sources.fetch_sitemap
    stored = state.load(f"sitemaps-{brand['id']}") or {"domains": {}}
    feed: dict[str, dict] = {}
    for comp in brand.get("competitors", [])[:8]:
        try:
            urls = fetch(comp)
        except CredentialMissing:
            raise
        known = set(stored["domains"].get(comp, []))
        new = [u for u in urls if u not in known] if known else []
        stored["domains"][comp] = urls
        feed[comp] = {
            "at": date.today().isoformat(),
            "total": len(urls),
            "new_urls": new[:20],
            "new_count": len(new),
            "first_check": not known,
        }
    stored["last_feed"] = feed
    state.save(f"sitemaps-{brand['id']}", stored)
    return feed


def serp_deep_dive(brand: dict, query: str, search=None, fetch=fetch_page) -> dict:
    """Reverse-engineer the top of the SERP for one query: who ranks, what
    structure and questions their pages share, which entities keep appearing."""
    if search is None:
        if not sources.serper_available():
            raise CredentialMissing("Serper key missing — SERP analysis needs live results")
        search = sources.serper_search
    serp = search(query)
    pages = []
    for r in serp["organic"][:MAX_DEEP_PAGES]:
        try:
            facts = fetch(r["link"])
        except CredentialMissing:
            break
        if facts.status == 200:
            pages.append(facts)

    # Structural patterns: heading themes shared by 2+ of the top pages.
    theme_counts: dict[str, int] = {}
    theme_display: dict[str, str] = {}
    for p in pages:
        seen_here = set()
        for h in p.h2 + p.h3:
            key = " ".join(sorted(_tokens(h)))
            if key and key not in seen_here:
                seen_here.add(key)
                theme_counts[key] = theme_counts.get(key, 0) + 1
                theme_display.setdefault(key, h)
    common_themes = [theme_display[k] for k, n in sorted(theme_counts.items(), key=lambda kv: -kv[1]) if n >= 2][:12]

    # Entities: significant terms appearing across multiple titles/headings.
    term_counts: dict[str, int] = {}
    for p in pages:
        for text in [p.title] + p.h1 + p.h2:
            for tok in _tokens(text):
                term_counts[tok] = term_counts.get(tok, 0) + 1
    entities = [t for t, n in sorted(term_counts.items(), key=lambda kv: -kv[1]) if n >= 3][:15]

    questions = list(dict.fromkeys(serp["paa"] + [q for p in pages for q in p.questions]))[:12]
    word_counts = [p.word_count for p in pages if p.word_count]
    return {
        "query": query,
        "at": date.today().isoformat(),
        "who_ranks": [{"domain": _domain(r["link"]), "title": r["title"], "position": r["position"]}
                      for r in serp["organic"]],
        "our_position": next((r["position"] for r in serp["organic"] if brand["domain"] in r["link"]), None),
        "common_themes": common_themes,
        "questions": questions,
        "entities": entities,
        "target_word_count": round(sum(word_counts) / len(word_counts)) if word_counts else None,
        "schema_seen": sorted({t for p in pages for t in p.schema_types}),
        "aio_present": serp["aio_present"],
        "pages_analyzed": len(pages),
    }
