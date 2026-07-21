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
