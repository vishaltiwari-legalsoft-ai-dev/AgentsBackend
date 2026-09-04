import csv
import io
import os
from datetime import date

from marketing_research_agent.schemas import DateRange
from marketing_research_agent.sources.sheets_source import SheetsSource, parse_tracker

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "tracker_tab.csv")
RANGE = DateRange(start=date(2026, 1, 1), end=date(2026, 12, 31))


def _rows():
    with open(FIX, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_parse_splits_channel_blocks():
    metrics, gaps = parse_tracker(_rows(), year=2026)
    channels = {m.channel for m in metrics}
    assert channels == {"META", "Google"}
    # two months of data per channel
    assert len(metrics) == 4


def test_meta_spend_uses_performance_column():
    """Official basis (decision 2026-07-27): the console must show the figures
    the team reads on the sheet — the Performance column, not Investment."""
    metrics, _ = parse_tracker(_rows(), year=2026)
    jan_meta = next(m for m in metrics if m.channel == "META" and m.date == date(2026, 1, 1))
    assert jan_meta.spend == 3603.88  # Performance, not Investment (3964.27)
    assert jan_meta.leads == 27 and jan_meta.qualified_leads == 21
    assert jan_meta.demos_booked == 11 and jan_meta.demos_completed == 4


def test_spend_falls_back_to_investment_when_performance_blank():
    rows = [
        ["Meta TestBrand", "Jan (Performance)", "Jan (Investment)"],
        ["Spend", "", "$500.00"],
        ["Leads", "3", ""],
    ]
    metrics, _ = parse_tracker(rows, year=2026)
    assert metrics[0].spend == 500.0


def test_brand_derived_from_title():
    metrics, _ = parse_tracker(_rows(), year=2026)
    assert all(m.utm_campaign == "TestBrand" for m in metrics)


def test_sheets_source_with_injected_fetcher():
    with open(FIX, encoding="utf-8") as fh:
        text = fh.read()
    src = SheetsSource("sheet1", "0", year=2026, fetcher=lambda sid, gid: text)
    metrics, gaps = src.fetch_campaign_metrics(RANGE)
    assert len(metrics) == 4


class _FakeValues:
    def __init__(self, by_title):
        self._by_title = by_title

    def get(self, spreadsheetId, range, valueRenderOption=None):
        title = range.strip("'")
        rows = self._by_title.get(title, [])
        return _FakeExec({"values": rows})


class _FakeSpreadsheets:
    def __init__(self, tabs, by_title):
        self._tabs = tabs
        self._values = _FakeValues(by_title)

    def get(self, spreadsheetId):
        return _FakeExec({
            "sheets": [
                {"properties": {"sheetId": t["gid"], "title": t["title"],
                                "hidden": t.get("hidden", False),
                                "gridProperties": {"rowCount": 100, "columnCount": 30}}}
                for t in self._tabs
            ]
        })

    def values(self):
        return self._values


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeService:
    def __init__(self, tabs, by_title):
        self._s = _FakeSpreadsheets(tabs, by_title)

    def spreadsheets(self):
        return self._s


def test_fetch_all_trackers_via_sheets_api_skips_non_trackers_and_rollups():
    """Primary discovery path (Sheets API): a vendor tracker + the consolidated
    roll-up + a junk tab -> only the vendor tracker is ingested (the roll-up
    duplicates the vendors' numbers and would double-count)."""
    from marketing_research_agent.sources.sheets_source import fetch_all_trackers

    tabs = [
        {"gid": 559258152, "title": "Meta 360 RA"},
        {"gid": 2088778899, "title": "Marketing 2026 Overall Report"},
        {"gid": 12345, "title": "Raw Notes"},
    ]
    by_title = {
        "Meta 360 RA": _rows(),
        "Marketing 2026 Overall Report": _rows(),  # parses fine; skipped by title
        "Raw Notes": [["just", "notes"], ["no", "months"]],
    }
    found = fetch_all_trackers("sheet1", 2026, service=_FakeService(tabs, by_title))
    assert len(found) == 1
    assert found[0]["tab"] == "Meta 360 RA" and found[0]["gid"] == 559258152
    assert len(found[0]["metrics"]) == 4


def test_parse_official_spend_reads_rollup_performance_spend():
    from marketing_research_agent.sources.sheets_source import parse_official_spend

    rows = [
        ["All", "Jan (Performance)", "Jan (Investment)", "1st Quarter",
         "July (Performance)", "July (Investment)"],
        ["Budget", "$100.00", "", "", "$77,100.20", "#N/A"],
        ["Spend", "$1,000.50", "#N/A", "", "$45,461.20", "#N/A"],
        ["Leads", "10", "", "", "305", ""],
    ]
    assert parse_official_spend(rows, 2026) == {"2026-01": 1000.50, "2026-07": 45461.20}


def test_fetch_official_spend_reads_the_rollup_tab():
    from marketing_research_agent.sources.sheets_source import fetch_official_spend

    tabs = [
        {"gid": 1, "title": "Meta 360 RA"},
        {"gid": 2, "title": "Marketing 2026 Overall Report"},
    ]
    by_title = {
        "Meta 360 RA": _rows(),
        "Marketing 2026 Overall Report": [
            ["All", "July (Performance)", "July (Investment)"],
            ["Spend", "$45,461.20", "#N/A"],
        ],
    }
    out = fetch_official_spend("sheet1", 2026, service=_FakeService(tabs, by_title))
    assert out == {"2026-07": 45461.20}


def _meta_tab_with_a_pasted_websites_block():
    """A vendor tab shaped like the live ones on 2026-08-15: its own META block,
    then a copy of the shared Websites block underneath."""
    return [
        ["Axenic LS Meta", "Aug (Performance)", "Aug (Investment)"],
        ["Spend", "$0.00", "#N/A"],
        ["Leads", "0", ""],
        ["Websites"],
        ["Spend", "$8,632.00", "$8,632.00"],
        ["Leads", "127", ""],
    ]


def test_a_websites_block_pasted_into_a_vendor_tab_is_not_ingested():
    """Regression, 2026-08-15: the shared Websites block sat in SEVEN vendor
    tabs plus its own. Ingesting it from a vendor tab attributed Websites'
    numbers to that vendor AND counted $8,632/month eight times."""
    metrics, gaps = parse_tracker(_meta_tab_with_a_pasted_websites_block(), year=2026)
    assert metrics == []                      # its own block is empty; nothing else is its
    assert any("pasted into this tab" in g.message for g in gaps)


def test_the_websites_tab_itself_still_reports_its_block():
    """The dedicated tab is the one place those rows are real."""
    rows = [
        ["Websites", "Aug (Performance)", "Aug (Investment)"],
        ["Spend", "$8,632.00", "$8,632.00"],
        ["Leads", "127", ""],
    ]
    metrics, _ = parse_tracker(rows, year=2026)
    assert [(m.channel, m.spend, m.leads) for m in metrics] == [("Websites", 8632.0, 127)]


def test_an_unknown_vendor_channel_is_other_not_total():
    """"Total" is the roll-up's channel, and _campaign_structured POPS it out as
    the report's totals. A vendor tab whose channel is unrecognised
    ("DanteAgency ChatGPT") therefore became the headline KPI strip."""
    rows = [["Barnacle Ads LS", "Aug (Performance)"], ["Spend", "$43.16"], ["Leads", "2"]]
    metrics, _ = parse_tracker(rows, year=2026)
    assert metrics[0].channel == "Other"

    # The consolidated roll-up (A1 = "All") keeps "Total" — that one IS the totals.
    rollup = [["All", "Aug (Performance)"], ["Spend", "$30,431.46"], ["Leads", "9"]]
    assert parse_tracker(rollup, year=2026)[0][0].channel == "Total"


def test_hidden_tabs_are_never_ingested():
    """Hidden tabs are Looker dumps and archives, and in the live workbook six of
    them sit BEFORE the vendors."""
    from marketing_research_agent.sources.sheets_source import fetch_all_trackers

    tabs = [
        {"gid": 1, "title": "Looker Studio per Brand (April)", "hidden": True},
        {"gid": 2, "title": "Meta 360 RA", "hidden": False},
    ]
    by_title = {"Looker Studio per Brand (April)": _rows(), "Meta 360 RA": _rows()}
    found = fetch_all_trackers("sheet1", 2026, service=_FakeService(tabs, by_title))
    assert [f["tab"] for f in found] == ["Meta 360 RA"]


def test_repeated_month_band_reads_the_leftmost_grid_and_says_so():
    """Regression, 2026-08: a second month-column band added to the right of the
    grid silently re-pointed every figure — the same Overall tab that read July
    spend $48,005.09 on 07-27 read $5,579.58 afterwards, a roll-up BELOW the
    vendor tabs it aggregates. The primary grid is the leftmost one."""
    from marketing_research_agent.sources.sheets_source import (
        parse_official_totals, scan_month_columns,
    )

    header = ["All", "July (Performance)", "July (Investment)", "1st Quarter",
              "YTD (Performance)", "July (Performance)", "July (Investment)"]
    cols, repeated = scan_month_columns(header)
    assert cols == [(7, 1, 2)]          # leftmost band, not columns 5/6
    assert repeated == [7]              # and the repeat is reported, not swallowed

    rows = [header, ["Spend", "$48,005.09", "", "", "", "$5,579.58", ""]]
    assert parse_official_totals(rows, 2026) == {"2026-07": {"spend": 48005.09}}


def test_investment_is_never_paired_across_bands():
    """Performance and Investment used to resolve independently, so a pair could
    be lifted from two different tables. No billed figure beats a wrong one."""
    from marketing_research_agent.sources.sheets_source import scan_month_columns

    # Investment sits LEFT of the Performance column that survived — different band.
    cols, _ = scan_month_columns(["All", "Jan (Investment)", "Feb (Performance)",
                                  "Jan (Performance)"])
    assert cols == [(1, 3, -1), (2, 2, -1)]


def test_parse_tracker_reports_a_repeated_month_as_a_gap():
    rows = [
        ["Meta TestBrand", "Jan (Performance)", "Jan (Investment)", "Jan (Performance)"],
        ["Spend", "$100.00", "", "$999.00"],
        ["Leads", "3", "", "99"],
    ]
    metrics, gaps = parse_tracker(rows, year=2026)
    assert metrics[0].spend == 100.0 and metrics[0].leads == 3
    assert any("more than one column band" in g.message for g in gaps)


def test_a_vendor_tab_left_on_the_all_scope_is_skipped_not_a_full_stop():
    """A1 is a dropdown — any tab can be left scoped to "All". That used to end
    the whole scan, so a pull ingested ZERO vendor tabs and still reported
    success. Only the tab NAME may stop the scan."""
    from marketing_research_agent.sources.sheets_source import fetch_all_trackers

    tabs = [
        {"gid": 1, "title": "Meta 360 RA"},          # dropdown left on "All"
        {"gid": 2, "title": "Hawksem LS Google"},    # a real vendor after it
        {"gid": 3, "title": "Marketing 2026 Overall Report"},
        {"gid": 4, "title": "Raw Notes"},
    ]
    scoped_to_all = [["All"] + _rows()[0][1:]] + _rows()[1:]
    by_title = {
        "Meta 360 RA": scoped_to_all,
        "Hawksem LS Google": _rows(),
        "Marketing 2026 Overall Report": _rows(),
        "Raw Notes": [["just", "notes"]],
    }
    found = fetch_all_trackers("sheet1", 2026, service=_FakeService(tabs, by_title))
    assert [f["tab"] for f in found] == ["Hawksem LS Google"]


def test_is_rollup_tab():
    from marketing_research_agent.sources.sheets_source import is_rollup_tab

    # title match survives whatever the dropdown left in A1
    assert is_rollup_tab("Marketing 2026 Overall Report", [["Meta 360 RA"]]) is True
    # A1 scope says it's the consolidated view
    assert is_rollup_tab("Some Tab", [["All", "Jan (Performance)"]]) is True
    assert is_rollup_tab("Some Tab", [["Overall"]]) is True
    # a plain vendor tab is not a roll-up
    assert is_rollup_tab("Meta 360 RA", [["Meta 360 RA"]]) is False
    assert is_rollup_tab("Meta 360 RA", []) is False


def _metric(channel, month, spend):
    from marketing_research_agent.schemas import CampaignMetric

    return CampaignMetric(channel=channel, campaign="c", utm_source="s", utm_medium="paid",
                          utm_campaign="b", spend=spend, leads=0, qualified_leads=0,
                          demos_booked=0, demos_completed=0, date=date(2026, month, 1))


def test_reconcile_flags_a_rollup_below_the_tabs_it_aggregates():
    """The check that would have caught 2026-08 on the day it happened: the
    Overall tab cannot report less media spend than the vendor tabs it sums."""
    from marketing_research_agent.sources.sheets_source import reconcile_official_spend

    fetched = [{"metrics": [_metric("META", 7, 30000.0), _metric("Google", 7, 9945.26)]}]
    official = {"2026-07": {"spend": 5579.58}}
    out = reconcile_official_spend(fetched, official, through=date(2026, 8, 15))
    assert len(out) == 1
    assert "$5,579.58" in out[0] and "$39,945.26" in out[0]


def test_reconcile_is_quiet_when_the_rollup_is_the_larger_figure():
    """The healthy shape — the roll-up adds ledger sources no vendor tab carries."""
    from marketing_research_agent.sources.sheets_source import reconcile_official_spend

    fetched = [{"metrics": [_metric("META", 7, 34873.09)]}]
    official = {"2026-07": {"spend": 48005.09}}
    assert reconcile_official_spend(fetched, official, through=date(2026, 8, 15)) == []


def test_reconcile_ignores_prefilled_future_months_and_non_media():
    """Vendor tabs pre-fill future retainer months the roll-up leaves at zero,
    and Websites spend is not media spend — neither is a mismatch."""
    from marketing_research_agent.sources.sheets_source import reconcile_official_spend

    fetched = [{"metrics": [_metric("META", 9, 10186.0), _metric("Websites", 7, 7486.0)]}]
    official = {"2026-07": {"spend": 100.0}, "2026-09": {"spend": 0.0}}
    assert reconcile_official_spend(fetched, official, through=date(2026, 8, 15)) == []


def test_xlsx_discovery_fallback_skips_rollups():
    """Fallback discovery path (whole-workbook xlsx via openpyxl)."""
    import io as _io

    import openpyxl

    from marketing_research_agent.sources.sheets_source import _fetch_all_trackers_xlsx

    wb = openpyxl.Workbook()
    tracker = wb.active
    tracker.title = "Meta 360 RA"
    for r in _rows():
        tracker.append(r)
    rollup = wb.create_sheet("Marketing 2026 Overall Report")
    for r in _rows():
        rollup.append(r)
    junk = wb.create_sheet("Raw Notes")
    junk.append(["just", "some", "notes"])
    buf = _io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    found = _fetch_all_trackers_xlsx("sheet1", 2026, xlsx_fetcher=lambda sid: data)
    assert len(found) == 1 and found[0]["tab"] == "Meta 360 RA"
# --- roll-up tab: the report ledger's full field set ------------------------
# The contract is the R[] ledger the Q1-vs-Q2 comparison report renders: every
# row in it must have a parseable source here. ``_LEDGER`` carries one entry per
# ledger row — its published label, the sheet row it reads, the occurrence that
# disambiguates it, and the figures the report publishes — so the fixture, the
# expectations and the coverage check can never drift apart.
#
# The roll-up's "(Not Actualized)" section carries TWO blocks whose rows share
# three labels verbatim: Projected first, Paying second. Reading the label alone
# put the Projected figures under the Paying names — the same shape of defect as
# 2026-08-15, where one block pasted into several tabs was counted eight times.

_M, _I, _P = "money", "int", "pct"

# (published ledger label | None, sheet row label, field, occurrence, Q1, Q2, format)
_LEDGER = [
    # --- Budget & Efficiency
    ("Budget", "Budget", "budget", 1, 248446.0, 199796.0, _M),
    ("Spend", "Spend", "spend", 1, 239581.57, 187299.79, _M),
    ("Leads", "Leads", "leads", 1, 1281, 1220, _I),
    ("Qualified Leads", "Qualified Leads", "qualified_leads", 1, 636, 515, _I),
    ("Qualified Lead Ratio", "Qualified Lead Ratio",
     "qualified_lead_ratio_pct", 1, 49.65, 42.21, _P),
    ("Cost per Lead", "Cost per Lead", "cost_per_lead", 1, 187.03, 153.52, _M),
    ("Cost per Qualified Lead", "Cost per Qualified Lead",
     "cost_per_qualified_lead", 1, 376.70, 363.69, _M),
    ("Qualified Demos Booked (SDR+VAPI+Direct)", "Qualified Demos Booked (SDR+VAPI+Direct)",
     "qual_demos_booked", 1, 431, 343, _I),
    ("Total Demos Completed (SDR+VAPI+Direct)", "Demos Completed (SDR+VAPI+Direct)",
     "demos_completed", 1, 272, 238, _I),
    ("Show-up Rate (SDR+VAPI+Direct)", "Total Show Up Rate (%) (SDR+VAPI+Direct)",
     "show_up_rate_pct", 1, 63.11, 69.39, _P),
    ("Lost DNC (Bad Lead)", "Lost DNC (Bad Lead)", "lost_dnc_bad_lead", 1, 124, 149, _I),
    ("Cost / Qualified Demo Booked (SDR+VAPI+Direct)",
     "Cost per Qualified Demo Booked (SDR+VAPI+Direct)",
     "cost_per_qual_demo_booked", 1, 555.87, 546.06, _M),
    # The ledger says "Qualified", the arithmetic says otherwise: 239,581.57 /
    # 880.81 = 272.0, the ALL-IN completed count, so this row is the sheet's
    # "Cost per Demo Completed (SDR+VAPI+Direct)".
    ("Cost / Qualified Demo Completed (SDR+VAPI+Direct)",
     "Cost per Demo Completed (SDR+VAPI+Direct)",
     "cost_per_demo_completed", 1, 880.81, 786.97, _M),
    # Not a ledger ROW — the denominator the ledger's Conversion Rate is computed
    # on, which the report has to show for its two cards to reconcile.
    (None, "Total Demos Completed (Direct)", "demos_completed_direct", 1, 248, 212, _I),
    # --- Projected — Actualized
    ("Number of Projected New Clients (Actualized)", "Number of Projected New Clients (Actualized)",
     "projected_new_clients", 1, 118, 133, _I),
    ("Total Projected Services Sold (Actualized)", "Total Projected Services Sold (Actualized)",
     "projected_services_sold", 1, 156, 224, _I),
    ("Projected Total Amount Sold ($) (Actualized)", "Projected Total Amount Sold ($) Actualized",
     "projected_amount_sold", 1, 479608.30, 649575.09, _M),
    ("Projected MRR w/o Setup Fees (Actualized)",
     "Projected MRR from New Sales w/o Set Up Fees (Actualized)",
     "projected_mrr_without_setup_fee", 1, 422607.70, 594269.84, _M),
    # --- Revenue — Actualized
    ("Number of Revenue Clients (Actualized)", "Number of Revenue Clients (Actualized)",
     "revenue_clients", 1, 48, 57, _I),
    ("Total Services Sold (Actualized)", "Total Services Sold (Actualized)",
     "services_sold", 1, 81, 135, _I),
    ("Revenue Amount Sold (Actualized)", "Revenue Amount Sold (Actualized)",
     "revenue_amount_sold", 1, 262947.70, 401614.09, _M),
    ("Revenue Amount Sold w/o Setup Fee (Actualized)",
     "Revenue Amount Sold w/o Setup Fee (Actualized)",
     "revenue_amount_sold_without_setup_fee", 1, 231098.70, 372182.84, _M),
    # --- Projected — Not Actualized  (occurrence 1 of the three shared labels)
    ("Number of Projected New Clients (Not Actualized)",
     "Number of Projected New Clients (Not Actualized)",
     "projected_new_clients_not_actualized", 1, 114, 136, _I),
    ("Total Services Sold (Not Actualized)", "Total Services Sold (Not Actualized)",
     "services_sold_not_actualized", 1, 149, 227, _I),
    ("Revenue Amount Sold ($) (Not Actualized)", "Revenue Amount Sold (Not Actualized)",
     "revenue_amount_sold_not_actualized", 1, 452607.70, 660703.09, _M),
    ("Revenue Amount Sold w/o Setup Fee (Not Actualized)",
     "Revenue Amount Sold w/o Setup Fee (Not Actualized)",
     "revenue_amount_sold_without_setup_fee_not_actualized", 1, 397087.70, 603397.84, _M),
    # --- Paying — Not Actualized  (occurrence 2: identical labels, second block)
    ("Number of Paying New Clients (Not Actualized)",
     "Number of Paying New Clients (Not Actualized)",
     "paying_new_clients", 1, 52, 58, _I),
    ("Total Services Sold (Not Actualized)", "Total Services Sold (Not Actualized)",
     "paying_services_sold", 2, 90, 137, _I),
    ("Revenue Amount Sold (Not Actualized)", "Revenue Amount Sold (Not Actualized)",
     "paying_revenue_amount_sold", 2, 290691.70, 409704.59, _M),
    ("Revenue Amount Sold w/o Setup Fee (Not Actualized)",
     "Revenue Amount Sold w/o Setup Fee (Not Actualized)",
     "paying_revenue_amount_sold_without_setup_fee", 2, 256943.70, 378958.84, _M),
    # --- Inbound Sales Pipeline
    ("Revenue Amount Sold (Inbound Sales Pipeline)", "Revenue Amount Sold (Inbound Sales Pipeline)",
     "inbound_pipeline_revenue_amount_sold", 1, 448996.54, 588791.09, _M),
    # --- Goal & Blended Financials
    ("% of Revenue Target Goal (Not Actualized)", "Percentage of Revenue Target Goal",
     "revenue_target_pct", 1, 94.29, 123.50, _P),
    ("Revenue Sold Goal Amount", "Revenue Sold Goal Amount",
     "revenue_sold_goal", 1, 480000.0, 535000.0, _M),
    ("Average Deal Amount", "Average Deal Amount", "average_deal_amount", 1, 4064.48, 4884.02, _M),
    ("Conversion Rate (%)", "Conversion Rate (%)", "conversion_rate_pct", 1, 19.35, 26.89, _P),
    # The sheet's bare "ROAS" row is the actualized one: 262,947.70 / 239,581.57
    # = 109.75%, exactly what the ledger publishes as "ROAS — Actualized (%)".
    ("ROAS — Actualized (%)", "ROAS", "roas_pct", 1, 109.75, 214.42, _P),
    ("CAC", "CAC", "cac", 1, 4991.28, 3285.96, _M),
]

# The one ledger row with no sheet row of its own, derived from the components
# of the same period: revenue amount sold (not actualized) / spend.
_LEDGER_DERIVED = ("ROAS — Not Actualized (%)", "roas_not_actualized_pct", 188.92, 352.75)

# Performance vs Investment decoys, so "Performance first" stays provable.
_INVESTMENT_DECOY = {"Budget": ("$260,000.00", "$210,000.00"),
                     "Spend": ("$248,110.00", "$195,000.00")}


def _cell(value, fmt):
    if fmt == _I:
        return f"{int(value):,}"
    if fmt == _P:
        return f"{value:.2f}%"
    return f"${value:,.2f}"


def _rollup_rows():
    """A roll-up tab carrying the ledger's own figures. The two month columns
    stand in for the report's two period columns (Q1, Q2)."""
    rows = [
        ["All", "Jan (Performance)", "Jan (Investment)",
         "Feb (Performance)", "Feb (Investment)", "1st Quarter"],
        # Billed-only row: blank Performance, so the Investment fallback is what
        # resolves it. Not a ledger row — it pins the read basis.
        ["Management Fees", "", "$12,000.00", "", "$13,500.00", "x"],
    ]
    for _pub, label, _field, _occ, q1, q2, fmt in _LEDGER:
        inv1, inv2 = _INVESTMENT_DECOY.get(label, ("", ""))
        rows.append([label, _cell(q1, fmt), inv1, _cell(q2, fmt), inv2, "x"])
    return rows


def _relabel(rows, old_label, new_label, occurrence=1):
    """Copy of *rows* with the *occurrence*-th ``old_label`` row renamed — how a
    row goes missing in real life (the team renames it), rather than deleted."""
    out, hits = [], 0
    for row in rows:
        if row and row[0] == old_label:
            hits += 1
            if hits == occurrence:
                row = [new_label] + row[1:]
        out.append(row)
    return out


def test_every_ledger_row_has_a_parseable_source():
    """The ledger is the contract: every row the report renders must resolve to
    a field, at the right occurrence, with the published figure. A row that goes
    unmapped is a card the report cannot fill — named here rather than found on
    a board slide."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    q1, q2 = got["2026-01"], got["2026-02"]

    unresolved = []
    for published, sheet_label, field, occurrence, want1, want2, _fmt in _LEDGER:
        if published is None:
            continue  # not a ledger row; asserted on its own below
        if q1.get(field) != want1 or q2.get(field) != want2:
            unresolved.append(
                f"{published!r} -> {field} (sheet row {sheet_label!r}, occurrence "
                f"{occurrence}): got {q1.get(field)}/{q2.get(field)}, "
                f"want {want1}/{want2}")
    assert not unresolved, "ledger rows with no parseable source:\n" + "\n".join(unresolved)

    published, field, want1, want2 = _LEDGER_DERIVED
    assert (q1[field], q2[field]) == (want1, want2), f"{published} is not derived correctly"

    # 37 ledger rows: 36 read from a sheet row, 1 derived.
    assert len([r for r in _LEDGER if r[0] is not None]) == 36


def test_conversion_rate_denominator_is_the_direct_demo_count():
    """The report showed Conversion Rate and Total Demos Completed as adjacent
    cards, and 48 / 272 = 17.65%, not the 19.35% printed beside it. The real
    denominator is the DIRECT completed count — 248, a number that appeared
    nowhere on the page. Parsing it is what lets the reader do the division.
    """
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    for key, demos in (("2026-01", 248), ("2026-02", 212)):
        m = got[key]
        assert m["demos_completed_direct"] == demos
        # The published conversion rate is revenue clients / DIRECT completions.
        assert round(m["revenue_clients"] / m["demos_completed_direct"] * 100, 2) == \
            m["conversion_rate_pct"]
        # And it is emphatically not the all-in count, which is also parsed.
        assert m["demos_completed"] != m["demos_completed_direct"]
        assert round(m["revenue_clients"] / m["demos_completed"] * 100, 2) != \
            m["conversion_rate_pct"]


def test_cost_per_demo_completed_is_the_all_in_row_the_ledger_mislabels():
    """The ledger row reads "Cost / Qualified Demo Completed (SDR+VAPI+Direct)",
    but 239,581.57 / 880.81 = 272.0 — the ALL-IN completed count, not a
    qualified subset. So it is the sheet's plain cost-per-demo-completed row and
    needs no separate qualified field. Pinned because the label invites one."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    for key in ("2026-01", "2026-02"):
        m = got[key]
        assert round(m["spend"] / m["cost_per_demo_completed"]) == m["demos_completed"]


def test_the_revenue_goal_is_read_every_period_never_pinned():
    """The goal MOVES between periods (480,000 -> 535,000). A constant anywhere
    in the stack would silently misstate % of target for one of them."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    assert got["2026-01"]["revenue_sold_goal"] == 480000.0
    assert got["2026-02"]["revenue_sold_goal"] == 535000.0
    # And the published % of target is that period's own two components.
    for key in ("2026-01", "2026-02"):
        m = got[key]
        assert round(m["revenue_amount_sold_not_actualized"] / m["revenue_sold_goal"] * 100, 2) == \
            m["revenue_target_pct"]


def test_the_ledger_spelling_of_projected_amount_sold_also_resolves():
    """The sheet writes "($) Actualized", the ledger "($) (Actualized)". Both
    spellings are candidates, so neither layout drops the row."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    rows = _relabel(_rollup_rows(), "Projected Total Amount Sold ($) Actualized",
                    "Projected Total Amount Sold ($) (Actualized)")
    assert parse_official_totals(rows, 2026)["2026-01"]["projected_amount_sold"] == 479608.30


def test_official_totals_read_the_paying_block_not_the_projected_one():
    """The three shared labels resolve by OCCURRENCE, not by first match.

    Occurrence 1 is Projected — Not Actualized, occurrence 2 is Paying. Getting
    this backwards duplicates the Projected figures into the Paying rows, which
    is a wrong number that looks entirely plausible on the console.
    """
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    jan, feb = got["2026-01"], got["2026-02"]

    # Paying block — the second occurrence of each shared label.
    assert (jan["paying_services_sold"], feb["paying_services_sold"]) == (90.0, 137.0)
    assert (jan["paying_revenue_amount_sold"],
            feb["paying_revenue_amount_sold"]) == (290691.70, 409704.59)
    assert (jan["paying_revenue_amount_sold_without_setup_fee"],
            feb["paying_revenue_amount_sold_without_setup_fee"]) == (256943.70, 378958.84)

    # Projected block — the first occurrence, unchanged by the new parameter.
    assert (jan["services_sold_not_actualized"],
            feb["services_sold_not_actualized"]) == (149.0, 227.0)
    assert (jan["revenue_amount_sold_not_actualized"],
            feb["revenue_amount_sold_not_actualized"]) == (452607.70, 660703.09)
    assert (jan["revenue_amount_sold_without_setup_fee_not_actualized"],
            feb["revenue_amount_sold_without_setup_fee_not_actualized"]) == (397087.70, 603397.84)

    # And explicitly: no Projected figure is standing in for a Paying one.
    for paying, projected in (
        ("paying_services_sold", "services_sold_not_actualized"),
        ("paying_revenue_amount_sold", "revenue_amount_sold_not_actualized"),
        ("paying_revenue_amount_sold_without_setup_fee",
         "revenue_amount_sold_without_setup_fee_not_actualized"),
    ):
        assert jan[paying] != jan[projected] and feb[paying] != feb[projected]


def test_official_totals_extended_fields_keep_the_performance_first_basis():
    """The added rows read the same way the original eight do: Performance
    column first, Investment only as the fallback (Management Fees is billed —
    its Performance cell is blank, so the Investment figure is the real one)."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    jan = parse_official_totals(_rollup_rows(), 2026)["2026-01"]
    assert jan["management_fees"] == 12000.0      # Investment fallback
    assert jan["budget"] == 248446.0              # Performance, not 260,000
    assert jan["spend"] == 239581.57              # Performance, not 248,110
    assert jan["lost_dnc_bad_lead"] == 124.0
    assert jan["inbound_pipeline_revenue_amount_sold"] == 448996.54
    assert jan["paying_new_clients"] == 52.0


def test_a_renamed_official_row_becomes_absent_never_zero():
    """Safe-by-construction: a removed or renamed sheet row must drop OUT of the
    payload. A zero would read as "the team sold nothing", which is a different
    and much more expensive claim than "the sheet no longer has this row"."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    rows = _relabel(_rollup_rows(), "Qualified Leads", "Qualified Leads (new)")
    jan = parse_official_totals(rows, 2026)["2026-01"]
    assert "qualified_leads" not in jan
    assert jan["leads"] == 1281.0           # its neighbours are unaffected
    assert all(v != 0 for v in jan.values())


def test_a_vanished_duplicate_block_reports_neither_field_rather_than_guessing():
    """Occurrence matching has a failure mode of its own, and this pins it.

    Lose one of the two blocks and the survivor becomes occurrence 1 — so a
    Paying figure would be published under the Projected name, which is the same
    silent-wrong-number outcome read from the other end. Nothing in the row says
    which block survived, so both fields go absent instead. It costs a figure the
    console can show as missing; the alternative costs a figure it shows as
    true.
    """
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    for occurrence in (1, 2):
        rows = _relabel(_rollup_rows(), "Total Services Sold (Not Actualized)",
                        "Total Services Sold (renamed)", occurrence=occurrence)
        jan = parse_official_totals(rows, 2026)["2026-01"]
        assert "services_sold_not_actualized" not in jan   # never the Paying 90
        assert "paying_services_sold" not in jan           # never the Projected 149
        # Only the shared label is withheld; the rest of both blocks still reads.
        assert jan["projected_new_clients_not_actualized"] == 114.0
        assert jan["paying_new_clients"] == 52.0
        assert jan["revenue_amount_sold_not_actualized"] == 452607.70
        assert jan["paying_revenue_amount_sold"] == 290691.70


def test_roas_not_actualized_is_derived_per_month_never_averaged():
    """No sheet row carries it: it is revenue_amount_sold_not_actualized / spend,
    recomputed from THAT period's own components. Averaging monthly ratios
    instead of recomputing them measures +1.74pp of error on this data."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    got = parse_official_totals(_rollup_rows(), 2026)
    jan, feb = got["2026-01"], got["2026-02"]

    assert jan["roas_not_actualized_pct"] == 188.92          # 452,607.70 / 239,581.57
    assert feb["roas_not_actualized_pct"] == 352.75          # 660,703.09 / 187,299.79
    for m in (jan, feb):
        assert round(m["revenue_amount_sold_not_actualized"] / m["spend"] * 100, 2) == \
            m["roas_not_actualized_pct"]
    # Each period stands on its own components — one figure is not the other's,
    # and neither is the mean of the two.
    mean = round((jan["roas_not_actualized_pct"] + feb["roas_not_actualized_pct"]) / 2, 2)
    assert jan["roas_not_actualized_pct"] != feb["roas_not_actualized_pct"] != mean
    # It is a distinct metric from the sheet's own (actualized) ROAS row.
    assert (jan["roas_pct"], feb["roas_pct"]) == (109.75, 214.42)


def test_roas_not_actualized_is_absent_when_an_input_is():
    """Same missing-field law as the parsed rows — a derived field with a missing
    input is absent, not 0. A 0% ROAS on a board report is a fire drill."""
    from marketing_research_agent.sources.sheets_source import parse_official_totals

    no_revenue = _relabel(_rollup_rows(), "Revenue Amount Sold (Not Actualized)",
                          "Revenue Amount Sold (Signed)", occurrence=1)
    jan = parse_official_totals(no_revenue, 2026)["2026-01"]
    assert "revenue_amount_sold_not_actualized" not in jan
    assert "roas_not_actualized_pct" not in jan

    no_spend = _relabel(_rollup_rows(), "Spend", "Spend (media)")
    jan = parse_official_totals(no_spend, 2026)["2026-01"]
    assert "spend" not in jan and "roas_not_actualized_pct" not in jan

    # A zero denominator is not a ratio either.
    zero_spend = [r if r[0] != "Spend" else ["Spend", "$0.00", "", "$0.00", "", "x"]
                  for r in _rollup_rows()]
    jan = parse_official_totals(zero_spend, 2026)["2026-01"]
    assert jan["spend"] == 0.0 and "roas_not_actualized_pct" not in jan


def test_row_for_occurrence_defaults_to_the_first_match():
    """The default keeps every pre-existing caller (parse_tracker, the vendor
    field map) resolving exactly the row it always resolved."""
    from marketing_research_agent.sources.sheets_source import _row_for

    rows = [["All"], ["Spend", "1"], ["Leads", "2"], ["Spend", "3"]]
    assert _row_for(rows, 1, len(rows), ["spend"]) == 1          # default = 1st
    assert _row_for(rows, 1, len(rows), ["spend"], 1) == 1
    assert _row_for(rows, 1, len(rows), ["spend"], 2) == 3
    assert _row_for(rows, 1, len(rows), ["spend"], 3) is None    # absent, not the last one
    # Label priority still wins over occurrence: a candidate that never appears
    # is skipped, and the next one is tried at the same occurrence.
    assert _row_for(rows, 1, len(rows), ["total spend", "spend"], 2) == 3


def test_the_original_eight_official_fields_are_untouched():
    """The extension is additive. These eight keys, their label lists and their
    order are what every current caller (headline strip, trends, reconciliation)
    reads, so they are pinned verbatim."""
    from marketing_research_agent.sources.sheets_source import _OFFICIAL_FIELD_LABELS

    legacy = {
        "budget": ["budget"],
        "spend": ["spend"],
        "leads": ["leads"],
        "qualified_leads": ["qualified leads"],
        "demos_booked": ["total demos booked (sdr+vapi+direct)", "total demos booked"],
        "qual_demos_booked": ["qualified demos booked (sdr+vapi+direct)",
                              "qualified demos booked"],
        "demos_completed": ["demos completed (sdr+vapi+direct)", "total demos completed"],
        "services_sold": ["total services sold (actualized)"],
    }
    for field, labels in legacy.items():
        got_labels, occurrence = _OFFICIAL_FIELD_LABELS[field]
        assert list(got_labels) == labels
        assert occurrence == 1
