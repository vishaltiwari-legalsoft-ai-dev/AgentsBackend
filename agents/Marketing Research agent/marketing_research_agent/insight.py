"""Insight / Ask engine.

Given a natural-language question (and optional timeframe), it: (1) selects the
most relevant tab(s) from the profiles, (2) pulls those grids, (3) produces a
grounded, plain-language insight with the real numbers and a recommendation.
Online uses the LLM for selection + analysis; offline falls back to deterministic
selection and a factual read so it always returns something useful.
"""

from __future__ import annotations

import json
import re

from . import analysis
from .profiles import TabProfile
from .sources.sheets_source import parse_tracker
from .workbook import compact_grid

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
               "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_CANON = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]

_STOP = {"the", "and", "for", "with", "from", "this", "that", "what", "which",
         "how", "much", "many", "are", "was", "were", "did", "does", "our",
         "give", "show", "tell", "me", "of", "in", "on", "to", "is", "it"}

_GRAN_WORDS = {
    "today": "daily", "daily": "daily", "day": "daily",
    "week": "weekly", "weekly": "weekly",
    "month": "monthly", "monthly": "monthly", "mtd": "monthly",
    "quarter": "monthly", "year": "monthly", "ytd": "monthly",
}


def infer_timeframe(question: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    q = question.lower()
    for w, g in _GRAN_WORDS.items():
        if w in q:
            return g
    return None


def target_month(question: str) -> tuple[str, int] | None:
    """Extract a specific month from the question, e.g. -> ("June", 6)."""
    q = question.lower()
    for name, num in _MONTH_NUM.items():
        if re.search(rf"\b{name}\b", q):
            return _MONTH_CANON[num], num
    for abbr, num in _MONTH_ABBR.items():
        if re.search(rf"\b{abbr}\b", q):
            return _MONTH_CANON[num], num
    return None


def _any_month(cell: str) -> bool:
    c = (cell or "").lower()
    return any(re.search(rf"\b{m}\b", c) for m in _MONTH_NUM)


def _date_month(cell: str) -> int | None:
    """Month number if the cell parses as a date (YYYY-MM-DD or M/D/YYYY)."""
    s = (cell or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-\d{1,2}", s)
    if m:
        return int(m.group(2))
    m = re.match(r"^(\d{1,2})[/-]\d{1,2}[/-]\d{2,4}", s)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 12 else None
    return None


def _time_columns(rows: list[list[str]], sample: int = 30) -> list[int]:
    """Columns that carry a month/date (by header keyword or by their values)."""
    header = rows[0] if rows else []
    cols: list[int] = []
    for i, h in enumerate(header):
        hl = (h or "").lower()
        if any(k in hl for k in ("month", "date", "day", "week")):
            cols.append(i)
    if cols:
        return cols
    width = max((len(r) for r in rows[:sample]), default=0)
    for i in range(width):
        hits = 0
        for r in rows[1:sample]:
            v = r[i] if i < len(r) else ""
            if _any_month(v) or _date_month(v):
                hits += 1
        if hits >= 3:
            cols.append(i)
    return cols


def slice_for_timeframe(rows: list[list[str]], month: tuple[str, int] | None,
                        max_rows: int = 140, max_cols: int = 26) -> list[list[str]]:
    """Return the slice of a tab relevant to the requested month.

    Wide tabs (months across columns) -> the label column plus the month's
    columns, all rows. Long tabs (a row per record with a month/date column) ->
    the header plus only the rows matching that month. Falls back to a larger
    top window when there is no month in the question or no match."""
    if not rows:
        return rows
    header = rows[0]

    if month:
        name_lc = month[0].lower()
        # WIDE: the header spreads months across columns.
        month_header_cols = [i for i, c in enumerate(header) if name_lc[:3] in (c or "").lower()]
        if month_header_cols and sum(1 for c in header if _any_month(c)) >= 2:
            keep = [0] + month_header_cols
            return [[(r[i] if i < len(r) else "") for i in keep] for r in rows[:max_rows]]

        # LONG: filter rows whose time column(s) match the month.
        tcols = _time_columns(rows)
        if tcols:
            matched = [header[:max_cols]]
            for r in rows[1:]:
                hit = False
                for i in tcols:
                    v = r[i] if i < len(r) else ""
                    if name_lc[:3] in (v or "").lower() or _date_month(v) == month[1]:
                        hit = True
                        break
                if hit:
                    matched.append(r[:max_cols])
                if len(matched) >= max_rows:
                    break
            if len(matched) > 1:
                return matched

    # Fallback: a larger top window than the tiny preview.
    return [r[:max_cols] for r in rows[:max_rows]]


def _q_words(question: str) -> list[str]:
    return [w for w in "".join(c if c.isalnum() else " " for c in question.lower()).split()
            if len(w) > 2 and w not in _STOP]


def _score(p: TabProfile, words: list[str], want_gran: str | None) -> float:
    if not p.useful:
        return -1.0
    text = " ".join([p.title, p.summary, " ".join(p.metrics), " ".join(p.platforms), p.kind]).lower()
    s = sum(2 for w in words if w in text)
    if want_gran and p.granularity == want_gran:
        s += 3
    if p.kind in ("performance_tracker", "leads_by_period", "lead_level"):
        s += 1
    if p.date_range:
        s += 0.5
    return s


def select_tabs(question: str, timeframe: str | None, profiles: list[TabProfile], *, max_tabs: int = 3) -> list[str]:
    """Pick the tab titles most relevant to the question. LLM when available."""
    want = infer_timeframe(question, timeframe)
    payload = analysis.llm_json(_SELECT_PROMPT.format(
        question=question,
        timeframe=want or "unspecified",
        profiles=json.dumps([
            {"title": p.title, "kind": p.kind, "granularity": p.granularity,
             "date_range": p.date_range, "summary": p.summary}
            for p in profiles if p.useful
        ], default=str),
    ))
    if isinstance(payload, dict) and isinstance(payload.get("tabs"), list):
        valid = {p.title for p in profiles}
        picked = [t for t in payload["tabs"] if t in valid][:max_tabs]
        if picked:
            return picked
    # Heuristic fallback.
    words = _q_words(question)
    ranked = sorted(profiles, key=lambda p: _score(p, words, want), reverse=True)
    picked = [p.title for p in ranked if _score(p, words, want) > 0][:max_tabs]
    if picked:
        return picked
    return [p.title for p in profiles if p.kind == "performance_tracker"][:1]


_SELECT_PROMPT = """You route a marketing question to the right spreadsheet tab(s). Given the
question, the wanted timeframe, and the available tab profiles, reply with ONLY
JSON: {{"tabs": [titles, most relevant first], "reason": "short"}}. Pick at most
3 tabs whose data can actually answer the question for that timeframe.

Question: {question}
Timeframe: {timeframe}
Tabs:
{profiles}
"""

_ANSWER_PROMPT = """You are Legal Soft's marketing analyst answering a busy marketing manager. Be
brief, decisive, and useful. Use ONLY the data provided (already filtered to the
requested timeframe where possible).

Shape your answer EXACTLY like this — it is rendered as discrete blocks, so a
single dense paragraph is unreadable:

  <one sentence: the direct answer — a number or a clear verdict>
  - <finding, with its figure>
  - <finding, with its figure>
  - <finding, with its figure>
  Recommend: <the single action worth taking>

Rules:
- The first line stands alone: the answer itself, no preamble. Max ~25 words.
- Then 2-5 bullet lines, each starting "- ". ONE point per bullet, each carrying
  the number or name that proves it. Never stack three findings into one bullet.
- Plain language. No headings, no bold, no asterisks — bullets and the lines
  above are the only structure.
- Summarize: give the totals and name only the top one or two and the worst one
  or two (e.g. who is over budget / burning money). Do NOT list every row.
- Be concrete: back every point with a specific number or name from the data —
  no vague generalities. When asked "how many", give the count.
- Never invent numbers.
- ALWAYS deliver your best read. If something is ambiguous or partly missing, make
  a reasonable assumption, note it in one short clause, and still give the answer.
  Do NOT say the data is confusing and do NOT refuse.
- Only if it is genuinely impossible to answer from the data, say so in one line
  and add: "Worth a quick sync with the marketing team to confirm."
- End with one line starting "Recommend:".

Question: {question}
Timeframe: {timeframe}
Rows provided per tab: {counts}
Data by tab:
{data}
"""


def _offline_answer(question: str, timeframe: str | None, selected: list[str],
                    grids: dict[str, list[list[str]]], profiles_by_title: dict[str, TabProfile],
                    year: int) -> str:
    # Same shape the LLM is asked for (lead line / "- " bullets / Recommend), so
    # the answer card renders offline reads as structured blocks too.
    bullets: list[str] = []
    for title in selected:
        prof = profiles_by_title.get(title)
        rows = grids.get(title, [])
        if prof and prof.kind == "performance_tracker":
            metrics, _ = parse_tracker(rows, year)
            from collections import defaultdict
            agg = defaultdict(lambda: [0.0, 0])
            for m in metrics:
                agg[m.channel][0] += m.spend
                agg[m.channel][1] += m.demos_completed
            if agg:
                bullets.extend(
                    f"- {ch}: ${v[0]:,.0f} spent for {v[1]} completed demos ({title})"
                    for ch, v in agg.items())
            else:
                bullets.append(f"- {title}: {prof.summary}")
        elif prof:
            cols = ", ".join(c for c in (rows[0] if rows else [])[:6] if c)
            bullets.append(f"- {title} ({prof.summary}): {len(rows)} rows; columns include {cols}")
        else:
            bullets.append(f"- {title}: {len(rows)} rows")
    scope = f" for {timeframe}" if timeframe and timeframe != "unspecified" else ""
    lead = (f"Read straight from {len(selected)} tab{'s' if len(selected) != 1 else ''}{scope} "
            f"— exact figures below.") if selected else "No usable tab matched that question."
    return "\n".join([lead, *bullets,
                      "Recommend: connect the live LLM for a deeper read; the figures above are exact."])


def answer(question: str, profiles: list[TabProfile], grids: dict[str, list[list[str]]],
           *, timeframe: str | None = None, year: int = 2026) -> dict:
    """Produce a grounded insight answer to a question."""
    want = infer_timeframe(question, timeframe)
    month = target_month(question)
    selected = select_tabs(question, want, profiles)
    by_title = {p.title: p for p in profiles}

    # Send the month-relevant slice of each tab (filtered rows / month columns),
    # not just the top corner — so "June vendor report" actually sees June.
    data = {t: slice_for_timeframe(grids.get(t, []), month) for t in selected}
    counts = {t: max(len(rows) - 1, 0) for t, rows in data.items()}
    timeframe_label = month[0] if month else (want or "unspecified")

    text = analysis.llm_text(_ANSWER_PROMPT.format(
        question=question, timeframe=timeframe_label,
        counts=json.dumps(counts),
        data=json.dumps(data, default=str)[:14000],
    ))
    if not text:
        text = _offline_answer(question, timeframe_label, selected, data, by_title, year)
    return {
        "question": question,
        "timeframe": month[0] if month else want,
        "answer": text.strip(),
        "used_tabs": selected,
    }
