"""Static configuration: export column maps, tracked competitors, ICP weights."""

from __future__ import annotations

import os

# Legal Soft's live performance tracker (Google Sheet) — the "View Copy ...
# (For AI Agent Use)" workbook, the designated fetch source since 2026-07-08.
# One tab per vendor engagement + the consolidated Overall Report tab; each is
# a transposed monthly grid parsed by `sources/sheets_source.py`.
SHEETS_SPREADSHEET_ID = os.environ.get(
    "MR_SHEETS_ID", "1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0"
)
# The grid has no year column; performance is tracked for the current plan year.
SHEETS_YEAR = int(os.environ.get("MR_SHEETS_YEAR", "2026"))
# Known brand tabs (gid -> optional brand override; None lets the parser derive
# the brand from the tab title). Extend as more tabs are confirmed/enumerated.
SHEETS_TABS: list[dict] = [
    {"gid": "2088778899", "brand": None},
]

# Maps a platform export's column header -> canonical field name.
COLUMN_MAPS: dict[str, dict[str, str]] = {
    "google_ads": {
        "Campaign": "campaign",
        "Cost": "spend",
        "Source": "utm_source",
        "Medium": "utm_medium",
        "Campaign name": "utm_campaign",
        "Leads": "leads",
        "Qualified leads": "qualified_leads",
        "Demos booked": "demos_booked",
        "Demos completed": "demos_completed",
        "Day": "date",
    },
    "meta": {
        "Campaign name": "campaign",
        "Amount spent (USD)": "spend",
        "utm_source": "utm_source",
        "utm_medium": "utm_medium",
        "utm_campaign": "utm_campaign",
        "Leads": "leads",
        "Qualified": "qualified_leads",
        "Demos booked": "demos_booked",
        "Demos completed": "demos_completed",
        "Day": "date",
    },
    "hubspot": {  # lead-level export
        "Record ID": "id",
        "Original Source": "utm_source",
        "Medium": "utm_medium",
        "Campaign": "utm_campaign",
        "Lead Channel": "channel",
        "Practice Area": "practice_area",
        "Lifecycle Stage": "stage",
        "Create Date": "created_at",
    },
}

CHANNEL_BY_PLATFORM = {"google_ads": "Google", "meta": "META", "hubspot": "Organic"}

# Channels whose "spend" is not media spend. The tracker sheet's own total
# keeps these out of blended spend, and the platform must reconcile with the
# sheet; their leads/demos still count (organic conversions are real).
NON_MEDIA_CHANNELS = frozenset({"Websites"})
NON_MEDIA_VENDOR_SLUGS = frozenset({"website"})

# The six named competitors (requirements §3.2).
COMPETITORS = [
    {"name": "BackOffice Betties", "url": "https://www.backofficebetties.com/"},
    {"name": "Remote Legal Staff", "url": "https://remotelegalstaff.com/"},
    {"name": "Virtual Latinos", "url": "https://virtuallatinos.com/"},
    {"name": "LawClerk", "url": "https://www.lawclerk.legal/"},
    {"name": "Smith.ai", "url": "https://smith.ai/"},
    {"name": "LexReception", "url": "https://www.lexreception.com/"},
]

# ICP fit scoring (requirements §3.4). Weights sum to 1.0.
ICP = {
    "weights": {
        "audience_size": 0.3,
        "engagement_rate": 0.3,
        "host_authority": 0.2,
        "practice_area_fit": 0.2,
    },
    "audience_size_norm": 100000.0,   # audience that scores 1.0 on size
    "min_score_to_surface": 0.5,
    "stale_outreach_days": 14,        # flag shows with no response after 14 days
}

# --- Report template extraction (Phase 2: a sample report -> a layout) -------
# ``template_extract`` reads an uploaded sample report ONCE and maps it onto
# the vendor report's section catalog. The model only picks layout — numbers
# never come from it — so the step is priced per upload and bounded up front.

#: OpenRouter model id. Vision + strict JSON-schema output are both required;
#: the request asks OpenRouter to route only to providers that honour them.
#: Sonnet 5.5 because it matched Opus 5.5 on the six-sample eval (12/12 over
#: two runs vs Opus 6/6) at half the price; Haiku 5.5 dropped to 4/6 on a
#: rerun (split the highlights block, invented a footer). 2026-10-08,
#: ``tests/template_extract_eval.py``. Re-run it before changing this.
TEMPLATE_EXTRACT_MODEL = os.environ.get("MR_TEMPLATE_MODEL", "anthropic/claude-sonnet-5.5")
#: Output cap for the first read, thinking included. A full layout is ~1.5K
#: tokens of JSON; the rest is headroom for the model's reasoning.
TEMPLATE_EXTRACT_MAX_TOKENS = int(os.environ.get("MR_TEMPLATE_MAX_TOKENS", "8000"))
#: Output cap for the one text-only repair call.
TEMPLATE_REPAIR_MAX_TOKENS = int(os.environ.get("MR_TEMPLATE_REPAIR_MAX_TOKENS", "4000"))
TEMPLATE_EXTRACT_TIMEOUT_S = float(os.environ.get("MR_TEMPLATE_TIMEOUT_S", "150"))
#: Hard per-upload ceiling. Checked BEFORE the first call against the worst
#: case (every input token plus both calls running to their output caps);
#: an upload whose worst case exceeds it is refused, never sent. Sized for the
#: default model at the 10-image limit (worst case ~$0.19; a typical 5-page
#: upload measured $0.042). Switching to Opus 5.5 (worst ~$0.38) needs this
#: raised too, deliberately.
TEMPLATE_EXTRACT_COST_CEILING_USD = float(
    os.environ.get("MR_TEMPLATE_COST_CEILING_USD", "0.25"))
#: USD per million tokens (input, output) as OpenRouter lists them on
#: 2026-10-08. A model missing here is refused unless both
#: ``MR_TEMPLATE_PRICE_IN_PER_M`` and ``MR_TEMPLATE_PRICE_OUT_PER_M`` are set —
#: the ceiling cannot be enforced against an unknown price.
TEMPLATE_EXTRACT_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-opus-5.5": (4.00, 20.00),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
}
#: OpenRouter ``reasoning.effort`` for the read. Layout mapping is recognition,
#: not deep reasoning; "low" holds the eval and keeps output tokens down.
TEMPLATE_EXTRACT_EFFORT = os.environ.get("MR_TEMPLATE_EFFORT", "low")
