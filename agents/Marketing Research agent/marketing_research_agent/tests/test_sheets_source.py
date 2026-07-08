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


def test_meta_spend_uses_investment_column():
    metrics, _ = parse_tracker(_rows(), year=2026)
    jan_meta = next(m for m in metrics if m.channel == "META" and m.date == date(2026, 1, 1))
    assert jan_meta.spend == 3964.27  # Investment, not Performance (3603.88)
    assert jan_meta.leads == 27 and jan_meta.qualified_leads == 21
    assert jan_meta.demos_booked == 11 and jan_meta.demos_completed == 4


def test_google_spend_falls_back_to_performance_when_no_investment():
    metrics, _ = parse_tracker(_rows(), year=2026)
    feb_google = next(m for m in metrics if m.channel == "Google" and m.date == date(2026, 2, 1))
    assert feb_google.spend == 19250.72  # Performance (Investment column empty)


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
