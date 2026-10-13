"""Technical site audit + pre-publish draft scoring.

The audit is diagnostic only (nothing is auto-changed); every issue carries a
plain-language fix so the to-do can go straight to whoever owns the site.
"""
from __future__ import annotations

import math
import os
import re
from datetime import date

from . import state
from .sources import (MAX_ALT_SRCS, CredentialMissing, PageFacts, fetch_page, fetch_sitemap,
                      fetch_text, norm_url)
from .topics import _tokens

MAX_PAGES = 80
PAGES_PER_ISSUE = 15
TITLE_MAX = 60
#: An image src missing alt on at least this share of crawled pages lives in
#: the shared header/footer — one template fix, not one finding per page.
TEMPLATE_SHARE = 0.5


def _issue(issue: str, severity: str, pages: list[str], fix: str, **extra) -> dict:
    return {"issue": issue, "severity": severity, "count": len(pages),
            "pages": pages[:PAGES_PER_ISSUE], "fix": fix, **extra}


# ------------------------------ text similarity ------------------------------
# Deterministic, dependency-free: is this sentence about the same thing as that
# one? Used to throw away model advice that only re-words a rule finding, to
# keep technical hygiene out of the expert review, and to keep the "this week"
# strip from saying one move twice.

_GENERIC = frozenset({
    "a", "an", "the", "and", "or", "but", "so", "of", "in", "on", "at", "to", "for", "from", "by",
    "with", "without", "into", "onto", "over", "under", "about", "across", "each", "every", "all",
    "any", "some", "most", "many", "several", "none", "no", "not", "only", "also", "just", "very",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "it", "its", "it's", "this", "that", "these", "those", "there", "their", "our", "your", "we",
    "you", "they", "them", "which", "who", "what", "when", "where", "how", "why", "than", "then",
    "can", "could", "should", "would", "will", "may", "might", "must", "more", "less", "one",
    "page", "site", "website", "homepage", "home", "url", "lack", "lacks", "lacking",
    "add", "adding", "write", "update", "improve", "make", "ensure", "include", "use", "create",
    "fix", "consider", "get", "give", "put", "set", "keep", "need", "needs", "go", "after",
})


def content_tokens(text: str) -> set[str]:
    """Lower-cased content words of ``text``: URLs, numbers and generic words
    removed, plural ``-s`` folded. The unit every similarity check here uses."""
    clean = re.sub(r"https?://\S+", " ", (text or "").lower())
    out: set[str] = set()
    for w in re.findall(r"[a-z][a-z0-9'-]*", clean):
        w = w.strip("'-")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) >= 2 and w not in _GENERIC:
            out.add(w)
    return out


def coverage(tokens: set[str], vocab: set[str]) -> float:
    """Share of ``tokens`` that ``vocab`` already accounts for (1.0 when empty —
    a sentence with no content words says nothing of its own)."""
    return (len(tokens & vocab) / len(tokens)) if tokens else 1.0


def similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when the smaller of the two sentences is (nearly) contained in the other."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


# Technical hygiene the audit owns: titles, meta descriptions, H1s, alt text,
# schema presence. An expert-review finding that is only about these is a
# second copy of an audit finding, in the model's words.
_HYGIENE_TOPIC = re.compile(
    r"\b(title tags?|page titles?|seo titles?|titles? (?:is|are|tag)|meta descriptions?|meta tags?|"
    r"h1s?|alt[- ](?:text|attributes?|tags?)|schema|structured data|json-?ld|"
    r"rich (?:results?|snippets?))\b", re.I)
HYGIENE_VOCAB = frozenset({
    "title", "tag", "meta", "description", "h1", "heading", "alt", "text", "attribute", "image",
    "schema", "structured", "data", "markup", "json-ld", "jsonld", "rich", "result", "snippet",
    "character", "char", "long", "length", "truncated", "truncate", "missing", "duplicate",
    "duplicated", "shorten", "trim", "unique", "empty", "multiple", "organization", "faq",
    "faqpage", "article", "blogposting", "localbusiness", "breadcrumb",
})


def is_hygiene(insight: str) -> bool:
    """True when a finding is only about technical hygiene the audit reports."""
    return bool(_HYGIENE_TOPIC.search(insight or "")) and \
        coverage(content_tokens(insight), set(HYGIENE_VOCAB)) >= 0.6


# ------------------------------ page kinds ------------------------------

_BLOG_ROOTS = frozenset({"blog", "blogs", "news", "articles", "article", "insights", "resources",
                         "posts", "post", "guides", "learn"})
_SERVICE_ROOTS = frozenset({"services", "service", "solutions", "practice-areas", "what-we-do",
                            "offerings"})
_ABOUT_SEGS = frozenset({"about", "about-us", "team", "our-team", "company", "who-we-are"})


def page_kind(url: str, hint: str | None = None) -> str:
    """home / service / blog / about / contact / other.

    The URL decides when it can (``/`` is home, ``/contact`` is contact, a post
    under ``/blog/`` is blog); ``hint`` — the corpus's per-page type — only
    fills in what the URL leaves open. A section index (``/blog``,
    ``/services``) is ``other``: it lists posts or services, it is not one.
    """
    if not (url or "").strip():  # a page that does not exist yet (a brief's planned article)
        return hint if hint in ("home", "service", "blog", "about", "contact") else "other"
    path = norm_url(url).partition("/")[2]
    segs = [s for s in path.split("/") if s]
    if not segs or segs in (["index.html"], ["home"]):
        return "home"
    first = segs[0]
    if first in _BLOG_ROOTS:
        return "blog" if len(segs) > 1 else "other"
    if first in _SERVICE_ROOTS:
        return "service" if len(segs) > 1 else "other"
    if first.startswith("contact"):
        return "contact"
    if first in _ABOUT_SEGS or first.startswith("about"):
        return "about"
    if any(w in ("service", "services") for s in segs for w in re.split(r"[-_.]+", s)):
        return "service"
    if hint in ("service", "blog", "about", "contact"):
        return hint
    return "other"


# ------------------------------ schema fit ------------------------------

ARTICLE_TYPES = frozenset({"Article", "BlogPosting", "NewsArticle", "TechArticle",
                           "ScholarlyArticle", "Report", "OpinionNewsArticle",
                           "AnalysisNewsArticle"})
LOCAL_TYPES = frozenset({"LocalBusiness", "ProfessionalService", "LegalService", "Attorney",
                         "AccountingService", "FinancialService", "Store", "Dentist", "Physician",
                         "MedicalBusiness", "HomeAndConstructionBusiness", "RealEstateAgent"})
ORG_TYPES = frozenset({"Organization", "Corporation", "OnlineBusiness", "OnlineStore", "NGO",
                       "EducationalOrganization", "MedicalOrganization",
                       "GovernmentOrganization"}) | LOCAL_TYPES
_SERVICE_PRESENT = frozenset({"Service", "Product", "FinancialProduct", "Course",
                              "SoftwareApplication"})

FAQ_RICH_RESULT_NOTE = (
    "Since 2023 Google shows FAQ rich results only for well-known, authoritative government "
    "and health sites, so for a business site the value is clean question-and-answer pairs "
    "that AI answers can lift — not a rich result."
)
#: Type-level "how to fix", one per recommendable type, in report order.
SCHEMA_FIX = {
    "Organization": "Add Organization JSON-LD to the home page: legal name, logo, contact point, "
                    "social profiles (sameAs). It defines the business entity for search and AI.",
    "WebSite": "Add WebSite JSON-LD to the home page with the site name you want shown in results.",
    "LocalBusiness": "Only if customers visit a location or you serve a defined area: add "
                     "LocalBusiness JSON-LD with the same name, address and phone the page shows.",
    "Service": "Add Service JSON-LD to each listed page: the service name, provider (the "
               "business) and area served — matching what the page says.",
    "BlogPosting": "Add BlogPosting JSON-LD to each post: headline, author, datePublished, "
                   "dateModified, image.",
    "FAQPage": "Mark up the question headings and their answers as FAQPage JSON-LD — exactly "
               "the Q&A visible on the page. " + FAQ_RICH_RESULT_NOTE,
}
SCHEMA_SEVERITY = {"Organization": "medium", "Service": "medium", "WebSite": "low",
                   "LocalBusiness": "low", "BlogPosting": "low", "FAQPage": "low"}


def _fit_view(page, kind_hint: str | None = None) -> dict:
    """The facts schema fit needs, from a ``PageFacts`` or a corpus entry.

    ``types`` is None when the source never recorded schema at all (a corpus
    entry crawled before schema was stored) — unknown, not "none".
    """
    if isinstance(page, dict):
        def get(key, default=None):
            return page.get(key, default)
    else:
        def get(key, default=None):
            return getattr(page, key, default)
    url = get("url") or ""
    types = get("schema_types")
    return {
        "url": url,
        "kind": page_kind(url, kind_hint or get("type")),
        "types": None if types is None else {str(t) for t in types},
        "q": int(get("q_headings") or 0),
        "phone": bool(get("has_phone")),
        "address": bool(get("has_address")),
        "errors": list(get("schema_errors") or []),
    }


def schema_fit(page, site_types=None, kind_hint: str | None = None) -> list[dict]:
    """Schema types this page should carry, each with the reason it fits.

    Rule-based on what the crawl actually saw — never "add schema" by default:
      * FAQPage — 2+ question-style headings and no FAQPage yet (low; see the note).
      * BlogPosting — a blog post with no Article-family markup.
      * Service — a service page with no Service/Product markup.
      * Organization / WebSite — home page only, and only if NO page on the site has it.
      * LocalBusiness — home page only, address or phone on the page, none on the site.
      * contact / about pages — nothing.
    ``site_types`` is every type seen site-wide; None = unknown, so the
    site-wide rules stay silent rather than guess. A page whose schema was never
    recorded, or whose JSON-LD fails to parse, gets nothing here (the parse
    error is its own finding).
    """
    view = _fit_view(page, kind_hint)
    if view["types"] is None or view["errors"] or view["kind"] in ("contact", "about"):
        return []
    types = view["types"]
    site = None if site_types is None else {str(t) for t in site_types}
    recs: list[dict] = []

    def rec(typ: str, reason: str) -> None:
        recs.append({"type": typ, "severity": SCHEMA_SEVERITY[typ], "reason": reason})

    if view["q"] >= 2 and "FAQPage" not in types:
        rec("FAQPage", f"{view['q']} question-style headings on the page and no FAQPage markup. "
            + FAQ_RICH_RESULT_NOTE)
    if view["kind"] == "blog" and not types & ARTICLE_TYPES:
        rec("BlogPosting", "A blog post with no Article/BlogPosting markup.")
    if view["kind"] == "service" and not (
            types & _SERVICE_PRESENT or any(t.endswith("Service") for t in types)):
        rec("Service", "A service page with no Service markup.")
    if view["kind"] == "home" and site is not None:
        if not site & ORG_TYPES:
            rec("Organization", "No page on the site carries Organization markup.")
        if "WebSite" not in site:
            rec("WebSite", "No page on the site carries WebSite markup.")
        if (view["phone"] or view["address"]) and not site & LOCAL_TYPES:
            shown = "an address" if view["address"] else "a phone number"
            rec("LocalBusiness", f"The home page shows {shown} and no page carries "
                "LocalBusiness markup — right only for a business customers visit or that "
                "serves a defined area; otherwise Organization is the type.")
    return recs


_SCHEMA_MENTION = re.compile(
    r"\b(schema|json-?ld|structured data|microdata|rich (?:results?|snippets?))\b", re.I)
_NAMED_TYPES = (
    ("FAQPage", re.compile(r"\bfaq(?:page)?\b", re.I)),
    ("Organization", re.compile(r"\borgani[sz]ation\b", re.I)),
    ("BlogPosting", re.compile(r"\b(?:blog ?posting|article)\b", re.I)),
    ("Service", re.compile(r"\bservice (?:schema|markup|structured data|json-?ld)\b", re.I)),
    ("LocalBusiness", re.compile(r"\blocal ?business\b", re.I)),
    ("WebSite", re.compile(r"\bwebsite (?:schema|markup|structured data|json-?ld)\b", re.I)),
    ("Product", re.compile(r"\bproduct (?:schema|markup)\b", re.I)),
    ("Review", re.compile(r"\b(?:review (?:schema|markup)|aggregate ?rating)\b", re.I)),
    ("HowTo", re.compile(r"\bhow-?to (?:schema|markup)\b|\bhowto\b", re.I)),
    ("BreadcrumbList", re.compile(r"\bbreadcrumb", re.I)),
)


def mentions_schema(text: str) -> bool:
    return bool(_SCHEMA_MENTION.search(text or ""))


def schema_claim_problem(text: str, page, site_types=None, kind_hint: str | None = None) -> str | None:
    """Why a piece of advice about schema does not fit ``page`` — None if it fits
    or never mentions schema. Every named type must be one ``schema_fit``
    recommends for that page; a vague "add structured data" needs at least one."""
    if not mentions_schema(text):
        return None
    if page is None:
        return "mentions schema but cites no crawled page to check it against"
    fit = {r["type"] for r in schema_fit(page, site_types, kind_hint)}
    if not fit:
        return "mentions schema, but no schema type fits this page's content"
    named = {typ for typ, rx in _NAMED_TYPES if rx.search(text)}
    if named and not named <= fit:
        wrong = ", ".join(sorted(named - fit))
        return f"suggests {wrong}, which does not fit this page (fits: {', '.join(sorted(fit))})"
    return None


# ------------------------------ grouping helpers ------------------------------

def template_srcs(src_lists: list[list[str]]) -> list[str]:
    """Image srcs missing alt on >= TEMPLATE_SHARE of the pages (at least 2).

    ``src_lists`` has one entry per crawled page, empty for pages with nothing
    missing — the share is of ALL crawled pages. Most widespread first.
    """
    n = len(src_lists)
    if n < 2:
        return []
    counts: dict[str, int] = {}
    for srcs in src_lists:
        for s in set(srcs or []):
            if not s.startswith("("):  # "(inline data: image)" is many images, not one
                counts[s] = counts.get(s, 0) + 1
    need = max(2, math.ceil(n * TEMPLATE_SHARE))
    return [s for s, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= need]


def has_page_specific_missing_alt(images_no_alt: int, srcs: list[str], template: set[str]) -> bool:
    """A page still has a missing-alt image of its own once template images are set aside.
    A full src list (the cap) or a count with no srcs is treated as yes — unknown is not clean."""
    if not images_no_alt:
        return False
    srcs = list(srcs or [])
    if not srcs or len(srcs) >= MAX_ALT_SRCS:
        return True
    return any(s not in template for s in srcs)


_TITLE_SEPS = (" | ", " - ", " — ", " – ", " · ", " :: ", " : ", " • ")


def title_template_suffix(titles: list[str]) -> str:
    """The `` | Brand`` suffix every title in ``titles`` ends with, from its
    first separator on — or "" when they share none (or there are < 2)."""
    if len(titles) < 2:
        return ""
    common = os.path.commonprefix([t[::-1] for t in titles])[::-1]
    starts = [i for i in (common.find(sep) for sep in _TITLE_SEPS) if i != -1]
    if not starts:
        return ""
    suffix = common[min(starts):]
    return suffix if suffix.strip(" |-—–·:•") else ""


def _site_checks(domain: str, sitemap_urls: list[str], get_text=fetch_text) -> list[dict]:
    """Foundation checks — the things that should exist before page-level fixes."""
    checks: list[dict] = []

    def check(name: str, ok: bool, note: str, fix: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "note": note, "fix": "" if ok else fix})

    check("Sitemap.xml", bool(sitemap_urls),
          f"{len(sitemap_urls)} URLs listed" if sitemap_urls else "not found at /sitemap.xml",
          "Publish a sitemap.xml and submit it in Search Console — without it Google discovers pages late.")
    robots = get_text(f"https://{domain}/robots.txt")
    robots_ok = robots["status"] == 200 and "user-agent" in robots["text"].lower()
    check("Robots.txt", robots_ok,
          "present" if robots_ok else "missing or empty",
          "Add a robots.txt — it directs crawlers and should point to the sitemap.")
    check("Robots links sitemap", robots_ok and "sitemap:" in robots["text"].lower(),
          "sitemap referenced" if robots_ok and "sitemap:" in robots["text"].lower() else "no Sitemap: line",
          "Add a `Sitemap: https://.../sitemap.xml` line to robots.txt.")
    http = get_text(f"http://{domain}/")
    https_ok = http["status"] in (200, 0) and http["final_url"].startswith("https://")
    check("HTTP → HTTPS redirect", https_ok,
          "redirects to https" if https_ok else f"lands on {http['final_url']}",
          "Force-redirect http:// to https:// — split versions dilute rankings and scare visitors.")
    return checks


def _title_length_issues(pages: list[PageFacts]) -> list[dict]:
    """Long titles — grouped when a shared CMS suffix is what makes them long.

    ``Paralegal Services | Legal Soft`` on forty pages is one template setting,
    not forty findings. Pages still over the limit without the suffix keep a
    per-page finding, because those need their own rewrite.
    """
    long_titles = [p for p in pages if len(p.title) > TITLE_MAX]
    out: list[dict] = []
    suffix = title_template_suffix([p.title for p in long_titles])
    if suffix:
        n = len(suffix)
        out.append(_issue(
            f"Title template adds {n} characters", "low", [p.url for p in long_titles],
            f"Every long title ends with “{suffix.strip()}” — shorten or drop it in the CMS "
            f"title template; one change fixes all {len(long_titles)} pages.",
            kind="title-template", suffix=suffix.strip(), suffix_chars=n))
        still_long = [p.url for p in long_titles if len(p.title) - n > TITLE_MAX]
    else:
        still_long = [p.url for p in long_titles]
    if still_long:
        out.append(_issue("Title longer than 60 characters", "low", still_long,
                          "Google truncates it — front-load the keyword and trim.",
                          kind="title-long"))
    return out


def _alt_issues(pages: list[PageFacts]) -> list[dict]:
    """Alt text, split three ways: template images (one fix in the header/footer),
    page-specific images missing the attribute, and ``alt=""`` (declared
    decorative — informational, never "missing", not scored)."""
    out: list[dict] = []
    template = template_srcs([p.images_no_alt_srcs for p in pages])
    tset = set(template)
    if template:
        on = [p.url for p in pages if tset & set(p.images_no_alt_srcs)]
        out.append(_issue(
            "Template image(s) without alt text — fix once in the header/footer", "low", on,
            "These images sit in the shared header/footer on most pages. Add alt text once in "
            "the site template (alt=\"\" only if the image is purely decorative).",
            kind="template-alt", srcs=template[:MAX_ALT_SRCS]))
    own = [p for p in pages
           if has_page_specific_missing_alt(p.images_no_alt, p.images_no_alt_srcs, tset)]
    if own:
        out.append(_issue(
            "Images without alt text", "low", [p.url for p in own],
            "Describe each image in alt text — accessibility + image search.",
            kind="page-alt",
            examples=[{"page": p.url, "srcs": [s for s in p.images_no_alt_srcs if s not in tset][:5]}
                      for p in own[:PAGES_PER_ISSUE]]))
    decorative = [p for p in pages if p.images_decorative]
    if decorative:
        out.append(_issue(
            "Images marked decorative (empty alt)", "low", [p.url for p in decorative],
            "Not missing: alt=\"\" tells screen readers to skip the image, which is right for "
            "pure decoration. Spot-check that none carries meaning (a chart, a team photo, a "
            "linked logo).",
            kind="decorative-alt", informational=True,
            images=sum(p.images_decorative for p in decorative)))
    return out


def _schema_issues(pages: list[PageFacts], kind_hints: dict[str, str | None]) -> list[dict]:
    """Broken JSON-LD, then one finding per schema type that FITS pages lacking it."""
    out: list[dict] = []
    broken = [p for p in pages if p.schema_errors]
    if broken:
        out.append(_issue(
            "Structured data that doesn't parse", "medium", [p.url for p in broken],
            "Search engines ignore a JSON-LD block with a syntax error, so this markup does "
            "nothing today — fix the syntax (validator.schema.org shows the line).",
            kind="schema-error",
            examples=[{"page": p.url, "error": p.schema_errors[0]} for p in broken[:PAGES_PER_ISSUE]]))

    site_types = {t for p in pages for t in p.schema_types}
    by_type: dict[str, list[tuple[str, dict]]] = {}
    for p in pages:
        for rec in schema_fit(p, site_types, kind_hints.get(norm_url(p.url))):
            by_type.setdefault(rec["type"], []).append((p.url, rec))

    def lack(n: int, noun: str) -> str:
        return f"{n} {noun} lacks" if n == 1 else f"{n} {noun}s lack"

    headline = {
        "Organization": lambda n: "Home page lacks Organization schema",
        "WebSite": lambda n: "Home page lacks WebSite schema",
        "LocalBusiness": lambda n: "Home page shows contact details but has no LocalBusiness schema",
        "Service": lambda n: f"{lack(n, 'service page')} Service schema",
        "BlogPosting": lambda n: f"{lack(n, 'blog post')} Article/BlogPosting schema",
        "FAQPage": lambda n: f"{lack(n, 'page')} FAQPage schema despite 2+ question headings",
    }
    for typ in SCHEMA_FIX:  # report order
        hits = by_type.get(typ)
        if not hits:
            continue
        out.append(_issue(
            headline[typ](len(hits)), SCHEMA_SEVERITY[typ], [u for u, _ in hits], SCHEMA_FIX[typ],
            kind="schema-fit", schema_type=typ,
            evidence=[{"page": u, "reason": r["reason"]} for u, r in hits[:PAGES_PER_ISSUE]]))
    return out


def site_audit(brand: dict, fetch=fetch_page, sitemap=fetch_sitemap, get_text=fetch_text,
               max_pages: int = MAX_PAGES) -> dict:
    """Crawl our own site (sitemap-first) and report what's technically broken."""
    domain = brand["domain"]
    sitemap_urls = sitemap(domain)
    urls = list(sitemap_urls)
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
    issues.extend(_title_length_issues(pages))
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
    corpus = state.load(f"corpus-{brand['id']}") or {}
    kind_hints = {norm_url(e.get("url") or ""): e.get("type") for e in corpus.get("pages", [])}
    issues.extend(_schema_issues(pages, kind_hints))
    issues.extend(_alt_issues(pages))

    site_checks = _site_checks(domain, sitemap_urls, get_text)

    weights = {"high": 12, "medium": 6, "low": 2}
    score = 100 - sum(weights[i["severity"]] * min(1, i["count"] / max(1, len(urls))) * 4
                      for i in issues if not i.get("informational"))
    score -= 8 * sum(1 for c in site_checks if not c["ok"])
    report = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "pages_checked": len(urls),
        "pages_ok": len(pages),
        "health_score": max(0, round(score)),
        "site_checks": site_checks,
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
