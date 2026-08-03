# Blog Writer (a9) rebuild — deep-research writing desk

**Date:** 2026-08-03 · **Status:** awaiting user approval · **Replaces:** the removed
12-step SEO Blog Writer (deleted in backend `1b01596` / frontend `af386f2`).

## Business outcome

A writer opens the Blog Writer, picks a brand, pastes one topic, and gets back a
blog their readers will trust: every claim backed by a cited source the agent
actually read — studies, expert articles, news, and real anecdotes from forums
and communities. They steer the draft paragraph by paragraph, then download it
as Markdown, HTML, or plain text, plus a visual-prompt document that tells a
designer (or the Graphics Designer agent) exactly what images the post needs.

## User journey

1. **Brand catalogue.** Opening the agent shows one panel per brand — the same
   brand registry the SEO agent uses (`seo_geo_agent.insights.list_brands()`),
   so brands are managed in one place. No setup screens (intelligence-first:
   the only thing the agent ever asks for is the topic).
2. **Brand panel = blog inventory.** Opening a brand lists every blog post
   currently on the brand's website (title + URL), from a sitemap scan with a
   Refresh button. This grounds two behaviours: never write what's already
   published, and suggest internal links to existing posts.
3. **Writing desk.** One input: paste the topic. Optional notes field stays
   collapsed and never blocks.
4. **Deep research.** The agent runs a multi-round research loop (see below)
   and shows live progress: which angle it is searching, which sources it is
   reading, how many evidence items it has banked.
5. **Draft review.** The draft renders as blocks (title, intro, sections,
   conclusion, sources). Every block has a comment box. A comment can be a line
   edit ("shorten this"), a rewrite ("make this about solo firms"), or a
   research redirect ("find real numbers for this claim") — the agent picks the
   cheapest action that honours the comment: rewrite from the existing evidence
   ledger, or run targeted research for that block first, then rewrite.
6. **Exports.** Three download buttons — `.md`, `.html`, `.txt` — all rendered
   from the same block structure. A fourth button downloads the visual-prompt
   document.

## Deep research engine (the core)

The goal is the deepest research the stack can honestly do:

- **Angles, not one query.** Each round fans the topic out across fixed source
  classes: (a) studies/statistics/reports, (b) expert how-to and practitioner
  articles, (c) recent news, (d) **anecdotal records** — forums, Reddit,
  community threads, case stories, (e) competing articles on the topic.
- **Read, don't skim.** For each promising SERP hit the agent fetches the page
  (`sources.fetch_text`) and extracts evidence items: claim, supporting quote,
  URL, source class, date if visible, and a credibility note.
- **Evidence ledger.** All items land in one deduplicated ledger. The draft may
  only assert what the ledger supports; each block stores the ledger ids it
  cites, and the Sources section is generated from the ledger.
- **Gap-driven rounds.** After each round the agent lists what's still
  unsupported (missing numbers, missing counter-view, no anecdote yet) and
  turns the gaps into next-round queries. It stops at saturation (a round adds
  no new evidence) or the round cap (default 4; "go deeper" button runs more).
- **Request-safe execution.** One round ≈ one `POST /research/step` call
  (30–60s), so Cloud Run timeouts are never hit and the desk shows real
  progress instead of a spinner.
- **Honest degradation.** Research needs the Serper key (`SEO_SERPER_API_KEY`,
  the same seam the SEO agent uses). Without it the desk says exactly that and
  refuses to fabricate research. No key → no fake citations, ever.

## Visual-prompt document

After the draft settles, the agent plans the post's visuals itself: how many
(driven by post length/structure), what kind (hero, data chart, process
diagram, illustration, screenshot-style), what theme (tied to the brand and the
section's content), and where each sits. Output = a standalone document with
one entry per visual: placement, type, theme, and a generation-ready prompt.
Downloadable as `.md`/`.txt` alongside the blog exports.

## Approaches considered

- **A — lean package on existing seams (recommended).** New
  `blog_writer_agent` package that imports `seo_geo_agent.sources` for search /
  fetch / sitemap / LLM (the exact pattern the old a9 used) and the shared
  brand registry. Fewest new files, one place to manage keys and brands.
- **B — build inside the SEO agent.** Rejected: the user explicitly separated
  blog writing from the SEO machinery; a2 stays an analyst, a9 stays a writer.
- **C — third-party deep-research API.** Rejected: another key, per-run cost,
  no control over source classes (anecdotes), and we already own search+fetch.

## Shape (fewest files that do the job)

Backend — `agents/Blog Writer agent/blog_writer_agent/`:

| File | Job |
|---|---|
| `state.py` | run + inventory persistence (Firestore, in-memory fallback — house pattern) |
| `inventory.py` | sitemap scan → blog list per brand |
| `research.py` | angle fan-out, evidence ledger, gap-driven rounds, saturation |
| `drafting.py` | outline + block drafting from the ledger; per-block revision ops |
| `visuals.py` | visual plan + prompt document |
| `export.py` | md / html / txt renderers |
| `llm.py` | thin adapter stamping `agent_id="a9"` |
| `tests/` | offline tests per module |

Wiring: `app/routers/blog_writer.py` (`/api/blog`), sys.path root in
`app/__init__.py`, router mount in `main.py`, a9 restored in
`agent_config.AGENTS` (`openrouter_model` = research+writing model).

Frontend: `components/console/blogwriter/` (one main view + small
catalogue/desk/draft subcomponents), `app/blogwriter.css`, an a9 section in
`lib/api.ts`, nav + route in `ConsoleApp.tsx`, a9 live again in
`console-data.ts`.

## Risk ledger

| Risk | Level | Note |
|---|---|---|
| Coupling | Low–Med | one deliberate seam: `seo_geo_agent.sources` (search/fetch/LLM) + `insights.list_brands()`; same imports the old a9 shipped with; covered by tests so an upstream change fails loudly |
| Rigidity | Low | draft is a JSON block list; exports and revision ops all derive from it, so new export formats or block types bolt on |
| Hardcode % | ~5% | angle list, round cap, per-round source cap — named constants in `research.py`; zero brand/topic hardcodes |
| Key dependency | Med | real research requires `SEO_SERPER_API_KEY`; behaviour without it is an explicit refusal, not degradation into fiction |
| Prod risk now | None | everything sits unpushed on `feat/seo-user-journey` in both repos |

## Out of scope

CMS auto-publish, generating the images themselves (the visual-prompt doc is
the hand-off; the Graphics Designer agent can consume it later), and the old
keyword-gap/Ahrefs-paste SEO machinery (deleted with the old a9).
