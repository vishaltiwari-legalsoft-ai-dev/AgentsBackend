"""SEO agent (a2) API — brands, insights, traffic-estimated to-dos, blog topics.

Mounted under ``/api/seo-geo``. Auth: any signed-in user reads and runs; only a
Creator edits the brand registry; the cron entry is gated by ``x-cron-key``
(matched against ``SEO_CRON_KEY``, endpoint is inert until that env var is set).
"""
from __future__ import annotations

import hmac
import logging
import os
import re

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.security import get_current_user, require_creator
from app.services.run_tracking import CHANGE, CRON, JOB, Activity, ActivityTrail
from seo_geo_agent import advisor as seo_advisor
from seo_geo_agent import gsc_oauth as seo_oauth
from seo_geo_agent import site_brain as seo_site
from seo_geo_agent import audit as seo_audit
from seo_geo_agent import briefs as seo_briefs
from seo_geo_agent import competitors as seo_competitors
from seo_geo_agent import insights, keywords as seo_keywords, sources
from seo_geo_agent import pages as seo_pages
from seo_geo_agent import state as seo_state
from seo_geo_agent.sources import CredentialMissing, ga_fetch_pages

router = APIRouter()
logger = logging.getLogger("agentos.seo_geo")

SEO_AGENT_ID = "a2"  # "SEO Analyst" slot in the frontend agent catalog
SEO_AGENT_NAME = "SEO Analyst"
TODO_STATUSES = {"todo", "assigned", "done"}


#: Every unit of SEO Analyst work lands here — see THE RULE in run_tracking.py.
trail = ActivityTrail(agent_id=SEO_AGENT_ID, agent_name=SEO_AGENT_NAME, category="seo")


def _for(act: Activity, brand: dict) -> None:
    """Stamp the brand this unit of work was about onto its trail row."""
    act.note(brand=brand.get("name"), brand_id=brand.get("id"))


class BrandIn(BaseModel):
    id: str = ""  # slug; derived from name when omitted
    name: str
    domain: str
    gsc_property: str = ""
    seeds: list[str] = []
    enabled: bool = True


class TodoStatusIn(BaseModel):
    status: str


class CompetitorsIn(BaseModel):
    domains: list[str]


class QueryIn(BaseModel):
    query: str


class KeywordIn(BaseModel):
    keyword: str


class PageIn(BaseModel):
    page: str


class DraftIn(BaseModel):
    text: str
    keyword: str


class AskIn(BaseModel):
    question: str


def _rows_28d(brand: dict) -> tuple[list, list[str]]:
    """Latest 28-day GSC rows, degrading to empty + a note when access is missing."""
    prop = brand.get("gsc_property") or f"sc-domain:{brand['domain']}"
    end = date.today()
    try:
        return sources.gsc_fetch(prop, end - timedelta(days=28), end), []
    except CredentialMissing as exc:
        return [], [sources.plain_note(f"Search Console: {exc}")]


# ------------------------- coded 503s for the live routes -------------------------
# Every SEO route that needs a live service (the AI model, live Google results,
# a crawl) used to answer ``detail=str(exc)``: the CredentialMissing text, which
# wraps whatever the provider raised — an ``<HttpError …>`` repr, an OpenRouter
# error dict, an env var name. The reply is now the OAuth routes' shape,
# ``{"code", "message"}``: a stable code the console can key on and one plain
# sentence. The raw text goes to the log at WARNING, never into a response.

SEO_OFFLINE = "seo_offline"                        # this server runs without live services
SEO_AI_BAD_ANSWER = "seo_ai_bad_answer"            # the model answered, in a shape we can't use
SEO_SEARCH_UNCONFIGURED = "seo_search_unconfigured"  # no live Google results set up here
SEO_SEARCH_UNAVAILABLE = "seo_search_unavailable"  # Serper refused (credits, 429, 5xx) or is unreachable
SEO_NEEDS_REFRESH = "seo_needs_refresh"            # a precondition: run a data refresh first
SEO_AI_UNAVAILABLE = "seo_ai_unavailable"          # the AI model could not be reached
SEO_UNAVAILABLE = "seo_unavailable"                # anything else a live service refused

#: First match wins — a wrapped failure carries the words of every layer.
_SEO_FAILURES = (
    (SEO_AI_BAD_ANSWER, re.compile(r"not the review object|wrong shape", re.I),
     "The AI model's answer couldn't be used, so nothing was saved — run {what} again."),
    (SEO_OFFLINE, re.compile(r"offline mode", re.I),
     "This server runs without live services, so {what} can't run here."),
    (SEO_SEARCH_UNCONFIGURED, re.compile(r"serper key missing|serper_api_key", re.I),
     "Live Google results aren't set up on this server, so {what} can't run."),
    (SEO_SEARCH_UNAVAILABLE, re.compile(r"serper search failed", re.I),
     "Live Google results couldn't be reached, so {what} couldn't run — try again in a few minutes."),
    (SEO_NEEDS_REFRESH, re.compile(r"run a data refresh first", re.I),
     "Run a data refresh first — {what} can't be built without the latest rank snapshots."),
    (SEO_AI_UNAVAILABLE, re.compile(r"llm unavailable|needs the ai model", re.I),
     "The AI model isn't available right now, so {what} couldn't run — try again shortly."),
)
_SEO_FAILURE_FALLBACK = "{what} couldn't run right now — try again shortly."


def _seo_unavailable(exc: Exception, what: str) -> HTTPException:
    """A 503 a live SEO route answers: ``{"code", "message"}``, raw text logged."""
    raw = str(exc)
    code, message = SEO_UNAVAILABLE, _SEO_FAILURE_FALLBACK
    for kind, pattern, sentence in _SEO_FAILURES:
        if pattern.search(raw):
            code, message = kind, sentence
            break
    text = message.format(what=what)
    logger.warning("SEO %s unavailable (%s): %s", what, code, raw[:2000])
    return HTTPException(status_code=503, detail={"code": code, "message": text[0].upper() + text[1:]})


def _brand_or_404(brand_id: str) -> dict:
    brand = next((b for b in insights.list_brands() if b["id"] == brand_id), None)
    if not brand:
        raise HTTPException(status_code=404, detail="Unknown brand")
    return brand


def _headline(run: dict | None, review: dict | None) -> str | None:
    """The one line a busy owner should read first on the brand card."""
    if run:
        todo = next((t for t in run.get("todos", []) if t.get("status") != "done"), None)
        if todo:
            gain = f" → est. +{todo['est_monthly_clicks']}/mo" if todo.get("est_monthly_clicks") else ""
            return f"Top action: {todo['action']}{gain}"
    if review and review.get("positioning"):
        return review["positioning"]
    return None


@router.get("/seo-geo/overview")
def overview(user=Depends(get_current_user)):
    """The brand cards plus the integration flags. ``sources.gsc`` is true only
    when some brand really has Search Console behind it (a stored grant, or a
    latest run that read Search Console rows); ``sources.gsc_connect`` says
    whether the Connect button can work on this server. Per brand, ``gsc`` is
    :func:`_gsc_view`; ``gsc_connected`` stays for consoles that predate it.

    GEO-only accounts (outside contractors) reach this route through the scope
    wall because the console shell reads it, but GEO uses no Search Console
    data: for them both per-brand fields are left out, so the connecting staff
    member's email, ``connected_at`` and the property never reach them."""
    brands = insights.list_brands()
    grants = seo_oauth.brand_connections(brands)  # one batched read, mismatch judged
    cards = []
    for brand in brands:
        # Per brand: the run, its status overlay and the site review — every
        # card field below is read off these, never off another document.
        run = insights.latest_run(brand["id"])
        review = seo_site.latest_review(brand["id"])
        gsc = _gsc_view(brand, grants.get(brand["id"]), run)
        cards.append({
            "brand": brand,
            "gsc_connected": gsc["connected"],
            "gsc": gsc,
            "headline": _headline(run, review),
            "site_review_at": (review or {}).get("at"),
            "ga_reading": bool(run and run.get("ga")),
            "last_run": run and _last_run_card(run),
        })
    body = {
        "sources": {
            "gsc": any(c["gsc"]["connected"] or c["gsc"]["reading"] for c in cards),
            "gsc_connect": seo_oauth.connect_ready(),
            "serp": sources.serper_available(),
        },
        "brands": cards,
    }
    if user.get("is_geo_only"):
        for card in cards:
            card.pop("gsc", None)
            card.pop("gsc_connected", None)
    return body


def _last_run_card(run: dict) -> dict:
    """An overview card's ``last_run``. ``open_count``: to-dos not done (the
    saved statuses already overlaid); ``drops``: keywords that lost places or
    fell out of the top 10 since the previous check; ``rank_check_failed``:
    this run's rank check failed and its figures are the last good check's."""
    summary = run.get("summary") or {}
    todos = run.get("todos") or []
    return {
        "at": run["at"],
        "summary": run["summary"],
        "degraded": run["degraded"],
        "todo_count": len(todos),
        "topic_count": len(run.get("topics") or []),
        "open_count": sum(1 for t in todos if t.get("status") != "done"),
        "drops": int(summary.get("moved_down") or 0) + int(summary.get("fell_off") or 0),
        "rank_check_failed": bool(summary.get("rank_check_failed")),
    }


@router.post("/seo-geo/brands")
def save_brand(payload: BrandIn, user=Depends(require_creator),
               act: Activity = trail.records("brand_saved", "Saved a brand", unit=CHANGE)):
    # Shared with the GEO editor's self-serve create route: one answer to "what
    # is a valid brand id / domain", in the module that owns brand records. The
    # copy that used to live here also kept the path when a full URL was pasted
    # ("brand.com/pricing"), which broke ``sc-domain:`` and every alias derived
    # from the domain.
    try:
        slug = insights.slugify_brand_id(payload.id or payload.name)
        domain = insights.normalize_domain(payload.domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    brand = {
        "id": slug,
        "name": payload.name.strip() or slug,
        "domain": domain,
        "gsc_property": payload.gsc_property.strip() or f"sc-domain:{domain}",
        "seeds": [s.strip() for s in payload.seeds if s.strip()][:10],
        "enabled": payload.enabled,
    }
    _for(act, brand)
    act.note(f"Brand saved — {brand['name']} ({domain})")
    return {"brands": insights.upsert_brand(brand)}


@router.delete("/seo-geo/brands/{brand_id}")
def remove_brand(brand_id: str, user=Depends(require_creator),
                 act: Activity = trail.records("brand_deleted", "Deleted a brand", unit=CHANGE)):
    _for(act, _brand_or_404(brand_id))
    act.note(f"Brand deleted — {brand_id}")
    return {"brands": insights.delete_brand(brand_id)}


@router.post("/seo-geo/run/{brand_id}")
def run_brand(brand_id: str, user=Depends(get_current_user),
              act: Activity = trail.records("run", "Full SEO refresh", unit=JOB)):
    """A full refresh, now. A source that fails (a Serper dry spell, Search
    Console, GA) is a note in ``degraded`` on a saved run — 200. Only a refresh
    that could produce nothing at all answers the coded 503."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        run = insights.run_brand(brand, trigger=f"manual:{user['email']}")
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the data refresh") from exc
    act.note(f"Full SEO refresh — {len(run['todos'])} to-dos, {len(run['topics'])} topics")
    return {"at": run["at"], "summary": run["summary"], "degraded": run["degraded"],
            "todo_count": len(run["todos"]), "topic_count": len(run["topics"])}


def _plan_of_action(brand_id: str, run: dict | None) -> list[dict]:
    """Top 3 next moves across every surface — the 'what do I do' strip.

    The Fix list renders below this strip, so its #1 item is never repeated
    here (not even re-worded as a keyword move), and no two moves in the strip
    are the same move in different words.

    A move that IS a Fix-list item is a reference, not a copy: it carries
    ``ref_todo_id`` and ``ref_index`` (its 1-based Fix-list position) and a
    ``label`` of at most eight words, and the console says "See Fix list #n"
    instead of printing the item's action and why a second time. Moves from
    other surfaces have ``ref_todo_id``/``ref_index`` None and a label too.
    ``action``/``detail`` stay on every item for consoles that predate this.
    """
    todos = (run or {}).get("todos", [])
    shown_below = [todos[0]["action"]] if todos else []
    plan: list[dict] = []

    def add(source: str, action: str, detail: str, label: str,
            ref: dict | None = None, index: int | None = None) -> bool:
        taken = shown_below + [p["action"] for p in plan]
        if any(seo_audit.similar(action, other) for other in taken):
            return False
        plan.append({"source": source, "action": action, "detail": detail, "label": label,
                     "ref_todo_id": ref["id"] if ref else None, "ref_index": index})
        return True

    for index, todo in enumerate(todos[1:], start=2):
        if todo.get("status") != "done" and add("fix list", todo["action"], todo["why"],
                                                insights.todo_label(todo), todo, index):
            break
    lab = seo_keywords.latest(brand_id) or {}
    for cluster in lab.get("clusters", []):
        if cluster.get("tier") == "high" and add(
                "keywords", f"Go after “{cluster['name']}”", cluster.get("recommendation", ""),
                insights.short_label(f"Go after “{cluster['name']}”")):
            break
    report = seo_audit.latest_audit(brand_id) or {}
    issue = next((i for i in report.get("issues", []) if i["severity"] == "high"), None)
    if issue:
        add("audit", f"Fix: {issue['issue']}", issue["fix"], insights.short_label(f"Fix: {issue['issue']}"))
    else:
        failed = next((c for c in report.get("site_checks", []) if not c["ok"]), None)
        if failed:
            add("audit", f"Fix: {failed['name']}", failed["fix"],
                insights.short_label(f"Fix: {failed['name']}"))
    return plan[:3]


@router.get("/seo-geo/brands/{brand_id}")
def brand_detail(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    conn = seo_oauth.brand_connections([brand]).get(brand_id)
    run = insights.latest_run(brand_id)
    return {
        "brand": brand,
        "run": run,
        "gsc": _gsc_view(brand, conn, run),
        "plan": _plan_of_action(brand_id, run),
        "site_review": seo_site.mark_blog_plan(seo_site.checked_review(brand_id), (run or {}).get("topics")),
    }


@router.post("/seo-geo/site-review/{brand_id}")
def run_site_review(brand_id: str, user=Depends(get_current_user),
                    act: Activity = trail.records("site_review", "Expert site review")):
    """Crawl the brand's site, build the corpus, and run the expert review.

    Its findings join the stored run's Fix list right away
    (``insights.fold_site_review``). ``fix_list_updated`` is false when no run
    exists yet — there is no Fix list until the first run, which then reads
    this review — and ``findings_on_fix_list`` is how many findings the Fix
    list now shows (its cap can leave some out)."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        review = seo_site.analyze(brand)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the site review") from exc
    updated, on_list = insights.fold_site_review(brand_id)
    act.note(f"Expert site review of {brand['domain']}"
             + (f" — {on_list} finding(s) on the Fix list" if updated else " — no run yet to add findings to"))
    return {**review, "fix_list_updated": updated, "findings_on_fix_list": on_list}


@router.get("/seo-geo/site-review/{brand_id}")
def get_site_review(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    run = insights.latest_run(brand_id) or {}
    return {"review": seo_site.mark_blog_plan(seo_site.checked_review(brand_id), run.get("topics"))}


# ------------------------- page intelligence -------------------------

@router.get("/seo-geo/pages/{brand_id}")
def get_pages(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"pages": seo_pages.latest(brand_id)}


@router.post("/seo-geo/pages/{brand_id}/refresh")
def refresh_pages(brand_id: str, user=Depends(get_current_user),
                  act: Activity = trail.records("pages_refresh", "Rebuilt page intelligence")):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    corpus = seo_state.load(f"corpus-{brand_id}") or {}
    if not corpus.get("pages"):
        raise HTTPException(status_code=409, detail="Run the site analysis first")
    rows, notes = _rows_28d(brand)
    ga_pages = []
    prop = brand.get("ga4_property")
    if prop:
        try:
            end = date.today()
            ga_pages = ga_fetch_pages(prop, end - timedelta(days=28), end)
        except CredentialMissing as exc:
            notes.append(sources.plain_note(f"Google Analytics: {exc}"))
    # Page advice a site to-do already covers is folded into it, and the stored
    # Fix list is brought in line — the rows must not point at an item that is
    # not there.
    site = seo_site.site_todos(brand_id)
    intel = seo_pages.build_page_intel(brand, corpus["pages"], ga_pages, rows, data_notes=notes,
                                       site_todos=site, audit_report=seo_audit.latest_audit(brand_id))
    insights.refold_page_advice(brand_id, intel, site)
    act.note(f"Rebuilt page intelligence for {brand['domain']}")
    return intel


@router.post("/seo-geo/todos/{brand_id}/{todo_id}")
def set_todo_status(brand_id: str, todo_id: str, payload: TodoStatusIn,
                    user=Depends(get_current_user),
                    act: Activity = trail.records("todo_status", "Moved a to-do", unit=CHANGE)):
    _for(act, _brand_or_404(brand_id))
    if payload.status not in TODO_STATUSES:
        raise HTTPException(status_code=422, detail=f"Status must be one of {sorted(TODO_STATUSES)}")
    insights.set_todo_status(brand_id, todo_id, payload.status)
    act.note(f"To-do {todo_id} → {payload.status}")
    return {"id": todo_id, "status": payload.status}


# ------------------------- keyword lab -------------------------

@router.post("/seo-geo/keywords/{brand_id}/run")
def run_keyword_lab(brand_id: str, user=Depends(get_current_user),
                    act: Activity = trail.records("keyword_lab", "Keyword lab run")):
    brand = seo_site.effective_seeds(_brand_or_404(brand_id))
    _for(act, brand)
    rows, notes = _rows_28d(brand)
    lab = seo_keywords.run_keyword_lab(
        brand, rows, trigger=f"manual:{user['email']}", extra_notes=notes
    )
    act.note(f"Keyword lab run — {len(lab.get('clusters', []))} clusters")
    return lab


@router.get("/seo-geo/keywords/{brand_id}")
def get_keywords(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"lab": seo_keywords.latest(brand_id)}


# ------------------------- competitors & SERP -------------------------

@router.get("/seo-geo/competitors/{brand_id}")
def get_competitors(brand_id: str, user=Depends(get_current_user)):
    """``checked_at``: the date of the latest rank snapshot (None before the
    first). ``history``: ``[{at, tracked, top3, top10, unranked}]``, one per
    stored snapshot, oldest first."""
    brand = _brand_or_404(brand_id)
    ranks_doc = seo_state.load(f"ranks-{brand_id}") or {}
    sitemap_doc = seo_state.load(f"sitemaps-{brand_id}") or {}
    snapshots = ranks_doc.get("snapshots") or []
    return {
        "tracked": brand.get("competitors", []),
        "suggested": ranks_doc.get("suggested_competitors", []),
        "shifts": seo_competitors.rank_shifts(brand_id, ranks_doc),
        "feed": sitemap_doc.get("last_feed", {}),
        "checked_at": snapshots[-1].get("at") if snapshots else None,
        "history": seo_competitors.rank_history(ranks_doc),
    }


@router.put("/seo-geo/competitors/{brand_id}")
def set_competitors(brand_id: str, payload: CompetitorsIn, user=Depends(require_creator),
                    act: Activity = trail.records("competitors_saved",
                                                  "Edited the tracked competitors", unit=CHANGE)):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    brand["competitors"] = [d.strip().lower() for d in payload.domains if d.strip()][:8]
    insights.upsert_brand(brand)
    act.note(f"Tracked competitors set to {', '.join(brand['competitors']) or 'none'}")
    return {"tracked": brand["competitors"]}


@router.post("/seo-geo/competitors/{brand_id}/track")
def track_competitors(brand_id: str, user=Depends(get_current_user),
                      act: Activity = trail.records("competitor_track",
                                                    "Rank snapshot + competitor sitemap check")):
    """Take a rank snapshot + check competitor sitemaps for new content, now."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    degraded: list[str] = []
    try:
        seo_competitors.rank_snapshot(brand)
    except CredentialMissing as exc:
        degraded.append(sources.plain_note(f"Rank tracking: {exc}"))
    feed = {}
    try:
        feed = seo_competitors.sitemap_watch(brand)
    except CredentialMissing as exc:
        degraded.append(sources.plain_note(f"Sitemap watch: {exc}"))
    return {"shifts": seo_competitors.rank_shifts(brand_id), "feed": feed, "degraded": degraded}


@router.post("/seo-geo/serp/{brand_id}")
def serp_xray(brand_id: str, payload: QueryIn, user=Depends(get_current_user),
              act: Activity = trail.records("serp_xray", "SERP X-ray")):
    """Reverse-engineer the top of the SERP for any query, on demand."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        result = seo_competitors.serp_deep_dive(brand, payload.query.strip())
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the SERP X-ray") from exc
    act.note(f"SERP X-ray: “{payload.query.strip()}”")
    return result


@router.get("/seo-geo/competitors/{brand_id}/profiles")
def get_competitor_profiles(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"profiles": seo_competitors.latest_profiles(brand_id)}


@router.post("/seo-geo/competitors/{brand_id}/profiles/refresh")
def refresh_competitor_profiles(brand_id: str, user=Depends(get_current_user),
                               act: Activity = trail.records("competitor_profiles",
                                                             "Rebuilt top-competitor profiles")):
    """Rebuild the top-5 competitor profiles: visibility, keywords won, content feed."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        profiles = seo_competitors.build_profiles(brand)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the competitor profiles") from exc
    return profiles


# ------------------------- briefs & decay plans -------------------------

@router.get("/seo-geo/briefs/{brand_id}")
def get_briefs(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"briefs": seo_briefs.list_briefs(brand_id)}


@router.post("/seo-geo/briefs/{brand_id}")
def build_brief(brand_id: str, payload: KeywordIn, user=Depends(get_current_user),
                act: Activity = trail.records("brief", "Built a content brief")):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    rows, _ = _rows_28d(brand)
    try:
        brief = seo_briefs.build_brief(brand, payload.keyword.strip(), rows)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the content brief") from exc
    act.note(f"Content brief for “{payload.keyword.strip()}”")
    return brief


@router.post("/seo-geo/update-plan/{brand_id}")
def build_update_plan(brand_id: str, payload: PageIn, user=Depends(get_current_user),
                      act: Activity = trail.records("update_plan", "Built an update plan")):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    rows, _ = _rows_28d(brand)
    try:
        plan = seo_briefs.update_plan(brand, payload.page.strip(), rows)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the update plan") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    act.note(f"Update plan for {payload.page.strip()}")
    return plan


# ------------------------- audit & draft scoring -------------------------

@router.post("/seo-geo/audit/{brand_id}/run")
def run_audit(brand_id: str, user=Depends(get_current_user),
              act: Activity = trail.records("audit", "Technical site audit")):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        report = seo_audit.site_audit(brand)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the site audit") from exc
    act.note(f"Technical site audit of {brand['domain']}")
    return report


@router.get("/seo-geo/audit/{brand_id}")
def get_audit(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"report": seo_audit.latest_audit(brand_id)}


@router.post("/seo-geo/draft-score/{brand_id}")
def draft_score(brand_id: str, payload: DraftIn, user=Depends(get_current_user),
                act: Activity = trail.records("draft_score", "Scored a draft")):
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    brief = next(
        (b for b in seo_briefs.list_briefs(brand_id)
         if b["keyword"].lower() == payload.keyword.strip().lower()),
        None,
    )
    score = seo_audit.score_draft(brand, payload.text, payload.keyword.strip(), brief)
    act.note(f"Scored a draft for “{payload.keyword.strip()}”")
    return score


# --------------------- Search Console connect (OAuth) ---------------------
#
# Google returns the browser to the CONSOLE (``<APP_PUBLIC_URL>/oauth/google``),
# which posts ``{code, state}`` here with the member's own bearer — the a12
# Inbox pattern. There is no public callback route. Any signed-in member may
# connect a brand (owner decision, 2026-10-14); disconnect stays Creator-only.
#
# Error contract: ``detail`` is ``{"code", "message"}`` — a stable code the
# console keys on and one plain sentence. What the server lacks (env var names)
# goes to the log as a WARNING, never into a response.

#: ``ConnectRefused.code`` -> HTTP status. Ours to fix is 503; a stale or
#: foreign connect link is 400; the member's Google account said no (or has no
#: access to the site) is 422; Google itself failed or refused the code is 502.
_GSC_STATUS = {
    seo_oauth.UNCONFIGURED: 503,
    seo_oauth.OFFLINE: 503,
    seo_oauth.STATE_INVALID: 400,
    seo_oauth.STATE_EXPIRED: 400,
    seo_oauth.STATE_OTHER_USER: 400,
    seo_oauth.SCOPE_NOT_GRANTED: 422,
    seo_oauth.NO_PROPERTY: 422,
    seo_oauth.PROPERTY_TAKEN: 409,
    seo_oauth.EXCHANGE_FAILED: 502,
    seo_oauth.GOOGLE_UNAVAILABLE: 502,
}


class OAuthCompleteIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=2048)


def _gsc_refused(exc: seo_oauth.ConnectRefused) -> HTTPException:
    status = _GSC_STATUS.get(exc.code, 502)
    if status >= 500:
        logger.warning("Search Console connect refused (%s): %s", exc.code, exc.log_detail or exc.message)
    else:
        logger.info("Search Console connect refused (%s): %s", exc.code, exc.log_detail or exc.message)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _gsc_view(brand: dict, grant: dict | None, run: dict | None) -> dict:
    """One brand's Search Console truth, the same shape on the overview card,
    the brand detail and the connect answer.

    ``connected`` / ``property`` / ``connected_by`` / ``connected_at`` describe
    a stored OAuth grant. ``mismatch`` is true when that stored grant is for a
    different brand or site (``gsc_oauth.foreign_grant``): the run ignores it,
    so ``connected`` is false and the property shown is the one to replace.
    ``service_account_property`` is the property set on the brand for the
    shared service account (``None`` when unset — the run then tries
    ``sc-domain:<domain>``); whether that account can actually read it is only
    known from a run, which is what ``reading`` says: the latest run got
    Search Console rows."""
    grant = grant or {}
    mismatch = bool(grant.get("mismatch"))
    return {
        "connected": bool(grant) and not mismatch,
        "mismatch": mismatch,
        "property": grant.get("property"),
        "connected_by": grant.get("connected_by"),
        "connected_at": grant.get("connected_at"),
        "service_account_property": brand.get("gsc_property") or None,
        "reading": _read_search_console(run),
    }


def _read_search_console(run: dict | None) -> bool:
    """Whether the latest run actually read Search Console rows.

    ``run.gsc_rows`` is the count the run fetched. Never ``summary.mode``: a run
    whose Search Console AND rank tracking both failed still carries the empty
    summary, whose mode is "search-console". A run stored before ``gsc_rows``
    existed counts only if it recorded impressions — Search Console returns no
    row without one, and the both-failed summary has zero."""
    run = run or {}
    if "gsc_rows" in run:
        return (run.get("gsc_rows") or 0) > 0
    summary = run.get("summary") or {}
    return summary.get("mode") == "search-console" and (summary.get("impressions_28d") or 0) > 0


@router.get("/seo-geo/oauth/start/{brand_id}")
def oauth_start(brand_id: str, user=Depends(get_current_user)):
    """The Google consent URL for this brand, bound to the caller. Refused
    with 503 before anyone is sent to Google if the connection could not be
    finished or stored here."""
    _brand_or_404(brand_id)
    try:
        return {"url": seo_oauth.auth_url(brand_id, user_id=str(user["id"]))}
    except seo_oauth.ConnectRefused as exc:
        raise _gsc_refused(exc) from exc


@router.post("/seo-geo/oauth/complete")
def oauth_complete(payload: OAuthCompleteIn, user=Depends(get_current_user),
                   act: Activity = trail.records("gsc_connect",
                                                 "Connected Search Console", unit=CHANGE)):
    """The console posts Google's ``{code, state}`` here. The state names the
    brand and must have been issued to this same caller; the answer is the
    brand's ``gsc`` view — never a token."""
    try:
        verified = seo_oauth.read_state(payload.state, user_id=str(user["id"]))
        brand = _brand_or_404(verified.brand_id)
        _for(act, brand)
        grant = seo_oauth.complete(verified, brand, code=payload.code,
                                   connected_by=str(user.get("email") or ""),
                                   user_id=str(user["id"]))
    except seo_oauth.ConnectRefused as exc:
        raise _gsc_refused(exc) from exc
    act.note(f"Search Console connected for {brand['id']} — reading {grant['property']}")
    return {"brand_id": brand["id"],
            "gsc": _gsc_view(brand, grant, insights.latest_run(brand["id"]))}


@router.post("/seo-geo/oauth/disconnect/{brand_id}")
def oauth_disconnect(brand_id: str, user=Depends(require_creator),
                     act: Activity = trail.records("gsc_disconnect",
                                                   "Disconnected Search Console", unit=CHANGE)):
    _for(act, _brand_or_404(brand_id))
    seo_oauth.disconnect(brand_id)
    act.note(f"Search Console disconnected for {brand_id}")
    return {"connected": False}


@router.post("/seo-geo/ask/{brand_id}")
def ask_expert(brand_id: str, payload: AskIn, user=Depends(get_current_user),
               act: Activity = trail.records("ask", "Asked the SEO strategist")):
    """Grounded SEO-strategist chat over everything the agent knows about the brand."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Ask a question")
    try:
        answer = seo_advisor.ask(brand, question)
    except CredentialMissing as exc:
        raise _seo_unavailable(exc, "the SEO strategist") from exc
    act.note(f"Asked: {question}")
    return answer


# ------------------------------- cron -------------------------------

@router.post("/seo-geo/cron/run")
def cron_run(request: Request, response: Response,
             act: Activity = trail.records("cron", "Scheduled SEO sweep",
                                           unit=JOB, actor=CRON)):
    """Scheduled per-brand sweep.

    Status is honest: 200 every brand ran, 207 some brands failed, 502 EVERY
    brand failed. Cloud Scheduler only reads the status code, so the old
    unconditional 200 meant a sweep could be dead for weeks with the reason
    buried in a response body nobody reads. 207 stays 2xx on purpose — one
    permanently broken brand must not put the job into an endless retry loop —
    and the log line is what a human alerts on.

    ONE rank snapshot per brand per sweep. A rank-tracking run takes it inside
    ``run_brand`` (or tries, and says so in its notes); only a Search Console
    brand, whose run takes none, gets it here. The sweep used to take a second
    one minutes after the run's, so the two latest snapshots were the same
    night (every shift ~0), the stored history covered half as many nights and
    the Serper spend doubled. A brand whose rankings weren't updated still ran:
    it is counted in ``rank_checks_failed`` and logged, not failed."""
    expected = os.environ.get("SEO_CRON_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="SEO_CRON_KEY not configured")
    if not hmac.compare_digest(request.headers.get("x-cron-key", ""), expected):
        raise HTTPException(status_code=403, detail="Bad cron key")
    results = {}
    rank_failed: list[str] = []
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        try:
            run = insights.run_brand(brand, trigger="cron")
            entry = {"ok": True, "todo_count": len(run["todos"])}
            # Tracking extras are best-effort: missing keys must not fail the
            # sweep, and they deliberately do NOT count toward the status below.
            if insights.checked_ranks(run):
                ranks_stale = bool((run.get("summary") or {}).get("rank_check_failed"))
                entry["ranks"] = ("not updated: the run's rank check failed" if ranks_stale
                                  else "updated by the run")
            else:
                try:
                    seo_competitors.rank_snapshot(brand)
                    entry["ranks"] = "updated"
                    ranks_stale = False
                except Exception as exc:  # noqa: BLE001
                    entry["ranks"] = f"skipped: {exc}"
                    ranks_stale = True
            if ranks_stale:
                rank_failed.append(brand["id"])
            try:
                seo_competitors.sitemap_watch(brand)
                entry["sitemaps"] = "updated"
            except Exception as exc:  # noqa: BLE001
                entry["sitemaps"] = f"skipped: {exc}"
            results[brand["id"]] = entry
        except Exception as exc:  # noqa: BLE001 — one bad brand must not kill the sweep
            logger.exception("seo cron failed for %s", brand["id"])
            results[brand["id"]] = {"ok": False, "error": str(exc)}
    ok = sum(1 for r in results.values() if r.get("ok"))
    failed = len(results) - ok
    out = {"brands": results, "ok": ok, "failed": failed, "rank_checks_failed": len(rank_failed)}
    if rank_failed:
        logger.warning("SEO cron sweep: rankings not updated for %d brand(s): %s",
                       len(rank_failed), ", ".join(rank_failed))
    if results and ok == 0:
        out["status"] = "failed"
        response.status_code = 502
        logger.error("SEO cron sweep FAILED: all %d brands errored", failed)
    elif failed:
        out["status"] = "partial"
        response.status_code = 207
        logger.warning("SEO cron sweep degraded: %d/%d brands failed", failed, len(results))
    else:
        out["status"] = "ok"
    act.note(f"Scheduled sweep across {len(results)} brands — {ok} ok, {failed} failed",
             status=str(out["status"]))
    return out
