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
import threading
from datetime import date
from typing import Callable, Sequence

from .. import config
from ..schemas import CampaignMetric, DataGap, DateRange, Lead

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Channel keywords that mark a block header row (exact, case-insensitive label).
# Also how a tab title is classified. A vendor whose channel is missing here is
# not a cosmetic problem: see `default_channel` in _find_blocks.
_CHANNEL_HEADERS = {
    "google": "Google", "meta": "META", "facebook": "META", "email": "Email",
    "website": "Websites", "websites": "Websites", "organic": "Organic",
    "linkedin": "LinkedIn",
    # 2026-08 vendor tabs — without these their tabs classified as "Total".
    "chatgpt": "ChatGPT", "microsoft": "Microsoft", "bing": "Microsoft",
    "twitter": "Twitter", "youtube": "YouTube", "tiktok": "TikTok",
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


def scan_month_columns(header: list[str]) -> tuple[list[tuple[int, int, int]], list[int]]:
    """``(columns, repeated_months)`` for a tracker header row.

    ``columns`` is (month_num, perf_col, inv_col) per real month; quarter and YTD
    rollup columns are skipped and ``inv_col`` is -1 when a month has no
    Investment column.

    **The FIRST occurrence of a month wins, and the repeat is reported.** This
    used to keep the last one, which is how a second month-column band added to
    the right of the grid silently re-pointed every figure: on 2026-07-27 the
    Overall tab read July spend $48,005.09, and after a layout change the same
    row on the same tab read $5,579.58 — a roll-up *below* the sum of the vendor
    tabs it aggregates. Worse, Performance and Investment were resolved
    independently, so a pair could straddle two different tables. The primary
    grid is the leftmost one; anything repeated to its right is a secondary view
    the parser has no basis for preferring, so it is ignored *and named* —
    ``repeated_months`` is what turns this from a silent swap into a data gap.
    """
    perf: dict[int, int] = {}
    inv: dict[int, int] = {}
    repeated: set[int] = set()
    for col, raw in enumerate(header):
        text = (raw or "").strip().lower()
        if "ytd" in text or "quarter" in text:
            continue
        m = re.match(r"([a-z]+)", text)
        if not m or m.group(1) not in _MONTHS:
            continue
        month = _MONTHS[m.group(1)]
        if "performance" in text:
            target = perf
        elif "investment" in text:
            target = inv
        else:
            continue
        if month in target:
            repeated.add(month)
            continue  # first band wins
        target[month] = col
    out = []
    for month in sorted(perf):
        i = inv.get(month, -1)
        # An Investment column left of its Performance column cannot be its
        # pair — it belongs to another band. Better no billed figure than one
        # lifted from a different table.
        if i < perf[month]:
            i = -1
        out.append((month, perf[month], i))
    return out, sorted(repeated)


def _month_columns(header: list[str]) -> list[tuple[int, int, int]]:
    """Columns only — see :func:`scan_month_columns` for the repeat report."""
    return scan_month_columns(header)[0]


_MONTH_NAMES = ("", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December")


def _repeat_gap_message(title: str, repeated: list[int]) -> str:
    names = ", ".join(_MONTH_NAMES[m] for m in repeated)
    return (f"'{title}': {names} appear(s) in more than one column band — read "
            f"the leftmost grid and ignored the repeat(s). Check the tab layout.")


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


def _find_blocks(rows: list[list[str]], title: str,
                 default_channel: str = "Total") -> list[tuple[str, int, int]]:
    """Return (channel, start_row, end_row_exclusive) for each channel block.

    The top block's channel comes from the tab title; sub-blocks are introduced
    by a bare channel-name row (e.g. ``GOOGLE``).

    ``default_channel`` is what an unrecognised title falls back to. "Total" is
    right for the consolidated roll-up (A1 = "All"/"Overall") and WRONG for a
    vendor tab — a vendor whose channel is not in ``_CHANNEL_HEADERS``
    ("DanteAgency ChatGPT") landed in a channel called "Total", which the report
    builder pops out and treats as the whole report's totals. Two small vendor
    tabs then WERE the headline KPI strip. Callers reading a vendor tab pass
    "Other" so an unknown channel stays a channel."""
    headers: list[tuple[str, int]] = []
    top_channel = _block_channel(title) or default_channel
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
    months, repeated = scan_month_columns(rows[0])
    if not months:
        return [], [DataGap("sheets", f"no month columns found in '{title}'")]

    metrics: list[CampaignMetric] = []
    gaps: list[DataGap] = []
    if repeated:
        gaps.append(DataGap("sheets", _repeat_gap_message(title, repeated)))
    # A vendor tab's own scope. Anything else in the tab is a sub-block.
    own_channel = _block_channel(title) or ("Total" if is_rollup_scope(rows) else "Other")
    for channel, start, end in _find_blocks(rows, title, default_channel=own_channel):
        # A channel that has its own dedicated tab is pasted into vendor tabs as
        # a reference block. Ingesting it from there is wrong twice over: the
        # rows are attributed to the wrong vendor, and the same block is counted
        # once per tab carrying it. On 2026-08-15 the live workbook had the
        # Websites block in SEVEN vendor tabs plus its own — $8,632/month
        # counted eight times ($60,424 of phantom spend a month) — and the two
        # vendors whose own block was still empty showed nothing BUT Websites'
        # numbers under their own name.
        if channel in config.NON_MEDIA_CHANNELS and channel != own_channel:
            gaps.append(DataGap("sheets", (
                f"{brand}: ignored a '{channel}' block pasted into this tab — "
                f"those rows belong to the '{channel}' tab and would be counted twice")))
            continue
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
            # Official basis (decision 2026-07-27): the team reads the sheet's
            # Performance columns, so every figure — spend included — is
            # Performance first, Investment (billed) fallback.
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

# --- Deadlines -------------------------------------------------------------
# Every Google call here runs inside a *sync* FastAPI handler, which means it
# runs on anyio's worker threadpool — 40 slots for the whole process. A call
# that stalls does not merely fail slowly, it takes a slot out of circulation;
# ~40 stalled sheet pulls wedge the service including /api/health. So each
# transport gets a deadline we chose, not one we inherited.
#
# googleapiclient does apply an implicit 60s socket timeout of its own
# (`googleapiclient.http.DEFAULT_HTTP_TIMEOUT_SEC`), so these calls were not
# literally unbounded — but 60s is far longer than any healthy Sheets call, it
# is invisible at the call site, and it is a *socket* timeout (per read), so a
# server dribbling bytes can still outlive it. Stating the number here makes it
# reviewable and tunable.
SHEETS_TIMEOUT_SECONDS = 30
# Token mint/refresh against oauth2.googleapis.com (or the metadata server): a
# tiny request with no reason to be slow. google-auth's own default is 120s.
AUTH_TIMEOUT_SECONDS = 10
# The CSV / xlsx export endpoints stream a whole tab or workbook, so they get
# more room than an API call — but still a fixed ceiling.
EXPORT_TIMEOUT_SECONDS = 60


class SheetsUnavailable(RuntimeError):
    """A Sheets read could not be completed.

    Deliberately distinct from an *empty* result. "The API refused us" and "the
    workbook genuinely has no roll-up tab" are different facts, and collapsing
    them into ``{}`` is how a 429 turned into a silent, permanent-looking "this
    workbook has no official totals". Callers may present this to a user; the
    message always names the actual cause.
    """


class _TimedRequest:
    """google-auth transport wrapper that stamps our deadline onto token calls.

    ``creds.refresh(request)`` never passes a timeout, so google-auth's 120s
    default applies. Wrapping the transport is the only seam that reaches it.
    """

    def __init__(self, inner, timeout: float):
        self._inner = inner
        self._timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        return self._inner(
            url, method=method, body=body, headers=headers,
            timeout=self._timeout if timeout is None else timeout, **kwargs,
        )


# ADC resolution is not free — it reads the key file or queries the Cloud Run
# metadata server — and `_default_fetcher` used to redo it for *every tab*, so
# one 6-tab ingest paid for it six times. Credentials are safe to share: the
# httplib2 transport built on top of them is what is per-client (below).
_creds_cache: dict[tuple[str, ...], object] = {}
_creds_lock = threading.Lock()
_refresh_lock = threading.Lock()


def cached_credentials(scopes: Sequence[str]):
    """ADC credentials for a scope set, resolved once per process."""
    key = tuple(sorted(scopes))
    creds = _creds_cache.get(key)
    if creds is not None:
        return creds
    import google.auth

    with _creds_lock:
        creds = _creds_cache.get(key)
        if creds is None:
            creds, _ = google.auth.default(scopes=list(key))
            _creds_cache[key] = creds
    return creds


def refresh_if_stale(creds) -> None:
    """Mint a token only when the cached one is missing or expired."""
    if getattr(creds, "valid", False):
        return
    from google.auth.transport.requests import Request

    with _refresh_lock:
        if getattr(creds, "valid", False):  # another thread beat us to it
            return
        creds.refresh(_TimedRequest(Request(), AUTH_TIMEOUT_SECONDS))


def timed_http(creds, timeout: float):
    """An authorised httplib2 transport carrying an explicit socket timeout.

    ``build(credentials=...)`` constructs its own transport internally, so
    passing ``http=`` is the only way to state a deadline. A *fresh*
    ``httplib2.Http`` per client is deliberate and not an oversight: httplib2
    is not thread-safe, and these clients are used from FastAPI worker threads.
    Building one costs nothing (the discovery document is bundled statically in
    google-api-python-client, so `build` opens no socket).
    """
    import google_auth_httplib2
    import httplib2

    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))


def _sheets_service():
    from googleapiclient.discovery import build

    creds = cached_credentials([SHEETS_SCOPE, DRIVE_SCOPE])
    return build(
        "sheets", "v4",
        http=timed_http(creds, SHEETS_TIMEOUT_SECONDS),
        cache_discovery=False,
    )


def workbook_meta(spreadsheet_id: str, *, service=None) -> dict:
    """Workbook title + tab list in one Sheets API call:
    ``{"title", "tabs": [{gid, title, hidden, rows, cols}]}``."""
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
    title = (meta.get("properties") or {}).get("title") or spreadsheet_id
    return {"title": title, "tabs": tabs}


def list_tabs(spreadsheet_id: str, *, service=None) -> list[dict]:
    """Every tab via the Sheets API: ``{gid, title, hidden, rows, cols}``."""
    return workbook_meta(spreadsheet_id, service=service)["tabs"]


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
    # Credentials are resolved once per process and the token is reused until it
    # actually expires — this used to re-run ADC + mint a fresh token per tab.
    import httpx

    creds = cached_credentials([DRIVE_SCOPE])
    refresh_if_stale(creds)
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {creds.token}"},
        follow_redirects=True, timeout=EXPORT_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.text


def _default_xlsx_fetcher(spreadsheet_id: str) -> bytes:
    import httpx

    creds = cached_credentials([DRIVE_SCOPE])
    refresh_if_stale(creds)
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {creds.token}"},
        follow_redirects=True, timeout=EXPORT_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.content


def is_rollup_title(title: str) -> bool:
    """True when the TAB NAME says roll-up (e.g. "Marketing 2026 Overall
    Report"). This is the workbook's layout boundary — vendors before it, ops
    sheets after — and it is stable whatever the dropdown leaves in A1."""
    return "overall" in (title or "").lower()


def is_rollup_scope(rows: list[list[str]]) -> bool:
    """True when A1 says the tab is currently showing the consolidated view.

    A1 is a dropdown: any tab can be left scoped to "All". That makes it a fine
    signal for "do not ingest THIS tab as a vendor" and a terrible one for
    "stop reading the workbook" — one mis-set dropdown on the first tab used to
    end the scan before a single vendor was read, and the pull still reported
    success. Only :func:`is_rollup_title` may stop the scan.
    """
    a1 = (rows[0][0] if rows and rows[0] else "").strip().lower()
    return a1 in ("all", "overall")


def is_rollup_tab(title: str, rows: list[list[str]]) -> bool:
    """True for the consolidated roll-up tab by either signal. Its numbers are
    the sum of the vendor tabs, so ingesting it alongside them double-counts
    every dollar."""
    return is_rollup_title(title) or is_rollup_scope(rows)


def is_rollup_platform(platform: str) -> bool:
    """True when a dataset-run platform key ("sheets:<tab>") points at the
    roll-up tab. Read-path guard: runs ingested before the fetch-time skip
    existed stay in storage forever and would double-count every dollar."""
    plat = str(platform or "")
    title = plat.split(":", 1)[1] if ":" in plat else plat
    return is_rollup_title(title)


# Team-level rows the roll-up tab reports itself. These are THE official
# figures (the roll-up aggregates ledger/raw sources — Referral, Websites, … —
# that have no vendor tab, so a vendor-tab sum can NEVER reproduce them).
# Order per field = label priority, exact stripped-lowercase match.
_OFFICIAL_FIELD_LABELS: dict[str, list[str]] = {
    "budget": ["budget"],
    "spend": ["spend"],
    "leads": ["leads"],
    "qualified_leads": ["qualified leads"],
    "demos_booked": ["total demos booked (sdr+vapi+direct)", "total demos booked"],
    "qual_demos_booked": ["qualified demos booked (sdr+vapi+direct)", "qualified demos booked"],
    "demos_completed": ["demos completed (sdr+vapi+direct)", "total demos completed"],
    "services_sold": ["total services sold (actualized)"],
}


def parse_official_totals(
    rows: list[list[str]], year: int, *, warnings: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """The roll-up tab's own team-level rows — the sheet's official monthly
    figures, the exact cells the team reads (Performance basis, Investment
    fallback). Keys are "YYYY-MM" → {field: value}.

    ``warnings`` (optional, appended to) collects layout notes. The roll-up is
    the one tab whose figures nothing else can cross-check field by field, so a
    repeated month band here needs saying even when the numbers look plausible.
    """
    if not rows:
        return {}
    months, repeated = scan_month_columns(rows[0])
    if not months:
        return {}
    title = (rows[0][0] if rows[0] else "").strip()
    if repeated and warnings is not None:
        warnings.append(_repeat_gap_message(title or "Overall tab", repeated))
    blocks = _find_blocks(rows, title)
    start, end = (blocks[0][1], blocks[0][2]) if blocks else (1, len(rows))
    out: dict[str, dict[str, float]] = {}
    for field, labels in _OFFICIAL_FIELD_LABELS.items():
        i = _row_for(rows, start, end, labels)
        if i is None:
            continue
        row = rows[i]
        cell = lambda c: row[c] if 0 <= c < len(row) else ""
        for month, perf, inv in months:
            v = _num(cell(perf))
            if v is None and inv >= 0:
                v = _num(cell(inv))
            if v is not None:
                out.setdefault(f"{year:04d}-{month:02d}", {})[field] = v
    return out


def parse_official_spend(rows: list[list[str]], year: int) -> dict[str, float]:
    """Spend-only view of :func:`parse_official_totals` (kept for callers that
    predate the full-totals read)."""
    return {k: v["spend"] for k, v in parse_official_totals(rows, year).items() if "spend" in v}


def fetch_official_totals(
    spreadsheet_id: str, year: int, *, service=None, warnings: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """Official monthly team-level figures from the consolidated roll-up tab.

    Returns ``{}`` for exactly one meaning: **the workbook was read successfully
    and has no roll-up tab carrying official rows.** Any failure to read it — a
    429, a revoked share, a timeout, the Sheets API disabled — raises
    :class:`SheetsUnavailable` instead.

    This used to ``return {}`` on any exception, which made a transient Sheets
    error indistinguishable from a workbook that genuinely has no Overall tab:
    the headline figures silently vanished and nothing anywhere said why. The
    caller needs to tell those apart, because they warrant opposite responses —
    surface an error vs. label the figure as a vendor-tab sum.
    """
    try:
        svc = service or _sheets_service()
        tabs = list_tabs(spreadsheet_id, service=svc)
    except Exception as exc:  # noqa: BLE001 — re-raised, never swallowed
        raise SheetsUnavailable(
            f"could not list the tabs of workbook {spreadsheet_id}: {exc}"
        ) from exc

    for tab in tabs:
        if not is_rollup_title(tab["title"]):
            continue
        try:
            rows = fetch_tab_values(spreadsheet_id, tab["title"], service=svc)[:120]
        except Exception as exc:  # noqa: BLE001 — re-raised, never swallowed
            raise SheetsUnavailable(
                f'could not read the roll-up tab "{tab["title"]}" of workbook '
                f"{spreadsheet_id}: {exc}"
            ) from exc
        official = parse_official_totals(rows, year, warnings=warnings)
        if official:
            return official
    # Read fine, nothing to report — the honest empty case.
    return {}


def fetch_official_spend(spreadsheet_id: str, year: int, *, service=None) -> dict[str, float]:
    """Spend-only view of :func:`fetch_official_totals` (back-compat).

    Raises :class:`SheetsUnavailable` on a read failure, same contract.
    """
    totals = fetch_official_totals(spreadsheet_id, year, service=service)
    return {k: v["spend"] for k, v in totals.items() if "spend" in v}


# The roll-up aggregates the vendor tabs PLUS ledger sources only it carries
# (Referral, Websites, …). Its media spend is therefore always >= the sum of the
# vendor tabs. When it comes back BELOW them, that is not a business fact — it is
# a read that landed on the wrong cells, and it is the single check that would
# have caught the 2026-08 breakage on the day it happened instead of a month
# later. A little slack absorbs rounding and the seconds between two reads.
OFFICIAL_SHORTFALL_TOLERANCE = 0.98


def reconcile_official_spend(
    fetched: list[dict], official: dict[str, dict[str, float]], *, through: date
) -> list[str]:
    """Months whose official spend is impossibly below the vendor-tab sum.

    Compared on media channels only (the vendor tabs' non-media blocks are the
    part we cannot be sure the roll-up's Spend row includes) and only for months
    up to ``through`` — vendor tabs pre-fill future retainer months that the
    roll-up legitimately leaves at zero.

    An empty list means the two halves agree. Every entry names both figures, so
    whoever reads it can go straight to the cell.
    """
    by_month: dict[str, float] = {}
    for tab in fetched:
        for m in tab.get("metrics") or []:
            if m.channel in config.NON_MEDIA_CHANNELS:
                continue
            key = f"{m.date.year:04d}-{m.date.month:02d}"
            by_month[key] = by_month.get(key, 0.0) + float(m.spend or 0.0)
    limit = f"{through.year:04d}-{through.month:02d}"
    out: list[str] = []
    for key, vendor_sum in sorted(by_month.items()):
        if key > limit or vendor_sum <= 0:
            continue
        off = (official.get(key) or {}).get("spend")
        if off is None:
            continue
        if off < vendor_sum * OFFICIAL_SHORTFALL_TOLERANCE:
            out.append(
                f"{key}: the Overall tab reports ${off:,.2f} spend but the vendor "
                f"tabs sum to ${vendor_sum:,.2f} — the roll-up cannot be below "
                f"the tabs it aggregates, so its columns are being misread"
            )
    return out


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
    nothing and are skipped. Returns ``{"tab", "gid"?, "metrics", "gaps"}``.

    An empty list means "read fine, no tab parsed as a tracker". If *both* read
    paths fail this raises :class:`SheetsUnavailable` naming both causes — the
    API error is the diagnostic one, and reporting only the fallback's failure
    sends whoever is debugging down the wrong road.
    """
    try:
        svc = service or _sheets_service()
        out: list[dict] = []
        for tab in list_tabs(spreadsheet_id, service=svc):
            # Hidden tabs are archives and Looker dumps, never a live vendor —
            # and they sit BEFORE the vendors, so this also saves their fetches.
            if tab.get("hidden"):
                continue
            rows = fetch_tab_values(spreadsheet_id, tab["title"], service=svc)[:max_rows]
            if is_rollup_title(tab["title"]):
                # Workbook layout rule (user, 2026-07-27): every tab BEFORE the
                # Overall Report is a vendor; everything after it is ops sheets
                # (raw data, HubSpot pulls, logs) — never ingest past it.
                break
            if is_rollup_scope(rows):
                continue  # left on the "All" scope — skip the tab, keep scanning
            metrics, gaps = parse_tracker(rows, year)
            if metrics:
                out.append({"tab": tab["title"], "gid": tab["gid"], "metrics": metrics, "gaps": gaps})
        return out
    except Exception as api_exc:  # noqa: BLE001 — the xlsx path is a real fallback
        try:
            return _fetch_all_trackers_xlsx(
                spreadsheet_id, year, xlsx_fetcher=xlsx_fetcher, max_rows=max_rows
            )
        except Exception as xlsx_exc:  # noqa: BLE001 — both paths down = honest raise
            raise SheetsUnavailable(
                f"could not read workbook {spreadsheet_id}: the Sheets API failed "
                f"({api_exc}) and the xlsx export fallback also failed ({xlsx_exc})"
            ) from api_exc


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
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue  # same rule as the API path
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
            if len(rows) >= max_rows:
                break
        if is_rollup_title(name):
            break  # same layout rule as the API path: vendors end at the roll-up
        if is_rollup_scope(rows):
            continue
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
