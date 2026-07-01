# Marketing Research agent

AI research agent for Legal Soft's Marketing department — campaign performance
reporting, competitor intelligence, lead/funnel analysis, and media-opportunity
research. See `agent.md` for the build spec.

## Layout

```
marketing_research_agent/
  schemas.py            # canonical dataclasses (the lingua franca)
  goals.py              # 2026 goals + red-flag thresholds (verbatim, as data)
  config.py             # column maps, tracked competitors, ICP weights
  analysis.py           # LLM narrate() + deterministic offline fallback
  reports.py            # build() the 7 deliverables -> dict + Markdown + HTML
  runs.py               # persist/load runs (disk + Firestore)
  schedule.py           # run_daily/weekly/biweekly/monthly + check_alerts
  notify.py             # pluggable delivery hook (default: log)
  sources/
    base.py             # DataSource protocol + CredentialMissingError
    csv_source.py       # CSV/Excel export ingestion (ships now)
    google_ads_source.py, meta_source.py, hubspot_source.py   # live stubs
    web_source.py       # fetch + cache web pages (competitor monitoring)
  modules/
    campaign_reporting.py   funnel_analysis.py
    competitor_intel.py     opportunity_research.py
  prompts/*.txt         # one per report kind
  tests/                # offline pytest suite + CSV fixtures
```

## Run the tests

From the `backend/` directory (uses the backend virtualenv):

```bash
MR_OFFLINE=1 .venv/Scripts/python.exe -m pytest \
  "agents/Marketing Research agent/marketing_research_agent/tests" \
  app/routers/tests/test_mr_router.py -q
```

`MR_OFFLINE=1` forces the deterministic narrative path and disables cloud writes.

## Use it (HTTP)

```
POST /api/mr/ingest-sheet  {}                 # pull the live Google-Sheets tracker (brand tabs)
POST /api/mr/ingest        file=<export.csv>  platform=google_ads|meta|hubspot
POST /api/mr/reports/daily_summary
GET  /api/mr/runs
GET  /api/mr/runs/{id}
POST /api/mr/schedule/daily
```

## Live Google Sheets source

`sources/sheets_source.py` pulls Legal Soft's shared performance tracker. The
service account (`lsagent@…`, viewer on the sheet) authenticates via ADC + the
read-only **Drive** scope and reads each tab through the authenticated CSV-export
endpoint — so the Google **Sheets API does not need to be enabled**.

Each tab is a transposed monthly grid (metric names down column A; month columns
in `(Performance)`/`(Investment)` pairs; quarter/YTD rollups skipped) and may hold
several channel blocks (e.g. a META block + a `GOOGLE` sub-block). `parse_tracker`
emits one `CampaignMetric` per channel-block per month. Spend uses the Investment
(actual-billed) column with a fallback to Performance; counts use Performance.

Configure the spreadsheet id, plan year, and known brand tabs in
`config.SHEETS_*` (env overrides: `MR_SHEETS_ID`, `MR_SHEETS_YEAR`).

## Adding a live data source

1. Implement the `DataSource` methods in `sources/<platform>_source.py`
   (the credential-gated stub is already there).
2. Set the platform's credential env vars (e.g. `GOOGLE_ADS_DEVELOPER_TOKEN`,
   `META_ACCESS_TOKEN`, `HUBSPOT_ACCESS_TOKEN`).
3. Select the live source in the router instead of `CsvSource` for that platform.

Modules and reports need no changes — they only speak canonical schemas.
