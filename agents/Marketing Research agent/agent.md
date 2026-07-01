# BUILD SPEC — Legal Soft Marketing Research Agent

> **Scope:** Marketing department only. This document is the self-contained build
> spec. Source requirements: `Marketing AI Agent .md`. Design: `docs/superpowers/specs/2026-06-30-marketing-research-agent-design.md`.

## 1. What this agent is

An AI research agent scoped to the Marketing **Coordinator** functions. It
eliminates manual research bottlenecks by aggregating campaign data, monitoring
competitors, analyzing the lead funnel, and surfacing media opportunities — then
emitting scheduled reports and triggered alerts.

It is a sibling of the Graphics Designer agent and follows the same conventions:
a self-contained package under `backend/agents/`, registered on `sys.path` by
`app/__init__.py`; a FastAPI router under `app/routers/`; a console view in the
Next.js frontend; file-based LLM prompts; runs persisted to disk/Firestore; LLM
via the shared OpenRouter service with a deterministic offline fallback.

Agent catalog id: **`a6`** ("Market Researcher" slot; Graphics Designer is `a1`).

## 2. Non-negotiable rules

1. **Adapter layer for data.** All marketing data enters through the
   `DataSource` interface (`sources/base.py`). CSV/Excel **export ingestion**
   (`sources/csv_source.py`) ships today and needs no external credentials. Live
   Google Ads / META / HubSpot connectors implement the *same* interface and are
   credential-gated stubs until keys are provisioned — no module or report
   changes when they go live.
2. **Canonical schemas only.** Modules and reports consume `schemas.py`
   dataclasses, never raw platform/CSV formats.
3. **Offline-capable.** Everything runs with `MR_OFFLINE=1` and no network. LLM
   calls go through `analysis.narrate`, which falls back to deterministic
   templates. `narrate` never raises.
4. **No heavy deps.** CSV ingestion uses the stdlib `csv` module. No pandas.
   `.xlsx` support is a future add (`openpyxl`).
5. **Goals/thresholds are data.** The 2026 per-channel goals and red-flag
   thresholds live in `goals.py`, copied verbatim from the requirements — not
   hardcoded inside logic.
6. **Safe division.** Cost metrics return `None` (never raise) when the
   denominator is 0; reports degrade with a structured note instead of crashing.

## 3. Feature modules (requirements §3 → code)

| Requirements | Module | Responsibility |
| :---- | :---- | :---- |
| §3.1 Campaign Performance Reporting | `modules/campaign_reporting.py` | Per-channel/UTM aggregation, CPL/CPQL/CPD/CAC, threshold flags, week-over-week, top UTM sources. |
| §3.2 Competitor Intelligence | `modules/competitor_intel.py` | Snapshot + content-hash diff of the 6 named competitors; LLM change summary; degrade on network failure. |
| §3.3 Lead Channel & Funnel Analysis | `modules/funnel_analysis.py` | UTM attribution, conversion by channel, best practice areas, drop-off points, high-volume/low-booking flags. |
| §3.4 Opportunity Research | `modules/opportunity_research.py` | ICP-fit scoring, ranking, 14-day stale-outreach flags, sponsor placement verification. |

## 4. 2026 goals & thresholds (`goals.py`, verbatim)

Per-channel cost-per-demo-booked / completed + completed-demo % goal for
Email / META / Google / Websites / Total. Red flags: cost-per-qualified-lead
`$600+`, `$3000+` spend with no demo, management fees `>$3000/mo`, CAC `$3000+`.
Report rules: cost-per-booking `> $150`, conversion drop `> 30%` vs prior 7-day
average. Collective targets (qualified-demo 2800–3000+, completed 2000+,
cost/qualified-demo $500–650, cost/completed $850–1000, revenue $185,000) in
`goals.COLLECTIVE`.

## 5. Deliverables (requirements §4 → `reports.KINDS`)

| Report | Cadence | Builder kind |
| :---- | :---- | :---- |
| Daily Marketing Performance Summary | Daily 3pm PST | `daily_summary` |
| Weekly Marketing Performance Summary | Mon 12pm PST | `weekly_summary` |
| Campaign Threshold Alert | Triggered | `threshold_alert` |
| Competitor Change Digest | Weekly | `competitor_digest` |
| New Podcast/Media Opportunity Report | Bi-weekly | `opportunity_report` |
| UTM Attribution Summary | Weekly | `utm_attribution` |
| ICP Audience Signal Report | Monthly | `icp_signal` |

Each `reports.build(kind, dataset, user_id)` returns `{id, kind, generated_at,
user_id, agent_id, structured, markdown, html}` and persists it as a run.
Scheduler entrypoints (`schedule.run_daily/weekly/biweekly/monthly`,
`check_alerts`) are pure functions a cron/Cloud Scheduler job calls.

## 6. API (`app/routers/marketing_research.py`, prefix `/api/mr`)

| Method | Path | Purpose |
| :---- | :---- | :---- |
| POST | `/api/mr/ingest` | Upload a CSV export (`file` + `platform`) → normalized dataset. |
| GET | `/api/mr/datasets` | List ingested datasets (+ data gaps) for the user. |
| POST | `/api/mr/reports/{kind}` | Generate one deliverable on demand. |
| GET | `/api/mr/runs` | List the user's report runs. |
| GET | `/api/mr/runs/{id}` | Fetch a saved report (structured + markdown + html). |
| POST | `/api/mr/schedule/{period}` | Trigger `daily|weekly|biweekly|monthly` (also the cron seam). |

## 7. Success metrics (requirements §5)

Weekly reporting < 30 min; threshold flags within the daily run; ≥5 new media
targets/month via `opportunity_research`; 100% of demo bookings attributed via
`funnel_analysis`; all 6 competitors tracked weekly via `competitor_intel`.

## 8. Deploy-time follow-ups (out of this build)

- Cloud Scheduler jobs → `POST /api/mr/schedule/{period}` at the §4 cadences.
- Provision live API credentials → implement the `sources/*_source.py` connectors.
- Email/Slack delivery → implement a `notify.Channel` and `notify.register(...)`.
- Excel `.xlsx` ingestion (adds `openpyxl`).
