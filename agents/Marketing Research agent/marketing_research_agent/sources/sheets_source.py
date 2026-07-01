"""Google Sheets data source — pulls Legal Soft's live performance tracker.

The tracker is a transposed monthly grid: metric names down column A, month
columns in ``(Performance)`` / ``(Investment)`` pairs, with quarter and YTD
rollups interleaved. One tab covers a brand and may contain several channel
blocks (e.g. a META block plus a ``GOOGLE`` sub-block).

Auth uses Application Default Credentials with the read-only Drive scope (already
enabled for this project), pulling each tab through the authenticated CSV-export
endpoint — so the Google Sheets API does not need to be enabled. The fetcher is
injectable so ``parse_tracker`` and this class can be unit-tested offline.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Callable

from ..schemas import CampaignMetric, DataGap, DateRange, Lead

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Channel keywords that mark a block header row (exact, case-insensitive label).
_CHANNEL_HEADERS = {
    "google": "Google", "meta": "META", "facebook": "META", "email": "Email",
    "website": "Websites", "websites": "Websites", "organic": "Organic",
    "linkedin": "LinkedIn",
}

# Acceptable exact row labels (stripped, case-insensitive) per canonical field.
# Order = priority; first match within a block wins.
_FIELD_LABELS = {
    "spend": ["spend"],
    "leads": ["leads"],
    "qualified_leads": ["qualified leads"],
    "demos_booked": [
        "total demos booked (sdr+vapi+direct)",
        "total demos booked",
        "qualified demos booked (sdr+vapi+direct)",
        "qualified demos booked",
    ],
    "demos_completed": [
        "demos completed (sdr+vapi+direct)",
        "total demos completed (direct)",
        "total demos completed",
    ],
}


def _num(s: str) -> float | None:
    s = (s or "").strip()
    if s in ("", "#N/A", "#DIV/0!", "#REF!", "-", "#VALUE!", "#NAME?"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("$", "").replace(",", "").replace("%", "").replace("(", "").replace(")", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _month_columns(header: list[str]) -> list[tuple[int, int, int]]:
    """From the header row, return (month_num, perf_col, inv_col) per real month.

    Quarter and YTD rollup columns are skipped. ``inv_col`` is -1 if a month has
    no Investment column."""
    perf: dict[int, int] = {}
    inv: dict[int, int] = {}
    for col, raw in enumerate(header):
        text = (raw or "").strip().lower()
        if "ytd" in text or "quarter" in text:
            continue
        m = re.match(r"([a-z]+)", text)
        if not m or m.group(1) not in _MONTHS:
            continue
        month = _MONTHS[m.group(1)]
        if "performance" in text:
            perf[month] = col
        elif "investment" in text:
            inv[month] = col
    out = []
    for month in sorted(perf):
        out.append((month, perf[month], inv.get(month, -1)))
    return out


def _block_channel(title_or_label: str) -> str | None:
    text = (title_or_label or "").strip().lower()
    for key, channel in _CHANNEL_HEADERS.items():
        if re.search(rf"\b{re.escape(key)}\b", text):
            return channel
    return None


def _brand_from_title(title: str) -> str:
    """`Meta 360 RA` -> `360 RA`; strip a leading channel word if present."""
    parts = (title or "").strip().split(None, 1)
    if parts and _block_channel(parts[0]):
        return parts[1].strip() if len(parts) > 1 else title.strip()
    return (title or "").strip()


def _find_blocks(rows: list[list[str]], title: str) -> list[tuple[str, int, int]]:
    """Return (channel, start_row, end_row_exclusive) for each channel block.

    The top block's channel comes from the tab title; sub-blocks are introduced
    by a bare channel-name row (e.g. ``GOOGLE``)."""
    headers: list[tuple[str, int]] = []
    # The top block's scope comes from A1: a channel-bearing title (e.g.
    # "Meta 360 RA") → that channel; otherwise it's the consolidated roll-up
    # (A1 = "All"/"Overall"), which the 2026 goals call "Total".
    top_channel = _block_channel(title) or "Total"
    headers.append((top_channel, 1))  # data starts just under the title row
    for i, row in enumerate(rows):
        if i == 0:
            continue
        label = (row[0] if row else "").strip()
        if label and label.lower() in _CHANNEL_HEADERS and len(label.split()) == 1:
            headers.append((_CHANNEL_HEADERS[label.lower()], i + 1))
    headers.sort(key=lambda h: h[1])
    blocks = []
    for idx, (channel, start) in enumerate(headers):
        end = headers[idx + 1][1] - 1 if idx + 1 < len(headers) else len(rows)
        blocks.append((channel, start, end))
    return blocks


def _row_for(rows: list[list[str]], start: int, end: int, candidates: list[str]) -> int | None:
    for want in candidates:
        for i in range(start, end):
            label = (rows[i][0] if i < len(rows) and rows[i] else "").strip().lower()
            if label == want:
                return i
    return None


def parse_tracker(rows: list[list[str]], year: int, brand: str | None = None) -> tuple[list[CampaignMetric], list[DataGap]]:
    """Parse a tracker tab into monthly ``CampaignMetric`` rows (one per channel
    block per month with data)."""
    if not rows:
        return [], [DataGap("sheets", "empty tab")]
    title = (rows[0][0] if rows[0] else "").strip()
    brand = brand or _brand_from_title(title)
    months = _month_columns(rows[0])
    if not months:
        return [], [DataGap("sheets", f"no month columns found in '{title}'")]

    metrics: list[CampaignMetric] = []
    gaps: list[DataGap] = []
    for channel, start, end in _find_blocks(rows, title):
        idx = {f: _row_for(rows, start, end, labels) for f, labels in _FIELD_LABELS.items()}
        if idx["spend"] is None and idx["leads"] is None:
            continue  # not a data block
        for missing in [f for f, i in idx.items() if i is None]:
            gaps.append(DataGap("sheets", f"{brand}/{channel}: no '{missing}' row"))

        def val(field: str, perf: int, inv: int) -> float | None:
            i = idx[field]
            if i is None:
                return None
            row = rows[i]
            cell = lambda c: row[c] if 0 <= c < len(row) else ""
            if field == "spend":  # actual billed (Investment), fallback to Performance
                return _num(cell(inv)) if inv >= 0 and _num(cell(inv)) is not None else _num(cell(perf))
            return _num(cell(perf)) if _num(cell(perf)) is not None else _num(cell(inv))

        for month, perf, inv in months:
            spend = val("spend", perf, inv)
            leads = val("leads", perf, inv)
            if not spend and not leads:
                continue
            metrics.append(CampaignMetric(
                channel=channel,
                campaign=f"{brand} · {channel}",
                utm_source=channel.lower(),
                utm_medium="paid",
                utm_campaign=brand,
                spend=spend or 0.0,
                leads=int(leads or 0),
                qualified_leads=int(val("qualified_leads", perf, inv) or 0),
                demos_booked=int(val("demos_booked", perf, inv) or 0),
                demos_completed=int(val("demos_completed", perf, inv) or 0),
                date=date(year, month, 1),
            ))
    return metrics, gaps


# --- Authenticated fetch ---------------------------------------------------

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _sheets_service():
    import google.auth
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=[SHEETS_SCOPE, DRIVE_SCOPE])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def list_tabs(spreadsheet_id: str, *, service=None) -> list[dict]:
    """Every tab via the Sheets API: ``{gid, title, hidden, rows, cols}``."""
    svc = service or _sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    tabs = []
    for s in meta.get("sheets", []):
        p = s["properties"]
        grid = p.get("gridProperties", {})
        tabs.append({
            "gid": p["sheetId"],
            "title": p["title"],
            "hidden": p.get("hidden", False),
            "rows": grid.get("rowCount"),
            "cols": grid.get("columnCount"),
        })
    return tabs


def fetch_tab_values(spreadsheet_id: str, title: str, *, service=None) -> list[list[str]]:
    """Read a tab's displayed values via the Sheets API as a list-of-lists of
    strings (FORMATTED_VALUE keeps the $ / % / , formatting parse_tracker strips)."""
    svc = service or _sheets_service()
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{title}'", valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    return [["" if c is None else str(c) for c in row] for row in resp.get("values", [])]


def fetch_all_tab_values(spreadsheet_id: str, titles: list[str], *, service=None) -> dict[str, list[list[str]]]:
    """Read many tabs in one Sheets API call (values.batchGet)."""
    svc = service or _sheets_service()
    resp = (
        svc.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=[f"'{t}'" for t in titles], valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    out: dict[str, list[list[str]]] = {}
    for title, vr in zip(titles, resp.get("valueRanges", [])):
        out[title] = [["" if c is None else str(c) for c in row] for row in vr.get("values", [])]
    return out


def _default_fetcher(spreadsheet_id: str, gid: str) -> str:
    import google.auth
    import httpx
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])
    creds.refresh(Request())
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {creds.token}"},
        follow_redirects=True, timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def _default_xlsx_fetcher(spreadsheet_id: str) -> bytes:
    import google.auth
    import httpx
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])
    creds.refresh(Request())
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {creds.token}"},
        follow_redirects=True, timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def fetch_all_trackers(
    spreadsheet_id: str,
    year: int,
    *,
    service=None,
    xlsx_fetcher: Callable[[str], bytes] | None = None,
    max_rows: int = 200,
) -> list[dict]:
    """Discover and parse every tab that is a performance tracker.

    Prefers the Google Sheets API (clean tab enumeration with gids/titles). Falls
    back to a whole-workbook xlsx export (openpyxl) if the Sheets API is
    unavailable. Non-tracker tabs (Looker dumps, raw data, pivots) parse to
    nothing and are skipped. Returns ``{"tab", "gid"?, "metrics", "gaps"}``."""
    try:
        svc = service or _sheets_service()
        out: list[dict] = []
        for tab in list_tabs(spreadsheet_id, service=svc):
            rows = fetch_tab_values(spreadsheet_id, tab["title"], service=svc)[:max_rows]
            metrics, gaps = parse_tracker(rows, year)
            if metrics:
                out.append({"tab": tab["title"], "gid": tab["gid"], "metrics": metrics, "gaps": gaps})
        return out
    except Exception:
        return _fetch_all_trackers_xlsx(spreadsheet_id, year, xlsx_fetcher=xlsx_fetcher, max_rows=max_rows)


def _fetch_all_trackers_xlsx(
    spreadsheet_id: str,
    year: int,
    *,
    xlsx_fetcher: Callable[[str], bytes] | None = None,
    max_rows: int = 200,
) -> list[dict]:
    """Fallback discovery: export the whole workbook as xlsx and scan every tab.
    Requires ``openpyxl`` (imported lazily)."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "openpyxl is required for the xlsx discovery fallback; "
            "`pip install openpyxl`, enable the Sheets API, or pass an explicit gid."
        ) from exc
    data = (xlsx_fetcher or _default_xlsx_fetcher)(spreadsheet_id)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[dict] = []
    for name in wb.sheetnames:
        ws = wb[name]
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
            if len(rows) >= max_rows:
                break
        metrics, gaps = parse_tracker(rows, year)
        if metrics:
            out.append({"tab": name, "metrics": metrics, "gaps": gaps})
    return out


class SheetsSource:
    def __init__(
        self,
        spreadsheet_id: str,
        gid: str,
        *,
        year: int,
        brand: str | None = None,
        fetcher: Callable[[str, str], str] | None = None,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.gid = str(gid)
        self.year = year
        self.brand = brand
        self.name = f"sheets:{spreadsheet_id}:{gid}"
        self._fetcher = fetcher or _default_fetcher

    def _rows(self) -> list[list[str]]:
        text = self._fetcher(self.spreadsheet_id, self.gid)
        return list(csv.reader(io.StringIO(text)))

    def fetch_campaign_metrics(self, range: DateRange) -> tuple[list[CampaignMetric], list[DataGap]]:
        metrics, gaps = parse_tracker(self._rows(), self.year, self.brand)
        lo, hi = range.start, range.end
        kept = [m for m in metrics if lo <= m.date <= hi]
        return kept, gaps

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]:
        # This tracker is channel-aggregate; it carries no lead-level rows.
        return [], []
