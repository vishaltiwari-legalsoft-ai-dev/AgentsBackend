"""Per-brand SEO insights + to-do list where every item has an estimated traffic gain.

The estimate model is deliberately simple and honest: a public CTR-by-position
curve applied to the query's own impressions. Every number the dashboard shows
is labelled an estimate; the goal is ranking the work, not forecasting revenue.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

from . import state, topics
from .sources import CredentialMissing, QueryStat, gsc_fetch

# Aggregate organic CTR by position (rounded from public CTR studies). Position
# 11+ uses the flat tail — precision there doesn't change any ranking decision.
CTR_BY_POS = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.069, 6: 0.052, 7: 0.041, 8: 0.032, 9: 0.027, 10: 0.023}
TAIL_CTR = 0.015

MIN_IMPRESSIONS = 100          # ignore queries too small to move the needle
DECAY_DROP = 0.30              # a page is "decaying" when clicks fall 30%+
MAX_TODOS = 25                 # dashboard shows work, not a data dump

DEFAULT_BRANDS = [
    {
        "id": "legalsoft",
        "name": "Legal Soft",
        "domain": "legalsoft.com",
        "gsc_property": "sc-domain:legalsoft.com",
        "seeds": ["legal virtual assistant", "law firm answering service"],
        "enabled": True,
    }
]


def ctr_at(position: float) -> float:
    return CTR_BY_POS.get(max(1, round(position)), TAIL_CTR)


# ------------------------------- brand registry -------------------------------

def list_brands() -> list[dict]:
    doc = state.load("brands")
    return doc["brands"] if doc and doc.get("brands") else [dict(b) for b in DEFAULT_BRANDS]


def upsert_brand(brand: dict) -> list[dict]:
    brands = list_brands()
    brands = [b for b in brands if b["id"] != brand["id"]] + [brand]
    brands.sort(key=lambda b: b["name"].lower())
    state.save("brands", {"brands": brands})
    return brands


def delete_brand(brand_id: str) -> list[dict]:
    brands = [b for b in list_brands() if b["id"] != brand_id]
    state.save("brands", {"brands": brands})
    state.delete(f"run-{brand_id}")
    state.delete(f"todos-{brand_id}")
    return brands


# ------------------------------- to-do building -------------------------------

def todo_id(brand_id: str, kind: str, page: str, query: str) -> str:
    """Stable across runs so a to-do keeps its assigned/done status after a refresh."""
    return hashlib.sha1(f"{brand_id}|{kind}|{page}|{query}".encode()).hexdigest()[:12]


def _todo(brand_id: str, kind: str, row: QueryStat, action: str, why: str, gain: float) -> dict:
    return {
        "id": todo_id(brand_id, kind, row.page, row.query),
        "kind": kind,
        "page": row.page,
        "query": row.query,
        "action": action,
        "why": why,
        "est_monthly_clicks": max(1, round(gain)),
        "position": round(row.position, 1),
        "impressions": row.impressions,
        "status": "todo",
    }


def build_todos(brand_id: str, rows: list[QueryStat], prev_rows: list[QueryStat]) -> list[dict]:
    todos: list[dict] = []
    for r in rows:
        if r.impressions < MIN_IMPRESSIONS:
            continue
        # Striking distance: page 1-2 but below the fold — content refresh moves it.
        if 4 <= r.position <= 15:
            target = max(3, round(r.position) - 3)
            gain = r.impressions * (ctr_at(target) - ctr_at(r.position))
            if gain >= 5:
                todos.append(_todo(
                    brand_id, "striking", r,
                    f"Refresh the content and add internal links for '{r.query}'",
                    f"Ranks #{round(r.position)} with {r.impressions:,} monthly impressions — "
                    f"moving to #{target} is a realistic content fix",
                    gain,
                ))
        # CTR gap: ranks well but the title/meta isn't earning the clicks it should.
        elif r.position <= 6 and r.ctr < 0.5 * ctr_at(r.position):
            gain = r.impressions * (0.8 * ctr_at(r.position) - r.ctr)
            if gain >= 5:
                todos.append(_todo(
                    brand_id, "ctr_gap", r,
                    f"Rewrite the title and meta description targeting '{r.query}'",
                    f"Ranks #{round(r.position)} but gets {r.ctr:.1%} CTR vs ~{ctr_at(r.position):.0%} "
                    "expected at that spot — the snippet isn't selling the click",
                    gain,
                ))

    # Decaying pages: clicks dropped vs the prior period — refresh recovers most of it.
    prev_by_page: dict[str, int] = {}
    for r in prev_rows:
        prev_by_page[r.page] = prev_by_page.get(r.page, 0) + r.clicks
    now_by_page: dict[str, int] = {}
    for r in rows:
        now_by_page[r.page] = now_by_page.get(r.page, 0) + r.clicks
    for page, prev_clicks in prev_by_page.items():
        now_clicks = now_by_page.get(page, 0)
        if prev_clicks >= 30 and now_clicks < prev_clicks * (1 - DECAY_DROP):
            top = max((r for r in rows if r.page == page), key=lambda r: r.impressions, default=None)
            row = top or QueryStat(query="(overall)", page=page, clicks=now_clicks,
                                   impressions=prev_clicks, ctr=0.0, position=0.0)
            todos.append(_todo(
                brand_id, "decay", row,
                "Update this page — refresh facts, date, and re-promote internally",
                f"Clicks fell from {prev_clicks} to {now_clicks} in 28 days; "
                "refreshed pages typically recover most of the loss",
                0.7 * (prev_clicks - now_clicks),
            ))

    todos.sort(key=lambda t: t["est_monthly_clicks"], reverse=True)
    return todos[:MAX_TODOS]


def _summary(rows: list[QueryStat], prev_rows: list[QueryStat], todos: list[dict]) -> dict:
    clicks = sum(r.clicks for r in rows)
    impressions = sum(r.impressions for r in rows)
    prev_clicks = sum(r.clicks for r in prev_rows)
    weighted_pos = (
        sum(r.position * r.impressions for r in rows) / impressions if impressions else 0.0
    )
    return {
        "clicks_28d": clicks,
        "clicks_prev_28d": prev_clicks,
        "impressions_28d": impressions,
        "avg_position": round(weighted_pos, 1),
        "est_potential_clicks": sum(t["est_monthly_clicks"] for t in todos),
    }


def _insight_bullets(summary: dict, todos: list[dict]) -> list[str]:
    bullets = []
    delta = summary["clicks_28d"] - summary["clicks_prev_28d"]
    trend = "up" if delta >= 0 else "down"
    bullets.append(
        f"Organic clicks are {trend} {abs(delta):,} vs the prior 28 days "
        f"({summary['clicks_prev_28d']:,} → {summary['clicks_28d']:,})."
    )
    striking = [t for t in todos if t["kind"] == "striking"]
    if striking:
        bullets.append(
            f"{len(striking)} page(s) sit just below the top results — the to-do list "
            f"estimates +{sum(t['est_monthly_clicks'] for t in striking):,} clicks/month if fixed."
        )
    decays = [t for t in todos if t["kind"] == "decay"]
    if decays:
        bullets.append(f"{len(decays)} page(s) are losing traffic and need a refresh.")
    if summary["est_potential_clicks"]:
        bullets.append(
            f"Full to-do list is worth an estimated +{summary['est_potential_clicks']:,} clicks/month."
        )
    return bullets


# ---------------------------------- runs ----------------------------------

def run_brand(brand: dict, trigger: str, today: date | None = None) -> dict:
    """Pull data, build insights + to-dos + blog topics for one brand, persist."""
    end = today or date.today()
    degraded: list[str] = []
    rows: list[QueryStat] = []
    prev_rows: list[QueryStat] = []
    prop = brand.get("gsc_property") or f"sc-domain:{brand['domain']}"
    try:
        rows = gsc_fetch(prop, end - timedelta(days=28), end)
        prev_rows = gsc_fetch(prop, end - timedelta(days=56), end - timedelta(days=29))
    except CredentialMissing as exc:
        degraded.append(f"Search Console: {exc}")

    todos = build_todos(brand["id"], rows, prev_rows)
    summary = _summary(rows, prev_rows, todos)
    topic_list, topic_notes = topics.build_topics(brand, rows, prev_rows)
    degraded.extend(topic_notes)

    run = {
        "brand_id": brand["id"],
        "at": end.isoformat(),
        "trigger": trigger,
        "degraded": degraded,
        "summary": summary,
        "insights": _insight_bullets(summary, todos) if rows else [],
        "todos": todos,
        "topics": topic_list,
    }
    state.save(f"run-{brand['id']}", run)
    return run


def latest_run(brand_id: str) -> dict | None:
    run = state.load(f"run-{brand_id}")
    if not run:
        return None
    # Overlay saved statuses so a data refresh never wipes assigned/done marks.
    overlay = (state.load(f"todos-{brand_id}") or {}).get("status", {})
    for todo in run.get("todos", []):
        if todo["id"] in overlay:
            todo["status"] = overlay[todo["id"]]
    return run


def set_todo_status(brand_id: str, item_id: str, status: str) -> None:
    doc = state.load(f"todos-{brand_id}") or {"status": {}}
    doc.setdefault("status", {})[item_id] = status
    state.save(f"todos-{brand_id}", doc)
