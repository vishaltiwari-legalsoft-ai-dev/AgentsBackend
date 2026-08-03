# Blog Writer (a9) Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the deep-research Blog Writer per the approved spec (`docs/superpowers/specs/2026-08-03-blog-writer-rebuild-design.md`): brand catalogue → blog inventory → topic desk → gap-driven research with an evidence ledger → per-block revision → md/html/txt + visual-prompt exports.

**Architecture:** New lean `blog_writer_agent` package that imports `seo_geo_agent.sources` (Serper search, page fetch, sitemap, LLM adapters) and `seo_geo_agent.insights.list_brands()` — the exact seams the old a9 used. Own Firestore collection (`blog_writer`) with a local-JSON offline fallback mirroring `seo_geo_agent/state.py`. One research round per HTTP call so Cloud Run never times out.

**Tech Stack:** FastAPI router at `/api/blog`, pytest offline tests (injectable `search`/`fetch`/`llm` fakes — the `site_brain.build_corpus` pattern), Next.js console view + house CSS file, OpenRouter LLM via `agent_id="a9"`.

## Global Constraints

- Honest degradation everywhere: no `SEO_SERPER_API_KEY` → `CredentialMissing` surfaces as HTTP 424 with the real message; the LLM/UI never fabricates research, citations, or visuals (standing user rule: no fake AI fallbacks).
- Draft blocks may cite only ledger ids that exist; unknown ids are dropped and noted in `notes`.
- Fewest files that do the job; touch only Blog Writer files + minimal wiring (standing user rule).
- All agent tests run fully offline (`BLOG_OFFLINE=1` conftest, fakes injected).
- Every LLM call goes through `blog_writer_agent.llm` so `agent_id="a9"` reaches `app.services.openrouter.get_llm`.
- Frontend bar: `npx tsc --noEmit` clean + vitest green; backend bar: pytest green (9 GD failures are known pre-existing).

## Risk ledger (user-mandated)

Coupling: one deliberate seam into `seo_geo_agent` (sources + brand registry), test-pinned. Rigidity: draft = JSON block list, exports derive from it. Hardcode: ~5% — angle list + round/read caps as named constants in `research.py`; zero brand hardcodes. Prod: nothing pushed until user go.

---

### Task 1: Package skeleton — llm adapter + state + wiring + conftest

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/__init__.py` (module map docstring)
- Create: `agents/Blog Writer agent/blog_writer_agent/llm.py`
- Create: `agents/Blog Writer agent/blog_writer_agent/state.py`
- Create: `agents/Blog Writer agent/blog_writer_agent/tests/__init__.py`, `tests/conftest.py`
- Test: `agents/Blog Writer agent/blog_writer_agent/tests/test_state.py`
- Modify: `app/__init__.py` (add `_BACKEND_ROOT / "agents" / "Blog Writer agent"` to `_AGENT_ROOTS`)

**Interfaces (produced):**
- `llm.llm_json(system: str, prompt: str)` / `llm.llm_text(system: str, prompt: str)` — delegate to `seo_geo_agent.sources.llm_json/llm_text` with `agent_id="a9"`.
- `state.save(doc_id: str, data: dict)`, `state.load(doc_id: str) -> dict | None`, `state.delete(doc_id: str)`, `state.use_cloud() -> bool` — collection `blog_writer`, offline switch `BLOG_OFFLINE=1`, local dir override `BLOG_LOCAL_DIR` (mirror of `seo_geo_agent/state.py`, minus the .env loader — reuse seo_geo's, which already ran).

**Steps:**
- [ ] conftest sets `BLOG_OFFLINE=1` + `BLOG_LOCAL_DIR=tmp_path` (autouse fixture)
- [ ] `test_state.py`: save/load roundtrip; load missing → None; delete removes; JSON-unsafe values (date) survive via round-trip
- [ ] Run tests → fail (module missing) → implement `state.py` → pass
- [ ] Implement `llm.py` (3 lines each fn); a9-binding test lands in Task 8 with the app-level suite
- [ ] Wire `app/__init__.py`; commit `feat(blog): blog_writer_agent skeleton — state + a9 llm adapter`

### Task 2: inventory.py — brand blog inventory

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/inventory.py`
- Test: `tests/test_inventory.py`

**Interfaces (produced):**
- `scan(brand: dict, *, sitemap=None, fetch=None) -> dict` — sitemap URLs → blog-post filter → titles for up to `TITLE_CAP=150` posts → `{"domain", "scanned" (iso), "posts": [{"url","title"}], "counts": {"sitemap_urls","blog_urls","titled"}, "notes": [str]}`; persists to `inventory-{brand_id}` and returns it.
- `latest(brand_id: str) -> dict | None` — loads the stored inventory.
- `BLOG_PATH_HINTS` — `("/blog", "/post", "/article", "/insights", "/news", "/resources", "/guides")`; plus `/YYYY/MM/` date-pattern URLs count as posts.

**Steps:**
- [ ] Tests with fake `sitemap`/`fetch`: non-blog URLs excluded; date-pattern URL included; cap respected with an honest `notes` entry ("titled first 150 of N"); result persisted (state.load returns it); `fetch` errors on one URL don't kill the scan (post kept with path-derived title, note added)
- [ ] Fail → implement → pass → commit `feat(blog): per-brand blog inventory from sitemap`

### Task 3: research.py — gap-driven deep-research loop

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/research.py`
- Test: `tests/test_research.py`

**Interfaces (produced):**
- Constants: `ANGLES` (5 dicts: `key` ∈ studies|experts|news|anecdotes|competitors, `query_tpl` e.g. `"{topic} statistics study"`, `"{topic} reddit experience"`), `ROUND_CAP=4`, `QUERIES_PER_ROUND=6`, `READS_PER_ROUND=8`, `MINI_READS=3`
- `new_run(brand: dict, topic: str, notes: str = "") -> dict` — `{"id" ("bw-" + slug + "-" + uuid4hex[:6]), "brand_id", "brand_name", "domain", "topic", "notes", "created" (iso), "status": "research", "rounds": [], "ledger": [], "gaps": [], "draft": None, "visuals": None}`; persisted `run-{id}`; appends `{id, brand_id, topic, created, status}` to `runs-index` doc.
- `research_step(run: dict, *, search=None, fetch=None, llm=None) -> dict` — one round: build queries (round 1 from `ANGLES`, later from `run["gaps"]`, capped `QUERIES_PER_ROUND`), Serper each, collect unread organic URLs, `fetch` up to `READS_PER_ROUND`, LLM-extract evidence items `{"id" ("ev-N"), "claim", "quote", "url", "source_name", "source_class", "date", "credibility"}`, dedupe (url+claim), extend ledger, LLM gap list → `run["gaps"]`; round record `{"n","queries":[{"angle","q","hits"}],"read":[urls],"added",  "gaps"}`; 0 new items → `status="saturated"`; `len(rounds)>=ROUND_CAP` → `status="capped"` (a further step call still allowed — "go deeper"); persists + returns run. `search=None` and Serper unavailable → raise `sources.CredentialMissing`.
- `mini_research(run: dict, queries: list[str], *, search, fetch, llm) -> list[dict]` — targeted ≤`MINI_READS` reads for block revision; returns the new ledger items (already appended + persisted).
- `save_run(run)` / `load_run(run_id) -> dict | None` / `list_runs() -> list[dict]` (from `runs-index`, newest first; status kept in sync).

**Steps:**
- [ ] Tests (fake search returns organic hits, fake fetch returns text, scripted fake llm): round 1 queries cover all 5 angles; evidence lands in ledger with ids; round 2 queries come from gaps; duplicate claims not re-added; zero-new → saturated; cap → capped; no search + no key (monkeypatch `serper_available` False) → CredentialMissing; runs-index ordering
- [ ] Fail → implement → pass → commit `feat(blog): gap-driven deep-research loop with evidence ledger`

### Task 4: drafting.py — ledger-grounded blocks + per-block revision

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/drafting.py`
- Test: `tests/test_drafting.py`

**Interfaces (produced):**
- `build_draft(run: dict, inventory: dict | None, *, llm) -> dict` — LLM gets topic, brand, ledger digest, existing post titles (overlap-avoidance + internal-link candidates); returns run with `run["draft"] = {"meta": {"title","description","slug"}, "blocks": [{"id" ("b1"...), "kind": "intro"|"section"|"conclusion", "heading", "text", "cites": [ev ids], "history": []}], "internal_links": [{"url","title"}], "notes": [str]}`; cites filtered to real ledger ids (dropped ids noted); persisted.
- `revise_block(run: dict, block_id: str, comment: str, *, llm, search=None, fetch=None) -> dict` — LLM classifies `{"action": "rewrite"|"research", "queries": [...]}`; `research` → `research.mini_research(run, queries, ...)` then rewrite with fresh ledger; rewrite pushes old text onto `block["history"]`, updates `text`/`cites` (validated), records `block["last_comment"]`; unknown block_id → `KeyError`; persisted.

**Steps:**
- [ ] Tests (scripted fake llm): draft blocks carry only valid cites + bogus id dropped with note; existing-post titles reach the LLM prompt (fake records it); rewrite path: history grows, text changes, no search called; research path: fake classifier returns research → mini_research fakes add `ev-N` items and the rewrite cites one; `KeyError` on bad block id
- [ ] Fail → implement → pass → commit `feat(blog): ledger-grounded draft blocks + per-block revision desk`

### Task 5: visuals.py — visual plan + prompt doc data

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/visuals.py`
- Test: `tests/test_visuals.py`

**Interfaces (produced):**
- `plan_visuals(run: dict, brand: dict, *, llm) -> dict` — requires a draft (else `ValueError`); LLM decides count/type/theme per the draft's blocks + brand; validates each entry `{"n", "section" (block heading), "type" ∈ hero|chart|diagram|illustration|photo, "theme", "prompt" (non-empty), "rationale"}`; at least 1 entry else honest `ValueError("visual plan came back empty")`; stores `run["visuals"] = {"items": [...], "notes": []}`; persisted.

**Steps:**
- [ ] Tests: valid plan stored; empty/malformed LLM output raises (no fabricated fallback plan); no draft → ValueError
- [ ] Fail → implement → pass → commit `feat(blog): agent-decided visual plan`

### Task 6: export.py — md / html / txt + visual-prompt renderers

**Files:**
- Create: `agents/Blog Writer agent/blog_writer_agent/export.py`
- Test: `tests/test_export.py`

**Interfaces (produced):**
- `to_markdown(run) -> str` — H1 title, intro, `##` sections, conclusion, in-text `[n]` markers from cites (n = 1-based ledger order of cited items), `## Sources` numbered list (name — url).
- `to_html(run) -> str` — standalone semantic HTML (`<!doctype html>` head with title+meta description, `<article>`, `<sup>[n]</sup>` cites, sources `<ol>`); no external assets.
- `to_text(run) -> str` — plain text, headings upper-cased, sources appended.
- `visuals_markdown(run) -> str` / `visuals_text(run) -> str` — one entry per visual: placement, type, theme, prompt.
- All raise `ValueError` when the needed stage (`draft` / `visuals`) is missing.

**Steps:**
- [ ] Tests: md has title/sections/`[1]`/Sources; html is standalone + escapes `<` in text; txt has no `#`/`<`; visuals docs list every entry; missing-stage ValueError
- [ ] Fail → implement → pass → commit `feat(blog): md/html/txt + visual-prompt exports`

### Task 7: Router `/api/blog`

**Files:**
- Create: `app/routers/blog_writer.py`
- Test: `app/routers/tests/test_blog_writer_router.py`
- Modify: `app/main.py` (import `blog_writer`, add to router loop)

**Interfaces (produced):** all auth = signed-in user (`get_current_user`); `CredentialMissing` → 424 with the exception message; `KeyError`/missing run → 404; stage-order violations → 409.
- `GET  /blog/brands` — enabled brands from `insights.list_brands()` + each brand's stored inventory counts (`inventory.latest`)
- `POST /blog/brands/{brand_id}/inventory` — run `inventory.scan` live; `GET` same path returns stored
- `POST /blog/runs` `{brand_id, topic, notes?}` (422 on blank topic, 404 unknown brand) → run
- `GET  /blog/runs` / `GET /blog/runs/{run_id}`
- `POST /blog/runs/{run_id}/research/step` — one round (424 without key)
- `POST /blog/runs/{run_id}/draft` — 409 until ledger non-empty
- `POST /blog/runs/{run_id}/blocks/{block_id}/comment` `{comment}` — revision (404 bad block)
- `POST /blog/runs/{run_id}/visuals` — 409 until draft exists
- `GET  /blog/runs/{run_id}/export?format=md|html|txt|visuals-md|visuals-txt` — Content-Disposition download; 404 until the stage exists

**Steps:**
- [ ] Router tests: TestClient full happy path with monkeypatched module fns (`research.research_step` etc. faked at router-import site) + each error path above
- [ ] Fail → implement router + wiring → pass → run **whole** backend suite → commit `feat(blog): /api/blog router — catalogue, desk, research, revision, exports`

### Task 8: a9 restored in Agent Config + override binding test

**Files:**
- Modify: `app/services/agent_config.py` (a9 back: name "Blog Writer", role "Deep-research blog drafts", category "copy", live True, fields `["openrouter_model"]`)
- Modify: `tests/test_agent_model_overrides.py` (docstring line back; admin payload set `{"a1","a6","a9"}` + `by_id["a9"]["fields"] == ["openrouter_model"]`; new `test_blog_llm_adapter_passes_a9` — monkeypatch `openrouter_service.get_llm`, `sources.state.use_cloud` True, call `blog_writer_agent.llm.llm_json`, assert `seen == ["a9"]`)

**Steps:**
- [ ] Tests first (fail: a9 missing) → restore entry → pass → commit `feat(blog): a9 Blog Writer live in Agent Config (writing/research model)`

### Task 9: Frontend — api surface + Blog Writer console view

**Files:**
- Modify: `lib/api.ts` (new a9 section: `BlogBrand`, `BlogInventory`, `BlogEvidence`, `BlogRound`, `BlogBlock`, `BlogDraft`, `BlogVisuals`, `BlogRun`, `BlogRunSummary` types mirroring the router JSON; calls `blogBrands()`, `blogInventory(brandId)`, `blogScanInventory(brandId)`, `blogRuns()`, `blogRun(id)`, `blogCreateRun({brand_id, topic, notes})`, `blogResearchStep(id)`, `blogBuildDraft(id)`, `blogCommentBlock(id, blockId, comment)`, `blogPlanVisuals(id)`, `blogExport(id, format) -> Blob`)
- Create: `components/console/blogwriter/BlogWriter.tsx` (catalogue → brand panel → desk → research live progress → draft blocks with per-block comment boxes → exports); split `DraftDesk` into `components/console/blogwriter/DraftDesk.tsx` if the main file passes ~800 lines (house ceiling: SeoAgent.tsx 794)
- Create: `app/blogwriter.css` (`.bw-*` classes, house tokens); Modify: `app/globals.css` (import)
- Modify: `components/console/ConsoleApp.tsx` (import BlogWriter, `"blog"` back in NAV_VIEWS, route line), `lib/console-data.ts` (BLOG_AGENT_ID + LIVE_AGENTS `"blog"` mapping back — tile copy already updated)

**UI notes:** brand catalogue = panel cards w/ inventory counts + "Open desk"; brand panel = two columns (published-blogs list w/ refresh · writing desk w/ one topic input + collapsed notes); research stage = live round ticker (angle chips, sources-read list, ledger counter, Go deeper + Write draft buttons); draft stage = block cards each w/ cite chips + comment box + "History" collapse; export bar = 4 downloads (md/html/txt/visual prompts). Every degraded/error string rendered verbatim from the backend — no invented copy.

**Steps:**
- [ ] api.ts types+calls → BlogWriter view → css → wiring → `npx tsc --noEmit` clean → `npx vitest run` green → commit `feat(blog): Blog Writer console — catalogue, desk, research, revision, exports`

### Task 10: Full verification + memory

**Steps:**
- [ ] Backend: full pytest (only the 9 known pre-existing GD failures allowed)
- [ ] Frontend: tsc + vitest green
- [ ] Update memory progress file; leave push decision to the user
