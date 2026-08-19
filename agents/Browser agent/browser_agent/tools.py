"""The non-browser half of the agent's toolbox.

The browser is the slowest and most fragile way to do almost anything, and for
some work it simply cannot do the job at all — Google Sheets paints its cells
onto a canvas, so no amount of clicking will ever read them. Where a real API
exists, we use it: it is faster, cheaper, and does not break when a site is
redesigned.

Nothing here is new capability for the company — Sheets reading, web search and
page fetching already exist and are proven. This module is the thin, honest
seam that lets a11 reach them, plus exactly one genuinely new thing: appending
to a sheet.

ON WRITING TO SHEETS — the important design decision:
There is no ``update`` and no ``clear`` in this file, and there never should be.
The agent appends to its own tab. Existing cells are not protected by a check
that could be reasoned around; they are unreachable because the capability was
never built. That is a much stronger promise than a guardrail.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("agentos.browser")

# Where findings go when the caller doesn't name a tab. Never an existing tab.
DEFAULT_TAB = os.environ.get("BROWSER_SHEET_TAB", "Agent findings")

MAX_ROWS_READ = 200
MAX_COLS_READ = 20
MAX_APPEND_ROWS = 500
MAX_SEARCH_RESULTS = 10
MAX_PAGE_CHARS = 12_000

WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# A write that hangs is worse than a read that hangs: the caller cannot tell
# whether the rows landed. Bounded so the agent gets an answer either way and
# can say so honestly. Same order as the MR read path — a Sheets round trip is
# a Sheets round trip.
SHEETS_WRITE_TIMEOUT_SECONDS = 30


class ToolError(RuntimeError):
    """A tool could not do its job. Carries a message meant for the user."""


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def _mr():
    """Marketing Research's sheet layer — the proven read path, reused as-is."""
    from marketing_research_agent import workbook
    from marketing_research_agent.sources import sheets_source
    from marketing_research_agent import sources_registry

    return sheets_source, workbook, sources_registry


def service_account_email() -> str:
    """The identity a user must share their sheet with."""
    _, _, registry = _mr()
    return registry.service_account_email()


def resolve_sheet(url_or_id: str) -> str:
    sheets_source, _, registry = _mr()  # noqa: F841 - registry is the parser
    sheet_id = registry.parse_spreadsheet_id(url_or_id or "")
    if not sheet_id:
        raise ToolError(
            f"{url_or_id!r} doesn't look like a Google Sheet link or id."
        )
    return sheet_id


def _wrap_google_error(exc: Exception, sheet_id: str, *, writing: bool = False) -> ToolError:
    """Turn an API error into something a person can act on."""
    text = str(exc)
    if "default credentials were not found" in text or "DefaultCredentialsError" in text:
        # A deployment problem, not a sharing problem — don't send someone off
        # to re-share a sheet that was never the issue.
        return ToolError(
            "This server has no Google credentials configured, so I can't reach "
            "Sheets at all. (Set GOOGLE_APPLICATION_CREDENTIALS locally; on Cloud "
            "Run the service account is attached automatically.)"
        )
    if "404" in text or "notFound" in text:
        return ToolError(
            f"I can't see that sheet. Share it with {service_account_email()} "
            f"({'Editor' if writing else 'Viewer'} is enough)."
        )
    if "403" in text or "permission" in text.lower():
        need = "Editor" if writing else "Viewer"
        return ToolError(
            f"I don't have {need} access to that sheet. Share it with "
            f"{service_account_email()} as {need}."
        )
    logger.warning("sheets error on %s: %s", sheet_id, exc)
    return ToolError(f"The Sheets API refused that request: {text[:300]}")


def sheet_tabs(url_or_id: str) -> dict:
    """Tab names in a workbook — the cheap 'what's in here?' call."""
    sheets_source, _, _ = _mr()
    sheet_id = resolve_sheet(url_or_id)
    try:
        meta = sheets_source.workbook_meta(sheet_id)
    except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
        raise _wrap_google_error(exc, sheet_id) from exc
    return {
        "sheet_id": sheet_id,
        "title": meta["title"],
        "tabs": [t["title"] for t in meta["tabs"] if not t.get("hidden")],
    }


def sheet_read(url_or_id: str, tab: str | None = None, max_rows: int = MAX_ROWS_READ) -> dict:
    """Read one tab, or a compact view of every tab when none is named."""
    sheets_source, workbook, _ = _mr()
    sheet_id = resolve_sheet(url_or_id)
    rows_cap = max(1, min(int(max_rows or MAX_ROWS_READ), MAX_ROWS_READ))

    try:
        if tab:
            rows = sheets_source.fetch_tab_values(sheet_id, tab)[:rows_cap]
            return {
                "sheet_id": sheet_id,
                "tab": tab,
                "rows": [r[:MAX_COLS_READ] for r in rows],
                "row_count": len(rows),
            }
        grids = workbook.fetch_workbook(sheet_id, max_rows=rows_cap)
        return {
            "sheet_id": sheet_id,
            "tabs": [
                {
                    "tab": g.title,
                    # Compact on purpose: a whole workbook of raw cells would
                    # swamp the context and buy nothing.
                    "rows": workbook.compact_grid(g.rows, max_rows=12, max_cols=MAX_COLS_READ),
                    "row_count": g.n_rows,
                }
                for g in grids
                if not g.hidden
            ],
        }
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap_google_error(exc, sheet_id) from exc


def _write_service():
    """A write-scoped Sheets client, separate from Marketing Research's.

    MR's service stays read-only scoped so "MR can only read" remains literally
    true rather than a claim about how carefully we call it. Only the transport
    plumbing is shared with MR (timeout + cached ADC lookup) — the scope, which
    is the part that matters, is this module's own.
    """
    from googleapiclient.discovery import build

    sheets_source, _, _ = _mr()
    creds = sheets_source.cached_credentials([WRITE_SCOPE])
    return build(
        "sheets", "v4",
        http=sheets_source.timed_http(creds, SHEETS_WRITE_TIMEOUT_SECONDS),
        cache_discovery=False,
    )


def _ensure_tab(service, sheet_id: str, tab: str) -> bool:
    """Create the tab if it isn't there. Returns True when it was created."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab in existing:
        return False
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    return True


def sheet_append(url_or_id: str, rows: list[list[Any]], tab: str | None = None) -> dict:
    """Append rows to the agent's own tab, creating it if needed.

    Deliberately the only write in the codebase: it can add rows and nothing
    else. No range is overwritten because no overwrite call exists.
    """
    if not rows:
        raise ToolError("There is nothing to write — give me at least one row.")
    if len(rows) > MAX_APPEND_ROWS:
        raise ToolError(f"That's {len(rows)} rows; I append at most {MAX_APPEND_ROWS} at a time.")

    sheet_id = resolve_sheet(url_or_id)
    target = (tab or DEFAULT_TAB).strip() or DEFAULT_TAB
    values = [["" if c is None else str(c) for c in row] for row in rows]

    try:
        service = _write_service()
        created = _ensure_tab(service, sheet_id, target)
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{target}'!A1",
            # RAW, never USER_ENTERED: the rows come from a model, and an
            # entered "=IMPORTRANGE(...)" would pull data nobody granted while
            # "=IMAGE(\"https://…\"&A1)" exfiltrates the row on render. Stored
            # as text, a formula is just characters.
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap_google_error(exc, sheet_id, writing=True) from exc

    return {
        "sheet_id": sheet_id,
        "tab": target,
        "tab_created": created,
        "rows_written": len(values),
    }


# --------------------------------------------------------------------------- #
# Web — search and read, without a browser
# --------------------------------------------------------------------------- #

def _seo():
    from seo_geo_agent import sources

    return sources


def web_search(query: str, limit: int = MAX_SEARCH_RESULTS) -> dict:
    """Search results without opening a tab or fighting a consent banner."""
    sources = _seo()
    query = (query or "").strip()
    if not query:
        raise ToolError("Give me something to search for.")
    try:
        found = sources.serper_search(query)
    except sources.CredentialMissing as exc:
        raise ToolError(f"Web search isn't available: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"The search failed: {str(exc)[:300]}") from exc

    cap = max(1, min(int(limit or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    return {
        "query": query,
        "results": [
            {"title": r.get("title", ""), "url": r.get("link", "")}
            for r in (found.get("organic") or [])[:cap]
        ],
        "related": (found.get("related") or [])[:5],
        "questions": (found.get("paa") or [])[:5],
    }


def read_url(url: str) -> dict:
    """A page's readable text, fetched directly. No tab, no rendering, no wait."""
    sources = _seo()
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError(f"{url!r} isn't a web address I can fetch.")
    try:
        page = sources.fetch_text(url)
    except sources.CredentialMissing as exc:
        raise ToolError(f"Page fetching isn't available: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Couldn't read that page: {str(exc)[:300]}") from exc

    status = page.get("status")
    if status and int(status) >= 400:
        raise ToolError(f"That page returned HTTP {status}.")
    return {
        "url": page.get("final_url") or url,
        "status": status,
        "text": str(page.get("text") or "")[:MAX_PAGE_CHARS],
    }


# --------------------------------------------------------------------------- #
# The registry the brain and the router both read
# --------------------------------------------------------------------------- #

TOOLS: dict[str, dict] = {
    "sheet_tabs": {
        "run": lambda a: sheet_tabs(a.get("sheet", "")),
        "args": "sheet",
        "help": 'List a workbook\'s tabs. {"tool":"sheet_tabs","sheet":"<url or id>"}',
    },
    "sheet_read": {
        "run": lambda a: sheet_read(a.get("sheet", ""), a.get("tab")),
        "args": "sheet, tab (optional — omit for a compact view of every tab)",
        "help": 'Read a sheet. {"tool":"sheet_read","sheet":"<url>","tab":"Leads"}',
    },
    "sheet_append": {
        # The tab is pinned, not taken from `a`: "its own tab" was only ever
        # true by convention, and a model naming an existing tab turned an
        # append-only tool into one that writes into live data.
        "run": lambda a: sheet_append(a.get("sheet", ""), a.get("rows") or [], DEFAULT_TAB),
        "args": "sheet, rows (list of lists)",
        "help": (
            'Add rows to a sheet. {"tool":"sheet_append","sheet":"<url>",'
            '"rows":[["Vendor","Note"]]} — always appends to the '
            f'"{DEFAULT_TAB}" tab, which it creates if missing; it CANNOT '
            "overwrite anything or write to any other tab."
        ),
    },
    "web_search": {
        "run": lambda a: web_search(a.get("query", "")),
        "args": "query",
        "help": 'Search the web. {"tool":"web_search","query":"..."}',
    },
    "read_url": {
        "run": lambda a: read_url(a.get("url", "")),
        "args": "url",
        "help": 'Read a page directly, no browser. {"tool":"read_url","url":"https://..."}',
    },
}


def run_tool(name: str, args: dict) -> dict:
    """Execute one tool. Raises ToolError with a message worth showing a person."""
    spec = TOOLS.get(name)
    if not spec:
        raise ToolError(f"There is no tool called {name!r}. Available: {', '.join(TOOLS)}")
    return spec["run"](args or {})


def grammar() -> str:
    """The tool list as the brain sees it."""
    return "\n".join(f'- {name}: {spec["help"]}' for name, spec in TOOLS.items())


def availability() -> dict:
    """What is actually usable right now — for the console, and to be honest in
    the prompt rather than offering a tool that will fail on first use."""
    sources = _seo()
    try:
        search_ok = bool(sources.serper_available())
    except Exception:  # noqa: BLE001
        search_ok = False
    sheets_ok = os.environ.get("BROWSER_OFFLINE", "0") != "1"
    return {
        "sheet_tabs": sheets_ok,
        "sheet_read": sheets_ok,
        "sheet_append": sheets_ok,
        "web_search": search_ok,
        "read_url": search_ok,
        "service_account": service_account_email(),
        "default_tab": DEFAULT_TAB,
    }
