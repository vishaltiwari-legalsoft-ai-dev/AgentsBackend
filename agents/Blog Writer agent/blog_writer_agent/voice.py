"""Brand voice study: read the brand's published posts and store how they write.

The profile persists per brand (``voice-{brand_id}``) and feeds every draft,
polish, and revision prompt so new posts sound like the brand, grounded in what
its published posts actually do — never an invented persona.
"""
from __future__ import annotations

from datetime import datetime, timezone

from seo_geo_agent import sources

from blog_writer_agent import llm as bw_llm
from blog_writer_agent import state

STUDY_CAP = 12     # posts actually read per study
_EXCERPT = 1500    # chars of body text per post fed to the analyst

_STUDY_SYSTEM = (
    "You are a brand voice analyst. From these published blog posts, describe "
    "how this brand writes so another writer can match it. Return JSON with "
    'keys: "tone", "pov", "typical_structure", "openers", "headings_style", '
    '"sentence_rhythm", "vocabulary", "cta_style", "topics", "dos" (list), '
    '"donts" (list), "summary" (3-4 sentences). Ground every observation in '
    "what the posts actually do; do not invent."
)


def study(brand: dict, inventory: dict | None, *, fetch=None, llm=None) -> dict:
    posts = (inventory or {}).get("posts") or []
    if not posts:
        raise ValueError("no blog inventory yet — scan the site first")
    fetch = fetch or sources.fetch_page
    llm = llm or bw_llm.llm_json

    domain = brand.get("domain", "")
    read: list[str] = []
    excerpts: list[str] = []
    measured: list[dict] = []
    h2_samples: list[str] = []
    for post in posts[:STUDY_CAP]:
        try:
            page = fetch(post["url"])
        except Exception:  # noqa: BLE001 — a dead page is skipped, not fatal
            continue
        page = page if isinstance(page, dict) else page.__dict__
        text = (page.get("text") or "").strip()
        if not text:
            continue
        read.append(post["url"])
        h2s = list(page.get("h2") or [])
        own_links = [u for u in (page.get("internal_links") or []) if domain and domain in u]
        measured.append(
            {
                "words": int(page.get("word_count") or 0),
                "h2": len(h2s),
                "h3": len(page.get("h3") or []),
                "internal_links": len(own_links),
            }
        )
        h2_samples.extend(h2s[:4])
        outline = " | ".join(h2s[:10])
        excerpts.append(
            f"### {page.get('title') or post['title']}\n"
            f"H2 outline: {outline}\n{text[:_EXCERPT]}"
        )
    if not excerpts:
        raise ValueError("could not read any posts — the pages returned no text")

    profile = llm(
        _STUDY_SYSTEM,
        f"Brand: {brand.get('name', brand['id'])} ({domain})\n\n" + "\n\n".join(excerpts),
    )
    if not isinstance(profile, dict) or not str(profile.get("summary", "")).strip():
        raise ValueError("voice study came back unusable — retry the study")

    doc = {
        "brand_id": brand["id"],
        "studied": datetime.now(timezone.utc).isoformat(),
        "posts_read": read,
        "count": len(read),
        "profile": profile,
        "structure": _structure_stats(measured, h2_samples),
    }
    state.save(f"voice-{brand['id']}", doc)
    return state.load(f"voice-{brand['id']}") or doc


def _median(values: list[int]) -> int:
    ordered = sorted(v for v in values if v > 0)
    return ordered[len(ordered) // 2] if ordered else 0


def _structure_stats(measured: list[dict], h2_samples: list[str]) -> dict:
    """Deterministic structure numbers measured from the posts — not LLM opinion."""
    words = _median([m["words"] for m in measured])
    h2 = _median([m["h2"] for m in measured])
    return {
        "posts_measured": len(measured),
        "median_words": words,
        "median_h2_sections": h2,
        "median_internal_links": _median([m["internal_links"] for m in measured]),
        "words_per_section": round(words / h2) if h2 else 0,
        "uses_h3": sum(1 for m in measured if m["h3"] > 0),
        "h2_samples": h2_samples[:10],
    }


def latest(brand_id: str) -> dict | None:
    return state.load(f"voice-{brand_id}")


def digest(voice: dict | None) -> str:
    """Compact profile text for prompts. Empty string when no study exists."""
    if not voice:
        return ""
    profile = voice.get("profile", {})
    lines = [f"{k}: {v}" for k, v in profile.items() if isinstance(v, str) and v.strip()]
    for key in ("dos", "donts"):
        values = profile.get(key) or []
        if values:
            lines.append(f"{key}: " + "; ".join(str(v) for v in values))
    stats = voice.get("structure") or {}
    if stats.get("median_words"):
        lines.append(
            f"measured structure (from {stats['posts_measured']} posts): "
            f"~{stats['median_words']} words total, {stats['median_h2_sections']} H2 sections "
            f"of ~{stats['words_per_section']} words each, "
            f"{stats['median_internal_links']} internal links woven into the body"
        )
        if stats.get("h2_samples"):
            lines.append("real H2 examples: " + " | ".join(stats["h2_samples"][:6]))
    return "\n".join(lines)
