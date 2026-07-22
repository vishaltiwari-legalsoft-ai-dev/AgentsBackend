"""Technical site audit + pre-publish draft scoring.

The audit is diagnostic only (nothing is auto-changed); every issue carries a
plain-language fix so the to-do can go straight to whoever owns the site.
"""
from __future__ import annotations

import re
from datetime import date

from . import state
from .sources import CredentialMissing, PageFacts, fetch_page, fetch_sitemap
from .topics import _tokens

MAX_PAGES = 80
PAGES_PER_ISSUE = 15


def _issue(issue: str, severity: str, pages: list[str], fix: str) -> dict:
    return {"issue": issue, "severity": severity, "count": len(pages),
            "pages": pages[:PAGES_PER_ISSUE], "fix": fix}


def site_audit(brand: dict, fetch=fetch_page, sitemap=fetch_sitemap, max_pages: int = MAX_PAGES) -> dict:
    """Crawl our own site (sitemap-first) and report what's technically broken."""
    domain = brand["domain"]
    urls = sitemap(domain)
    if not urls:
        home = fetch(f"https://{domain}/")
        urls = [f"https://{domain}/"] + [
            u for u in home.internal_links
            if domain in u and u.startswith("http")
        ]
    urls = list(dict.fromkeys(urls))[:max_pages]

    pages: list[PageFacts] = []
    broken: list[str] = []
    for url in urls:
        facts = fetch(url)
        if facts.status == 200:
            pages.append(facts)
        else:
            broken.append(f"{url} ({facts.status or 'unreachable'})")

    titles: dict[str, list[str]] = {}
    metas: dict[str, list[str]] = {}
    for p in pages:
        if p.title:
            titles.setdefault(p.title.lower(), []).append(p.url)
        if p.meta_description:
            metas.setdefault(p.meta_description.lower(), []).append(p.url)

    issues = []
    if broken:
        issues.append(_issue("Broken or unreachable pages", "high", broken,
                             "Fix or redirect these URLs — every one wastes crawl budget and user trust."))
    missing_title = [p.url for p in pages if not p.title]
    if missing_title:
        issues.append(_issue("Missing page title", "high", missing_title,
                             "Write a unique title (< 60 chars) with the page's target keyword."))
    dupe_titles = [u for urls_ in titles.values() if len(urls_) > 1 for u in urls_]
    if dupe_titles:
        issues.append(_issue("Duplicate titles", "medium", dupe_titles,
                             "Each page needs its own title, or Google picks who ranks for you."))
    long_titles = [p.url for p in pages if len(p.title) > 60]
    if long_titles:
        issues.append(_issue("Title longer than 60 characters", "low", long_titles,
                             "Google truncates it — front-load the keyword and trim."))
    missing_meta = [p.url for p in pages if not p.meta_description]
    if missing_meta:
        issues.append(_issue("Missing meta description", "medium", missing_meta,
                             "Write a 140-160 char pitch for the click — it's your ad copy in the results."))
    dupe_metas = [u for urls_ in metas.values() if len(urls_) > 1 for u in urls_]
    if dupe_metas:
        issues.append(_issue("Duplicate meta descriptions", "low", dupe_metas,
                             "Differentiate them per page."))
    h1_missing = [p.url for p in pages if not p.h1]
    if h1_missing:
        issues.append(_issue("Missing H1", "medium", h1_missing,
                             "One clear H1 per page stating what it's about."))
    h1_multi = [p.url for p in pages if len(p.h1) > 1]
    if h1_multi:
        issues.append(_issue("Multiple H1s", "low", h1_multi,
                             "Keep one H1; demote the rest to H2."))
    no_schema = [p.url for p in pages if not p.schema_types]
    if no_schema:
        issues.append(_issue("No structured data", "medium", no_schema,
                             "Add Organization/Service/FAQ schema — it feeds rich results and AI answers."))
    alt_pages = [p.url for p in pages if p.images_no_alt > 3]
    if alt_pages:
        issues.append(_issue("Images without alt text (4+ on page)", "low", alt_pages,
                             "Describe images in alt text — accessibility + image search."))

    weights = {"high": 12, "medium": 6, "low": 2}
    score = max(0, 100 - sum(weights[i["severity"]] * min(1, i["count"] / max(1, len(urls))) * 4
                             for i in issues))
    report = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "pages_checked": len(urls),
        "pages_ok": len(pages),
        "health_score": round(score),
        "issues": issues,
    }
    state.save(f"audit-{brand['id']}", report)
    return report


def latest_audit(brand_id: str) -> dict | None:
    return state.load(f"audit-{brand_id}")


# ------------------------------ draft scoring ------------------------------

_HEADING_RE = re.compile(r"^\s{0,3}#{1,4}\s+(.+)$", re.MULTILINE)


def score_draft(brand: dict, text: str, keyword: str, brief: dict | None = None) -> dict:
    """Pre-publish check: does this draft actually target the keyword and cover
    what the SERP demands? Returns 0-100 with itemized pass/fail checks."""
    words = text.split()
    total = len(words)
    kw_toks = _tokens(keyword)
    text_toks = _tokens(text)
    headings = _HEADING_RE.findall(text)
    target_len = (brief or {}).get("target_word_count") or 1000

    checks: list[dict] = []

    def check(name: str, ok: bool, note: str, weight: int = 1) -> None:
        checks.append({"name": name, "ok": bool(ok), "note": note, "weight": weight})

    first_100 = _tokens(" ".join(words[:100]))
    check("Keyword up front", kw_toks <= first_100,
          f"'{keyword}' should appear in the first 100 words", 2)
    check("Keyword in a heading", any(kw_toks & _tokens(h) for h in headings),
          "At least one heading should carry the target keyword", 2)
    check("Enough headings", len(headings) >= 3,
          f"{len(headings)} heading(s) found — use 3+ so the page is scannable", 1)
    check("Length vs top pages", total >= 0.7 * target_len,
          f"~{total} words vs ~{target_len} the ranking pages average", 2)
    sentences = [s for s in re.split(r"[.!?]+\s", text) if s.strip()]
    avg_sentence = (total / len(sentences)) if sentences else 0
    check("Readable sentences", 0 < avg_sentence <= 28,
          f"Avg sentence ≈ {round(avg_sentence)} words (keep under 28)", 1)
    check("Brand present", brand["name"].lower() in text.lower(),
          "Mention the brand at least once (trust + entity signal)", 1)
    if brief:
        qs = brief.get("questions", [])
        covered = sum(1 for q in qs if len(_tokens(q) & text_toks) >= max(2, len(_tokens(q)) - 2))
        check("Questions covered", not qs or covered >= (len(qs) + 1) // 2,
              f"{covered}/{len(qs)} of the questions searchers ask are addressed", 2)
        ents = brief.get("entities", [])
        hit = sum(1 for e in ents if e.lower() in text.lower())
        check("Entities covered", not ents or hit >= (len(ents) + 1) // 2,
              f"{hit}/{len(ents)} expected entities/terms appear", 1)

    earned = sum(c["weight"] for c in checks if c["ok"])
    possible = sum(c["weight"] for c in checks)
    score = round(100 * earned / possible) if possible else 0
    verdict = "publish-ready" if score >= 80 else "needs work" if score >= 60 else "rework"
    return {"score": score, "verdict": verdict, "keyword": keyword, "word_count": total, "checks": checks}
