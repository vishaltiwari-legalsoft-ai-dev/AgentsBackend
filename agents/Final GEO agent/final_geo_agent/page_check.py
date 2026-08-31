"""Page Check — URL or draft in; verdict, pros, cons, cannibalization out.

One call sits over the Content Optimizer (``opt_pipeline.analyze``) and the
brand's own site data (a2's ``corpus-{brand}`` and ``pages-{brand}`` docs) and
answers the question a writer actually has: will this page help, or is it
fighting a page the site already has? Everything here is arithmetic over
what other modules produced — no model call anywhere — so a check costs one
SERP snapshot and nothing more.

Feature parity, URL vs paste. A live page could be measured on the 13 HTML
features ``opt_extract`` sees; a pasted draft only ever yields the 10 markdown
features of ``opt_pipeline.draft_features``. Scored on different feature sets
the same content would get different numbers depending on how it arrived, so
the URL path is rendered to markdown first (headings, paragraphs, list items,
tables, images — from the same boilerplate-stripped DOM ``opt_extract`` reads,
under the same block rules) and both paths go through ``draft_features``. The
three HTML-only features (h1_count, internal_links, external_links) are never
scored here.

Google's AI Overview is no part of any verdict: the SERP provider cannot
return one, so ``meta.aio_present`` is structurally False and either "has" or
"lacks an AI Overview" would be invented.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

from lxml import html as lxml_html

from seo_geo_agent import sources, state

from . import opt_extract, opt_pipeline, opt_score, opt_structure, opt_text
from .opt_config import ExtractCfg, load_config

# Overlap is the Jaccard of content tokens: "cold brew coffee recipe" vs
# "how to make cold brew coffee" is 3/5 = 0.6, the floor for calling two
# queries the same intent; 0.8 needs them near-identical.
CORPUS_OVERLAP_EVIDENCE = 0.6
CORPUS_OVERLAP_HIGH = 0.8
GSC_OVERLAP_HIGH = 0.8
SERP_TOP = 10
# a PAA question counts as answered when at least this share of its content
# words (minus the target query's own words) appears in the draft
PAA_ANSWERED_SHARE = 0.5
HELPS_MAX_GAPS = 3

_QUESTION_WORDS = frozenset(
    "how what why when where which who can could should do does did is are will".split()
)
_TITLE_SPLIT = re.compile(r"\s*\|\s*|\s+[-–—:·»]\s+")
_HEADING = re.compile(r"h[1-3]")


# ------------------------------------------------------------------ overlap

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in opt_text.STOPWORDS_EN}


def cannibal_overlap(a: str, b: str) -> float:
    """Jaccard over lowercase alnum tokens minus stopwords: 1.0 when two
    queries carry the same content words, 0.0 when they share none or either
    is empty."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _page_key(ref: str) -> tuple[str, str]:
    """(host, path) with scheme, www., query, fragment and trailing slash
    dropped; a bare path yields an empty host."""
    p = urlparse(ref.strip())
    host = (p.hostname or "").removeprefix("www.")
    path = (p.path or "/").rstrip("/") or "/"
    return host, path.lower()


def _same_page(checked: tuple[str, str] | None, ref: str) -> bool:
    if checked is None or not ref:
        return False
    host, path = _page_key(ref)
    return path == checked[1] and (not host or host == checked[0])


# ----------------------------------------------------------- page → markdown

def _text(el) -> str:
    return " ".join(" ".join(el.itertext()).split())


def html_to_markdown(html: str, cfg: ExtractCfg) -> tuple[str, dict]:
    """Content markdown plus ``{"title", "h1"}`` for one page.

    Walks the boilerplate-stripped DOM ``opt_extract`` sections come from,
    under the same block rules (min chars, link density), emitting only what
    ``draft_features`` can read back: ``#`` headings, paragraphs, ``-`` list
    items, pipe tables, ``![alt](src)`` images.
    """
    try:
        root = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 — extract_document flags this doc unparseable
        return "", {"title": "", "h1": ""}
    title_el = root.find(".//title")
    facts = {"title": _text(title_el) if title_el is not None else "", "h1": ""}
    opt_extract._drop_boilerplate(root, cfg)

    # blocks are blank-line separated, as in a hand-written draft: list items
    # and table rows stay contiguous so a list is one block, not one per item
    blocks: list[list[str]] = []
    consumed: set = set()
    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str) or el in consumed:
            continue
        if _HEADING.fullmatch(tag):
            text = _text(el)
            if text:
                blocks.append([f"{'#' * int(tag[1])} {text}"])
                if tag == "h1" and not facts["h1"]:
                    facts["h1"] = text
            consumed.update(el.iter())
        elif tag == "li":
            text = _text(el)
            if text and opt_extract._link_density(el) <= cfg.link_density_max:
                if blocks and blocks[-1][-1].startswith("- "):
                    blocks[-1].append(f"- {text}")
                else:
                    blocks.append([f"- {text}"])
            consumed.update(d for d in el.iter() if d.tag != "img")
        elif tag == "table":
            rows = [[_text(c) for c in tr.iter("th", "td")] for tr in el.iter("tr")]
            rows = [r for r in rows if any(r)]
            if rows:
                blocks.append(
                    ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * len(rows[0])]
                    + ["| " + " | ".join(r) + " |" for r in rows[1:]]
                )
            consumed.update(el.iter())
        elif tag == "img":
            src = el.get("src", "")
            if src.startswith("data:"):
                continue
            alt = (el.get("alt") or "").strip()
            try:
                big = (int(el.get("width", 0)) >= cfg.min_content_image_px
                       and int(el.get("height", 0)) >= cfg.min_content_image_px)
            except ValueError:
                big = False
            if alt or big:
                blocks.append([f"![{alt}]({src})"])
        elif tag in ("p", "blockquote"):
            text = _text(el)
            if len(text) >= cfg.min_block_chars and opt_extract._link_density(el) <= cfg.link_density_max:
                blocks.append([text])
            consumed.update(d for d in el.iter() if d.tag != "img")
    return "\n\n".join("\n".join(b) for b in blocks), facts


def _markdown_fallback(md: str, doc: opt_extract.CleanDoc) -> str:
    """Div-soup pages keep their text outside <p>, so the DOM walk sees
    headings and no body. The CleanDoc text (trafilatura / density view)
    stands in as paragraphs; lists and tables then read 0, which the page
    flags say."""
    headings = [line for line in md.splitlines() if line.startswith("#")]
    paragraphs = [p.strip() for p in doc.text.split("\n") if p.strip()]
    return "\n\n".join(headings + paragraphs)


# ------------------------------------------------------------ target query

def target_query_from_title(title: str, brand: dict) -> str:
    """The query a page title implies: site-name segments and separators
    dropped, the brand name removed wherever it appears, the longest
    segment left standing, lowercased. Empty when only the brand remains."""
    domain = (brand.get("domain") or "").lower().removeprefix("www.")
    names = {n for n in (
        (brand.get("name") or "").lower(), domain, domain.split(".")[0] if domain else "",
    ) if n}
    kept: list[str] = []
    for seg in _TITLE_SPLIT.split(title or ""):
        low = " ".join(seg.lower().split())
        if not low or low.removeprefix("www.") in names:
            continue
        for n in names:
            low = re.sub(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", " ", low)
        low = re.sub(r"\s+", " ", low).strip(" -–—:|,.")
        if low:
            kept.append(low)
    return max(kept, key=len) if kept else ""


# --------------------------------------------------------------- pros / cons

def paa_unanswered(questions: list[str], keyword: str, draft: str) -> list[dict]:
    """"People also ask" questions whose content words the draft never uses.
    Lexical on purpose (lemma match) — it can miss a paraphrased answer, so
    the message names the missing words and lets the reader judge."""
    draft_lemmas = {opt_text.lemma(t) for t in re.findall(r"[a-z0-9]+", draft.lower())}
    skip = _tokens(keyword) | _QUESTION_WORDS
    out: list[dict] = []
    for q in questions:
        terms = {opt_text.lemma(t) for t in _tokens(q) - skip}
        if not terms:
            continue
        missing = sorted(t for t in terms if t not in draft_lemmas)
        if (len(terms) - len(missing)) / len(terms) >= PAA_ANSWERED_SHARE:
            continue
        out.append({
            "kind": "paa", "priority": 1.5,
            "message": f"People also ask: '{q}' — not answered (draft never mentions: {', '.join(missing)})",
        })
    return out


# ---------------------------------------------------------- cannibalization

def cannibalization(brand: dict, keyword: str, results: list[dict], checked_url: str = "") -> dict:
    """Is another page on this site already after the same query? Three
    sources, each its own evidence kind: the snapshot's own-domain SERP rows,
    the a2 site corpus (target_query / title) and Search Console's best query
    per page. The checked page itself is never evidence against itself."""
    checked = _page_key(checked_url) if checked_url else None
    evidence: list[dict] = []
    high = False
    own_rows = 0
    for r in results:
        if r.get("excluded") != "own domain":
            continue
        own_rows += 1
        if _same_page(checked, r.get("url", "")):
            continue
        rank = r.get("rank")
        evidence.append({"kind": "serp", "url": r.get("url", ""),
                         "detail": f"already ranks #{rank} for '{keyword}'"})
        high = high or (rank is not None and rank <= SERP_TOP)

    corpus_pages = (state.load(f"corpus-{brand['id']}") or {}).get("pages") or []
    for p in corpus_pages:
        url = p.get("url") or ""
        if _same_page(checked, url):
            continue
        ov, text = max(
            ((cannibal_overlap(keyword, p.get(field) or ""), p.get(field) or "")
             for field in ("target_query", "title")),
            key=lambda pair: pair[0],
        )
        if ov >= CORPUS_OVERLAP_EVIDENCE:
            evidence.append({"kind": "corpus", "url": url,
                             "detail": f"targets '{text}' — {round(ov * 100)}% overlap with '{keyword}'"})
            high = high or ov >= CORPUS_OVERLAP_HIGH

    gsc_pages = (state.load(f"pages-{brand['id']}") or {}).get("pages") or []
    for p in gsc_pages:
        best = p.get("best_query") or ""
        url = p.get("url") or p.get("path") or ""
        if not best or _same_page(checked, url):
            continue
        if cannibal_overlap(keyword, best) >= GSC_OVERLAP_HIGH:
            pos = p.get("position")
            where = f" at position {pos:g}" if isinstance(pos, (int, float)) else ""
            evidence.append({"kind": "gsc", "url": url,
                             "detail": f"already earns Search Console clicks for '{best}'{where} "
                                       f"({p.get('clicks', 0)} clicks)"})
            high = True

    domain = brand.get("domain") or "this site"
    if high:
        risk, note = "high", f"{len(evidence)} page(s) on {domain} already target this query — a new page would compete with them."
    elif evidence:
        risk, note = "medium", f"{len(evidence)} page(s) on {domain} overlap this query — check them before publishing a second one."
    elif corpus_pages or gsc_pages or own_rows:
        risk, note = "low", f"No other page on {domain} targets this query in the SERP, the site corpus or Search Console."
    else:
        risk, note = "unknown", (f"Site analysis has not run for this brand and no page of {domain} is in this SERP — "
                                 "run the SEO site scan to check for overlapping pages.")
    return {"risk": risk, "evidence": evidence, "note": note}


# ------------------------------------------------------------------ verdict

def verdict(report: dict | None, cannibal: dict, meta: dict,
            bands: dict[str, opt_structure.StructureBand], page_flags=()) -> dict:
    """The label a writer acts on, with the numbers that produced it."""
    reasons: list[str] = []
    n_ev = len(cannibal.get("evidence", []))
    degraded = not report or report.get("winners_median") is None or not meta.get("n_docs")
    if cannibal.get("risk") == "high":
        label = "likely cannibalizes"
        reasons.append(f"cannibalization risk high — {n_ev} page(s) on the site already target this query")
    elif degraded:
        label = "cannot tell"
        reasons.append(
            "no draft score" if not report
            else f"no usable winners to compare against ({meta.get('n_docs', 0)} usable)"
        )
    else:
        total, median, gaps = report["total"], report["winners_median"], report.get("gaps", [])
        label = "likely helps" if total >= median and len(gaps) <= HELPS_MAX_GAPS else "needs work"
    if report and report.get("winners_median") is not None:
        reasons.append(f"score {report['total']} vs winners' median {report['winners_median']}")
        reasons.append(f"{len(report.get('gaps', []))} gap(s) against the winners' profile")
    if cannibal.get("risk") == "medium":
        reasons.append(f"cannibalization risk medium — {n_ev} page(s) on the site overlap this query")
    reasons.extend(meta.get("warnings", []))

    confidence = "high"
    if (meta.get("warnings") or meta.get("volatility") == "volatile"
            or any(b.confidence != "high" for b in bands.values())):
        confidence = "medium"
    if (degraded or meta.get("degraded") or (report or {}).get("degraded")
            or any(b.confidence == "low" for b in bands.values())
            or set(page_flags) & {"thin", "language_mismatch", "markdown_fallback"}):
        confidence = "low"
    return {"label": label, "reasons": reasons, "confidence": confidence}


# -------------------------------------------------------------------- check

def check(brand: dict, *, url: str = "", draft: str = "", keyword: str = "",
          locale: str = "en-US", provider=None, embedder=None, fetch=None) -> dict:
    """One page (by URL) or one draft (markdown) against today's winners for
    its target query, plus the brand's own pages. Returns the whole analysis
    doc with a ``page_check`` block.

    ``ValueError`` for anything the caller can fix (both or neither input, an
    unfetchable or unreadable page, no derivable target query);
    ``CredentialMissing`` when the SERP provider has no key.
    """
    url, draft, keyword = url.strip(), draft.strip(), keyword.strip()
    if bool(url) == bool(draft):
        raise ValueError("send exactly one of url or draft")
    cfg = load_config()
    page_flags: list[str] = []
    source_url = ""
    if url:
        source_url, html = (fetch or sources.fetch_html)(url)
        lang = (locale.partition("-")[0] or "en").lower()
        cdoc = opt_extract.extract_document(html, cfg.extract, url=source_url, expected_lang=lang)
        if "unparseable" in cdoc.flags:
            raise ValueError(f"{source_url} is not parseable HTML")
        if "interstitial" in cdoc.flags:
            raise ValueError(f"{source_url} served a consent/sign-in wall instead of the page")
        draft_md, facts = html_to_markdown(html, cfg.extract)
        page_flags = list(cdoc.flags)
        if len(draft_md.split()) < cdoc.word_count / 2:
            draft_md = _markdown_fallback(draft_md, cdoc)
            page_flags.append("markdown_fallback")
        if keyword:
            source = "given"
        else:
            keyword = (target_query_from_title(facts["h1"], brand)
                       or target_query_from_title(facts["title"], brand))
            if not keyword:
                raise ValueError(f"could not derive a target query from the title of {source_url} — pass keyword")
            source = "page_title"
    else:
        draft_md = draft
        if keyword:
            source = "given"
        else:
            heading = next(
                (line.lstrip("#").strip() for line in draft.splitlines() if line.lstrip().startswith("#")), "",
            )
            keyword = " ".join(heading.lower().split())
            if not keyword:
                raise ValueError("keyword required — the draft has no heading to derive one from")
            source = "draft_heading"
    if not draft_md.strip():
        raise ValueError(f"{source_url or 'the draft'} has no readable content to score")

    doc = opt_pipeline.analyze(
        keyword, locale, draft_md, own_domain=brand.get("domain", ""),
        brand_id=brand["id"], provider=provider, embedder=embedder,
    )
    report = doc.get("last_report") or {}
    cannibal = cannibalization(brand, keyword, doc["results"], source_url)
    bands = {f: opt_structure.StructureBand(**b) for f, b in doc["structure_bands"].items()}
    block = {
        "source_url": source_url,
        "target_query": keyword,
        "target_query_source": source,
        "verdict": verdict(report or None, cannibal, doc["meta"], bands, page_flags),
        "pros": list(report.get("strengths", [])),
        "cons": list(report.get("gaps", [])) + paa_unanswered(doc["meta"].get("paa", []), keyword, draft_md),
        "cannibalization": cannibal,
        "page_flags": page_flags,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "disclaimer": opt_score.DISCLAIMER,
    }
    return opt_pipeline.attach_page_check(brand["id"], doc["meta"]["analysis_id"], block)
