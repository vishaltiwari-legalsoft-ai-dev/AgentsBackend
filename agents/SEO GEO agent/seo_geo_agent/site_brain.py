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


SUMMARY_SYSTEM = (
    "You are an SEO content analyst building a site inventory for a senior consultant. "
    "Extract signal, not fluff — the consultant will diagnose from your notes alone. "
    "Answer with JSON only: a list, one object per PAGE, in the same order:\n"
    '{"summary": "2 sentences — what the page offers, to whom, and anything notably missing",\n'
    ' "type": one of ' + str(list(PAGE_TYPES)) + ",\n"
    ' "target_query": "the single search query this page seems built to rank for, or empty string",\n'
    ' "cta": true or false — does the page push one clear next step (book/call/signup),\n'
    ' "topics": [up to 4 short topic phrases]}'
)


def _summarize_batch(batch: list) -> list[dict]:
    """One LLM call summarising up to SUMMARY_BATCH pages."""
    pages_text = "\n\n".join(
        f"PAGE {i}: {f.url}\nTITLE: {f.title}\nHEADINGS: {'; '.join((f.h1 + f.h2)[:6])}\nTEXT: {f.text[:1500]}"
        for i, f in enumerate(batch)
    )
    raw = sources.llm_json(SUMMARY_SYSTEM, pages_text)
    if not isinstance(raw, list) or len(raw) != len(batch):
        raise CredentialMissing("summary batch returned wrong shape")
    return [
        {"summary": str(r.get("summary", ""))[:400],
         "type": r.get("type") if r.get("type") in PAGE_TYPES else "other",
         "target_query": str(r.get("target_query", ""))[:90],
         "cta": bool(r.get("cta", False)),
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
                entry.update({k: cached[k] for k in ("summary", "type", "topics", "target_query", "cta")
                              if k in cached})
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
        target = f' → targets "{p["target_query"]}"' if p.get("target_query") else ""
        no_cta = " [no clear CTA]" if p.get("cta") is False and p.get("type") in ("home", "service") else ""
        words = f" ({p['word_count']}w)" if p.get("word_count") else ""
        lines.append(f"- [{p.get('type', 'other')}] {p['url']}{words} — {p.get('summary', p['title'])}"
                     + target + no_cta + (f" (topics: {topics})" if topics else ""))
    return "\n".join(lines)[:16000]


ISSUE_CATEGORIES = ("intent", "content", "architecture", "trust", "conversion", "ai-search", "local", "other")
SCORECARD_KEYS = ("intent", "content_depth", "architecture", "trust", "conversion", "ai_search")

EXPERT_SYSTEM = (
    "You are a senior SEO consultant (15+ years, service-business specialist) delivering a paid "
    "website review. You are blunt, specific, and allergic to generic advice — every finding names "
    "the exact page and the exact change, like an auditor who bills by the hour.\n"
    "\n"
    "How to reason (silently, before answering):\n"
    "1. Work out the business model, what they sell, and who the buyers are.\n"
    "2. Map the pages to the buyer journey: discover -> compare -> decide -> contact.\n"
    "3. Walk every lens below looking for concrete failures with page-level evidence.\n"
    "\n"
    "Evaluation lenses:\n"
    "- INTENT: does each key page target ONE clear search query (see the 'targets' notes)? "
    "Title/heading/content aligned to it? Two pages competing for the same query = cannibalization.\n"
    "- CONTENT DEPTH: thin pages on money topics (see word counts); missing answers buyers need "
    "before contacting (pricing, process, timelines, comparisons); stale-looking content.\n"
    "- ARCHITECTURE: topic clusters — do service pages have supporting content? Sections that exist "
    "but don't connect; unclear structure visible from the URL/type mix.\n"
    "- TRUST / E-E-A-T: testimonials, case studies, named people with credentials, certifications, "
    "review counts, about-page depth, contact transparency.\n"
    "- CONVERSION: commercial pages flagged [no clear CTA]; value proposition clarity; trust "
    "elements near the ask. SEO traffic that can't convert is wasted.\n"
    "- AI-SEARCH READINESS: answer-first paragraphs, FAQ blocks, quotable definitions — what AI "
    "Overviews and chatbots can lift and cite.\n"
    "- LOCAL (service businesses): are locations/service areas stated anywhere?\n"
    "\n"
    "Calibration — a GOOD issue looks like:\n"
    '{"insight": "The paralegal service page never states pricing, turnaround or hiring process — '
    'the three questions every buyer asks first", "evidence": "https://site.com/paralegal (180w) — '
    'three short paragraphs, no FAQ, [no clear CTA]", "action": "Add a 3-step How it works section, '
    'a Pricing-from block, and a 5-question FAQ to /paralegal", "priority": "high", '
    '"category": "content"}\n'
    'A WORTHLESS issue you must never produce: "Improve content quality on service pages".\n'
    "\n"
    "STRICT RULES:\n"
    "- Never invent traffic numbers, percentages, rankings, or competitor claims — you only know "
    "what is in SITE PAGES.\n"
    "- Every issue cites page-level evidence from the listed pages.\n"
    "- Give the " + str(MAX_ISSUES) + " highest-impact issues, ordered by impact — not a laundry list.\n"
    "\n"
    "Answer with JSON only:\n"
    '{"positioning": "one sentence — what the site sells and to whom",\n'
    ' "scorecard": {' + ", ".join(f'"{k}": {{"grade": 1-5, "note": "one blunt line"}}' for k in SCORECARD_KEYS) + "},\n"
    ' "strengths": [up to 4, each citing a page],\n'
    ' "issues": [up to ' + str(MAX_ISSUES) + ' calibrated objects, category one of ' + str(list(ISSUE_CATEGORIES)) + "],\n"
    ' "suggested_seeds": [8-12 queries real buyers would type, mixed intent],\n'
    ' "covered_topics": [up to 10], "missing_topics": [up to 8 with clear buyer demand]}'
)


def expert_review(brand: dict, corpus: dict) -> dict:
    """One expert pass over the digest -> evidence-backed review, persisted."""
    notes = list(corpus.get("degraded", []))
    try:
        raw = sources.llm_json(
            EXPERT_SYSTEM,
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
            "category": i.get("category") if i.get("category") in ISSUE_CATEGORIES else "other",
        }
        for i in raw.get("issues", []) if isinstance(i, dict) and i.get("insight")
    ][:MAX_ISSUES]

    scorecard = {}
    for key in SCORECARD_KEYS:
        cell = raw.get("scorecard", {}).get(key)
        if isinstance(cell, dict) and cell.get("grade") is not None:
            try:
                grade = max(1, min(5, int(cell["grade"])))
            except (TypeError, ValueError):
                continue
            scorecard[key] = {"grade": grade, "note": str(cell.get("note", ""))[:200]}

    review = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "page_count": corpus["page_count"],
        "positioning": str(raw.get("positioning", ""))[:300],
        "scorecard": scorecard,
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
