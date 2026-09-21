"""Workbook ingestion — read every tab of the live spreadsheet into grids.

This is the substrate the agent reasons over: instead of parsing one fixed tab,
it pulls all tabs (campaign metadata, leads-by-platform, month-on-month, raw
exports, …) so the profiler and insight engine can decide what to use.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .sources.sheets_source import _sheets_service, fetch_all_tab_values, list_tabs


@dataclass
class TabGrid:
    title: str
    gid: int
    hidden: bool
    rows: list[list[str]]
    n_rows: int
    n_cols: int
    # Appended with defaults so every existing positional construction (and the
    # profile cache) still loads. `fetch_workbook` caps each tab at `max_rows`,
    # and a capped tab that says nothing about it is a tab whose totals are
    # quietly short — the Ask engine refuses to total a truncated tab, which it
    # can only do if the cut is carried here instead of being invisible.
    truncated: bool = False
    source_rows: int | None = None


def fetch_workbook(spreadsheet_id: str, *, service=None, max_rows: int = 200) -> list[TabGrid]:
    """Every tab as a (capped) grid, read in two Sheets API calls total.

    A tab longer than ``max_rows`` comes back with ``truncated=True`` and the
    full row count in ``source_rows``. The cap is a token budget, not a claim
    about the sheet."""
    svc = service or _sheets_service()
    tabs = list_tabs(spreadsheet_id, service=svc)
    titles = [t["title"] for t in tabs]
    values = fetch_all_tab_values(spreadsheet_id, titles, service=svc)
    grids: list[TabGrid] = []
    for t in tabs:
        full = values.get(t["title"], [])
        rows = full[:max_rows]
        grids.append(TabGrid(
            title=t["title"],
            gid=t["gid"],
            hidden=t.get("hidden", False),
            rows=rows,
            n_rows=len(rows),
            n_cols=max((len(r) for r in rows), default=0),
            truncated=len(full) > len(rows),
            source_rows=len(full),
        ))
    return grids


def truncation_map(grids: list[TabGrid]) -> dict[str, int]:
    """``{tab title: rows dropped}`` for every tab the read cut short.

    This is what the Ask engine is handed so a truncated tab can be quoted but
    never totalled, and so the omission reaches the answer payload instead of
    being discovered as a wrong number."""
    out: dict[str, int] = {}
    for g in grids:
        if not getattr(g, "truncated", False):
            continue
        # A grid flagged truncated but carrying no `source_rows` (an older
        # cache, or a caller that set the flag by hand) still had rows dropped -
        # we just do not know how many. Leaving it out of this map made it look
        # complete, and a short tab that looks complete gets TOTALLED. One is
        # the honest floor: at least one row is missing.
        known = (g.source_rows - g.n_rows) if g.source_rows is not None else 0
        out[g.title] = max(known, 1)
    return out


def compact_grid(rows: list[list[str]], max_rows: int = 12, max_cols: int = 16) -> list[list[str]]:
    """A small, token-bounded view of a grid — enough to classify or cite."""
    out = []
    for r in rows[:max_rows]:
        out.append([(c or "")[:40] for c in r[:max_cols]])
    return out


def grid_signature(grids: list[TabGrid]) -> str:
    """Stable hash of the workbook's shape + headers, used to cache profiles.
    Changes when tabs/columns change (re-profile) but not on every value tweak."""
    h = hashlib.sha256()
    for g in sorted(grids, key=lambda x: x.title):
        header = "|".join((g.rows[0] if g.rows else [])[:20])
        h.update(f"{g.title}~{g.n_rows}x{g.n_cols}~{header}\n".encode("utf-8"))
    return h.hexdigest()[:16]
