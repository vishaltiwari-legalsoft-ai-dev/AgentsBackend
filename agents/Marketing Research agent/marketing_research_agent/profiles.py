"""Tab profiling — the agent's understanding of every tab.

Each tab is classified (kind, time granularity, date range, platforms, metrics,
one-line summary) so the insight engine can pick the right tab for a question /
timeframe. Online this uses the LLM; offline a deterministic heuristic keeps it
working. Profiles are cached against a workbook signature so re-profiling only
happens when the workbook's shape changes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import analysis
from .workbook import TabGrid, compact_grid, grid_signature

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
_PLATFORMS = ["google", "meta", "facebook", "instagram", "email", "website",
              "organic", "linkedin", "youtube", "podcast"]
_METRIC_WORDS = ["spend", "budget", "lead", "qualified", "demo", "booked",
                 "completed", "cac", "revenue", "roas", "conversion", "show",
                 "cost per", "ratio", "no show", "cancel"]


@dataclass
class TabProfile:
    title: str
    gid: int
    kind: str                 # performance_tracker | leads_by_period | lead_level | vendor_spend | contacts | pivot | raw_data | looker_link | control | other
    granularity: str          # daily | weekly | monthly | none
    date_range: str | None
    platforms: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    summary: str = ""
    useful: bool = True
    hidden: bool = False


def _scan(grid: TabGrid, n_rows: int = 6) -> str:
    cells = []
    for r in grid.rows[:n_rows]:
        cells.extend(c for c in r)
    return " \n ".join(cells).lower()


def _months_present(text: str) -> list[int]:
    found = {m for w, m in _MONTHS.items() if re.search(rf"\b{w}\b", text)}
    return sorted(found)


def _heuristic_profile(grid: TabGrid, year: int) -> TabProfile:
    t = grid.title.lower()
    head = _scan(grid)
    months = _months_present(head)

    if "looker" in t:
        kind = "looker_link"
    elif "control" in t or "dropdown" in t:
        kind = "control"
    elif "pivot" in t:
        kind = "pivot"
    elif "raw" in t:
        kind = "raw_data"
    elif "vendor" in t and ("spend" in t or "budget" in t):
        kind = "vendor_spend"
    elif "hubspot" in t or "demo" in t:
        kind = "lead_level"
    elif "contact" in t or "submission" in t:
        kind = "contacts"
    elif "month to month" in t or ("leads" in t and "tracker" in t):
        kind = "leads_by_period"
    elif "overall" in t or "report" in t or "tracker" in t:
        kind = "performance_tracker"
    elif grid.n_rows > 1:
        kind = "other"
    else:
        kind = "other"

    has_perf_inv = "(performance)" in head or "(investment)" in head
    if has_perf_inv or len(months) >= 2:
        granularity = "monthly"
    elif "week" in head or "week" in t:
        granularity = "weekly"
    elif re.search(r"\b20\d\d-\d\d-\d\d\b", head) or "date" in head:
        granularity = "daily"
    else:
        granularity = "none"

    date_range = None
    if months:
        date_range = f"{_MONTH_NAMES[months[0]]}–{_MONTH_NAMES[months[-1]]} {year}" if len(months) > 1 else f"{_MONTH_NAMES[months[0]]} {year}"

    platforms = sorted({p.capitalize() for p in _PLATFORMS if re.search(rf"\b{p}\b", head)})
    metrics = []
    for r in grid.rows[:60]:
        lbl = (r[0] if r else "").strip().lower()
        for w in _METRIC_WORDS:
            if w in lbl and lbl and lbl not in metrics:
                metrics.append((r[0] or "").strip())
                break
        if len(metrics) >= 8:
            break

    useful = kind not in ("looker_link", "control") and grid.n_rows > 1
    summary = f"{kind.replace('_', ' ').title()}"
    if granularity != "none":
        summary += f", {granularity}"
    if date_range:
        summary += f" ({date_range})"
    if platforms:
        summary += f" — {', '.join(platforms)}"

    return TabProfile(
        title=grid.title, gid=grid.gid, kind=kind, granularity=granularity,
        date_range=date_range, platforms=platforms, metrics=metrics[:8],
        summary=summary, useful=useful, hidden=grid.hidden,
    )


_PROFILE_PROMPT = """You classify one tab of a marketing spreadsheet. Given the tab title and its
top rows, reply with ONLY a JSON object:
{{"kind": one of [performance_tracker, leads_by_period, lead_level, vendor_spend, contacts, pivot, raw_data, looker_link, control, other],
 "granularity": one of [daily, weekly, monthly, none],
 "date_range": short string or null,
 "platforms": [channel names present],
 "metrics": [up to 8 key metric/column names],
 "summary": one concise sentence describing what this tab is and what it is good for,
 "useful": true if it holds analyzable marketing data, false for control/looker/empty tabs}}

Title: {title}
Dimensions: {rows} rows x {cols} cols
Top rows:
{grid}
"""


def _llm_profile(grid: TabGrid, year: int) -> TabProfile:
    base = _heuristic_profile(grid, year)
    payload = analysis.llm_json(_PROFILE_PROMPT.format(
        title=grid.title, rows=grid.n_rows, cols=grid.n_cols,
        grid=json.dumps(compact_grid(grid.rows), default=str),
    ))
    if not isinstance(payload, dict):
        return base
    return TabProfile(
        title=grid.title, gid=grid.gid,
        kind=str(payload.get("kind") or base.kind),
        granularity=str(payload.get("granularity") or base.granularity),
        date_range=payload.get("date_range") or base.date_range,
        platforms=payload.get("platforms") or base.platforms,
        metrics=(payload.get("metrics") or base.metrics)[:8],
        summary=str(payload.get("summary") or base.summary),
        useful=bool(payload.get("useful", base.useful)),
        hidden=grid.hidden,
    )


# --- caching ---------------------------------------------------------------

def _cache_path() -> Path:
    root = Path(os.environ.get("MR_RUNS_DIR") or (Path(__file__).resolve().parents[1] / "runs"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "workbook_profiles.json"


def load_cached(signature: str) -> list[TabProfile] | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("signature") != signature:
        return None
    return [TabProfile(**d) for d in data.get("profiles", [])]


def save_cache(signature: str, profiles: list[TabProfile]) -> None:
    _cache_path().write_text(
        json.dumps({"signature": signature, "profiles": [asdict(p) for p in profiles]}, indent=2),
        encoding="utf-8",
    )


def profile_workbook(grids: list[TabGrid], *, year: int, use_cache: bool = True, deep: bool = True) -> list[TabProfile]:
    """Profile every tab.

    ``deep`` uses the LLM (refined, cached by workbook signature); otherwise a
    fast heuristic pass (no LLM, not cached) for snappy catalog display. A cached
    deep profile is always preferred when available."""
    sig = grid_signature(grids)
    if use_cache:
        cached = load_cached(sig)
        if cached is not None:
            return cached
    if not deep:
        return [_heuristic_profile(g, year) for g in grids]
    profiles = [_llm_profile(g, year) for g in grids]
    save_cache(sig, profiles)
    return profiles
