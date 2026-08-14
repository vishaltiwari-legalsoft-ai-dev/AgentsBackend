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
