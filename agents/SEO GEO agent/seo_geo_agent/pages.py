"""Page-level intelligence: GA traffic + Search Console + on-page health per page."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

from . import state
from .sources import CredentialMissing, llm_text

MAX_AI_PAGES = 25
SYSTEM = ("You are an SEO consultant. For each page return ONE imperative sentence: "
          "the single most impactful next action. Strict JSON only: "
          '[{"path": str, "recommendation": str}]')

PAGE_FLAGS = ("no-title", "title-long", "no-meta", "meta-long", "thin", "no-h1", "images-no-alt")

TITLE_MAX = 60
META_MAX = 160
THIN_WORDS = 300

NO_REC_NOTE = "AI recommendations unavailable — showing rule-based advice"


def _path(url: str) -> str:
    return (urlparse(url).path or "/") if "//" in url else (url or "/")


def _flags(entry: dict) -> list[str]:
    """On-page health flags from a corpus entry — every key read with a default
    so older cached entries (missing meta_description/h1_count/images_no_alt) don't crash."""
    flags: list[str] = []
    title = (entry.get("title") or "").strip()
    meta = (entry.get("meta_description") or "").strip()
    word_count = entry.get("word_count") or 0
    h1_count = entry.get("h1_count", 0) or 0
    images_no_alt = entry.get("images_no_alt", 0) or 0

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


def _heuristic_rec(page: dict) -> str:
    """Deterministic, honest recommendation when AI is unavailable (or for pages
    outside the top-traffic slice the AI call covers)."""
    flags = page.get("flags") or []
    if "no-title" in flags:
        return "Write a title tag targeting its main query."
    if "no-meta" in flags:
        return "Add a meta description — the snippet is unsold."
    if "thin" in flags:
        return "Expand this page past 300 words; it can't rank as-is."
    if "title-long" in flags:
        return "Shorten the title under 60 characters."
    if "no-h1" in flags:
        return "Add an H1 matching the target query."
    if "images-no-alt" in flags:
        return "Add alt text to images."
    return "Healthy — keep it fresh and add internal links to weaker pages."


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
        "best_query": None, "flags": [], "recommendation": "",
        "word_count": 0,
    })


def build_page_intel(brand: dict, corpus_pages: list[dict], ga_pages: list[dict], gsc_rows: list) -> dict:
    """Merge GA + Search Console + on-page health into one per-page table, keyed
    by URL path. A page appears if it's in ANY source; missing-source fields
    default to 0/None. Adds one best-effort AI recommendation pass (top-traffic
    pages only), falling back to honest heuristics for the rest / on failure."""
    merged: dict[str, dict] = {}

    for cp in corpus_pages:
        path = _path(cp.get("url", "") or "")
        e = _entry(merged, path)
        e["url"] = cp.get("url")
        e["title"] = cp.get("title") or None
        e["word_count"] = cp.get("word_count", 0) or 0
        e["flags"] = _flags(cp)

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

    merged_list = sorted(merged.values(), key=lambda p: (-p["views"], -p["clicks"]))

    notes: list[str] = []
    ai_used = True
    try:
        recs = _ai_recs(merged_list[:MAX_AI_PAGES])
    except (CredentialMissing, ValueError):  # degrade to heuristics, never crash the run
        ai_used = False
        notes.append(NO_REC_NOTE)
        recs = {}

    for p in merged_list:
        p["recommendation"] = recs.get(p["path"]) or _heuristic_rec(p)

    doc = {"brand_id": brand["id"], "at": date.today().isoformat(),
           "ai": ai_used, "notes": notes, "pages": merged_list}
    state.save(f"pages-{brand['id']}", doc)
    return doc


def latest(brand_id: str) -> dict | None:
    return state.load(f"pages-{brand_id}")
