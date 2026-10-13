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
import re
from datetime import date

import httpx

from . import audit, sources, state
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
            entry = {"url": url, "title": facts.title, "hash": h, "word_count": facts.word_count,
                     "meta_description": facts.meta_description, "h1_count": len(facts.h1),
                     "images_total": facts.images_total,
                     "images_no_alt": facts.images_no_alt,
                     "images_decorative": facts.images_decorative,
                     "images_no_alt_srcs": list(facts.images_no_alt_srcs),
                     "schema_types": list(facts.schema_types),
                     "schema_errors": list(facts.schema_errors),
                     "q_headings": facts.q_headings,
                     "has_phone": facts.has_phone, "has_address": facts.has_address}
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


def _digest_line(p: dict) -> str:
    """One page as the expert model sees it — also the ground truth its numbers are checked against."""
    topics = ", ".join(p.get("topics", []))
    target = f' → targets "{p["target_query"]}"' if p.get("target_query") else ""
    no_cta = " [no clear CTA]" if p.get("cta") is False and p.get("type") in ("home", "service") else ""
    words = f" ({p['word_count']}w)" if p.get("word_count") else ""
    return (f"- [{p.get('type', 'other')}] {p['url']}{words} — {p.get('summary', p['title'])}"
            + target + no_cta + (f" (topics: {topics})" if topics else ""))


def _digest(corpus: dict) -> str:
    return "\n".join(_digest_line(p) for p in corpus["pages"])[:16000]


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


# --------------------------- grounding the review ---------------------------
# The model is told to cite pages and not invent numbers; this is where that is
# checked, in code, before anything is stored or shown. A finding that fails a
# check is dropped and recorded in ``grounding`` with the reason — never
# silently, so the UI can say how many were thrown out and why.

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
_URL_RE = re.compile(r"https?://[^\s,;()\[\]<>\"'“”]+")
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d[\d,]*(?:\.\d+)?)")


def _numbers(text: str) -> set[str]:
    """Numbers in ``text`` (URLs removed), normalised: ``1,200`` == ``1200``, ``4.0`` == ``4``.
    A digit glued to a letter in front (H1, B2B) is a name, not a number."""
    out: set[str] = set()
    for raw in _NUM_RE.findall(_URL_RE.sub(" ", text or "")):
        n = raw.replace(",", "").rstrip(".")
        if not n:
            continue
        try:
            val = float(n)
        except ValueError:
            continue
        out.add(str(int(val)) if val == int(val) else str(val))
    return out


def _cited_pages(text: str, crawled: dict[str, dict]) -> list[dict]:
    """Crawled pages cited by full URL in ``text``, in citation order."""
    out: list[dict] = []
    for raw in _URL_RE.findall(text or ""):
        page = crawled.get(sources.norm_url(raw.rstrip(".,:!?")))
        if page is not None and page not in out:
            out.append(page)
    return out


def ground_issues(raw_issues, corpus: dict, client_line: str = "") -> tuple[list[dict], dict]:
    """Validate the model's issues against the crawl -> ``(kept, grounding)``.

    In order: a technical-hygiene finding the audit already owns is dropped;
    the issue must cite (in evidence or insight) the URL of a crawled page; every
    number in the insight and evidence must appear in the input the model was
    given for the cited page(s) — the action may carry recommendation numbers;
    a mention of schema / JSON-LD / rich results must fit the cited page per
    ``audit.schema_fit``; and per category + page only the highest-priority
    issue survives. ``grounding`` = ``{kept, dropped: [{reason, insight, detail}]}``.
    """
    pages = corpus.get("pages") or []
    crawled = {sources.norm_url(p.get("url") or ""): p for p in pages if p.get("url")}
    known = all("schema_types" in p for p in pages)
    site_types = {t for p in pages for t in p.get("schema_types") or []} if (pages and known) else None
    client_nums = _numbers(client_line)

    dropped: list[dict] = []

    def drop(reason: str, insight: str, detail: str) -> None:
        dropped.append({"reason": reason, "insight": insight[:200], "detail": detail[:200]})

    candidates: list[dict] = []
    for i in raw_issues if isinstance(raw_issues, list) else []:
        if not isinstance(i, dict) or not str(i.get("insight") or "").strip():
            drop("malformed", str(i)[:200] if not isinstance(i, dict) else "",
                 "not an issue object with an insight")
            continue
        issue = {
            "insight": str(i.get("insight", ""))[:300],
            "evidence": str(i.get("evidence", ""))[:300],
            "action": str(i.get("action", ""))[:300],
            "priority": i.get("priority") if i.get("priority") in PRIORITY_RANK else "medium",
            "category": i.get("category") if i.get("category") in ISSUE_CATEGORIES else "other",
        }
        insight = issue["insight"]
        if audit.is_hygiene(insight):
            drop("audit-owned", insight, "title / meta / H1 / alt / schema presence — "
                 "the technical audit reports this")
            continue
        cited = _cited_pages(issue["evidence"], crawled) or _cited_pages(insight, crawled)
        if not cited:
            cited_any = _URL_RE.search(issue["evidence"] + " " + insight)
            drop("no-evidence-url", insight,
                 "cites a URL that was not crawled" if cited_any else "cites no page URL")
            continue
        allowed = set(client_nums)
        for page in cited:
            allowed |= _numbers(_digest_line(page))
        invented = sorted(_numbers(insight + " " + issue["evidence"]) - allowed)
        if invented:
            drop("invented-number", insight,
                 f"{', '.join(invented[:5])} not in the data for {cited[0]['url']}")
            continue
        problem = audit.schema_claim_problem(
            " ".join((insight, issue["evidence"], issue["action"])), cited[0], site_types)
        if problem:
            drop("schema-not-fit", insight, problem)
            continue
        issue["url"] = cited[0]["url"]
        candidates.append(issue)

    best: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for issue in candidates:
        key = (issue["category"], sources.norm_url(issue["url"]))
        held = best.get(key)
        if held is None:
            best[key] = issue
            order.append(key)
        elif PRIORITY_RANK[issue["priority"]] < PRIORITY_RANK[held["priority"]]:
            drop("duplicate", held["insight"], f"same {key[0]} finding for {issue['url']} at higher priority")
            best[key] = issue
        else:
            drop("duplicate", issue["insight"], f"same {key[0]} finding for {issue['url']} already kept")
    kept = [best[k] for k in order]
    over = kept[MAX_ISSUES:]
    for issue in over:
        drop("over-limit", issue["insight"], f"more than {MAX_ISSUES} findings")
    kept = kept[:MAX_ISSUES]
    return kept, {"kept": len(kept), "dropped": dropped}


def expert_review(brand: dict, corpus: dict) -> dict:
    """One expert pass over the digest -> evidence-backed review, persisted.

    The answer is validated before it is stored (see ``ground_issues``). An
    answer that is not a JSON object is a failed review — an honest
    ``CredentialMissing`` (503 at the route), never an unexplained 500 and
    never a canned list.
    """
    notes = list(corpus.get("degraded", []))
    client_line = f"CLIENT: {brand['name']} ({brand['domain']})"
    try:
        raw = sources.llm_json(
            EXPERT_SYSTEM,
            f"{client_line}\nSITE PAGES:\n{_digest(corpus)}",
        )
    except CredentialMissing as exc:
        raise CredentialMissing(f"Site review needs the AI model: {exc}") from exc
    if not isinstance(raw, dict):
        raise CredentialMissing(
            f"Site review failed: the AI model answered with a JSON {type(raw).__name__}, "
            "not the review object — nothing was saved; run the review again"
        )

    issues, grounding = ground_issues(raw.get("issues"), corpus, client_line)
    _carry_statuses(brand["id"], issues)

    scorecard = {}
    raw_scorecard = raw.get("scorecard") if isinstance(raw.get("scorecard"), dict) else {}
    for key in SCORECARD_KEYS:
        cell = raw_scorecard.get(key)
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
        "strengths": [str(s)[:200] for s in _as_list(raw.get("strengths"))[:4]],
        "issues": issues,
        "grounding": grounding,
        "suggested_seeds": [str(s)[:80] for s in _as_list(raw.get("suggested_seeds"))[:12]],
        "covered_topics": [str(t)[:80] for t in _as_list(raw.get("covered_topics"))[:10]],
        "missing_topics": [str(t)[:80] for t in _as_list(raw.get("missing_topics"))[:8]],
        "degraded": notes,
    }
    state.save(f"sitereview-{brand['id']}", review)
    return review


def _as_list(value) -> list:
    """A model field that should be a list — anything else reads as empty, not as
    a string sliced into characters."""
    return value if isinstance(value, list) else []


def analyze(brand: dict, fetch=fetch_page, sitemap=fetch_sitemap) -> dict:
    """Full pass: crawl -> summarise -> expert review."""
    return expert_review(brand, build_corpus(brand, fetch=fetch, sitemap=sitemap))


def latest_review(brand_id: str) -> dict | None:
    return state.load(f"sitereview-{brand_id}")


def _todo_id(brand_id: str, category: str, url: str) -> str:
    """Category + the page it is about — NOT the model's wording, which changes
    every run. Same finding on the same page = same id = same status."""
    return hashlib.sha1(f"{brand_id}|site|{category}|{sources.norm_url(url)}".encode()).hexdigest()[:12]


def _legacy_todo_id(brand_id: str, issue: dict) -> str:
    """The id site to-dos had before they were keyed by page (wording-based).
    Read only, so statuses set under it carry over once."""
    raw = f"{brand_id}|site|{issue.get('category', 'other')}|{str(issue.get('insight', ''))[:60]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _issue_url(issue: dict) -> str:
    """The page an issue is about: its grounded ``url``, else (a review stored
    before grounding existed) the first URL in its evidence."""
    if issue.get("url"):
        return str(issue["url"])
    found = _URL_RE.search(str(issue.get("evidence", "")))
    return found.group(0).rstrip(".,:!?") if found else ""


def _status_overlay(brand_id: str) -> dict:
    return (state.load(f"todos-{brand_id}") or {}).get("status", {}) or {}


def _carry_statuses(brand_id: str, issues: list[dict]) -> None:
    """Stamp each new issue with the status its predecessor had — same category,
    same page — when that status is only known under the old, wording-based id.
    (Under the new id the saved overlay already applies by itself.)"""
    previous = latest_review(brand_id) or {}
    overlay = _status_overlay(brand_id)
    carried: dict[tuple[str, str], str] = {}
    for old in previous.get("issues", []) or []:
        url = _issue_url(old)
        status = (old.get("carried_status")
                  or overlay.get(_legacy_todo_id(brand_id, old)))
        if url and status:
            carried.setdefault((old.get("category", "other"), sources.norm_url(url)), status)
    for issue in issues:
        status = carried.get((issue["category"], sources.norm_url(issue["url"])))
        if status:
            issue["carried_status"] = status


def site_todos(brand_id: str) -> list[dict]:
    """Review issues as Fix-list items.

    Ids come from category + page URL, so a re-run that words the same finding
    differently keeps its done/assigned mark; ``page`` is that URL, not the
    evidence sentence. A status recorded under the previous wording-based id is
    carried over rather than lost.
    """
    review = latest_review(brand_id) or {}
    overlay = _status_overlay(brand_id)
    todos: list[dict] = []
    seen: set[str] = set()
    issues = sorted(review.get("issues", []) or [],
                    key=lambda i: PRIORITY_RANK.get(i.get("priority"), 1))  # stable
    for issue in issues:
        url = _issue_url(issue)
        category = issue.get("category", "other")
        # A review stored before grounding may cite no URL at all; it keeps its
        # old id rather than collapsing with every other URL-less finding.
        tid = _todo_id(brand_id, category, url) if url else _legacy_todo_id(brand_id, issue)
        if tid in seen:
            continue  # pre-grounding reviews could hold two findings for one page
        seen.add(tid)
        todos.append({
            "id": tid,
            "kind": "site",
            "category": category,
            "page": url,
            "query": "",
            "action": issue.get("action", ""),
            "why": f"{issue.get('insight', '')} (evidence: {issue.get('evidence', '')})",
            "est_monthly_clicks": None,
            "position": 0,
            "impressions": None,
            "status": (overlay.get(tid) or overlay.get(_legacy_todo_id(brand_id, issue))
                       or issue.get("carried_status") or "todo"),
            "_priority": PRIORITY_RANK.get(issue.get("priority"), 1),
        })
    todos.sort(key=lambda t: t["_priority"])
    for t in todos:
        del t["_priority"]
    return todos


#: Fix-list slots held for site-review findings. Search-data to-dos come first
#: and used to fill the whole list, so site findings were cut without a word.
SITE_TODO_SLOTS = 5


def merge_todos(search_todos: list[dict], site: list[dict], cap: int) -> tuple[list[dict], int]:
    """Search-data to-dos then site to-dos, cut fairly at ``cap``.

    Up to SITE_TODO_SLOTS site findings are guaranteed a place; any room either
    list leaves goes to the other. Returns ``(shown, not_shown)`` — the cut is
    counted, never silent.
    """
    reserve = min(len(site), SITE_TODO_SLOTS, cap)
    head = list(search_todos[: cap - reserve])
    tail = list(site[: cap - len(head)])
    room = cap - len(head) - len(tail)
    if room > 0:
        head += search_todos[len(head): len(head) + room]
    shown = head + tail
    return shown, len(search_todos) + len(site) - len(shown)


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
