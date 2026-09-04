"""SEO agent (a2) API — brands, insights, traffic-estimated to-dos, blog topics.

Mounted under ``/api/seo-geo``. Auth: any signed-in user reads and runs; only a
Creator edits the brand registry; the cron entry is gated by ``x-cron-key``
(matched against ``SEO_CRON_KEY``, endpoint is inert until that env var is set).
"""
from __future__ import annotations

import hmac
import html
import logging
import os
import secrets

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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
        return [], [f"Search Console: {exc}"]


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
    cards = []
    for brand in insights.list_brands():
        run = insights.latest_run(brand["id"])
        review = seo_site.latest_review(brand["id"])
        cards.append({
            "brand": brand,
            "gsc_connected": bool(seo_oauth.connection(brand["id"])),
            "headline": _headline(run, review),
            "last_run": run and {
                "at": run["at"],
                "summary": run["summary"],
                "degraded": run["degraded"],
                "todo_count": len(run["todos"]),
                "topic_count": len(run["topics"]),
            },
        })
    return {
        "sources": {"gsc": sources.gsc_available(), "serp": sources.serper_available()},
        "brands": cards,
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
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    run = insights.run_brand(brand, trigger=f"manual:{user['email']}")
    act.note(f"Full SEO refresh — {len(run['todos'])} to-dos, {len(run['topics'])} topics")
    return {"at": run["at"], "summary": run["summary"], "degraded": run["degraded"],
            "todo_count": len(run["todos"]), "topic_count": len(run["topics"])}


def _plan_of_action(brand_id: str, run: dict | None) -> list[dict]:
    """Top 3 next moves across every surface — the 'what do I do' strip."""
    plan: list[dict] = []
    todo = next((t for t in (run or {}).get("todos", []) if t.get("status") != "done"), None)
    if todo:
        plan.append({"source": "fix list", "action": todo["action"], "detail": todo["why"]})
    lab = seo_keywords.latest(brand_id) or {}
    cluster = next((c for c in lab.get("clusters", []) if c.get("tier") == "high"), None)
    if cluster:
        plan.append({"source": "keywords", "action": f"Go after “{cluster['name']}”",
                     "detail": cluster.get("recommendation", "")})
    report = seo_audit.latest_audit(brand_id) or {}
    issue = next((i for i in report.get("issues", []) if i["severity"] == "high"), None)
    if issue:
        plan.append({"source": "audit", "action": f"Fix: {issue['issue']}", "detail": issue["fix"]})
    else:
        failed = next((c for c in report.get("site_checks", []) if not c["ok"]), None)
        if failed:
            plan.append({"source": "audit", "action": f"Fix: {failed['name']}", "detail": failed["fix"]})
    return plan[:3]


@router.get("/seo-geo/brands/{brand_id}")
def brand_detail(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    conn = seo_oauth.connection(brand_id)
    run = insights.latest_run(brand_id)
    return {
        "brand": brand,
        "run": run,
        "gsc": {"connected": bool(conn), "property": (conn or {}).get("property")},
        "plan": _plan_of_action(brand_id, run),
        "site_review": seo_site.latest_review(brand_id),
    }


@router.post("/seo-geo/site-review/{brand_id}")
def run_site_review(brand_id: str, user=Depends(get_current_user),
                    act: Activity = trail.records("site_review", "Expert site review")):
    """Crawl the brand's site, build the corpus, and run the expert review."""
    brand = _brand_or_404(brand_id)
    _for(act, brand)
    try:
        review = seo_site.analyze(brand)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    act.note(f"Expert site review of {brand['domain']}")
    return review


@router.get("/seo-geo/site-review/{brand_id}")
def get_site_review(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"review": seo_site.latest_review(brand_id)}


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
            notes.append(f"Google Analytics: {exc}")
    intel = seo_pages.build_page_intel(brand, corpus["pages"], ga_pages, rows, data_notes=notes)
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
    brand = _brand_or_404(brand_id)
    ranks_doc = seo_state.load(f"ranks-{brand_id}") or {}
    sitemap_doc = seo_state.load(f"sitemaps-{brand_id}") or {}
    return {
        "tracked": brand.get("competitors", []),
        "suggested": ranks_doc.get("suggested_competitors", []),
        "shifts": seo_competitors.rank_shifts(brand_id),
        "feed": sitemap_doc.get("last_feed", {}),
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
        degraded.append(str(exc))
    feed = {}
    try:
        feed = seo_competitors.sitemap_watch(brand)
    except CredentialMissing as exc:
        degraded.append(f"Sitemap watch: {exc}")
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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

def _oauth_redirect(request: Request) -> str:
    base = os.environ.get("SEO_OAUTH_REDIRECT_BASE", "") or str(request.base_url).rstrip("/")
    if "localhost" not in base and base.startswith("http://"):
        base = "https://" + base.removeprefix("http://")  # Cloud Run sits behind TLS proxy
    return f"{base}/api/seo-geo/oauth/callback"


def _close_page(title: str, body: str, status: int = 200, *, strong: str = "") -> HTMLResponse:
    """The OAuth landing page — the ONE place this backend serves HTML.

    Every caller value is attacker-reachable (``?error=`` comes straight off the
    query string), so ``title``, ``body`` and ``strong`` are all escaped: markup
    never comes from a value. ``body`` may carry one ``{strong}`` marker, which
    is where the escaped ``strong`` text is emphasised — the tag is wrapped
    around already-escaped text, so the value itself can never carry markup.

    The CSP grants a fresh per-response nonce to the two inline blocks this
    function authors and nothing else: injected script has no nonce, so
    ``default-src 'none'`` still applies to it. Never a constant nonce — a
    predictable one is the same as ``unsafe-inline``.
    """
    nonce = secrets.token_urlsafe(16)
    safe_body = html.escape(body)
    if strong:
        safe_body = safe_body.replace("{strong}", f"<b>{html.escape(strong)}</b>")
    return HTMLResponse(
        f"<html><head><style nonce=\"{nonce}\">"
        "body{font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center}"
        f"</style></head><body><h2>{html.escape(title)}</h2>"
        f"<p>{safe_body}</p><p>You can close this tab.</p>"
        f"<script nonce=\"{nonce}\">setTimeout(()=>window.close(),4000)</script>"
        "</body></html>",
        status_code=status,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/seo-geo/oauth/start/{brand_id}")
def oauth_start(brand_id: str, request: Request, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    try:
        return {"url": seo_oauth.auth_url(brand_id, _oauth_redirect(request))}
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/seo-geo/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Google redirects the customer's browser here — gated by the signed state."""
    if error:
        return _close_page("Not connected", f"Google returned: {error}", status=400)
    try:
        brand = _brand_or_404(seo_oauth.read_state(state))
        result = seo_oauth.complete(brand, code, _oauth_redirect(request))
        return _close_page(
            "Search Console connected ✓",
            f"{brand['name']} is now reading data from {{strong}}. "
            "Go back to the dashboard and hit Refresh data.",
            strong=result["property"],
        )
    except ValueError as exc:
        return _close_page("Not connected", str(exc), status=400)
    except CredentialMissing as exc:
        return _close_page("Not connected", str(exc), status=503)


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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
    and the log line is what a human alerts on."""
    expected = os.environ.get("SEO_CRON_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="SEO_CRON_KEY not configured")
    if not hmac.compare_digest(request.headers.get("x-cron-key", ""), expected):
        raise HTTPException(status_code=403, detail="Bad cron key")
    results = {}
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        try:
            run = insights.run_brand(brand, trigger="cron")
            entry = {"ok": True, "todo_count": len(run["todos"])}
            # Tracking extras are best-effort: missing keys must not fail the
            # sweep, and they deliberately do NOT count toward the status below.
            try:
                seo_competitors.rank_snapshot(brand)
                entry["ranks"] = "updated"
            except Exception as exc:  # noqa: BLE001
                entry["ranks"] = f"skipped: {exc}"
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
    out = {"brands": results, "ok": ok, "failed": failed}
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
