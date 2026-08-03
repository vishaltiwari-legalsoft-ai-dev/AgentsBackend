# SEO agent — full user-journey dashboard (pages, competitors, blog plan)

**Date:** 2026-08-03 · **Status:** approved · **Execution:** subagent-driven, cumulative (all phases), 3-reviewer gate at the end.

## Business goal

Complete the a2 SEO agent so it matches the user's 7-step journey: brand
catalogue → brand dashboard (GA + page-level analytics + keyword pool +
summary + per-page AI recommendation) → Top-10 action list → top-5
competitor analysis → competitor content feed with estimated reach →
Top-10 non-cannibalizing blog topics.

Already built (do NOT rebuild): brand catalogue/cards, GA4 overview
(`run["ga"]`, shipped 2026-08-03), keyword lab, SEO summary + insight
bullets, ranked fix list with what/why/impact, competitor rank tracking +
discovery + sitemap watch, blog topics engine, briefs, site review.
Serper key is set locally in `backend/.env` and live-verified.

## New in this build

1. **Page-level intelligence (Phase 1)** — one table per brand: every page
   with GA traffic (sessions/views/engagement), Search Console clicks +
   best query + position (when available), on-page health flags
   (title/meta/thin/h1/alt) from the crawl corpus, and a one-line
   recommendation per page (AI; honest heuristic fallback labelled
   `ai:false` — NEVER badge a fallback as AI).
2. **Top-10 framing (Phase 1)** — Fix list shows a numbered Top 10 with a
   "show all" expander.
3. **Competitor profiles (Phase 2)** — top 5 competitors (manual list on
   the brand wins, else SERP-discovered): visibility % across tracked
   keywords, average position, keywords they beat us on, recent posts from
   the sitemap feed with topic + labelled reach estimate.
4. **Blog plan upgrade (Phase 3)** — topics gain search intent; competitor
   new-content topics feed the candidate pool; candidates that overlap an
   existing site page are marked avoided (shown, never hidden — a9 rule);
   dashboard frames the Top 10.

## Constraints (project law)

- Graceful degradation everywhere: every external call path raises
  `CredentialMissing` and callers record a plain-language note. No crash,
  no fake numbers, estimates always labelled.
- No new pip/npm dependencies. No parallel rails — extend the existing
  run/state/router seams.
- Business-first UI copy; reuse `seo-*` css patterns; additive css only.
- Tests: pytest per module (offline, fake services), tsc + vitest clean.

## Verification gate

All prior seo tests + new ones green; tsc; vitest; then THREE reviewer
agents (spec compliance, correctness/bugs, simplicity+honest-data) and
their findings fixed before the work is called done.
