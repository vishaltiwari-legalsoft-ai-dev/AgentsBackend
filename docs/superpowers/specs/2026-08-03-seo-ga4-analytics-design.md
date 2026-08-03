# SEO agent — Google Analytics (GA4) live metrics for Legal Soft

**Date:** 2026-08-03 · **Status:** approved (auto-discover option chosen)

## Business goal

The SEO agent (a2) currently reasons from Search Console / rank tracking only.
The user has granted our shared service account **Viewer access to Legal Soft's
Google Analytics**. Wire that in so every run also carries real website
analytics, and the console shows them. Legal Soft is the only brand that needs
this today — no new brand plumbing.

## What the user gets

A **Website Analytics** section on the Legal Soft brand detail (and numbers
stored in the run for future decision-blending), last 28 days vs previous 28:

1. Traffic snapshot — sessions, users, new users, engagement rate, pageviews
2. Top pages by views
3. Channel split (Organic / Direct / Referral / Social …) with organic trend
4. Key events (GA4 conversions), when configured

## Design decisions

- **No new SDK.** `google-api-python-client` (already a dependency) serves the
  GA4 Data API (`analyticsdata` v1beta) and Admin API (`analyticsadmin`
  v1beta) with the same service-account ADC used for Search Console.
- **Auto-discovery, zero questions.** If a brand has no `ga4_property`, the
  agent lists the account summaries the SA can see and matches a property to
  the brand (domain root / name token; or the single visible property). The
  match is persisted on the brand so discovery runs once.
- **Graceful degradation (existing pattern).** All GA calls raise
  `CredentialMissing` on failure; `run_brand` records a plain-language note in
  `degraded` and the run continues. Key events are fetched separately so a
  missing metric never sinks the whole overview.
- **Data rides inside the run doc** (`run["ga"]`), so `brand_detail` needs no
  new endpoint and history persists like everything else.

## Shape of `run["ga"]`

```json
{
  "property": "properties/123", "property_name": "Legal Soft",
  "totals":      {"sessions": 0, "users": 0, "new_users": 0, "engagement_rate": 0.0, "avg_session_sec": 0.0, "pageviews": 0},
  "prev_totals": {"…same keys…"},
  "top_pages": [{"path": "/", "views": 0, "sessions": 0}],
  "channels":  [{"channel": "Organic Search", "sessions": 0, "prev_sessions": 0}],
  "key_events": [{"event": "generate_lead", "count": 0}]
}
```

`null` when GA is unavailable (with a `degraded` note saying why).

## Files touched (4 + tests)

| File | Change |
|---|---|
| `seo_geo_agent/sources.py` | `ga_discover_property()`, `ga_fetch_overview()` (batchRunReports ×3 + separate key-events report), service builders |
| `seo_geo_agent/insights.py` | `run_brand`: discovery-if-needed → fetch → `run["ga"]` + one GA insight bullet; degradation note otherwise |
| `newfrontend/lib/api.ts` | `SeoGa` types on `SeoRun` |
| `newfrontend/components/console/seo/SeoAgent.tsx` | Website Analytics section (reuses `seo-stat` tiles, `Delta`, list patterns) |

Tests: `tests/test_seo_ga.py` — discovery matching, response parsing, key-event
isolation, run_brand integration + degradation (fake services injected via the
existing `service=` parameter pattern; offline mode stays offline).

## Risk ledger

- New dependencies: **0**. New endpoints: **0**.
- Hardcodes: **~0%** — property auto-discovered and stored per brand;
  Legal Soft is already the default brand.
- Coupling: low — same adapter seam as `gsc_fetch`; engine math untouched.
- Rigidity: low — multi-brand GA works the moment another brand's GA is
  shared with the SA.
- Cloud Run note: metadata-server tokens carry fixed scopes; if GA rejects
  them in prod the run degrades honestly (never crashes). Local dev uses the
  SA key file, which google-api-python-client scopes automatically.
