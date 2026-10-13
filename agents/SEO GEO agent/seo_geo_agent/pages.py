"""Page-level intelligence: GA traffic + Search Console + on-page health per page."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

from . import audit, sources, state
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

# ------------------------------ advice categories ------------------------------
# Text similarity to the chip ("Shorten the title…" vs "title too long") missed
# rewordings that carry a new title: the line was still about the title, on a
# row whose chip already says the title is the problem. So every AI line is
# CATEGORISED, and the category decides: hygiene the row or the audit owns is
# dropped; a site-wide move (internal links, a clear next step) goes to ONE
# place — the Fix list — instead of six rows.

REC_CATEGORIES = ("title", "meta", "h1", "alt", "schema", "cta", "internal-links", "content", "other")
HYGIENE_CATEGORIES = frozenset({"title", "meta", "h1", "alt", "schema"})
#: Categories that are one site-wide move when several rows (or a site to-do) say them.
COLLAPSE_CATEGORIES = ("internal-links", "cta")
COLLAPSE_MIN_ROWS = 3
_CATEGORY_RULES = (
    ("title", re.compile(r"(?<!job )(?<!sub)\btitle(?: tags?)?s?\b", re.I)),
    ("meta", re.compile(r"\bmeta(?: descriptions?| tags?)?\b|\bsnippets?\b", re.I)),
    ("h1", re.compile(r"\bh1s?\b|\bmain heading\b", re.I)),
    ("alt", re.compile(r"\balt(?:[- ]text| attributes?| tags?)\b|\bimage descriptions?\b", re.I)),
    ("schema", audit._SCHEMA_MENTION),
    ("cta", re.compile(r"\bcalls?[- ]to[- ]action\b|\bctas?\b|\bforms?\b|\bclick[- ]to[- ]call\b|"
                       r"\bbook(?:ing)? (?:a |an )?(?:call|demo|meeting|consultation)\b|\bcalendly\b|"
                       r"\bbuttons?\b|\bnext step\b|\bphone numbers?\b", re.I)),
    ("internal-links", re.compile(r"\binternal(?:ly)? link|\bcross[- ]?link|\binterlink|"
                                  r"\blinks? (?:to|from|pointing)\b|\blinking (?:to|from)\b", re.I)),
    ("content", re.compile(r"\b(?:content|expand|sections?|faqs?|case stud(?:y|ies)|testimonials?|"
                           r"videos?|screenshots?|examples?|descriptions?|trust signals?|publish|"
                           r"articles?|blog|guides?|words?|copy|proof|reviews?|statistics|samples?|"
                           r"demonstrations?)\b", re.I)),
)
#: The row chips that already say a hygiene category's problem.
_CATEGORY_FLAGS = {"title": {"no-title", "title-long"}, "meta": {"no-meta", "meta-long"},
                   "h1": {"no-h1"}, "alt": {"images-no-alt"}, "schema": set()}
_SUGGESTION_MAX = {"title": TITLE_MAX, "meta": META_MAX}
#: A quoted span: opens after a space / "(" / ":" and closes before a space or
#: punctuation, so the apostrophe in "Legal Soft's" is not a closing quote.
_QUOTED = re.compile(r"(?:(?<=\s)|^|(?<=[(:]))['‘“\"](.+?)['’”\"](?=[\s.,;:!?)]|$)")


def rec_category(text: str) -> str:
    """What one piece of advice is about — one of :data:`REC_CATEGORIES`."""
    for name, rx in _CATEGORY_RULES:
        if rx.search(text or ""):
            return name
    return "other"


def suggested_text(rec: str) -> str | None:
    """The new title / meta a line proposes: its last quoted span."""
    spans = _QUOTED.findall(rec or "")
    return spans[-1].strip() if spans else None


def _audit_category(issue: dict) -> str | None:
    """The hygiene category an audit finding owns, if any. Decorative alt
    text is information, not a finding about the page."""
    kind, name = issue.get("kind"), str(issue.get("issue") or "").lower()
    if kind in ("schema-fit", "schema-error"):
        return "schema"
    if kind in ("template-alt", "page-alt"):
        return "alt"
    if kind in ("title-long", "title-template") or "title" in name:
        return "title"
    if "meta description" in name:
        return "meta"
    if "h1" in name:
        return "h1"
    return None


def audit_owned(report: dict | None) -> dict[str, set[str]]:
    """``{category: {path, …}}`` — the pages each hygiene category is already
    an audit finding for."""
    owned: dict[str, set[str]] = {}
    for issue in (report or {}).get("issues") or []:
        category = _audit_category(issue)
        if not category:
            continue
        urls = list(issue.get("pages") or []) + [e.get("page") for e in issue.get("evidence") or []
                                                  if isinstance(e, dict)]
        owned.setdefault(category, set()).update(_path(u) for u in urls if isinstance(u, str) and u)
    return owned


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
                 site_types, owned: dict[str, set[str]] | None = None) -> tuple[dict[str, str], dict]:
    """Keep AI advice that says something the page row does not already say.

    Dropped (and recorded): advice for a path that was not sent; a proposed
    title over 60 characters or meta over 160 (``suggestion-too-long``); a
    hygiene line (title / meta / H1 / alt / schema) when the row's chip or
    the audit (``owned``, from :func:`audit_owned`) already reports it, or
    that re-words the flags (``repeats-flags``); and schema advice that does
    not fit the page (``audit.schema_claim_problem``). Each kept line's
    category rides in ``grounding["categories"]``.
    """
    owned = owned or {}
    kept: dict[str, str] = {}
    categories: dict[str, str] = {}
    dropped: list[dict] = []

    def drop(path: str, reason: str, rec: str, detail: str) -> None:
        dropped.append({"path": path, "reason": reason, "recommendation": rec[:200], "detail": detail[:200]})

    for path, rec in recs.items():
        row = rows.get(path)
        if row is None or path not in corpus_by_path:
            drop(path, "unknown-page", rec, "not one of the crawled pages sent for advice")
            continue
        flags = row.get("flags") or []
        category = rec_category(rec)
        limit = _SUGGESTION_MAX.get(category)
        proposed = suggested_text(rec) if limit else None
        if proposed and len(proposed) > limit:
            drop(path, "suggestion-too-long", rec,
                 f"the suggested {category} is {len(proposed)} characters — over the {limit} limit")
            continue
        if category in HYGIENE_CATEGORIES:
            chip = sorted(set(flags) & _CATEGORY_FLAGS[category])
            if chip or path in owned.get(category, set()):
                drop(path, "repeats-flags", rec,
                     f"{category} advice — " + (f"the row already shows {', '.join(chip)}" if chip
                                               else "the technical audit already reports it"))
                continue
        if rewords_flags(rec, flags):
            drop(path, "repeats-flags", rec, f"restates {', '.join(flags)}")
            continue
        problem = audit.schema_claim_problem(rec, corpus_by_path[path], site_types)
        if problem:
            drop(path, "schema-not-fit", rec, problem)
            continue
        kept[path] = rec
        categories[path] = category
    return kept, {"kept": len(kept), "dropped": dropped, "categories": categories}


def _collapse(kept: dict[str, str], categories: dict[str, str], order: list[str],
              site_todos: list[dict] | None, dropped: list[dict]) -> list[dict]:
    """Site-wide advice in ONE place -> ``fix_items``; the rows stop saying it.

    For each of :data:`COLLAPSE_CATEGORIES`: when :data:`COLLAPSE_MIN_ROWS`+
    rows give it, or a site to-do on the Fix list is already that move, the
    rows' lines are dropped (``moved-to-fix-list``) and one fix item names the
    affected paths and the site to-do (``todo_id``, None = a new item)."""
    held: dict[str, dict] = {}
    for todo in site_todos or []:
        category = rec_category(f"{todo.get('action', '')} {todo.get('why', '')}")
        if category in COLLAPSE_CATEGORIES:
            held.setdefault(category, todo)
    items: list[dict] = []
    for category in COLLAPSE_CATEGORIES:
        paths = [p for p in order if p in kept and categories.get(p) == category]
        todo = held.get(category)
        if not paths or (len(paths) < COLLAPSE_MIN_ROWS and todo is None):
            continue
        where = f"Fix-list item “{todo['action'][:80]}”" if todo else "one Fix-list item"
        for p in paths:
            dropped.append({"path": p, "reason": "moved-to-fix-list", "recommendation": kept.pop(p)[:200],
                            "detail": f"{category} advice on {len(paths)} pages — listed once, as the {where}"[:200]})
        items.append({"category": category, "paths": paths, "todo_id": todo["id"] if todo else None})
    return items


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
        "rec_source": "flags", "rec_chip": None, "rec_category": None, "word_count": 0,
    })


def build_page_intel(brand: dict, corpus_pages: list[dict], ga_pages: list[dict], gsc_rows: list,
                      data_notes: list[str] | None = None, *, site_todos: list[dict] | None = None,
                      audit_report: dict | None = None) -> dict:
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
    categories: dict[str, str] = {}
    fix_items: list[dict] = []
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
            recs, grounding = _ground_recs(raw_recs, merged, corpus_by_path, site_types,
                                           audit_owned(audit_report))
            categories = grounding.pop("categories")
            fix_items = _collapse(recs, categories, [p["path"] for p in merged_list], site_todos,
                                  grounding["dropped"])
            grounding["kept"] = len(recs)

    for p in merged_list:
        p["ai"] = p["path"] in recs
        if p["ai"]:
            p["recommendation"], p["rec_source"], p["rec_chip"] = recs[p["path"]], "ai", None
            p["rec_category"] = categories.get(p["path"])
        else:
            p["recommendation"], p["rec_source"], p["rec_chip"] = _rule_rec(p)
            p["rec_category"] = None

    doc = {"brand_id": brand["id"], "at": date.today().isoformat(),
           "ai": ai_used, "notes": notes, "grounding": grounding, "pages": merged_list,
           "fix_items": fix_items}
    state.save(f"pages-{brand['id']}", doc)
    return doc


def latest(brand_id: str) -> dict | None:
    doc = state.load(f"pages-{brand_id}")
    if doc:
        # A doc stored before notes were made plain still carries raw exception text.
        doc["notes"] = [sources.plain_note(n, log=False) for n in doc.get("notes") or []
                        if isinstance(n, str)]
    return doc
