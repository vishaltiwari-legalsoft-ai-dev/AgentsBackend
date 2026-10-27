"""PR Writer agent (a13) — the AI core: content-type specs, prompts, QA, screening.

Blueprint: ghi/docs/pr-writer-blueprint.md. Platform plan:
.claude/agent-memory/senior-systems-engineer/project-pr-writer-plan-2026-10-27.md

- ``content_types`` — the eight content types as data (lengths, parts, quotes,
                      boilerplates, byline rules). One source the prompts, the
                      renderer and QA all read.
- ``evidence``      — the evidence list drafting may cite. Every id, URL, date
                      and owner is stamped by code, never by the model.
- ``llm``           — model adapter (agent_id="a13"): one call, local JSON
                      repair, at most one model repair, then an honest raise.
- ``prompts``       — per-step prompts, strict output schemas, and the step
                      plan (slot, timeout, call budget) the router executes.
- ``escalation``    — keyword prefilter + classifier for litigation /
                      regulatory / data incident / layoffs / M&A / crisis.
                      A classifier failure raises; it never defaults to safe.
- ``render``        — structured draft -> markdown (release line, dateline,
                      quote markers, verbatim boilerplate, media contact, ###).
- ``qa``            — pure deterministic checks over a draft.

No persistence and no HTTP here on purpose: the router (app/) owns requests,
the shared store owns state. Everything in this package is callable offline
with an injected ``call``.
"""
