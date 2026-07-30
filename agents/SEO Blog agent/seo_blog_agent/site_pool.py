"""Website-first intelligence (spec §10a): scrape the brand's site once, then
know it. Profile = blog inventory (with topic fingerprints), keyword pool
(LLM-clustered themes, honest fallback), topic suggestions that never
silently collide with existing posts. Scraped data is labeled site_scan."""
from __future__ import annotations

import re
from datetime import date

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules, state
from .research import tokens


def _domain(website: str) -> str:
    return sources.domain_of(website.strip())


def _doc_id(domain: str) -> str:
    return f"site-{domain.replace('.', '-')}"


def _is_blog(url: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", url.lower())
    return any(h in path for h in rules.BLOG_PATH_HINTS)


def _keyword_pool(domain: str, pages: list[dict], posts: list[dict], llm) -> tuple[list[dict], list[str]]:
    counts: dict[str, int] = {}
    for e in pages + posts:
        for src in [e["title"], e["meta_description"], *e["headings"]]:
            for t in tokens(src):
                counts[t] = counts.get(t, 0) + 1
    top = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:80]]
    fallback = [{"name": t, "keywords": [t], "covered_by": []} for t in top[:20]]
    try:
        raw = llm(
            'JSON only: {"themes": [{"name": str, "keywords": [str], "covered_by": [str]}]}.',
            f"Site {domain}. Cluster these site-mined terms into 6-12 content themes its marketer "
            f"would target: {top}. Page titles for context: {[p['title'] for p in (pages + posts)[:30]]}. "
            "covered_by = URLs from this list of existing blog posts that already cover the theme "
            f"(use [] when none): {[p['url'] for p in posts][:30]}",
        )
        themes = [
            {"name": str(t.get("name", ""))[:80],
             "keywords": [str(k)[:60] for k in t.get("keywords", []) if k][:10],
             "covered_by": [str(u) for u in t.get("covered_by", [])][:5]}
            for t in raw.get("themes", []) if isinstance(t, dict) and t.get("name")
        ]
        if themes:
            return themes, []
        return fallback, ["LLM returned no themes — raw term pool shown"]
    except CredentialMissing as exc:
        return fallback, [f"theme clustering skipped ({exc}) — raw term pool shown"]


def scan_site(website: str, fetch=None, sitemap=None, llm=None) -> dict:
    fetch = fetch or sources.fetch_page
    sitemap = sitemap or sources.fetch_sitemap
    llm = llm or sources.llm_json
    domain = _domain(website)
    degraded: list[str] = []
    urls = sitemap(domain)  # CredentialMissing propagates — router turns it into a 503
    if not urls:
        urls = [f"https://{domain}/"]
        degraded.append("no sitemap.xml found — scanned the homepage only; Re-scan after fixing it")
    blog_urls = [u for u in urls if _is_blog(u)]
    picked = (blog_urls + [u for u in urls if not _is_blog(u)])[:rules.SITE_SCAN_CAP]
    if len(urls) > len(picked):
        degraded.append(f"scanned {len(picked)} of {len(urls)} sitemap URLs (cap keeps the scan fast)")
    pages: list[dict] = []
    posts: list[dict] = []
    for u in picked:
        f = fetch(u)
        if f.status != 200:
            degraded.append(f"could not fetch {u} (status {f.status})")
            continue
        entry = {"url": u, "title": f.title, "h1": f.h1[:1], "headings": (f.h2 + f.h3)[:12],
                 "meta_description": f.meta_description, "word_count": f.word_count}
        if _is_blog(u):
            fp = tokens(f.title) | tokens(" ".join(f.h1 + f.h2[:6]))
            posts.append({**entry, "fingerprint": sorted(fp)})
        else:
            pages.append(entry)
    pool, notes = _keyword_pool(domain, pages, posts, llm)
    degraded += notes
    profile = {"domain": domain, "scanned": date.today().isoformat(),
               "counts": {"sitemap_urls": len(urls), "scanned": len(picked),
                          "posts": len(posts), "pages": len(pages)},
               "pages": pages, "posts": posts, "pool": pool,
               "data_source": "site_scan", "degraded": degraded}
    state.save(_doc_id(domain), profile)
    index = state.load("sites-index") or {"sites": []}
    entry = {"domain": domain, "scanned": profile["scanned"], "counts": profile["counts"]}
    index["sites"] = [entry] + [s for s in index["sites"] if s["domain"] != domain]
    state.save("sites-index", index)
    return profile


def load_site(domain: str) -> dict | None:
    return state.load(_doc_id(_domain(domain)))


def list_sites() -> list[dict]:
    return (state.load("sites-index") or {"sites": []})["sites"]


def cannibalization(profile: dict, keyword: str) -> list[dict]:
    """Existing posts a new piece on `keyword` would collide with — flagged, never hidden."""
    kw = tokens(keyword)
    out = []
    for p in profile.get("posts", []):
        fp = set(p["fingerprint"])
        if not kw or not fp:
            continue
        overlap = len(kw & fp) / len(kw)
        if overlap >= rules.CANNIBAL_OVERLAP:
            out.append({"url": p["url"], "title": p["title"], "overlap": round(overlap, 2)})
    return sorted(out, key=lambda x: -x["overlap"])[:3]


def suggest_topics(profile: dict, llm=None) -> dict:
    llm = llm or sources.llm_json
    degraded: list[str] = []
    candidates = []
    for theme in profile["pool"]:
        for k in theme["keywords"]:
            candidates.append({"keyword": k, "theme": theme["name"],
                               "covered_by": theme["covered_by"],
                               "collisions": cannibalization(profile, k)})
    fresh = [c for c in candidates if not c["collisions"] and not c["covered_by"]]
    risky = [c for c in candidates if c["collisions"] or c["covered_by"]]
    try:
        raw = llm(
            'JSON only: {"topics": [{"keyword": str, "angle": str}]}.',
            f"From these uncovered candidate keywords for {profile['domain']}: "
            f"{[c['keyword'] for c in fresh][:40]} — pick the {rules.TOPIC_SUGGESTIONS} strongest "
            "blog topics and give each a one-line angle. Keywords only from the list.",
        )
        picked = [{"keyword": str(t.get("keyword", ""))[:80], "angle": str(t.get("angle", ""))[:160]}
                  for t in raw.get("topics", []) if isinstance(t, dict) and t.get("keyword")]
        picked = picked[:rules.TOPIC_SUGGESTIONS]
        if not picked:
            degraded.append("LLM returned no topics — showing uncovered pool keywords")
            picked = [{"keyword": c["keyword"], "angle": f"theme: {c['theme']}"}
                      for c in fresh[:rules.TOPIC_SUGGESTIONS]]
    except CredentialMissing as exc:
        degraded.append(f"topic ranking skipped ({exc}) — showing uncovered pool keywords")
        picked = [{"keyword": c["keyword"], "angle": f"theme: {c['theme']}"}
                  for c in fresh[:rules.TOPIC_SUGGESTIONS]]
    for t in picked:
        t["collisions"] = cannibalization(profile, t["keyword"])
    return {"suggested": picked,
            "avoided": [{"keyword": c["keyword"], "collisions": c["collisions"],
                         "covered_by": c["covered_by"]} for c in risky[:10]],
            "degraded": degraded}


def internal_links(profile: dict, keyword: str) -> list[str]:
    kw = tokens(keyword)
    scored = []
    for p in profile.get("pages", []) + profile.get("posts", []):
        t = tokens(p["title"]) | tokens(" ".join(p.get("headings", [])[:6]))
        if kw & t:
            scored.append((len(kw & t), p["url"]))
    return [u for _, u in sorted(scored, key=lambda x: (-x[0], x[1]))[:3]]
