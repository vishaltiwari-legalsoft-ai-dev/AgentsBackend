# SEO Blog Writer Agent — Design Spec

Date: 2026-07-30
Status: Approved design (user-reviewed conversationally); pending written-spec review
Source of truth for the workflow: the content team's 12-step "Legal Soft Content Creation Steps" doc (provided by Vishal, 2026-07-30)

## 1. Business outcome

A new hub agent that turns the content team's 12-step manual SEO blog process (~4-6 hours per post) into a 3-gate assisted pipeline (~15-20 minutes of writer time: kickoff + three approvals + final edit). The output bar is a publish-ready draft with a compliance checklist proving every rule in the team's process was followed — not generic AI prose.

The team's process is the spec. Every one of the 12 steps maps to an agent phase (table in §4). Nothing in the team's craft is dropped; the agent does the labor, the writer keeps the judgment.

## 2. Decisions locked with the user (2026-07-30)

| Decision | Choice |
|---|---|
| Ahrefs-dependent data (volume, competitor organic keywords, DR) | **Paste-in kickoff primary** (writer pastes Ahrefs exports, ~5 min) **+ Serper-only fallback** when nothing is pasted, with honest provenance labeling |
| Hub placement | **New dedicated agent tile** ("SEO Blog Writer", new agent id). Engine reuses a2's `sources.py` via import; a3 Copywriter slot untouched |
| Review checkpoints | **3 gates**: keyword sheet → outline+citations → draft |
| Writing/brand guidelines (team step 11) | No team docs exist yet → **build v1 defaults** distilled from Legal Soft's live blogs, kept as an editable file; replaced by team docs later |
| Draft delivery | **Console editable draft + one-click copy + .md/.docx download**. No CMS/Google Doc integration in v1 |

## 3. Risk ledger (upfront)

| Risk | Level | Containment |
|---|---|---|
| Coupling | Low-Med | Single import boundary: blog engine imports `serper_search`, `fetch_page`, `llm_json`/`llm_text` from a2's `sources.py`. Nothing else of a2 is touched. If a2's signatures change, one adapter file absorbs it. |
| Rigidity | Low | Each stage persists its own JSON artifact (a2's `state.py` pattern: local JSON + Firestore in cloud). Any stage can be independently re-run. |
| Hardcode % | ~10% | All tunables in one `rules.py`: DR threshold (70), keyword-count rule (+0 to +2 over #1 page), word/link targets (+10-20%), LSI count (10), evaluator loop cap (3), citation retry cap (3). |
| Top quality risk | Citation hallucination | Every citation URL must be live-fetched and the claim verified on-page before it enters the outline. No verification → no citation. See §8. |

## 4. Workflow mapping — team's 12 steps → agent phases

| Team step | Agent phase | Stage/Gate |
|---|---|---|
| 1. Keyword in Ahrefs, US-only | Kickoff form: keyword + US geo lock + optional Ahrefs paste-in (keyword metrics, competitor organic keywords) | Stage 1 |
| 2. SERP overview, top-3 + SERP features (PAA, video, AI Overview) | Serper SERP fetch; features recorded | Stage 1 |
| 3a. Intent / page type / audience of top-3 | LLM classification per competitor page | Stage 1 |
| 3b. Competitor organic-keyword gap (Ahrefs Site Explorer) | Parse pasted competitor-keywords CSVs → overlap/gap analysis; fallback: SERP-derived related terms, labeled `serp_estimated` | Stage 1 |
| 4. Keyword usage count on #1 page, density, LSI top-10 | Fetch #1 page → exact-count main keyword (target = same to +2), frequent-terms benchmark, LSI candidates filtered to 10 natural fits | Stage 1 → **Gate 1: Keyword Target Sheet** |
| 5. Top-3 section outlines (H1-H4, FAQs) + feature audit (E-E-A-T, key takeaways, tables, tools, what they lack) | `fetch_page` heading extraction + LLM feature audit per competitor | Stage 2 |
| 6. Meta title/description/URL analysis → better unique ones | Meta extraction + drafted meta for our article | Stage 2 |
| 7. External-link count + word count of top-3 → targets +10-20% | Computed from fetched pages | Stage 2 |
| 8. Build our outline (cover all + add what they lack + weave keywords) → LLM evaluator vs top-3 outlines | Outline generation → evaluator loop (score coverage/intent/differentiation, revise, max 3 loops, then surface best + honest scorecard) | Stage 2 |
| 9. Find studies/stats with exact URLs, specific attribution; DR 70+ only | LLM citation candidates → live URL fetch → claim-on-page verification → DR vetting: candidate domains listed at Stage 2 with a paste box for their Ahrefs DR values (citation domains only exist after candidates are found, so this paste cannot happen at kickoff); 70+ enforced on provided values, rest flagged "DR unverified" → retry failures (max 3 rounds) | Stage 2 |
| 10. Final outline with citations mapped to sections | Merge → **Gate 2: Final Outline** (inline-editable) | Stage 2 |
| 11. Guidelines + rules + outline → Claude draft | v1 guidelines file + Final Outline + hard rules → full draft | Stage 3 |
| 12. Written draft | Draft + compliance checklist → **Gate 3: writer edits, copies, downloads** | Stage 3 |

## 5. User flow (3 screens = 3 gates)

1. **Research** — kickoff form: main keyword, US geo (fixed v1), two optional paste boxes (Ahrefs keyword metrics; competitor organic-keywords CSV per top-3 URL). Agent produces the **Keyword Target Sheet**: keyword metrics, SERP features present, per-competitor intent/page-type, gap keywords tagged main/secondary/long-tail/AI-overview-opportunity, usage benchmarks, LSI top-10. Writer edits (add/remove/retag keywords) and approves. Mixed intent across top-3 → flagged here for the writer to resolve.
2. **Outline** — competitor outlines side-by-side, feature-audit chips (has/lacks), drafted meta title/description/URL, word-count and link-count targets, verified citations with a DR paste box (writer pastes Ahrefs DR values for the candidate citation domains; 70+ enforced on provided values, DR badge or "unverified" flag per citation), evaluator scorecard (our outline vs each competitor). Outline is inline-editable; writer approves.
3. **Draft** — full draft in an editable view; right rail shows the live **compliance checklist**: main keyword count in range, 10/10 LSI present, word count in target band, all citations present with specific attribution, all outline sections/FAQs/features present, meta attached. One-click copy; download as .md or .docx.

A runs list shows the writer's previous blogs (keyword, date, stage, status) so runs can be resumed at any gate.

## 6. Data provenance rules (no-fake-fallbacks)

- Every artifact carries `data_source`: `"ahrefs_pasted"` or `"serp_estimated"`.
- No Ahrefs paste → volume/DR columns are absent, replaced by an honest note ("DR unverified — paste Ahrefs DR list to enforce 70+"). Estimated numbers are never presented as Ahrefs data.
- Degraded operations append to a `degraded` notes list on the artifact (a2 pattern) shown in the UI.

## 7. Architecture

### Backend engine — new package `backend/agents/SEO Blog agent/seo_blog_agent/` (~8 files)

| Module | Responsibility |
|---|---|
| `research.py` | Steps 1-4: SERP intel, competitor classification, keyword gap, usage benchmarks, LSI |
| `outline.py` | Steps 5-8: competitor outline extraction, feature audit, meta drafting, targets, outline generation + evaluator loop |
| `citations.py` | Steps 9-10: candidate sourcing, live verification, DR vetting, section mapping |
| `drafting.py` | Steps 11-12: draft generation + compliance checker |
| `ahrefs_paste.py` | Tolerant parsers: Ahrefs CSV/text exports → typed data |
| `rules.py` | Every threshold/target constant in one place |
| `state.py` | Run persistence (a2 pattern: local JSON dev / Firestore cloud) |
| `guidelines.md` | v1 writing + brand guidelines (distilled from Legal Soft's live blogs); editable file, swapped for team docs later |

Imports from a2 (`seo_geo_agent.sources`): `serper_search`, `fetch_page`, `llm_json`, `llm_text`, `CredentialMissing`. Nothing else.

### API — new router `app/routers/seo_blog.py`

- `POST /seo-blog/runs` — kickoff (keyword + optional pastes)
- `GET /seo-blog/runs` / `GET /seo-blog/runs/{id}` — list / detail
- `POST /seo-blog/runs/{id}/approve-keywords` — Gate 1 (accepts writer edits)
- `POST /seo-blog/runs/{id}/build-outline` — Stage 2 compute
- `POST /seo-blog/runs/{id}/vet-citations` — pasted DR values → re-run citation vetting
- `POST /seo-blog/runs/{id}/approve-outline` — Gate 2 (accepts inline edits)
- `POST /seo-blog/runs/{id}/draft` — Stage 3 generation
- `PATCH /seo-blog/runs/{id}/draft` — save writer edits
- `GET /seo-blog/runs/{id}/export?format=md|docx` — download

Auth: same `get_current_user` dependency as other agent routers.

### Frontend

- New hub tile **"SEO Blog Writer"** (new agent id in `lib/console-data.ts`; a3 Copywriter untouched).
- One view file: 3-stage studio (stage pills, working indicator, runs list) following existing console patterns; additive CSS.

## 8. Error handling & escalation

- Serper or LLM credentials missing → run refuses to start with a clear message (`CredentialMissing` pattern).
- Mixed search intent in top-3 → Gate 1 flag; writer decides direction.
- Evaluator loop fails to beat competitors in 3 iterations → present best version with the honest scorecard; writer decides.
- Citation target unmet after 3 retry rounds → ship with verified citations only + "short by N" flag. An unverified citation never enters the outline.
- Competitor page fetch fails (paywall/bot-block) → that competitor's columns marked unavailable, analysis proceeds on the rest, `degraded` note added.

## 9. Testing

Offline-first, matching a2: SERP/page fixtures, fake LLM, no live calls in CI (existing conftest offline guard). Coverage focus: paste parsers per Ahrefs export format, gap analysis, keyword-count benchmarking, citation verifier (live/dead/claim-mismatch/DR cases), compliance checker, gate state machine. Bar: pytest green + tsc clean.

## 10. Out of scope (v2 candidates)

- DataForSEO (or Ahrefs API) full-auto data — replaces paste-in
- Google Doc export / WordPress publish
- Guidelines upload-and-edit panel in the console
- Multi-geo (v1 is US-only, matching the team's process)
- Internal-link suggestions from GSC data (a2 already holds this data; natural v2 tie-in)
