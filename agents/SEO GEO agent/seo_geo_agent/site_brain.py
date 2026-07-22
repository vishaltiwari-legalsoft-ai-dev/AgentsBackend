"""Site brain — read the brand's own website, then advise like a consultant.

Pipeline: crawl (sitemap-first, capped) -> per-page summaries (LLM, cached by
content hash so re-runs only pay for changed pages) -> one expert pass over
the digest -> evidence-backed issues (become Fix-list items), auto-seeds for
the keyword lab, and a covered/missing topic map.

Honesty rule baked into the prompt and the schema: the model may quote the
site as evidence but may NOT invent traffic numbers — quantified estimates
only ever come from rank/volume data elsewhere in the agent.
"""
from __future__ import annotations

import hashlib
from datetime import date

import httpx

from . import sources, state
from .sources import CredentialMissing, FETCH_UA, fetch_page, fetch_sitemap

MAX_PAGES = 50
SUMMARY_BATCH = 8
MAX_ISSUES = 10

PAGE_TYPES = ("home", "service", "blog", "about", "contact", "other")


def _hash(facts) -> str:
    return hashlib.sha1(f"{facts.title}|{facts.text[:2000]}".encode()).hexdigest()[:12]


def _fallback_summary(facts) -> dict:
    headings = "; ".join((facts.h1 + facts.h2)[:4])
    return {"summary": f"{facts.title or facts.url}. Sections: {headings}" if headings else (facts.title or facts.url),
            "type": "other", "topics": []}


def _summarize_batch(batch: list) -> list[dict]:
    """One LLM call summarising up to SUMMARY_BATCH pages."""
    pages_text = "\n\n".join(
        f"PAGE {i}: {f.url}\nTITLE: {f.title}\nHEADINGS: {'; '.join((f.h1 + f.h2)[:6])}\nTEXT: {f.text[:1500]}"
        for i, f in enumerate(batch)
    )
    raw = sources.llm_json(
        "You are an SEO content analyst. Answer with JSON only: a list, one object per PAGE, "
        'in order: {"summary": "2 sentences — what the page offers and who it serves", '
        '"type": one of ' + str(list(PAGE_TYPES)) + ', "topics": [up to 4 short topic phrases]}.',
        pages_text,
    )
    if not isinstance(raw, list) or len(raw) != len(batch):
        raise CredentialMissing("summary batch returned wrong shape")
    return [
        {"summary": str(r.get("summary", ""))[:400],
         "type": r.get("type") if r.get("type") in PAGE_TYPES else "other",
         "topics": [str(t)[:60] for t in r.get("topics", [])[:4]]}
        for r in raw
    ]


def build_corpus(brand: dict, fetch=fetch_page, sitemap=fetch_sitemap, max_pages: int = MAX_PAGES) -> dict:
    """Crawl the site into a cached, summarised corpus. Only changed pages hit the LLM."""
    domain = brand["domain"]
    notes: list[str] = []
    urls = sitemap(domain)
    client = httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": FETCH_UA})
    try:
        if not urls:
            home = fetch(f"https://{domain}/", client)
            urls = [f"https://{domain}/"] + [u for u in home.internal_links if domain in u and u.startswith("http")]
        urls = list(dict.fromkeys(urls))[:max_pages]

        old = {p["url"]: p for p in (state.load(f"corpus-{brand['id']}") or {}).get("pages", [])}
        pages: list[dict] = []
        fresh: list = []  # PageFacts needing a new summary
        for url in urls:
            facts = fetch(url, client)
            if facts.status != 200 or not (facts.title or facts.text):
                continue
            h = _hash(facts)
            cached = old.get(url)
            entry = {"url": url, "title": facts.title, "hash": h, "word_count": facts.word_count}
            if cached and cached.get("hash") == h and cached.get("summary"):
                entry.update({k: cached[k] for k in ("summary", "type", "topics")})
            else:
                fresh.append(facts)
                entry["_facts"] = facts
            pages.append(entry)
    finally:
        client.close()

    # Summarise changed pages in batches; degrade to title+headings without the LLM.
    summaries: dict[str, dict] = {}
    for i in range(0, len(fresh), SUMMARY_BATCH):
        batch = fresh[i : i + SUMMARY_BATCH]
        try:
            for facts, summary in zip(batch, _summarize_batch(batch)):
                summaries[facts.url] = summary
        except CredentialMissing as exc:
            if not notes:
                notes.append(f"Content summaries degraded (no AI): {exc}")
            for facts in batch:
                summaries[facts.url] = _fallback_summary(facts)
    for entry in pages:
        facts = entry.pop("_facts", None)
        if facts is not None:
            entry.update(summaries[facts.url])

    corpus = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "page_count": len(pages),
        "pages": pages,
        "degraded": notes,
    }
    state.save(f"corpus-{brand['id']}", corpus)
    return corpus


def _digest(corpus: dict) -> str:
    lines = []
    for p in corpus["pages"]:
        topics = ", ".join(p.get("topics", []))
        lines.append(f"- [{p.get('type', 'other')}] {p['url']} — {p.get('summary', p['title'])}"
                     + (f" (topics: {topics})" if topics else ""))
    return "\n".join(lines)[:14000]


def expert_review(brand: dict, corpus: dict) -> dict:
    """One expert pass over the digest -> evidence-backed review, persisted."""
    notes = list(corpus.get("degraded", []))
    try:
        raw = sources.llm_json(
            "You are a senior SEO consultant reviewing a client's website. Answer with JSON only: "
            '{"positioning": "one sentence — what the site sells and to whom", '
            '"strengths": [up to 4 short strings], '
            '"issues": [up to ' + str(MAX_ISSUES) + ' objects {"insight": str, "evidence": "specific page URL or quote", '
            '"action": "imperative fix a marketer can execute", "priority": "high"|"medium"|"low", '
            '"category": "content"|"structure"|"trust"|"other"}], '
            '"suggested_seeds": [8-12 keyword phrases buyers would search], '
            '"covered_topics": [up to 10], "missing_topics": [up to 8]}. '
            "STRICT RULES: never invent traffic numbers, percentages, or rankings; every issue needs "
            "evidence from the pages listed; actions must be specific to THIS site.",
            f"CLIENT: {brand['name']} ({brand['domain']})\nSITE PAGES:\n{_digest(corpus)}",
        )
    except CredentialMissing as exc:
        raise CredentialMissing(f"Site review needs the AI model: {exc}") from exc

    issues = [
        {
            "insight": str(i.get("insight", ""))[:300],
            "evidence": str(i.get("evidence", ""))[:300],
            "action": str(i.get("action", ""))[:300],
            "priority": i.get("priority") if i.get("priority") in ("high", "medium", "low") else "medium",
            "category": i.get("category") if i.get("category") in ("content", "structure", "trust", "other") else "other",
        }
        for i in raw.get("issues", []) if isinstance(i, dict) and i.get("insight")
    ][:MAX_ISSUES]

    review = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "page_count": corpus["page_count"],
        "positioning": str(raw.get("positioning", ""))[:300],
        "strengths": [str(s)[:200] for s in raw.get("strengths", [])[:4]],
        "issues": issues,
        "suggested_seeds": [str(s)[:80] for s in raw.get("suggested_seeds", [])[:12]],
        "covered_topics": [str(t)[:80] for t in raw.get("covered_topics", [])[:10]],
        "missing_topics": [str(t)[:80] for t in raw.get("missing_topics", [])[:8]],
        "degraded": notes,
    }
    state.save(f"sitereview-{brand['id']}", review)
    return review


def analyze(brand: dict, fetch=fetch_page, sitemap=fetch_sitemap) -> dict:
    """Full pass: crawl -> summarise -> expert review."""
    return expert_review(brand, build_corpus(brand, fetch=fetch, sitemap=sitemap))


def latest_review(brand_id: str) -> dict | None:
    return state.load(f"sitereview-{brand_id}")


def _todo_id(brand_id: str, category: str, insight: str) -> str:
    return hashlib.sha1(f"{brand_id}|site|{category}|{insight}".encode()).hexdigest()[:12]


def site_todos(brand_id: str) -> list[dict]:
    """Review issues as Fix-list items — stable ids so status survives re-runs."""
    review = latest_review(brand_id) or {}
    order = {"high": 0, "medium": 1, "low": 2}
    todos = [
        {
            "id": _todo_id(brand_id, issue["category"], issue["insight"][:60]),
            "kind": "site",
            "page": issue["evidence"] if issue["evidence"].startswith("http") else "",
            "query": "",
            "action": issue["action"],
            "why": f"{issue['insight']} (evidence: {issue['evidence']})",
            "est_monthly_clicks": None,
            "position": 0,
            "impressions": None,
            "status": "todo",
            "_priority": order[issue["priority"]],
        }
        for issue in review.get("issues", [])
    ]
    todos.sort(key=lambda t: t["_priority"])
    for t in todos:
        del t["_priority"]
    return todos


def effective_seeds(brand: dict) -> dict:
    """Brand copy whose seeds fall back to the site review's suggestions."""
    if [s for s in brand.get("seeds", []) if s.strip()]:
        return brand
    suggested = (latest_review(brand["id"]) or {}).get("suggested_seeds", [])
    if not suggested:
        return brand
    out = dict(brand)
    out["seeds"] = suggested[:10]
    return out
