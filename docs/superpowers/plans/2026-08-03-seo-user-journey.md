# SEO User-Journey Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the a2 SEO agent's user journey — page-level analytics with AI recs, Top-10 action framing, top-5 competitor profiles with content feed + reach estimates, and a non-cannibalizing Top-10 blog plan.

**Architecture:** Everything extends the existing seams: `sources.py` adapters (CredentialMissing degradation), `state.py` persistence (`save/load` doc-per-brand), `insights.run_brand` composition, `/api/seo-geo/*` router, `SeoAgent.tsx` tabs. No new dependencies, no parallel rails.

**Tech Stack:** FastAPI + google-api-python-client (GA4 `analyticsdata` v1beta) + Serper.dev + httpx (backend); Next.js/TS + existing `seo-*` css (frontend).

**Repos:** backend = `c:\Users\ACER\Desktop\ghi\backend` (venv: `.venv\Scripts\python.exe`), frontend = `c:\Users\ACER\Desktop\ghi\newfrontend`. Agent package: `backend/agents/SEO GEO agent/seo_geo_agent/`.

## Global Constraints

- Every external call degrades via `CredentialMissing` + plain-language note; never crash a run.
- Estimates always labelled; heuristic fallbacks carry `"ai": false` — NEVER present a fallback as AI (hard project rule).
- No new pip/npm dependencies. Additive css only. Tests offline (`SEO_OFFLINE=1` via tests/conftest.py) with fake services injected through existing `service=`/`search=`/`fetch=` parameters.
- Test commands: backend `cd backend; .venv\Scripts\python.exe -m pytest "agents\SEO GEO agent\seo_geo_agent\tests" -q` · frontend `cd newfrontend; npx tsc --noEmit` and `npx vitest run`.
- Commit after every task (repo = the one you edited). Windows paths need quotes (folder has spaces).

## Reference interfaces (already in the codebase — do not reinvent)

- `sources.llm_text(system, prompt, *, agent_id=None) -> str` (raises CredentialMissing) — sources.py:266
- `sources.serper_search(query) -> dict` with `organic: [{position, link, title, ...}]` — sources.py:230; `serper_available()` — :226
- `sources.fetch_page(url) -> PageFacts(url,status,title,meta_description,h1,h2,h3,internal_links,images_no_alt,word_count,text)` — sources.py:305/383
- `sources.fetch_sitemap(domain, cap=500) -> list[str]` — sources.py:443
- `sources.ga_fetch_overview(...)`, `GA_TOTAL_METRICS`, `_ga_service(api)` — GA4 pattern to mirror
- `sources.gsc_fetch(prop,start,end,service=None) -> list[QueryStat(query,page,clicks,impressions,ctr,position)]`
- `state.save/load/delete(doc_id)` (docs: `run-{b}`, `corpus-{b}`, `ranks-{b}`, `sitemaps-{b}`)
- `insights.run_brand` composes the run; `insights.upsert_brand`; `insights.ctr_at(position)` CTR curve
- `site_brain.build_corpus` persists `corpus-{brand_id}` = `{"pages":[{url,title,hash,word_count,summary,type,topics,target_query,cta}],...}`
- `competitors.tracked_keywords(brand)`, `rank_snapshot`, `rank_shifts`, `sitemap_watch(brand)` (reads `brand["competitors"]`), doc `ranks-{b}` has `suggested_competitors`
- `keywords.intent_of(keyword) -> str`, `keywords.latest(brand_id)` → `{"clusters":[{name,intent,keywords,...}]}`
- `topics._tokens(text)` token-set helper; `topics.build_topics(brand_like, rows, prev_rows) -> (topic_list, notes)`
- Router: `backend/app/routers/seo_geo.py` (`_brand_or_404`, `_rows_28d(brand) -> (rows, notes)`, `require_creator`, `get_current_user`)
- Frontend: `newfrontend/components/console/seo/SeoAgent.tsx` (tabs, `fmt`, `Delta`, `GaSection`), `labs.tsx` (KeywordsView etc.), types in `newfrontend/lib/api.ts`, css `newfrontend/app/seo.css`

---

### Task 1: GA per-page report (`ga_fetch_pages`)

**Files:**
- Modify: `backend/agents/SEO GEO agent/seo_geo_agent/sources.py` (after `ga_fetch_overview`)
- Test: `backend/agents/SEO GEO agent/seo_geo_agent/tests/test_seo_pages.py` (new)

**Interfaces:**
- Produces: `ga_fetch_pages(prop: str, start: date, end: date, service=None, limit: int = 50) -> list[dict]` — `[{"path": str, "views": int, "sessions": int, "engagement_rate": float}]`, ordered by views desc. Raises `CredentialMissing` on failure.

- [ ] **Step 1: Write the failing test** (reuse `FakeData`/`_Exec` shapes from `tests/test_seo_ga.py` — copy the tiny fakes, don't import across test files):

```python
"""Page-level intelligence tests: GA pages, merge, health flags, AI recs."""
from datetime import date

import pytest

from seo_geo_agent.sources import CredentialMissing, ga_fetch_pages


class _Exec:
    def __init__(self, payload, fail=False):
        self._payload, self._fail = payload, fail

    def execute(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._payload


class FakePagesData:
    def __init__(self, payload):
        self._payload = payload

    def properties(self):
        return self

    def runReport(self, property=None, body=None):
        return _Exec(self._payload)


def test_ga_fetch_pages_parses_rows():
    svc = FakePagesData({"rows": [
        {"dimensionValues": [{"value": "/pricing"}],
         "metricValues": [{"value": "1200"}, {"value": "900"}, {"value": "0.7"}]},
    ]})
    pages = ga_fetch_pages("properties/2", date(2026, 7, 6), date(2026, 8, 3), service=svc)
    assert pages == [{"path": "/pricing", "views": 1200, "sessions": 900, "engagement_rate": 0.7}]
```

- [ ] **Step 2: Run it, verify it fails** with ImportError (`ga_fetch_pages` missing).
- [ ] **Step 3: Implement** in sources.py, mirroring `ga_fetch_overview`'s error handling exactly:

```python
def ga_fetch_pages(prop: str, start: date, end: date, service=None, limit: int = 50) -> list[dict]:
    """Per-page GA traffic, ordered by views — feeds the page intelligence table."""
    svc = service or _ga_service("analyticsdata")
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}, {"name": "engagementRate"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": limit,
    }
    try:
        resp = svc.properties().runReport(property=prop, body=body).execute()
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"Google Analytics rejected {prop}: {exc}") from exc
    return [
        {"path": (r.get("dimensionValues") or [{}])[0].get("value", ""),
         "views": int(float(r["metricValues"][0]["value"])),
         "sessions": int(float(r["metricValues"][1]["value"])),
         "engagement_rate": float(r["metricValues"][2]["value"])}
        for r in resp.get("rows", [])
    ]
```

- [ ] **Step 4: Run the new test file + the full agent suite; all green.**
- [ ] **Step 5: Commit** `feat(seo): GA4 per-page traffic report`.

---

### Task 2: Corpus page facts for health flags

**Files:**
- Modify: `backend/agents/SEO GEO agent/seo_geo_agent/site_brain.py:91` (the `entry = {...}` line in `build_corpus`)
- Test: append to `tests/test_seo_pages.py`

**Interfaces:**
- Produces: corpus page entries additionally carry `"meta_description": str, "h1_count": int, "images_no_alt": int`. Old cached entries may lack them — all consumers must `.get(...)` with defaults.

- [ ] **Step 1: Failing test** (call `build_corpus` with injected fakes — mirror the injection style of existing site_brain callers; sitemap returns one URL, fetch returns a PageFacts):

```python
from seo_geo_agent import site_brain
from seo_geo_agent.sources import PageFacts


def test_corpus_entries_carry_on_page_facts():
    facts = PageFacts(url="https://x.com/a", status=200, title="A", meta_description="d",
                      h1=["A"], word_count=500, text="body")
    corpus = site_brain.build_corpus(
        {"id": "b", "domain": "x.com"},
        fetch=lambda url, client=None: facts,
        sitemap=lambda domain, client=None: ["https://x.com/a"],
    )
    entry = corpus["pages"][0]
    assert (entry["meta_description"], entry["h1_count"], entry["images_no_alt"]) == ("d", 1, 0)
```

(Note: offline mode makes `_summarize_batch` raise CredentialMissing → fallback summaries; that's fine.)
- [ ] **Step 2: Verify it fails** (KeyError).
- [ ] **Step 3: Implement** — extend the entry dict in `build_corpus`:

```python
entry = {"url": url, "title": facts.title, "hash": h, "word_count": facts.word_count,
         "meta_description": facts.meta_description, "h1_count": len(facts.h1),
         "images_no_alt": facts.images_no_alt}
```

- [ ] **Step 4: Full suite green** (existing site_brain tests must not break).
- [ ] **Step 5: Commit** `feat(seo): corpus keeps on-page facts for page health`.

---

### Task 3: Page intelligence module + run wiring + endpoints

**Files:**
- Create: `backend/agents/SEO GEO agent/seo_geo_agent/pages.py`
- Modify: `insights.py` (`run_brand`: refresh pages doc after the GA block), `backend/app/routers/seo_geo.py` (2 endpoints)
- Test: append to `tests/test_seo_pages.py`; router test in `backend/app/routers/tests/test_seo_geo_router.py` (mirror an existing GET test there)

**Interfaces:**
- Consumes: `ga_fetch_pages` (Task 1), corpus entries (Task 2), `QueryStat` rows, `sources.llm_text`, `insights.ctr_at`.
- Produces: module `pages` with:
  - `build_page_intel(brand: dict, corpus_pages: list[dict], ga_pages: list[dict], gsc_rows: list) -> dict` → persisted as `pages-{brand_id}`: `{"brand_id", "at": iso, "ai": bool, "notes": [str], "pages": [PAGE]}` where `PAGE = {"path", "url", "title", "views", "sessions", "engagement_rate", "clicks", "impressions", "position", "best_query", "flags": [str], "recommendation": str, "word_count"}`
  - `latest(brand_id) -> dict | None`
  - `PAGE_FLAGS` labels: `no-title`, `title-long` (>60 chars), `no-meta`, `meta-long` (>160), `thin` (<300 words), `no-h1`, `images-no-alt`
- Router produces: `GET /api/seo-geo/pages/{brand_id}` → `{"pages": doc-or-null}`; `POST /api/seo-geo/pages/{brand_id}/refresh` → rebuilt doc (uses cached corpus; 409 `detail="Run the site analysis first"` when no corpus).

Merge rule: key = URL path (`/pricing`); corpus URLs → `urlparse(u).path or "/"`; GSC `QueryStat.page` same treatment; GA paths already paths. A page appears if it's in ANY source; missing-source fields default to 0/None. Sort: views desc, then clicks desc.

Recommendation rule: try ONE `llm_text` call for the top 25 pages by traffic (system prompt: SEO consultant, return strict JSON `[{"path":..., "recommendation": one imperative sentence}]`); on any failure fall back to `_heuristic_rec(page)` per page and set doc `"ai": False` + note `"AI recommendations unavailable — showing rule-based advice"`. `_heuristic_rec` priority order: no-title → "Write a title tag targeting its main query."; no-meta → "Add a meta description — the snippet is unsold."; thin → "Expand this page past 300 words; it can't rank as-is."; title-long → "Shorten the title under 60 characters."; no-h1 → "Add an H1 matching the target query."; images-no-alt → "Add alt text to images."; healthy+clicks → "Healthy — keep it fresh and add internal links to weaker pages."

- [ ] **Step 1: Failing tests** — at minimum:

```python
from seo_geo_agent import pages as pages_mod
from seo_geo_agent.sources import QueryStat


def _corpus_page(url="https://x.com/pricing", **kw):
    base = {"url": url, "title": "Pricing", "word_count": 800, "meta_description": "d",
            "h1_count": 1, "images_no_alt": 0}
    base.update(kw)
    return base


def test_merge_joins_three_sources_by_path():
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page()],
        ga_pages=[{"path": "/pricing", "views": 100, "sessions": 80, "engagement_rate": 0.5}],
        gsc_rows=[QueryStat(query="cost", page="https://x.com/pricing", clicks=10,
                            impressions=200, ctr=0.05, position=4.0)],
    )
    p = doc["pages"][0]
    assert (p["path"], p["views"], p["clicks"], p["best_query"]) == ("/pricing", 100, 10, "cost")


def test_health_flags_fire():
    doc = pages_mod.build_page_intel(
        {"id": "b", "domain": "x.com"},
        corpus_pages=[_corpus_page(title="", meta_description="", word_count=100, h1_count=0)],
        ga_pages=[], gsc_rows=[],
    )
    flags = doc["pages"][0]["flags"]
    assert {"no-title", "no-meta", "thin", "no-h1"} <= set(flags)


def test_offline_recs_are_honest_heuristics():
    doc = pages_mod.build_page_intel({"id": "b", "domain": "x.com"},
                                     corpus_pages=[_corpus_page(meta_description="")],
                                     ga_pages=[], gsc_rows=[])
    assert doc["ai"] is False  # offline → llm_text raises → heuristic path
    assert "meta description" in doc["pages"][0]["recommendation"].lower()
    assert pages_mod.latest("b")["pages"]  # persisted
```

- [ ] **Step 2: Verify failures** (module missing).
- [ ] **Step 3: Implement `pages.py`** (~120 lines). Skeleton:

```python
"""Page-level intelligence: GA traffic + Search Console + on-page health per page."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlparse

from . import state
from .sources import CredentialMissing, llm_text

MAX_AI_PAGES = 25
SYSTEM = ("You are an SEO consultant. For each page return ONE imperative sentence: "
          "the single most impactful next action. Strict JSON only: "
          '[{"path": str, "recommendation": str}]')


def _path(url: str) -> str:
    return (urlparse(url).path or "/") if "//" in url else (url or "/")

# _flags(entry) -> list[str]; _heuristic_rec(page) -> str  (rules in the task header)
# _ai_recs(pages) -> dict[path, rec]  — one llm_text call, json.loads, strict; raises on bad JSON

def build_page_intel(brand, corpus_pages, ga_pages, gsc_rows) -> dict:
    # merge dicts keyed by _path; compute flags from corpus entry (use .get defaults);
    # best_query = highest-clicks (then impressions) QueryStat per page; position = that row's;
    # sort views desc then clicks desc; AI top MAX_AI_PAGES with heuristic fallback for the rest;
    # on CredentialMissing/ValueError → all heuristic, ai=False, note appended
    ...
    doc = {"brand_id": brand["id"], "at": date.today().isoformat(),
           "ai": ai_used, "notes": notes, "pages": merged}
    state.save(f"pages-{brand['id']}", doc)
    return doc


def latest(brand_id: str) -> dict | None:
    return state.load(f"pages-{brand_id}")
```

Fill in the merge/flags/heuristics fully — no stubs. In `insights.run_brand`, after the GA block insert (never fatal):

```python
    try:
        corpus = state.load(f"corpus-{brand['id']}") or {}
        if corpus.get("pages"):
            ga_pages = []
            if ga:
                ga_pages = ga_fetch_pages(
                    ga["property"], end - timedelta(days=28), end)
            pages_mod.build_page_intel(brand, corpus["pages"], ga_pages, rows)
    except CredentialMissing as exc:
        degraded.append(f"Page analytics: {exc}")
```

(import `from . import pages as pages_mod`, `from .sources import ga_fetch_pages`). Router:

```python
@router.get("/seo-geo/pages/{brand_id}")
def get_pages(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"pages": seo_pages.latest(brand_id)}


@router.post("/seo-geo/pages/{brand_id}/refresh")
def refresh_pages(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    corpus = seo_state.load(f"corpus-{brand_id}") or {}
    if not corpus.get("pages"):
        raise HTTPException(status_code=409, detail="Run the site analysis first")
    rows, _ = _rows_28d(brand)
    ga_pages = []
    prop = brand.get("ga4_property")
    if prop:
        try:
            end = date.today()
            ga_pages = ga_fetch_pages(prop, end - timedelta(days=28), end)
        except CredentialMissing:
            pass
    return seo_pages.build_page_intel(brand, corpus["pages"], ga_pages, rows)
```

(match the router's existing import style/aliases; `_rows_28d` already returns `[], notes` when GSC is missing).
- [ ] **Step 4: All backend tests green.**
- [ ] **Step 5: Commit** `feat(seo): page-level intelligence — GA + GSC + health + per-page recs`.

---

### Task 4: Frontend — Pages tab + Top-10 fix-list framing

**Files:**
- Modify: `newfrontend/lib/api.ts` (types + fetchers), `newfrontend/components/console/seo/SeoAgent.tsx` (new tab), `newfrontend/app/seo.css` (additive `.seo-pages__*`)

**Interfaces:**
- Consumes: `GET /api/seo-geo/pages/{id}`, `POST /api/seo-geo/pages/{id}/refresh` (Task 3 shapes).
- Produces: `SeoPageIntel`/`SeoPagesDoc` types; `seoPages(id)` + `seoPagesRefresh(id)` fetchers; a `"pages"` entry in the `SeoTab` union + `Tabs` items + a `PagesView`.

- [ ] **Step 1: types + fetchers in api.ts** (mirror `seoBrandDetail`):

```ts
export interface SeoPageIntel {
  path: string; url: string; title: string;
  views: number; sessions: number; engagement_rate: number;
  clicks: number; impressions: number; position: number | null;
  best_query: string | null; flags: string[]; recommendation: string;
  word_count: number;
}
export interface SeoPagesDoc {
  brand_id: string; at: string; ai: boolean; notes: string[]; pages: SeoPageIntel[];
}
export const seoPages = (id: string) => getJson<{ pages: SeoPagesDoc | null }>(`/api/seo-geo/pages/${id}`);
export const seoPagesRefresh = (id: string) => postJson<SeoPagesDoc>(`/api/seo-geo/pages/${id}/refresh`, {});
```

- [ ] **Step 2: PagesView in SeoAgent.tsx** — table-style rows (follow the fix-list row pattern): path + title, traffic numbers (`fmt`), flags as `seo-chip seo-chip--sev-medium` chips, recommendation line under each row (prefix "AI:" only when `doc.ai`, else "Rule:"); empty state when no doc: "Run the site analysis first — then refresh this tab." with a Refresh button calling `seoPagesRefresh`. Register tab `{ value: "pages", label: "Pages", count: pagesDoc?.pages.length }` right after "Fix list".
- [ ] **Step 3: Top-10 framing** — in the fix-list render, slice to 10 with numbered rank badges and a "Show all N" toggle button (`useState`), title the section "Top 10 actions".
- [ ] **Step 4: `npx tsc --noEmit` and `npx vitest run` green.**
- [ ] **Step 5: Commit** `feat(seo): Pages tab + Top-10 action framing`.

---

### Task 5: Competitor profiles engine + endpoints

**Files:**
- Modify: `backend/agents/SEO GEO agent/seo_geo_agent/competitors.py` (add functions), `backend/app/routers/seo_geo.py` (2 endpoints)
- Test: `tests/test_seo_competitors.py` (new; check for an existing competitors test file first and extend it instead if present)

**Interfaces:**
- Consumes: `ranks-{b}` doc (snapshots + suggested_competitors), `sitemap_watch`, `fetch_page`, `serper_search`, `keywords.latest`, `insights.ctr_at`, `topics._tokens`.
- Produces:
  - `resolve_top5(brand: dict, ranks_doc: dict | None) -> list[str]` — `brand.get("competitors")` first, topped up from `suggested_competitors`, deduped, max 5.
  - `build_profiles(brand, search=None, fetch=None, fetch_sitemap=None) -> dict` persisted `competitor-profiles-{brand_id}`: `{"at", "notes": [str], "profiles": [PROFILE]}`, `PROFILE = {"domain", "visibility_pct": int, "avg_position": float | None, "keywords_won": [{"keyword","their_position","our_position"}], "recent_posts": [{"url","title","topic","est_monthly_clicks": int | None, "estimate_basis": str}], "hot_topics": [str]}`
  - `latest_profiles(brand_id) -> dict | None`
- Router: `GET /api/seo-geo/competitors/{brand_id}/profiles` → `{"profiles": doc-or-null}`; `POST .../profiles/refresh` → rebuilt doc (503 with detail on CredentialMissing, mirroring `run_site_review`).

Math (from the LATEST snapshot in `ranks-{b}`): for each competitor domain — `visibility_pct` = round(100 × kws-where-domain-in-`top` / tracked-kws); `avg_position` = mean 1-based index+1 of domain in each `top` list where present (None if never); `keywords_won` = kws where domain present AND (our `position` is None OR their index+1 < ours). Content feed: run `sitemap_watch({**brand, "competitors": top5}, ...)`; for each competitor take `new_urls[:5]`, `fetch_page` each for title; `topic` = longest h1 or title; reach estimate ONLY when a keyword-lab keyword's tokens ⊆ title tokens AND that lab entry has `volume_est`: then `serper_search(topic)` → their position → `est_monthly_clicks = round(volume_est * ctr_at(position))`, `estimate_basis = "lab volume × CTR curve"`; else `est_monthly_clicks = None`, basis `"no volume data — reach unknown"` (HONEST). Cap: ≤2 serper calls per competitor; `hot_topics` = top token bigrams across that competitor's new_urls slugs (plain string munging, no AI). No ranks snapshot at all → raise `CredentialMissing("Run a data refresh first — competitor discovery needs rank snapshots")`.

- [ ] **Step 1: Failing tests** — `resolve_top5` manual-first/topped-up/deduped; `build_profiles` visibility+keywords_won math from a crafted ranks doc with 2 snapshots; honest `None` estimate when no volume match; persistence via `latest_profiles`. Inject `search=`/`fetch=`/`fetch_sitemap=` fakes (return simple dicts/PageFacts).
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** (~140 lines in competitors.py, following its existing style).
- [ ] **Step 4: Backend suite green.**
- [ ] **Step 5: Commit** `feat(seo): top-5 competitor profiles — visibility, keywords won, content feed with honest reach estimates`.

---

### Task 6: Frontend — Competitors tab upgrade

**Files:**
- Modify: `newfrontend/lib/api.ts`, `newfrontend/components/console/seo/labs.tsx` (CompetitorsView), `newfrontend/app/seo.css` (additive)

**Interfaces:**
- Consumes: Task 5 endpoints/shapes. Types: `SeoCompetitorProfile`, `SeoCompetitorProfilesDoc` mirroring PROFILE above; fetchers `seoCompetitorProfiles(id)`, `seoCompetitorProfilesRefresh(id)`.

- [ ] **Step 1: types + fetchers** (exact shapes from Task 5).
- [ ] **Step 2: CompetitorsView upgrade** — keep the existing rank-shift content; ADD a "Top competitors" grid above it: one card per profile (domain, visibility % stat, avg position, keywords-won count) expanding (useState) to keywords-won rows + recent posts (title → url link, topic chip, `est_monthly_clicks` shown as `~N clicks/mo (estimate)` ONLY when non-null, else "reach unknown") + hot-topic chips + a Refresh button on the section calling refresh endpoint with toast on 503 detail.
- [ ] **Step 3: tsc + vitest green.**
- [ ] **Step 4: Commit** `feat(seo): competitor profile cards with content feed`.

---

### Task 7: Blog plan upgrade — intent, competitor gaps, anti-cannibalization

**Files:**
- Modify: `backend/agents/SEO GEO agent/seo_geo_agent/topics.py`, `insights.py` (pass new args)
- Test: append to `tests/test_seo_geo.py` (topics tests live there)

**Interfaces:**
- Consumes: corpus pages (Task 2 shapes), `competitor-profiles-{b}` doc (Task 5), `keywords.intent_of`.
- Produces: `build_topics(brand_like, rows, prev_rows, corpus_pages=None, competitor_topics=None)` — same `(topic_list, notes)` return, backward compatible (existing 3-arg calls still pass). Each topic dict gains `"intent": str` and optional `"avoided": bool, "avoided_reason": str`. Competitor-sourced candidates get `source="competitor-content"`. Avoided topics stay in the list (a9 rule: never hidden) but sort after live ones; live list caps at 10.

Rules: `intent = keywords.intent_of(topic keyword)`. Cannibalization: a candidate is avoided when its token set (via `_tokens`) is a subset of — or shares ≥80% of its tokens with — any corpus page's `target_query` or `title` tokens; `avoided_reason = f"overlaps {page['url']}"`. Competitor candidates: from `competitor_topics` (list of `hot_topics` strings + recent-post topics passed by `run_brand`), scored like other sources but never above user-seed topics. In `insights.run_brand`, pass `corpus_pages=(state.load(f"corpus-{brand['id']}") or {}).get("pages")` and `competitor_topics` from `competitors.latest_profiles(brand["id"])` (flatten `hot_topics` + post topics; None-safe).

- [ ] **Step 1: Failing tests** — cannibalizing candidate marked avoided + reason + sorted last; competitor topic appears with `source="competitor-content"`; every topic has `intent`; 3-arg call still works (backward compat).
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** (read topics.py first; extend, don't rewrite scoring).
- [ ] **Step 4: Backend suite green.**
- [ ] **Step 5: Commit** `feat(seo): blog plan — intent, competitor gaps, cannibalization guard`.

---

### Task 8: Frontend — Topics Top-10 + intent + avoided

**Files:**
- Modify: `newfrontend/lib/api.ts` (`SeoTopic` gains `intent?: string; avoided?: boolean; avoided_reason?: string`), `newfrontend/components/console/seo/SeoAgent.tsx` (topics tab render)

- [ ] **Step 1: types.**
- [ ] **Step 2: render** — section title "Next 10 blogs to publish"; live topics numbered 1–10 with an intent chip (`seo-chip`); avoided topics in a collapsed "Avoided (would cannibalize existing pages)" block listing keyword + reason — visible, never hidden.
- [ ] **Step 3: tsc + vitest green.**
- [ ] **Step 4: Commit** `feat(seo): Top-10 blog plan with intent and avoided-topics transparency`.

---

### Task 9: Three-reviewer gate + fixes + final verify

- [ ] **Step 1:** Dispatch three parallel reviewer subagents against the diff since commit `d6612ef` (backend) / `57d0427` (frontend):
  - R1 spec-compliance: every requirement in `docs/superpowers/specs/2026-08-03-seo-user-journey-design.md` → point to code, list gaps.
  - R2 correctness: bugs, edge cases (empty docs, missing keys on old cached corpus entries, degradation paths actually catch), test honesty.
  - R3 simplicity + honest-data: no file sprawl, no parallel rails, every estimate labelled, no fake-AI badging, additive css only.
- [ ] **Step 2:** Fix every confirmed finding (TDD for behavior bugs).
- [ ] **Step 3:** Full verify: backend pytest suite, `npx tsc --noEmit`, `npx vitest run` — all green, output shown.
- [ ] **Step 4:** Final commits; report ready-for-visual-pass (do NOT push — push goes straight to prod).
