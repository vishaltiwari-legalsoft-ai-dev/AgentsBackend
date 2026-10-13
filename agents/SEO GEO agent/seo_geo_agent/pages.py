"""Page-level intelligence: GA traffic + Search Console + on-page health per page."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

from . import audit, state
from .sources import CredentialMissing, llm_text

MAX_AI_PAGES = 25
SYSTEM = ("You are an SEO consultant. For each page return ONE imperative sentence: "
          "the single most impactful next action. Strict JSON only: "
          '[{"path": str, "recommendation": str}]')

TITLE_MAX = 60
META_MAX = 160
THIN_WORDS = 300

NO_REC_NOTE = "AI recommendations unavailable — showing rule-based advice"
NOT_CRAWLED_FLAG = "not-crawled"

#: ``rec_chip`` ids: a rule verdict that reads the same on every row it applies
#: to, so the row carries a stable id the console renders as a chip instead of
#: the same sentence repeated down the table. Every rule verdict is one; the
#: only sentences left are the AI pass's page-specific advice. The row's
#: ``word_count`` is there for a "thin" chip that wants to say how thin.
#:
#: ``not-crawled``: known to GA/Search Console but not in the site crawl —
#: re-run the site analysis to audit it. ``meta-long``: the meta description
#: runs past 160 characters and gets truncated.
NOT_CRAWLED_CHIP = NOT_CRAWLED_FLAG
THIN_CHIP = "thin-content"
META_LONG_CHIP = "meta-long"
HEALTHY_CHIP = "healthy"
REC_CHIPS = (NOT_CRAWLED_CHIP, THIN_CHIP, META_LONG_CHIP, HEALTHY_CHIP)

#: The rule sentence behind each flag — also the text AI advice is compared
#: against: advice that only says this again, in other words, is dropped.
FLAG_TEXT = {
    "no-title": "Write a title tag targeting its main query.",
    "no-meta": "Add a meta description — the snippet is unsold.",
    "thin": "Expand this page past 300 words; it can't rank as-is.",
    "title-long": "Shorten the title under 60 characters.",
    "no-h1": "Add an H1 matching the target query.",
    "images-no-alt": "Add alt text to images.",
    "meta-long": "Tighten the meta description under 160 characters — long snippets get truncated.",
}
_FLAG_SYNONYMS = {
    "no-title": {"title", "tag", "missing", "keyword", "main"},
    "title-long": {"title", "tag", "shorten", "shorter", "trim", "long", "length", "character",
                   "truncate", "truncated", "front-load", "keyword", "cut", "reduce", "concise"},
    "no-meta": {"meta", "description", "snippet", "missing", "compelling", "click", "ctr", "pitch",
                "tag", "sell"},
    "meta-long": {"meta", "description", "tighten", "shorten", "shorter", "trim", "long", "length",
                  "character", "snippet", "truncate", "concise"},
    "thin": {"expand", "thin", "word", "content", "length", "longer", "depth", "deepen", "rank",
             "flesh", "substantive", "copy"},
    "no-h1": {"h1", "heading", "missing", "match", "main", "tag", "clear"},
    "images-no-alt": {"alt", "text", "image", "describe", "descriptive", "attribute", "missing",
                      "tag", "accessibility"},
}
FLAG_VOCAB = {f: audit.content_tokens(t) | _FLAG_SYNONYMS[f] for f, t in FLAG_TEXT.items()}
#: Flags the technical audit already reports as ONE grouped site finding. On a
#: page row they are chips — the same sentence on forty rows is noise.
AUDIT_GROUPED_FLAGS = frozenset({"no-title", "title-long", "no-meta", "no-h1", "images-no-alt"})
REWORD_SHARE = 0.6


def _path(url: str) -> str:
    return (urlparse(url).path or "/") if "//" in url else (url or "/")


def _flags(entry: dict, template_srcs: set[str] = frozenset()) -> list[str]:
    """On-page health flags from a corpus entry — every key read with a default
    so older cached entries (missing meta_description/h1_count/images_no_alt) don't crash.

    ``images-no-alt`` means an image of THIS page's own: a header/footer image
    missing alt on most pages is one template finding in the audit, not a chip
    on every row. Entries crawled before srcs were stored fall back to the count."""
    flags: list[str] = []
    title = (entry.get("title") or "").strip()
    meta = (entry.get("meta_description") or "").strip()
    word_count = entry.get("word_count") or 0
    h1_count = entry.get("h1_count", 0) or 0
    count = entry.get("images_no_alt", 0) or 0
    srcs = entry.get("images_no_alt_srcs")
    images_no_alt = (bool(count) if srcs is None
                     else audit.has_page_specific_missing_alt(count, srcs, template_srcs))

    if not title:
        flags.append("no-title")
    elif len(title) > TITLE_MAX:
        flags.append("title-long")

    if not meta:
        flags.append("no-meta")
    elif len(meta) > META_MAX:
        flags.append("meta-long")

    if word_count < THIN_WORDS:
        flags.append("thin")

    if not h1_count:
        flags.append("no-h1")

    if images_no_alt:
        flags.append("images-no-alt")

    return flags


def _rule_rec(page: dict) -> tuple[str, str, str | None]:
    """Deterministic advice when AI is unavailable (or for pages outside the
    top-traffic slice) -> ``(recommendation, rec_source, rec_chip)``.

    Every rule verdict would be the same sentence on every row it fits, so each
    is a chip: ``("", "flags", chip)`` with one of :data:`REC_CHIPS`, in the
    precedence the sentences had — not crawled, thin content, long meta
    description, then healthy when nothing is flagged. A flag the audit already
    groups (title, meta, H1, alt) is its own chip in ``flags`` (``rec_chip``
    None). Page-specific advice comes only from the AI pass, as a sentence.
    """
    flags = page.get("flags") or []
    if NOT_CRAWLED_FLAG in flags:
        return "", "flags", NOT_CRAWLED_CHIP
    if "thin" in flags:
        return "", "flags", THIN_CHIP
    if "meta-long" in flags:
        return "", "flags", META_LONG_CHIP
    if any(f in AUDIT_GROUPED_FLAGS for f in flags):
        return "", "flags", None
    return "", "flags", HEALTHY_CHIP


def rewords_flags(recommendation: str, flags: list[str]) -> bool:
    """True when AI advice only restates the page's own flag chips.

    Deterministic: the share of the advice's content words that the flags'
    rule text (plus its synonyms) already accounts for. "Shorten the title to
    under 60 characters" on a ``title-long`` page is the chip again; "Lead the
    title with 'legal VA pricing'" names something the chip does not."""
    vocab: set[str] = set()
    for f in flags:
        vocab |= FLAG_VOCAB.get(f, set())
    if not vocab:
        return False
    return audit.coverage(audit.content_tokens(recommendation), vocab) >= REWORD_SHARE


def _ground_recs(recs: dict[str, str], rows: dict[str, dict], corpus_by_path: dict[str, dict],
                 site_types) -> tuple[dict[str, str], dict]:
    """Keep AI advice that says something the page row does not already say.

    Dropped (and recorded): advice for a path that was not sent, advice that
    re-words the page's flags, and schema advice that does not fit the page
    (``audit.schema_claim_problem``).
    """
    kept: dict[str, str] = {}
    dropped: list[dict] = []
    for path, rec in recs.items():
        row = rows.get(path)
        if row is None or path not in corpus_by_path:
            dropped.append({"path": path, "reason": "unknown-page", "recommendation": rec[:200],
                            "detail": "not one of the crawled pages sent for advice"})
            continue
        if rewords_flags(rec, row.get("flags") or []):
            dropped.append({"path": path, "reason": "repeats-flags", "recommendation": rec[:200],
                            "detail": f"restates {', '.join(row['flags'])}"})
            continue
        problem = audit.schema_claim_problem(rec, corpus_by_path[path], site_types)
        if problem:
            dropped.append({"path": path, "reason": "schema-not-fit", "recommendation": rec[:200],
                            "detail": problem[:200]})
            continue
        kept[path] = rec
    return kept, {"kept": len(kept), "dropped": dropped}


def _ai_recs(pages: list[dict]) -> dict[str, str]:
    """One llm_text call covering the top-traffic pages. Raises CredentialMissing
    (offline/provider failure) or ValueError (bad JSON shape) — caller degrades."""
    lines = "\n".join(
        f"- path: {p['path']} | title: {p['title'] or '(missing)'} | views: {p['views']} | "
        f"clicks: {p['clicks']} | flags: {', '.join(p['flags']) or 'none'}"
        for p in pages
    )
    raw = llm_text(SYSTEM, f"Pages:\n{lines}")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("AI recommendations: expected a JSON list")
    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        path, rec = item.get("path"), item.get("recommendation")
        if isinstance(path, str) and isinstance(rec, str) and rec.strip():
            out[path] = rec.strip()[:300]
    return out


def _entry(merged: dict, path: str) -> dict:
    return merged.setdefault(path, {
        "path": path, "url": None, "title": None,
        "views": 0, "sessions": 0, "engagement_rate": 0.0,
        "clicks": 0, "impressions": 0, "position": None,
        "best_query": None, "flags": [], "recommendation": "", "ai": False,
        "rec_source": "flags", "rec_chip": None, "word_count": 0,
    })


def build_page_intel(brand: dict, corpus_pages: list[dict], ga_pages: list[dict], gsc_rows: list,
                      data_notes: list[str] | None = None) -> dict:
    """Merge GA + Search Console + on-page health into one per-page table, keyed
    by URL path. A page appears if it's in ANY source; missing-source fields
    default to 0/None. Adds one best-effort AI recommendation pass (top-traffic
    pages only), falling back to honest heuristics for the rest / on failure.
    Pages known only from GA/GSC — never crawled into the corpus — have no
    on-page facts to audit, so they're flagged ``not-crawled`` instead of
    silently passing as healthy, and are never sent to the AI pass.
    ``data_notes`` carries upstream GA/GSC degradation notes from the caller so
    a zeroed-out metric always shows the reason it's zero, not just a number."""
    merged: dict[str, dict] = {}
    crawled: set[str] = set()
    corpus_by_path: dict[str, dict] = {}
    template = set(audit.template_srcs([cp.get("images_no_alt_srcs") or [] for cp in corpus_pages]))

    for cp in corpus_pages:
        path = _path(cp.get("url", "") or "")
        crawled.add(path)
        corpus_by_path[path] = cp
        e = _entry(merged, path)
        e["url"] = cp.get("url")
        e["title"] = cp.get("title") or None
        e["word_count"] = cp.get("word_count", 0) or 0
        e["flags"] = _flags(cp, template)

    for gp in ga_pages:
        path = _path(gp.get("path", "") or "/")
        e = _entry(merged, path)
        e["views"] = gp.get("views", 0) or 0
        e["sessions"] = gp.get("sessions", 0) or 0
        e["engagement_rate"] = gp.get("engagement_rate", 0.0) or 0.0

    by_path: dict[str, list] = {}
    for row in gsc_rows:
        by_path.setdefault(_path(row.page), []).append(row)
    for path, rows in by_path.items():
        e = _entry(merged, path)
        e["clicks"] = sum(r.clicks for r in rows)
        e["impressions"] = sum(r.impressions for r in rows)
        best = max(rows, key=lambda r: (r.clicks, r.impressions))
        e["position"] = best.position
        e["best_query"] = best.query

    for path, e in merged.items():
        if path not in crawled:
            e["flags"] = [NOT_CRAWLED_FLAG]

    merged_list = sorted(merged.values(), key=lambda p: (-p["views"], -p["clicks"]))
    ai_candidates = [p for p in merged_list if p["path"] in crawled][:MAX_AI_PAGES]

    notes: list[str] = list(data_notes or [])
    ai_used = False
    recs: dict[str, str] = {}
    grounding: dict = {"kept": 0, "dropped": []}
    if ai_candidates:
        try:
            raw_recs = _ai_recs(ai_candidates)
            ai_used = True
        except (CredentialMissing, ValueError):  # degrade to heuristics, never crash the run
            notes.append(NO_REC_NOTE)
        else:
            # Schema state is "unknown" for corpora crawled before it was stored —
            # the site-wide schema rules then stay silent instead of guessing.
            known = all("schema_types" in cp for cp in corpus_pages)
            site_types = ({t for cp in corpus_pages for t in cp.get("schema_types") or []}
                          if corpus_pages and known else None)
            recs, grounding = _ground_recs(raw_recs, merged, corpus_by_path, site_types)

    for p in merged_list:
        p["ai"] = p["path"] in recs
        if p["ai"]:
            p["recommendation"], p["rec_source"], p["rec_chip"] = recs[p["path"]], "ai", None
        else:
            p["recommendation"], p["rec_source"], p["rec_chip"] = _rule_rec(p)

    doc = {"brand_id": brand["id"], "at": date.today().isoformat(),
           "ai": ai_used, "notes": notes, "grounding": grounding, "pages": merged_list}
    state.save(f"pages-{brand['id']}", doc)
    return doc


def latest(brand_id: str) -> dict | None:
    return state.load(f"pages-{brand_id}")
