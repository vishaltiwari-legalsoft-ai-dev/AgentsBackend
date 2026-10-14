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

from . import audit, sources, state, topics
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


#: Corpus keys an absence claim ("no form", "no links to /x") is checked
#: against, and the schema-fit facts that came with them. An entry without
#: ``forms`` was crawled before they existed: such claims are unverifiable.
CLAIM_FACT_KEYS = ("forms", "tel_links", "mailto_links", "booking_links", "link_paths",
                   "link_paths_capped", "faq_pairs", "headings_total", "headings_linked",
                   "address_text")
#: The corpus is ONE Firestore document (1 MiB). Link targets are the only
#: per-page list that can grow large (a 300-link mega-menu on every page), so
#: they share a fixed budget; a page trimmed to fit is marked capped.
LINK_PATHS_BUDGET = 350_000


def page_claim_facts(facts) -> dict:
    """The corpus-entry slice of ``PageFacts`` that grounding checks claims against."""
    return {k: (list(getattr(facts, k)) if isinstance(getattr(facts, k), list) else getattr(facts, k))
            for k in CLAIM_FACT_KEYS}


def _fit_link_budget(pages: list[dict], budget: int = LINK_PATHS_BUDGET) -> None:
    """Trim every page's ``link_paths`` to an equal share of ``budget``
    characters when the corpus would exceed it, marking trimmed pages capped."""
    size = sum(len(p) + 3 for e in pages for p in e.get("link_paths") or [])
    if size <= budget or not pages:
        return
    share = budget // len(pages)
    for entry in pages:
        kept, used = [], 0
        for path in entry.get("link_paths") or []:
            used += len(path) + 3
            if used > share:
                entry["link_paths_capped"] = True
                break
            kept.append(path)
        entry["link_paths"] = kept


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
                     "has_phone": facts.has_phone, "has_address": facts.has_address,
                     **page_claim_facts(facts)}
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
                notes.append(sources.plain_note(f"Content summaries degraded (no AI): {exc}"))
            for facts in batch:
                summaries[facts.url] = _fallback_summary(facts)
    for entry in pages:
        facts = entry.pop("_facts", None)
        if facts is not None:
            entry.update(summaries[facts.url])

    _fit_link_budget(pages)
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
    page_topics = ", ".join(p.get("topics", []))
    target = f' → targets "{p["target_query"]}"' if p.get("target_query") else ""
    no_cta = " [no clear CTA]" if p.get("cta") is False and p.get("type") in ("home", "service") else ""
    words = f" ({p['word_count']}w)" if p.get("word_count") else ""
    return (f"- [{p.get('type', 'other')}] {p['url']}{words} — {p.get('summary', p['title'])}"
            + target + no_cta + (f" (topics: {page_topics})" if page_topics else ""))


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
#: "the #1 question", "the 2nd objection", "your top 3 pages": a rank or an
#: ordinal, not a quantity the page has to state.
_RANK_MARKER = re.compile(r"#\s?\d+\b|\b\d+(?:st|nd|rd|th)\b|\btop[\s-]+\d+\b", re.I)


def _numbers(text: str, claims_only: bool = False) -> set[str]:
    """Numbers in ``text`` (URLs removed), normalised: ``1,200`` == ``1200``, ``4.0`` == ``4``.
    A digit glued to a letter in front (H1, B2B) is a name, not a number.
    ``claims_only``: leave out ranks and ordinals (#1, 2nd, top 3) — what a
    finding claims, as opposed to everything the data mentions."""
    out: set[str] = set()
    text = _URL_RE.sub(" ", text or "")
    if claims_only:
        text = _RANK_MARKER.sub(" ", text)
    for raw in _NUM_RE.findall(text):
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


# ------------------------------ digest artifacts ------------------------------
# The expert model reads a digest line per page — "(920w)", "[no clear CTA]" —
# and echoes those markers, and "in the provided text", back into its prose.
# None of that means anything to the person reading the review.

_DIGEST_ARTIFACTS = (
    re.compile(r"\s*\(\s*\d[\d,]*\s*w(?:ords?)?\s*\)", re.I),
    re.compile(r"\s*\[\s*no clear CTA\s*\]", re.I),
    re.compile(r"\s*\b(?:in|from|within) (?:the )?(?:provided|supplied|given) "
               r"(?:text|content|digest|summar(?:y|ies)|excerpts?|notes)\b", re.I),
    re.compile(r"\s*\b(?:in|from) the (?:site |page )?digest\b", re.I),
)
#: A space a removed marker left in front of closing punctuation. A quote
#: closes only when nothing word-like follows it — "claims 'pre-vetted' staff"
#: keeps the space before its opening quote; ‘ and “ always open. ``'s``
#: after a space is a possessive ("firm 's" -> "firm's").
_SPACE_BEFORE_CLOSER = re.compile(
    r"\s+(?=[,.;:!?)\]]|['’\"”](?:[\s,.;:!?)\]]|$)|['’]s\b)")


def clean_review_text(text) -> str:
    """``text`` without the digest's markers: word counts, ``[no clear CTA]``,
    "in the provided text", "in the digest"."""
    out = str(text or "")
    for rx in _DIGEST_ARTIFACTS:
        out = rx.sub("", out)
    out = re.sub(r"\(\s*\)|\[\s*\]", "", out)
    out = _SPACE_BEFORE_CLOSER.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


# ------------------------------- absence claims -------------------------------
# "The contact page has no form", "no links to /practice-areas/family". The
# model judges from a text digest that never showed it forms, phone links or
# the link graph, so an absence it reports is a guess. Each one is checked
# against what the crawl recorded for the cited page: the page contradicts it
# (dropped, ``contradicted-by-page``), the page confirms it (kept), or the
# crawl has no fact to check it with (dropped, ``unverifiable-absence``) —
# an absence claim nobody can check is never kept.

_ABSENCE_CUE = re.compile(
    r"\b(?:no|not|never|lacks?|lacking|missing|without|absent|zero|none|"
    r"doesn['’]t|does not|don['’]t|do not|isn['’]t|is not|aren['’]t|are not|"
    r"has no|have no|fails? to|omits?|omitted)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[.;!?]\s|\s[—–]\s|\s-\s|\bbut\b|\bhowever\b|\bwhereas\b|\bwhile\b", re.I)
_PATH_RE = re.compile(r"(?<![\w.:/])/[a-z0-9][a-z0-9_\-/]*", re.I)
_DIGEST_PROVENANCE = re.compile(
    r"\b(?:in|from|within) (?:the )?(?:provided|supplied|given) (?:text|content|digest|"
    r"summar(?:y|ies)|excerpts?)\b|\b(?:in|from) the digest\b|"
    r"\bcontent(?: of [\w ]{1,40})? not visible\b|\bnot visible in (?:the )?(?:text|digest|summar)", re.I)
#: The thing a clause says is missing.
_CLAIM_OBJECTS = (
    ("form", re.compile(r"\bforms?\b", re.I)),
    ("phone", re.compile(r"\b(?:phone(?: numbers?)?|telephone|click[- ]to[- ]call|call (?:buttons?|links?))\b|"
                         r"\btel:", re.I)),
    ("booking", re.compile(r"\b(?:calendars?|calendly|booking|book(?:ing)? (?:a )?(?:call|meeting|demo|"
                           r"consultation)|schedul(?:er|ing)|appointments?)\b", re.I)),
    ("contact", re.compile(r"\bcontact (?:info(?:rmation)?|details?|options?|methods?|points?)\b|"
                           r"\bways? to (?:contact|reach)\b|\be-?mail(?: address(?:es)?)?\b", re.I)),
    ("cta", re.compile(r"\b(?:ctas?|calls?[- ]to[- ]action|next[- ]steps?|conversion paths?)\b", re.I)),
    ("link", re.compile(r"\b(?:cross[- ]?|inter)?link(?:s|ed|ing)?\b", re.I)),
)
#: "calendar link", "form links": the link IS the other object, not a claim
#: about the page's links.
_LINK_QUALIFIERS = frozenset({"calendar", "calendly", "booking", "call", "form", "forms", "contact",
                              "phone", "email", "e-mail", "social", "scheduling", "demo"})
_TARGET_FILLER = frozenset({
    "the", "a", "an", "to", "from", "your", "their", "its", "our", "other", "related", "relevant",
    "key", "top", "any", "these", "those", "more", "all", "main", "important", "core", "each",
    "every", "specific", "individual", "page", "pages", "content", "section", "sections", "site",
    "website", "internal", "this", "that", "them", "it", "etc", "and", "or", "of", "for", "on",
    "in", "with", "back", "corresponding", "appropriate", "reciprocal", "outbound", "inbound",
    "external", "clear", "visible", "direct", "obvious", "useful", "helpful", "working",
})
_HOME_WORDS = frozenset({"home", "homepage"})
_CONTACT_SEG = re.compile(r"^(?:contact|get-in-touch|talk-to|book|schedule|get-started|"
                          r"request-a|consult)", re.I)


def _path_of(url: str) -> str:
    return "/" + sources.norm_url(url).partition("/")[2].rstrip("/") if url else "/"


def _contact_forms(page: dict) -> list[str]:
    return [f for f in page.get("forms") or [] if f not in sources.NON_CONTACT_FORMS]


def _booking(page: dict) -> list[str]:
    return list(page.get("booking_links") or []) + [
        f for f in page.get("forms") or [] if f in sources.BOOKING_EMBEDS]


def _stem(word: str) -> str:
    w = word.lower().strip("'’-")
    if w.endswith("ies") and len(w) > 4:
        return w[:-3]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _link_target(after: str, before: str = "") -> tuple[str, list[str] | str | None]:
    """What a "no links … X" clause is about -> ``(direction, target)``.

    ``direction`` is "out" (links TO something) or "in" (linked FROM
    somewhere). ``target`` is a list of paths, a slug to look for in paths,
    "" for "any link at all", or None when nothing checkable was named.
    ``before`` is the word in front of "link": in "no case-study link" it
    names the target."""
    paths = [p.rstrip("/.,") for p in _PATH_RE.findall(after)]
    words = re.findall(r"[a-z][a-z'’\-]*", after.lower())
    direction = "in" if (words[:1] == ["from"] or "pointing" in words[:3]) else "out"
    if paths:
        return direction, paths
    if (direction == "out" and words[:1] not in (["to"], ["into"]) and before
            and before not in _TARGET_FILLER and not _ABSENCE_CUE.fullmatch(before)):
        return direction, _stem(before)
    content = [w for w in words if w not in _TARGET_FILLER and w not in ("pointing", "linking")]
    if direction == "in":
        if not content or set(content[:2]) & {"anywhere", "anything"}:
            return direction, ""
        if content[0] in _HOME_WORDS:
            return direction, ["/"]
        return direction, None
    if not words or words[0] not in ("to", "into", "toward", "towards"):
        return direction, ""          # "has no internal links": links to anything
    if not content:
        return direction, None
    return direction, "-".join(_stem(w) for w in content[:3])


def _claims(text: str) -> list[tuple[str, str, str]]:
    """Every ``(object, rest of clause, word before it)`` that ``text`` says is absent."""
    clean = _URL_RE.sub(" ", text or "")
    out: list[tuple[str, str, str]] = []
    for clause in _CLAUSE_BREAK.split(clean):
        cue = _ABSENCE_CUE.search(clause or "")
        if not cue:
            continue
        rest = clause[cue.end():]
        for name, rx in _CLAIM_OBJECTS:
            for m in rx.finditer(rest):
                before = (re.findall(r"[a-z][a-z\-]*", rest[: m.start()].lower())[-1:] or [""])[0]
                if name == "link" and before in _LINK_QUALIFIERS:
                    continue
                out.append((name, rest[m.end():], before))
                break
    return out


def _check_claim(name: str, after: str, before: str, page: dict,
                 crawled: dict[str, dict]) -> tuple[str, str]:
    """One absence claim against one crawled page -> ``(verdict, detail)``,
    verdict "contradicted" / "verified" / "unverifiable"."""
    url = page.get("url") or ""
    phone = list(page.get("tel_links") or []) or (["a phone number"] if page.get("has_phone") else [])
    if name == "phone":
        if phone:
            return "contradicted", f"{url} shows {phone[0]}"
        return ("verified", "") if "has_phone" in page else ("unverifiable", "")
    if "forms" not in page:
        return "unverifiable", f"{url} was crawled before forms, links and booking were recorded"
    forms, booking = _contact_forms(page), _booking(page)
    mail = list(page.get("mailto_links") or [])
    if name == "form":
        return (("contradicted", f"{url} has a form ({', '.join(forms)})") if forms
                else ("verified", ""))
    if name == "booking":
        return (("contradicted", f"{url} has a booking link ({booking[0]})") if booking
                else ("verified", ""))
    if name == "contact":
        found = ([f"a form ({', '.join(forms)})"] if forms else []) + phone[:1] + mail[:1] + booking[:1]
        return (("contradicted", f"{url} shows {', '.join(found)}") if found else ("verified", ""))
    if name == "cta":
        contact_paths = [p for p in page.get("link_paths") or []
                         if _CONTACT_SEG.match(p.strip("/").split("/")[0] or "")]
        found = ([f"a form ({', '.join(forms)})"] if forms else []) + booking[:1] + phone[:1] \
            + [f"a link to {p}" for p in contact_paths[:1]]
        return (("contradicted", f"{url} has {', '.join(found)}") if found else ("verified", ""))
    # links
    direction, target = _link_target(after, before)
    if target is None:
        return "unverifiable", "names no page the links should point to"
    own = _path_of(url)
    if direction == "in":
        sources_pages = [p for p in crawled.values() if p is not page]
        if isinstance(target, list):
            sources_pages = [p for p in sources_pages if _path_of(p.get("url") or "") in target]
        if not sources_pages or any("link_paths" not in p for p in sources_pages):
            return "unverifiable", "the linking pages' links were not recorded"
        hit = next((p for p in sources_pages if own in (p.get("link_paths") or [])), None)
        if hit:
            return "contradicted", f"{hit.get('url')} links to {own}"
        if any(p.get("link_paths_capped") for p in sources_pages):
            return "unverifiable", "not every link on the linking pages was recorded"
        return "verified", ""
    paths = [p for p in page.get("link_paths") or [] if p != own]
    capped = bool(page.get("link_paths_capped"))
    if target == "":
        if paths:
            return "contradicted", f"{url} links to {paths[0]}"
        return ("unverifiable", "") if capped else ("verified", "")
    if isinstance(target, list):
        named = [t.lower() for t in target]
        hit = next((p for p in paths for t in named if p.lower() == t or p.lower().endswith(t)), None)
    else:
        hit = next((p for p in paths if target in p.lower()), None)
    if hit:
        return "contradicted", f"{url} links to {hit}"
    return ("unverifiable", "") if capped else ("verified", "")


def absence_problem(issue: dict, cited: list[dict], crawled: dict[str, dict]) -> tuple[str, str] | None:
    """``(reason, detail)`` when the issue says the cited page lacks something
    and the crawl contradicts that — or has no fact to check it — else None."""
    claims: list[tuple[str, str, str]] = []
    for field in ("insight", "evidence"):
        claims += _claims(str(issue.get(field) or ""))
    text = f"{issue.get('insight', '')} {issue.get('evidence', '')}"
    if not claims:
        if _DIGEST_PROVENANCE.search(_URL_RE.sub(" ", text)):
            return ("unverifiable-absence",
                    "says something is absent from the page digest — the digest is not the page")
        return None
    if not cited:
        return "unverifiable-absence", "no crawled page to check the claim against"
    unverifiable = ""
    for name, after, before in claims:
        for page in cited:
            verdict, detail = _check_claim(name, after, before, page, crawled)
            if verdict == "contradicted":
                return "contradicted-by-page", detail
            if verdict == "unverifiable" and not unverifiable:
                unverifiable = detail or f"{page.get('url')} has no {name} fact to check against"
    if unverifiable:
        return "unverifiable-absence", unverifiable
    return None


def ground_issues(raw_issues, corpus: dict, client_line: str = "") -> tuple[list[dict], dict]:
    """Validate the model's issues against the crawl -> ``(kept, grounding)``.

    In order: a technical-hygiene finding the audit already owns is dropped;
    the issue must cite (in evidence or insight) the URL of a crawled page; every
    number in the insight and evidence must appear in the input the model was
    given for the cited page(s) — the action may carry recommendation numbers,
    and a rank or ordinal (#1, 2nd, top 3) is not a number to find; the drop
    says which field held it (``field``: "insight", what the console shows, or
    "evidence", carried along as ``evidence`` because the console doesn't);
    a mention of schema / JSON-LD / rich results must fit the cited page per
    ``audit.schema_fit``; a claim that the page lacks a form, phone, contact
    route, booking link, CTA or a link must survive :func:`absence_problem`;
    and per category + page only the highest-priority issue survives. Kept
    text is stripped of digest markers (:func:`clean_review_text`).
    ``grounding`` = ``{kept, dropped: [{reason, insight, detail}]}``; an
    "invented-number" drop adds ``field``, ``numbers`` and, for "evidence",
    ``evidence``.
    """
    pages = corpus.get("pages") or []
    crawled = {sources.norm_url(p.get("url") or ""): p for p in pages if p.get("url")}
    known = all("schema_types" in p for p in pages)
    site_types = {t for p in pages for t in p.get("schema_types") or []} if (pages and known) else None
    client_nums = _numbers(client_line)

    dropped: list[dict] = []

    def drop(reason: str, insight: str, detail: str, **extra) -> None:
        dropped.append({"reason": reason, "insight": clean_review_text(insight)[:200],
                        "detail": detail[:200], **extra})

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
        stated = sorted(_numbers(insight, claims_only=True) - allowed)
        behind = sorted(_numbers(issue["evidence"], claims_only=True) - allowed)
        if stated:
            drop("invented-number", insight,
                 f"{', '.join(stated[:5])} not in the data for {cited[0]['url']}",
                 field="insight", numbers=stated[:5])
            continue
        if behind:
            drop("invented-number", insight,
                 f"the evidence behind it cites {', '.join(behind[:5])}, not in the data for "
                 f"{cited[0]['url']}",
                 field="evidence", numbers=behind[:5],
                 evidence=clean_review_text(issue["evidence"])[:200])
            continue
        problem = audit.schema_claim_problem(
            " ".join((insight, issue["evidence"], issue["action"])), cited[0], site_types)
        if problem:
            drop("schema-not-fit", insight, problem)
            continue
        absent = absence_problem(issue, cited, crawled)
        if absent:
            drop(absent[0], insight, absent[1])
            continue
        issue["url"] = cited[0]["url"]
        for key in ("insight", "evidence", "action"):
            issue[key] = clean_review_text(issue[key])
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
            scorecard[key] = {"grade": grade, "note": clean_review_text(cell.get("note", ""))[:200]}

    review = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "page_count": corpus["page_count"],
        "positioning": clean_review_text(raw.get("positioning", ""))[:300],
        "scorecard": scorecard,
        "strengths": [clean_review_text(s)[:200] for s in _as_list(raw.get("strengths"))[:4]],
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


_STORED_NUMBER_DETAIL = re.compile(r"^(.+?) not in the data for ")


def _stored_number_field(drop: dict) -> dict:
    """An "invented-number" drop stored before ``field`` was recorded, with the
    field read off its ``detail`` ("15, 20 not in the data for …") where that is
    certain: a number the insight states -> "insight"; numbers the insight
    never mentions -> "evidence", the only other field checked. A number the
    insight holds only as a rank ("#1") stays unmarked — today's check would
    not count it, and the stored drop doesn't say whether the evidence held it."""
    if not isinstance(drop, dict) or drop.get("reason") != "invented-number" or "field" in drop:
        return drop
    found = _STORED_NUMBER_DETAIL.match(str(drop.get("detail") or ""))
    if not found:
        return drop
    listed = [n.strip() for n in found.group(1).split(",") if n.strip()]
    insight = str(drop.get("insight") or "")
    stated = [n for n in listed if n in _numbers(insight, claims_only=True)]
    if stated:
        return {**drop, "field": "insight", "numbers": stated}
    unmentioned = [n for n in listed if n not in _numbers(insight)]
    if unmentioned:
        return {**drop, "field": "evidence", "numbers": unmentioned}
    return drop


def recheck_review(review: dict, corpus: dict) -> dict:
    """``review`` as it may be shown today, against today's crawl.

    A review stored before a check existed (or before the crawl recorded the
    facts it needs) still holds findings that check would drop. Its text is
    stripped of digest markers, and every issue is held to
    :func:`absence_problem` again: one the crawl contradicts, or cannot check,
    moves to ``grounding.dropped`` with that reason — never silently. A number
    drop stored without its ``field`` gets one where the stored detail settles
    it (:func:`_stored_number_field`). Pure: the stored review is not rewritten."""
    out = dict(review)
    pages = corpus.get("pages") or []
    crawled = {sources.norm_url(p.get("url") or ""): p for p in pages if p.get("url")}
    grounding = dict(out.get("grounding") or {})
    dropped = [_stored_number_field(d) for d in grounding.get("dropped") or []]
    kept: list[dict] = []
    for issue in out.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue = dict(issue, **{k: clean_review_text(issue.get(k, "")) for k in ("insight", "evidence", "action")})
        url = _issue_url(issue)
        cited = [crawled[sources.norm_url(url)]] if url and sources.norm_url(url) in crawled else []
        cited += [p for p in _cited_pages(str(issue.get("evidence", "")), crawled) if p not in cited]
        problem = absence_problem(issue, cited, crawled)
        if problem:
            dropped.append({"reason": problem[0], "insight": issue["insight"][:200],
                            "detail": problem[1][:200]})
            continue
        kept.append(issue)
    if out.get("grounding") is not None or len(kept) != len(out.get("issues") or []):
        out["grounding"] = {**grounding, "kept": len(kept), "dropped": dropped}
    out["issues"] = kept
    if "positioning" in out:
        out["positioning"] = clean_review_text(out["positioning"])
    if isinstance(out.get("strengths"), list):
        out["strengths"] = [clean_review_text(s) for s in out["strengths"]]
    if isinstance(out.get("scorecard"), dict):
        out["scorecard"] = {k: (dict(v, note=clean_review_text(v.get("note", ""))) if isinstance(v, dict) else v)
                            for k, v in out["scorecard"].items()}
    out["degraded"] = [sources.plain_note(n, log=False) for n in out.get("degraded") or []
                       if isinstance(n, str)]
    return out


def checked_review(brand_id: str) -> dict | None:
    """The latest review, rechecked against the latest crawl
    (:func:`recheck_review`) — what the console and the Fix list show."""
    review = latest_review(brand_id)
    if not review:
        return review
    return recheck_review(review, state.load(f"corpus-{brand_id}") or {})


def mark_blog_plan(review: dict | None, plan_topics) -> dict | None:
    """``review`` with ``missing_topics_detail``: each missing topic as
    ``{"topic", "in_blog_plan"}``, the latter its 1-based place among the blog
    plan's live topics (``plan_topics``: the latest run's ``topics``) when the
    plan carries it — same words, case and spacing aside — else None. So the
    console can point at the plan instead of listing the topic twice.

    ``missing_topics`` stays a list of strings: a console that renders them as
    text must not be handed objects. Pure."""
    if not review:
        return review
    places: dict[str, int] = {}
    live = [t for t in plan_topics or [] if isinstance(t, dict) and not t.get("avoided")]
    for n, topic in enumerate(live, start=1):
        places.setdefault(topics.topic_key(str(topic.get("keyword") or "")), n)
    return {**review, "missing_topics_detail": [
        {"topic": m, "in_blog_plan": places.get(topics.topic_key(m))}
        for m in _as_list(review.get("missing_topics")) if isinstance(m, str) and m.strip()]}


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
    review = checked_review(brand_id) or {}
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


#: ``origin`` of a Fix-list item made from the page pass's advice (it is a
#: ``kind: "site"`` item, so a console that predates it draws it as a site fix).
PAGE_ADVICE = "page-advice"
PAGE_ADVICE_ACTION = {
    "internal-links": "Add internal links to these pages from related pages on your site",
    "cta": "Give each of these pages one clear next step — a form, a booking link or a call button",
}
PAGE_ADVICE_LABEL = {"internal-links": "internal linking", "cta": "a clear next step"}


def fold_page_advice(brand_id: str, site: list[dict], fix_items) -> list[dict]:
    """Site to-dos with the page pass's collapsed advice in ONE place.

    ``fix_items`` (from ``pages.build_page_intel``) are advice categories the
    page rows no longer repeat. Each lands on the site to-do it names
    (``todo_id``) as that item's ``pages`` — its own page first — or, when no
    site to-do covers it, as one new ``kind: "site"`` item with
    ``origin: "page-advice"``. Ids are stable per category, so a status set on
    the item survives the next run.
    """
    out = [dict(t) for t in site]
    by_id = {t["id"]: t for t in out}
    overlay = _status_overlay(brand_id)
    for item in fix_items or []:
        category, paths = item.get("category"), list(item.get("paths") or [])
        if not paths or category not in PAGE_ADVICE_ACTION:
            continue
        held = by_id.get(item.get("todo_id"))
        if held is not None:
            own = [_path_of(held["page"])] if held.get("page") else []
            held["pages"] = list(dict.fromkeys(own + paths))
            continue
        tid = hashlib.sha1(f"{brand_id}|site|{PAGE_ADVICE}|{category}".encode()).hexdigest()[:12]
        shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        out.append({
            "id": tid,
            "kind": "site",
            "origin": PAGE_ADVICE,
            "category": category,
            "page": "",
            "pages": paths,
            "query": "",
            "action": PAGE_ADVICE_ACTION[category],
            "why": (f"The page review suggested {PAGE_ADVICE_LABEL[category]} on {len(paths)} pages "
                    f"({shown}) — one site-wide move, listed once here instead of on each page."),
            "est_monthly_clicks": None,
            "position": 0,
            "impressions": None,
            "status": overlay.get(tid) or "todo",
        })
    return out


_LOAD_REVIEW = object()


def effective_seeds(brand: dict, review: dict | None | object = _LOAD_REVIEW) -> dict:
    """Brand copy carrying every seed the agent should start from.

    `seed_entries` (read by the blog plan): the user's typed seeds, tagged
    source "seed" — hard requirements — followed by the site review's
    `suggested_seeds`, tagged source "suggested seed" and given no protection.
    Deduped case-insensitively; a typed seed always wins over a suggestion.

    `seeds` keeps its earlier meaning for rank tracking and the keyword lab: the
    typed seeds, or the review's suggestions only when none were typed. Merging
    there would silently change which keywords those spend Serper calls and
    model prompts on.

    Pass `review` when the caller already loaded it (None = no review exists).
    """
    if review is _LOAD_REVIEW:
        review = latest_review(brand["id"])
    raw_suggested = (review or {}).get("suggested_seeds") or []

    seen: set[str] = set()
    entries: list[dict] = []
    for source, values in (("seed", brand.get("seeds") or []), ("suggested seed", raw_suggested)):
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            key = " ".join(value.split()).lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append({"text": value.strip(), "source": source})

    out = dict(brand)
    out["seed_entries"] = entries
    if not any(e["source"] == "seed" for e in entries) and raw_suggested:
        out["seeds"] = raw_suggested[:10]
    return out
