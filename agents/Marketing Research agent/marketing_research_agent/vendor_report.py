"""Vendor performance report — the numbers, computed from one vendor sweep.

Two layers, kept apart on purpose:

* :func:`compute` is **pure**. It takes a sweep (the newest daily capture of
  every vendor tab in a month), the resolved targets, and optionally the
  previous month's sweep and the roll-up tab's capture, and returns one plain,
  JSON-serialisable dict, the report. No I/O, no clock, no model. Every figure
  is arithmetic over the sweep. Every sentence is a fixed template filled with
  those figures, so the same sweep always produces the same report, byte for byte.
* :func:`build` is the I/O seam: read the sweep(s) and targets, call
  :func:`compute`, persist the result as an ``mr_runs`` run (idempotent on its
  inputs). The routes call this, and nothing else in the report does I/O.

The renderer (:mod:`.vendor_report_render`) reads only the dict, so a stored run
re-renders without re-deriving anything.

Rules a reader of the output can rely on:

1. **Absent is not zero.** A row a vendor tab does not report is ``None`` here,
   ``—`` on the page, and named in ``missing``. A portfolio total is published
   only when every vendor reports the field.
2. **Ratios are recomputed from summed components**, never averaged, and a
   zero denominator gives ``None`` with its reason, never a 0% or $0.
3. **One denominator per word.** "Qualified demos booked" is the show-rate
   denominator everywhere; "demos booked (all)" is a separate count. The sample
   template printed one column labelled "Demos booked" totalling 16 beside a
   show rate dividing by 14.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import config, goals

#: Bumped whenever the same inputs would produce different numbers, rows or
#: sentences. Part of the idempotency key, so a bump re-derives.
GENERATOR_VERSION = "mr-vendor-report/1"

KIND = "vendor_report"

MONEY, PCT, INT = "money", "pct", "int"

#: The only template Phase 1 has. A request names it explicitly to force it
#: (Phase 2 adds saved template versions; a build never falls back silently).
BUILTIN_TEMPLATE = {"kind": "builtin"}

# --- metric catalog ------------------------------------------------------------
# One vocabulary for the JSON, the renderer's tiles and the template placeholders
# (``{{total_spend}}``). Labels live here and nowhere else.

METRICS: dict[str, tuple[str, str]] = {
    "total_budget": ("Total Budget", MONEY),
    "total_spend": ("Total Spend", MONEY),
    "budget_utilized_pct": ("Budget Utilized", PCT),
    "total_leads": ("Total Leads", INT),
    "qualified_leads": ("Qualified Leads", INT),
    "ql_ratio_pct": ("QL Ratio", PCT),
    "demos_booked": ("Demos Booked (all)", INT),
    "qual_demos_booked": ("Qual. Demos Booked", INT),
    "demos_completed": ("Demos Completed", INT),
    "show_rate_pct": ("Show Rate", PCT),
    "dnc_bad_leads": ("DNC Bad Leads", INT),
    "cost_per_lead": ("Cost / Lead", MONEY),
    "cost_per_qualified_lead": ("Cost / Qual. Lead", MONEY),
    "cost_per_qual_demo_booked": ("Cost / Qual. Demo Booked", MONEY),
    "cost_per_demo_completed": ("Cost / Demo Completed", MONEY),
    "projected_revenue": ("Projected Revenue", MONEY),
}

#: Additive portfolio fields: report key -> vendor-row field.
_ADDITIVE = {
    "total_budget": "budget",
    "total_spend": "spend",
    "total_leads": "leads",
    "qualified_leads": "qualified_leads",
    "demos_booked": "demos_booked",
    "qual_demos_booked": "qual_demos_booked",
    "demos_completed": "demos_completed",
    "dnc_bad_leads": "dnc_bad_leads",
    "projected_revenue": "projected_revenue",
}

#: Ratios: key -> (numerator, denominator, scale, reason when the denominator is 0).
_RATIOS = {
    "budget_utilized_pct": ("spend", "budget", 100.0, "no budget set ($0)"),
    "ql_ratio_pct": ("qualified_leads", "leads", 100.0, "no leads yet"),
    "show_rate_pct": ("demos_completed", "qual_demos_booked", 100.0,
                      "no qualified demos booked yet"),
    "cost_per_lead": ("spend", "leads", 1.0, "no leads yet"),
    "cost_per_qualified_lead": ("spend", "qualified_leads", 1.0, "no qualified leads yet"),
    "cost_per_qual_demo_booked": ("spend", "qual_demos_booked", 1.0,
                                  "no qualified demos booked yet"),
    "cost_per_demo_completed": ("spend", "demos_completed", 1.0, "no demos completed yet"),
}

#: The ratios the per-vendor scorecard prints (the cost-per ratios are
#: portfolio-only — on a vendor with one lead they are noise).
_VENDOR_RATIOS = ("budget_utilized_pct", "ql_ratio_pct", "show_rate_pct")

#: Vendor-row field -> (dot path into ``canonical.team_overall``, mode).
#: ``pair`` = the {performance, investment} block, read on the Performance basis.
_FIELDS: dict[str, tuple[str, str]] = {
    "budget": ("budget", "pair"),
    "spend": ("spend", "pair"),
    "leads": ("leads.total", "value"),
    "qualified_leads": ("leads.qualified", "value"),
    "dnc_bad_leads": ("leads.lost_dnc_bad_lead", "value"),
    "demos_booked": ("demos.total_booked_all", "value"),
    "qual_demos_booked": ("demos.qualified_booked_all", "value"),
    "demos_completed": ("demos.completed_all", "value"),
    # Verified 2026-10-08 against the stored 2026-09-02 vendor docs: every vendor
    # tab carries the row "Projected Total Amount Sold ($) Actualized" (HawkSEM
    # LS Meta = 3,671), which is exactly the label snapshots._TEAM_MAP maps here.
    # A tab without the row still reads None, i.e. absent, never $0.
    "projected_revenue": ("projected_revenue.total_amount_sold_actualized", "value"),
}

#: Labels for the vendor-row fields, used in ``missing``.
_FIELD_LABELS = {
    "budget": "Budget", "spend": "Spend", "leads": "Leads",
    "qualified_leads": "Qualified Leads", "dnc_bad_leads": "DNC Bad Leads",
    "demos_booked": "Demos Booked (all)", "qual_demos_booked": "Qual. Demos Booked",
    "demos_completed": "Demos Completed", "projected_revenue": "Projected Revenue",
}

#: Raw counts never charted against a benchmark (there is no fixed % for a count).
COUNT_KEYS = ("total_leads", "qualified_leads", "qual_demos_booked", "demos_completed")

# --- channels ------------------------------------------------------------------

#: Tab-title keyword -> channel. Matched on whole words, first match in title
#: order wins. A title matching none is "Other" and is listed by vendor name.
CHANNEL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Meta", ("meta", "facebook", "instagram")),
    ("Google", ("google",)),
    ("Email", ("email",)),
    ("Microsoft", ("microsoft", "bing")),
    ("LinkedIn", ("linkedin",)),
    ("Twitter", ("twitter",)),
)
#: The channels charted by name; every other channel folds into "Other".
CHARTED_CHANNELS = ("Meta", "Google", "Email")
OTHER = "Other"
#: Report channel -> goals.CHANNEL_GOALS key. A channel without one has no
#: benchmark set of its own, so the Total set applies.
_GOAL_SET = {"Meta": "META", "Google": "Google", "Email": "Email"}
TOTAL_SET = "Total"


def channel_of(title: str) -> tuple[str, str | None]:
    """``(channel, matched keyword)`` for one tab title; ``("Other", None)`` when
    no keyword matches."""
    lookup = {kw: ch for ch, kws in CHANNEL_KEYWORDS for kw in kws}
    for token in re.findall(r"[a-z0-9]+", (title or "").lower()):
        if token in lookup:
            return lookup[token], token
    return OTHER, None


def _bucket(channel: str) -> str:
    return channel if channel in CHARTED_CHANNELS else OTHER


# --- small helpers ---------------------------------------------------------------

def _get(node: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _div(n: float | None, d: float | None, scale: float = 1.0) -> float | None:
    if n is None or d is None or d == 0:
        return None
    return round(n / d * scale, 2)


def money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


def count(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def fmt(v: float | None, kind: str) -> str:
    return money(v) if kind == MONEY else pct(v) if kind == PCT else count(v)


def _plural(n: float, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _and(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _seg(*parts: Any) -> dict:
    """A sentence as segments: ``str`` is plain text, ``(text,)`` is emphasised.
    ``text`` is the flat sentence, for a client that does not style segments."""
    segs = []
    for p in parts:
        if not p:
            continue
        if isinstance(p, tuple):
            segs.append({"t": str(p[0]), "em": True})
        else:
            segs.append({"t": str(p), "em": False})
    return {"text": "".join(s["t"] for s in segs), "segments": segs}


def _month_name(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{calendar.month_name[m]} {y}"


def _short_date(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{calendar.month_abbr[d.month]} {d.day}"


def _is_non_media(doc: Mapping) -> bool:
    return (doc.get("vendor_slug") or "") in config.NON_MEDIA_VENDOR_SLUGS


def _is_rollup(doc: Mapping) -> bool:
    return "overall" in (doc.get("vendor_slug") or "")


#: ``vendor_sweep``'s exclusion reasons (plus this report's own), in words.
_EXCLUSION_TEXT = {
    "hidden": "hidden tab in the workbook",
    "stale_capture": "written by an earlier capture run that day (tab since renamed or "
                     "removed), not by the final run",
    "non_media": "non-media tab (organic/website) — kept out of paid vendor figures",
}


def _sweep_rollup(sweep: Mapping) -> dict | None:
    """The roll-up tab's doc from the same sweep (``vendor_sweep``'s ``rollup``
    key), or ``None``. ``docs`` is vendor tabs only by contract; the "overall"
    guard in :func:`compute` still keeps a stray roll-up from being counted."""
    r = sweep.get("rollup")
    return r if isinstance(r, dict) else None


def _match_key(name: str) -> str:
    """A tab title with the workbook's housekeeping removed: "(New)" and a
    leading "Copy of" mark the same vendor's tab, not a different vendor."""
    t = (name or "").lower()
    t = re.sub(r"\(new\)", " ", t)
    t = re.sub(r"^\s*copy of\s+", " ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))


#: Brand markers in tab titles. A title that differs from last month's ONLY by
#: adding or dropping one of these ("EHackers Email" → "EHackers LS Email") is a
#: rename of the same vendor's tab — never a swap of one for the other.
_BRAND_MARKERS = frozenset({"ls", "ra"})


def _pair_across_months(rows: Sequence[dict], prev_rows: Sequence[dict]) -> dict[int, dict]:
    """``{index into rows: previous row}`` — which tab last month is the same
    vendor as each tab this month, decided in three passes, strongest first:

    1. **gid** — the same tab, whatever it is called now (a true rename);
    2. **title** — ignoring "(New)"/"Copy of". This is the common case: the team
       re-creates most tabs each month (on 2026-09-02 only 1 of 18 kept its
       August gid), so gid alone would call 17 vendors new and 17 gone;
    3. **brand marker** — among what is left, a title that only adds or drops
       one LS/RA token, and only when exactly one tab on each side fits.
    Anything still unpaired is a genuinely new or removed tab."""
    pairs: dict[int, dict] = {}
    used: set[int] = set()
    by_gid: dict[Any, int] = {}
    for j, p in enumerate(prev_rows):
        if p.get("gid") is not None:
            by_gid.setdefault(p["gid"], j)
    for i, r in enumerate(rows):
        j = by_gid.get(r.get("gid")) if r.get("gid") is not None else None
        if j is not None and j not in used:
            pairs[i] = prev_rows[j]
            used.add(j)
    by_key: dict[str, list[int]] = {}
    for j, p in enumerate(prev_rows):
        if j not in used:
            by_key.setdefault(_match_key(p["name"]), []).append(j)
    for i, r in enumerate(rows):
        if i in pairs:
            continue
        free = [j for j in by_key.get(_match_key(r["name"]), []) if j not in used]
        if free:
            pairs[i] = prev_rows[free[0]]
            used.add(free[0])

    def marker_rename(a: str, b: str) -> bool:
        ta, tb = set(_match_key(a).split()), set(_match_key(b).split())
        diff = ta ^ tb
        return len(diff) == 1 and diff <= _BRAND_MARKERS and (ta < tb or tb < ta)

    left_cur = [i for i in range(len(rows)) if i not in pairs]
    left_prev = [j for j in range(len(prev_rows)) if j not in used]
    for i in left_cur:
        cands = [j for j in left_prev if marker_rename(rows[i]["name"], prev_rows[j]["name"])]
        if len(cands) == 1:
            back = [k for k in left_cur
                    if marker_rename(rows[k]["name"], prev_rows[cands[0]]["name"])]
            if back == [i]:
                pairs[i] = prev_rows[cands[0]]
                used.add(cands[0])
                left_prev.remove(cands[0])
    return pairs


# --- vendor rows -----------------------------------------------------------------

def _vendor_row(doc: Mapping, notes: list[dict]) -> dict:
    team = (doc.get("canonical") or {}).get("team_overall") or {}
    # The sheet's own tab title, verbatim (snapshots.capture_tab stores it). No
    # case changes and no slug fallback: a name the sheet does not have is not
    # printed as if it did.
    title = doc.get("vendor")
    name = title if isinstance(title, str) and title.strip() else None
    if name is None:
        name = f"Untitled tab (gid {doc.get('gid')})"
        notes.append({"vendor": name, "field": "name",
                      "note": "the capture stored no tab title for this tab"})
    row: dict[str, Any] = {
        "name": name, "slug": doc.get("vendor_slug"), "gid": doc.get("gid"),
        "captured_at": doc.get("captured_at"),
    }
    for field, (path, mode) in _FIELDS.items():
        raw = _get(team, path)
        if mode == "pair":
            perf = _num((raw or {}).get("performance")) if isinstance(raw, dict) else None
            inv = _num((raw or {}).get("investment")) if isinstance(raw, dict) else None
            if perf is None and inv is not None:
                notes.append({"vendor": name, "field": field,
                              "note": f"{_FIELD_LABELS[field]} read from the Investment "
                                      "column — the Performance cell is blank"})
                perf = inv
            row[field] = perf
        else:
            row[field] = _num(raw)
    channel, keyword = channel_of(name)
    row["channel"] = channel
    row["channel_keyword"] = keyword
    for key in _VENDOR_RATIOS:
        n, d, scale, _reason = _RATIOS[key]
        row[key] = _div(row[n], row[d], scale)
    return row


def _sum(rows: Sequence[dict], field: str) -> tuple[float | None, list[str]]:
    """The field summed over every vendor — or ``None`` plus the vendors that
    did not report it. A partial sum is a wrong number nobody can see."""
    absent = [r["name"] for r in rows if r.get(field) is None]
    if absent or not rows:
        return None, absent
    return round(sum(r[field] for r in rows), 2), []


# --- targets ---------------------------------------------------------------------

def _channel_mix(rows: Sequence[dict], targets: dict) -> dict:
    """Which benchmark set applies, and why (owner decision: ≥ the threshold of
    paid spend in one channel → that channel's set, else Total)."""
    thr = goals.thresholds(targets)["vendor_channel_mix_pct"]
    spend_by: dict[str, float] = {}
    for r in rows:
        if r["spend"]:
            spend_by[r["channel"]] = spend_by.get(r["channel"], 0.0) + r["spend"]
    total = sum(spend_by.values())
    shares = {ch: round(v / total * 100, 2) for ch, v in spend_by.items()} if total else {}
    dominant = max(shares, key=lambda c: (shares[c], c)) if shares else None
    share = shares.get(dominant) if dominant else None
    if not total:
        applied, reason = TOTAL_SET, "no paid spend is recorded yet, so the Total benchmarks apply"
    elif share is not None and share >= thr and dominant in _GOAL_SET:
        applied = _GOAL_SET[dominant]
        reason = (f"portfolio is {share:.0f}% {dominant}-channel spend this month, at or "
                  f"above the {thr:g}% rule, so {dominant}-specific benchmarks apply")
    elif share is not None and share >= thr:
        applied = TOTAL_SET
        reason = (f"{dominant} holds {share:.0f}% of paid spend but has no benchmark set "
                  "of its own, so the Total benchmarks apply")
    else:
        applied = TOTAL_SET
        reason = (f"no channel holds {thr:g}% of paid spend (largest: {dominant} at "
                  f"{share:.0f}%), so the Total benchmarks apply")
    return {"applied_set": applied,
            "applied_label": "Meta" if applied == "META" else applied,
            "dominant_channel": dominant, "dominant_share_pct": share,
            "threshold_pct": thr, "shares_pct": dict(sorted(shares.items())),
            "reason": reason}


def _show_target_for(channel: str, targets: dict) -> float | None:
    goal = goals.channel_goal(_GOAL_SET.get(channel, TOTAL_SET), targets)
    return round(goal.completed_demo_pct * 100, 2) if goal else None


# --- status pills ----------------------------------------------------------------

STRONG_START, TOO_EARLY, CHECK_IN = "Strong Start", "Too Early", "Check In"
STATUSES = (STRONG_START, TOO_EARLY, CHECK_IN)


def _status(row: dict, thr: Mapping[str, float]) -> tuple[str, str]:
    """``(status, rule)`` for one vendor. Check In outranks Strong Start: a
    vendor that needs a question asked is shown as needing it."""
    spend, dnc, leads = row["spend"], row["dnc_bad_leads"], row["leads"]
    if dnc is not None and spend is not None and spend == 0 \
            and dnc >= thr["vendor_check_in_min_dnc_zero_spend"]:
        return CHECK_IN, "dnc_zero_spend"
    if leads and dnc is not None and leads >= thr["vendor_check_in_min_leads"] \
            and dnc / leads * 100 >= thr["bad_lead_rate_red"]:
        return CHECK_IN, "bad_lead_rate"
    if spend is not None and spend >= thr["spend_no_demo_limit"] and row["demos_booked"] == 0:
        return CHECK_IN, "spend_no_demo"
    completed, show, target = row["demos_completed"], row["show_rate_pct"], row["show_rate_target"]
    if completed is not None and completed >= thr["vendor_strong_start_min_completed"] \
            and show is not None and target is not None and show >= target:
        return STRONG_START, "completions"
    if row["qualified_leads"] is not None \
            and row["qualified_leads"] >= thr["vendor_strong_start_min_qualified_leads"]:
        return STRONG_START, "qualified_volume"
    if row["qual_demos_booked"] is not None \
            and row["qual_demos_booked"] >= thr["vendor_watch_min_qual_booked"]:
        return TOO_EARLY, "booked_watch"
    return TOO_EARLY, "default"


def _completed_of(row: dict) -> str:
    """"2 of its 2 qualified demos booked" — completions over the show-rate
    denominator, worded so it cannot be read as a booking count."""
    q = row["qual_demos_booked"]
    return (f"{count(row['demos_completed'])} of its {count(q)} qualified "
            f"{_plural(q or 0, 'demo')} booked")


def _top_ql(rows: Sequence[dict]) -> float | None:
    vals = [r["qualified_leads"] for r in rows if r["qualified_leads"] is not None]
    return max(vals) if vals else None


def _action(row: dict, rows: Sequence[dict], thr: Mapping[str, float]) -> dict:
    rule, v = row["status_rule"], row["name"]
    if rule == "completions":
        rev = row["projected_revenue"]
        tail = (f", and {money(rev)} in projected revenue is already on the books"
                if rev else "")
        return _seg(f"No action needed — it has completed {_completed_of(row)}{tail}. "
                    "Keep tracking.")
    if rule == "qualified_volume":
        top = _top_ql(rows)
        most = (" is the most of any vendor so far" if row["qualified_leads"] == top
                else " already")
        return _seg(f"Monitor the {count(row['qual_demos_booked'])} qualified demos booked — "
                    f"{count(row['qualified_leads'])} qualified leads{most}.")
    if rule == "dnc_zero_spend":
        parts = []
        if row["demos_booked"]:
            n = row["demos_booked"]
            parts.append(f"{count(n)} {_plural(n, 'demo was', 'demos were')} booked")
        n = row["dnc_bad_leads"]
        parts.append(f"{count(n)} DNC bad {_plural(n, 'lead was', 'leads were')} logged")
        return _seg(f"Confirm with the vendor why {_and(parts)} with $0 spend recorded.")
    if rule == "bad_lead_rate":
        rate = row["dnc_bad_leads"] / row["leads"] * 100
        return _seg(f"Ask the vendor about lead quality — {count(row['dnc_bad_leads'])} of "
                    f"{count(row['leads'])} leads ({rate:.1f}%) were DNC bad leads, at or "
                    f"above the {thr['bad_lead_rate_red']:g}% red line.")
    if rule == "spend_no_demo":
        return _seg(f"Ask the vendor why {money(row['spend'])} has been spent with no demo "
                    "booked yet.")
    if rule == "booked_watch":
        n = row["leads"] or 0
        return _seg(f"Monitor the {count(row['qual_demos_booked'])} qualified demos booked as "
                    f"an early read — still just {count(n)} {_plural(n, 'lead')} so far.")
    return _seg("Monitor for first spend and lead flow.")


# --- the report --------------------------------------------------------------------

def _rollup_reconciliation(rows: Sequence[dict], removed: Sequence[dict],
                           rollup: Mapping | None, sweep_date: str) -> dict:
    """The vendor rows' totals against the roll-up tab of the SAME sweep, with
    the non-media tabs taken out of the roll-up first. Printed, not asserted:
    the roll-up aggregates sources no vendor tab carries, so a difference is
    information, not an error."""
    if rollup is None:
        return {"available": False,
                "reason": "The roll-up tab wasn't captured in this pull, so vendor totals "
                          "aren't compared against it."}
    if (rollup.get("date") or "") != sweep_date:
        return {"available": False,
                "reason": f"The roll-up tab in this pull is dated {rollup.get('date')}, not "
                          f"{sweep_date}, so vendor totals aren't compared against it."}
    r_row = _vendor_row(rollup, [])
    removed_rows = [_vendor_row(d, []) for d in removed]
    out_rows = []
    for key in ("total_budget", "total_spend", "total_leads", "qualified_leads",
                "qual_demos_booked", "demos_booked", "demos_completed", "dnc_bad_leads"):
        field = _ADDITIVE[key]
        vendor_sum, _absent = _sum(rows, field)
        roll = r_row.get(field)
        less = None
        if roll is not None and all(x.get(field) is not None for x in removed_rows):
            less = round(roll - sum(x[field] for x in removed_rows), 2)
        diff = None if (less is None or vendor_sum is None) else round(less - vendor_sum, 2)
        out_rows.append({"key": key, "label": METRICS[key][0], "format": METRICS[key][1],
                         "vendor_sum": vendor_sum, "rollup_less_removed": less,
                         "difference": diff})
    return {
        "available": True,
        "rollup_tab": r_row["name"],
        "rollup_captured_at": rollup.get("captured_at"),
        "removed": [x["name"] for x in removed_rows],
        "rows": out_rows,
        "differences": [r for r in out_rows if r["difference"] not in (None, 0)],
        "unmeasured": [r["label"] for r in out_rows if r["difference"] is None],
        "matches": all(r["difference"] == 0 for r in out_rows),
    }


def _new_this_period(rows: Sequence[dict], previous: Mapping | None,
                     sweep_date: str) -> dict:
    """Budget movement since the previous month's final sweep.

    Budgets only. Revenue is deliberately NOT compared: the tracker's figures
    are month-to-date and restart on the 1st, so last month's month-end revenue
    against this month's early days reads as "revenue rolled off" for every
    vendor that sold anything — a comparison of two different things."""
    empty = {"compared_to": None, "budget_changes": [], "newly_funded": [], "paused": [],
             "still_zero": [], "renamed": [], "first_seen": [], "not_in_sweep": []}
    if not previous or not previous.get("docs"):
        return {**empty, "sentences": [
            _seg("No sweep from the previous month to compare against.")]}
    prev_rows = [_vendor_row(d, []) for d in previous["docs"]
                 if not _is_rollup(d) and not _is_non_media(d)]
    pairs = _pair_across_months(rows, prev_rows)
    paired_prev = {id(p) for p in pairs.values()}
    when = _short_date(str(previous.get("date") or sweep_date))
    out = {k: [] for k in empty if k != "compared_to"}

    def label(item: dict) -> str:
        return item["vendor"] + (f" (was {item['was']})" if item["was"] else "")

    for i, r in enumerate(rows):
        p = pairs.get(i)
        b = r["budget"]
        if p is None:
            out["first_seen"].append({"vendor": r["name"], "was": None, "budget": b})
            continue
        renamed = _match_key(p["name"]) != _match_key(r["name"])
        item = {"vendor": r["name"], "was": p["name"] if renamed else None,
                "budget": b, "previous": p["budget"]}
        if renamed:
            out["renamed"].append({"vendor": r["name"], "was": p["name"]})
        pb = p["budget"]
        if b == 0 and pb:
            out["paused"].append(item)
        elif b == 0:
            out["still_zero"].append(item)
        elif b and not pb:
            out["newly_funded"].append(item)
        elif b is not None and pb is not None and b != pb:
            out["budget_changes"].append({**item, "change": round(b - pb, 2)})
    out["not_in_sweep"] = [p["name"] for p in prev_rows if id(p) not in paired_prev]
    out["budget_changes"].sort(key=lambda c: (-abs(c["change"]), c["vendor"]))

    s: list[dict] = []
    if out["budget_changes"]:
        s.append(_seg(f"Budget changes since {when}: ", "; ".join(
            f"{label(c)} {money(c['previous'])} → {money(c['budget'])}"
            for c in out["budget_changes"]), "."))
    for f in out["newly_funded"]:
        was = "was $0" if f["previous"] == 0 else "budget was not set"
        s.append(_seg(f"{label(f)} now has a ", (money(f["budget"]),),
                      f" budget allocated ({was} on {when})."))
    for r in out["renamed"]:
        moved = next((x for k in ("budget_changes", "newly_funded", "paused", "still_zero")
                      for x in out[k] if x["vendor"] == r["vendor"]), None)
        if moved is None:  # a rename with no budget movement still gets said once
            budget = next(x["budget"] for x in rows if x["name"] == r["vendor"])
            s.append(_seg(f"{r['vendor']} (was {r['was']}) now has a "
                          f"{money(budget)} budget." if budget is not None else
                          f"{r['vendor']} (was {r['was']}) has no budget set."))
    for p in out["paused"]:
        s.append(_seg(f"{label(p)} now shows a $0 budget (was {money(p['previous'])} on "
                      f"{when}), consistent with being paused."))
    for z in out["still_zero"]:
        before = "as on" if z["previous"] == 0 else "budget not set on"
        s.append(_seg(f"{label(z)} shows a $0 budget ({before} {when})."))
    for f in out["first_seen"]:
        s.append(_seg(f"{f['vendor']} is a new tab this month ({money(f['budget'])} budget) — "
                      f"no matching tab in the {when} sweep."))
    if out["not_in_sweep"]:
        s.append(_seg(f"In the {when} sweep but not this one: {_and(out['not_in_sweep'])}."))
    if not s:
        s.append(_seg(f"No budget changes since {when}."))
    return {"compared_to": previous.get("date"), **out, "sentences": s}


def compute(sweep: Mapping, *, targets: dict, previous: Mapping | None = None,
            rollup: Mapping | None = None) -> dict:
    """One sweep in, the whole report out — see the module docstring.

    ``sweep`` is ``snapshots.vendor_sweep()``'s shape: ``{"date", "docs",
    "excluded"}``. A roll-up doc found in ``docs`` is used as ``rollup`` (and
    never counted as a vendor); non-media tabs (Website) are excluded from every
    figure and named. ``targets`` is one resolved ``goals.get_targets`` dict.
    """
    sweep_date = str(sweep.get("date") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", sweep_date):
        raise ValueError(f"vendor sweep carries no usable date ({sweep_date!r})")
    ym = sweep_date[:7]
    thr = goals.thresholds(targets)

    docs = sorted((d for d in (sweep.get("docs") or []) if isinstance(d, dict)),
                  key=lambda d: (str(d.get("captured_at") or ""), str(d.get("vendor") or "")))
    if rollup is None:
        rollup = _sweep_rollup(sweep)
    non_media = [d for d in docs if _is_non_media(d)]
    paid_docs = [d for d in docs if not _is_rollup(d) and not _is_non_media(d)]

    basis_notes: list[dict] = []
    rows = [_vendor_row(d, basis_notes) for d in paid_docs]
    for r in rows:
        r["show_rate_target"] = _show_target_for(r["channel"], targets)

    # --- portfolio ---------------------------------------------------------
    missing: list[dict] = []
    portfolio: dict[str, float | None] = {}
    sums: dict[str, float | None] = {}
    for key, field in _ADDITIVE.items():
        total, absent = _sum(rows, field)
        portfolio[key] = sums[field] = total
        if absent:
            missing.append({"metric": key, "label": METRICS[key][0], "vendors": absent,
                            "reason": "not reported on the vendor tab"})
    for key, (n, d, scale, reason) in _RATIOS.items():
        portfolio[key] = _div(sums[n], sums[d], scale)
        if portfolio[key] is None:
            why = (reason if sums[n] is not None and sums[d] is not None
                   else "a component total is withheld (see above)")
            missing.append({"metric": key, "label": METRICS[key][0],
                            "vendors": "portfolio", "reason": why})
    for key in _VENDOR_RATIOS:
        n, d, _scale, reason = _RATIOS[key]
        undefined = [r["name"] for r in rows
                     if r[key] is None and r[n] is not None and r[d] == 0]
        if undefined:
            missing.append({"metric": key, "label": METRICS[key][0], "vendors": undefined,
                            "reason": reason})

    # --- benchmarks --------------------------------------------------------
    mix = _channel_mix(rows, targets)
    target_items = goals.vendor_report_targets(targets, mix["applied_set"])
    movers, not_charted = [], []
    for t in target_items:
        actual = portfolio.get(t["key"])
        if t["target"] is None:
            not_charted.append({"key": t["key"], "label": t["label"], "reason": "no target set"})
            missing.append({"metric": t["key"], "label": t["label"], "vendors": "portfolio",
                            "reason": "no target set — left out of the movers chart"})
            continue
        if actual is None:
            not_charted.append({"key": t["key"], "label": t["label"],
                                "reason": _RATIOS[t["key"]][3]})
            continue
        if t["polarity"] == "down":
            gap = (t["target"] - actual) / t["target"] * 100
        else:
            gap = (actual - t["target"]) / t["target"] * 100
        movers.append({"key": t["key"], "label": t["label"], "format": t["format"],
                       "actual": actual, "target": t["target"], "basis": t["basis"],
                       "polarity": t["polarity"], "gap_pct": round(gap, 2),
                       "beating": gap >= 0})
    movers.sort(key=lambda m: (-m["gap_pct"], m["key"]))

    # --- statuses -----------------------------------------------------------
    for r in rows:
        r["status"], r["status_rule"] = _status(r, thr)
    for r in rows:
        r["action"] = _action(r, rows, thr)

    day = int(sweep_date[8:10])
    days_in_month = calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]
    small = ((portfolio["total_leads"] or 0) < thr["vendor_small_sample_min_leads"]
             or (portfolio["qual_demos_booked"] or 0) < thr["vendor_small_sample_min_demos"])

    report: dict[str, Any] = {
        "generator": GENERATOR_VERSION,
        "kind": KIND,
        "year_month": ym,
        "month_label": _month_name(ym),
        "as_of": sweep_date,
        "title": f"{calendar.month_name[int(ym[5:7])]} {day}",
        "day_of_month": day,
        "days_in_month": days_in_month,
        "month_complete": day == days_in_month,
        "small_sample": small,
        "sweep": {
            "date": sweep_date,
            "captured_at": max((str(d.get("captured_at") or "") for d in docs), default=None),
            "vendor_count": len(rows),
            "excluded": [{**e, "reason_text": _EXCLUSION_TEXT.get(e.get("reason"),
                                                                  str(e.get("reason")))}
                         for e in (sweep.get("excluded") or []) if isinstance(e, dict)]
            + [{"tab": str(d.get("vendor") or d.get("vendor_slug")),
                "slug": d.get("vendor_slug"), "reason": "non_media",
                "reason_text": _EXCLUSION_TEXT["non_media"]}
               for d in non_media],
        },
        "portfolio": portfolio,
        "metrics": {k: {"label": v[0], "format": v[1]} for k, v in METRICS.items()},
        "targets": {**mix, "items": target_items,
                    "show_rate_goals": {name: _show_target_for(name, targets)
                                        for name in ("Meta", "Google", "Email")}},
        "movers": movers,
        "movers_not_charted": not_charted,
        "vendors": rows,
        "missing": missing,
        "basis_notes": basis_notes,
        "rules": {k: thr[k] for k in sorted(thr) if k.startswith("vendor_")
                  or k in ("bad_lead_rate_red", "spend_no_demo_limit")},
    }
    report["channels"] = _channels(rows)
    report["reconciliation"] = _rollup_reconciliation(rows, non_media, rollup, sweep_date)
    if not report["reconciliation"]["available"]:
        missing.append({"metric": "reconciliation", "label": "Roll-up reconciliation",
                        "vendors": "portfolio", "reason": report["reconciliation"]["reason"]})
    report["new_this_period"] = _new_this_period(rows, previous, sweep_date)
    report["standouts"] = _standouts(rows)
    report["watch_items"] = _watch_items(report, thr)
    report["actions"] = _actions(rows)
    report["notes"] = _notes(report)
    return report


def _channels(rows: Sequence[dict]) -> dict:
    buckets = []
    for name in CHARTED_CHANNELS + (OTHER,):
        members = [r for r in rows if _bucket(r["channel"]) == name]
        spend, _a = _sum(members, "spend") if members else (0.0, [])
        rev, _b = _sum(members, "projected_revenue") if members else (0.0, [])
        buckets.append({"channel": name, "spend": spend, "projected_revenue": rev,
                        "vendors": [r["name"] for r in members]})
    other = [{"vendor": r["name"],
              "channel": r["channel"] if r["channel"] != OTHER else "unrecognised"}
             for r in rows if _bucket(r["channel"]) == OTHER]
    return {"buckets": buckets, "other_vendors": other}


def _standouts(rows: Sequence[dict]) -> list[dict]:
    """Positive facts, one per (vendor, fact) — the sample repeated one."""
    out: list[dict] = []
    with_rev = [r for r in rows if r["projected_revenue"]]
    only_rev = with_rev[0]["name"] if len(with_rev) == 1 else None
    mentioned_rev: set[str] = set()
    top = _top_ql(rows)
    for r in rows:
        if r["status_rule"] == "completions":
            rev = r["projected_revenue"]
            tail = []
            if rev:
                mentioned_rev.add(r["name"])
                tail = [", and already shows ", (money(rev),), " in projected revenue"]
                if r["name"] == only_rev:
                    tail.append(" — the only revenue signal in the portfolio so far")
            item = _seg(f"{r['name']} has ", (f"completed {_completed_of(r)}",), *tail, ".")
            out.append({"vendor": r["name"], "fact": "completions", **item})
        elif r["status_rule"] == "qualified_volume":
            most = (" — the most of any vendor so far" if r["qualified_leads"] == top else "")
            item = _seg(f"{r['name']} already has ",
                        (f"{count(r['qualified_leads'])} qualified leads",),
                        f" out of {count(r['leads'])} total{most}.")
            out.append({"vendor": r["name"], "fact": "qualified_volume", **item})
    for r in with_rev:
        if r["name"] in mentioned_rev:
            continue
        only = " — the only revenue signal in the portfolio so far" if r["name"] == only_rev else ""
        item = _seg(f"{r['name']} has ", (money(r["projected_revenue"]),),
                    f" in projected revenue on the books{only}.")
        out.append({"vendor": r["name"], "fact": "projected_revenue", **item})
    return out


def _watch_items(report: Mapping, thr: Mapping[str, float]) -> list[dict]:
    rows, p = report["vendors"], report["portfolio"]
    out: list[dict] = []
    for r in rows:
        if r["status"] != CHECK_IN:
            continue
        if r["status_rule"] == "dnc_zero_spend":
            n = r["dnc_bad_leads"]
            booked = (f" and {count(r['demos_booked'])} "
                      f"{_plural(r['demos_booked'], 'demo')} booked" if r["demos_booked"] else "")
            item = _seg(f"{r['name']} logged ", (f"{count(n)} DNC bad {_plural(n, 'lead')}",),
                        f"{booked} with zero spend recorded — worth confirming with the vendor.")
        elif r["status_rule"] == "bad_lead_rate":
            item = _seg(f"{r['name']}: ", (f"{count(r['dnc_bad_leads'])} of "
                                           f"{count(r['leads'])} leads",),
                        f" were DNC bad leads — at or above the "
                        f"{thr['bad_lead_rate_red']:g}% red line.")
        else:
            item = _seg(f"{r['name']} has spent ", (money(r["spend"]),),
                        " with no demo booked yet.")
        out.append({"vendor": r["name"], "fact": r["status_rule"], **item})
    funded = [r for r in rows if r["budget"]]
    idle = [r for r in funded if r["spend"] == 0]
    if idle:
        early = (" — normal this early in the month, but" if report["day_of_month"] <= 3
                 else " —")
        out.append({"vendor": None, "fact": "zero_spend", **_seg(
            (f"{len(idle)} of {len(funded)}",), " funded vendors show ", ("$0",),
            f" spend so far{early} worth checking campaigns are live as expected.")})
    show, qdb = p.get("show_rate_pct"), p.get("qual_demos_booked")
    target = next((m["target"] for m in report["movers"] if m["key"] == "show_rate_pct"), None)
    if show is not None and target is not None and show < target:
        tail = (f", but on only {count(qdb)} qualified demos booked — not yet meaningful."
                if report["small_sample"]
                else f" across {count(qdb)} qualified demos booked.")
        out.append({"vendor": None, "fact": "show_rate", **_seg(
            "Portfolio show rate sits at ", (pct(show),),
            f" against a {target:g}% target{tail}")})
    return out


def _actions(rows: Sequence[dict]) -> list[dict]:
    """Check In first (it asks for something), then Strong Start, then the Too
    Early vendors worth watching; everyone else folds into one catch-all row."""
    order = {CHECK_IN: 0, STRONG_START: 1, TOO_EARLY: 2}
    named = [r for r in rows if r["status"] != TOO_EARLY or r["status_rule"] == "booked_watch"]
    named.sort(key=lambda r: order[r["status"]])  # stable: tab order within a status
    out = [{"vendor": r["name"], "status": r["status"], "rule": r["status_rule"],
            **r["action"]} for r in named]
    rest = [r for r in rows if r not in named]
    if rest:
        idle = sum(1 for r in rest if r["spend"] == 0)
        text = (f"Monitor for first spend and lead flow — {idle} of these {len(rest)} vendors "
                "have $0 spend so far." if idle else
                f"Keep monitoring — none of these {len(rest)} vendors has reached a status "
                "threshold yet.")
        out.append({"vendor": "All other vendors", "status": TOO_EARLY, "rule": "catch_all",
                    "vendors": [r["name"] for r in rest], **_seg(text)})
    return out


def _notes(report: Mapping) -> dict:
    """Every prose line the page prints, as fixed templates over computed facts."""
    p, rows, mix = report["portfolio"], report["vendors"], report["targets"]
    day, month = report["day_of_month"], report["month_label"].split(" ")[0]
    strong = [r for r in rows if r["status"] == STRONG_START]
    check = [r for r in rows if r["status"] == CHECK_IN]

    opener = "First read of the month — " if day <= 3 else f"Day {day} of {month} — "
    thesis_parts: list[Any] = [opener, (f"{money(p['total_spend'])} spent",), " against a ",
                               (f"{money(p['total_budget'])} budget",),
                               f" ({pct(p['budget_utilized_pct'])} utilized)."]
    if strong:
        facts = []
        for r in strong:
            if r["status_rule"] == "completions":
                facts.append(f"{r['name']} has completed {_completed_of(r)}")
            else:
                facts.append(f"{r['name']} already has {count(r['qualified_leads'])} "
                             "qualified leads")
        n = len(strong)
        word = {1: "One", 2: "Two", 3: "Three"}.get(n, str(n))
        thesis_parts.append(f" {word} early {_plural(n, 'signal stands', 'signals stand')} "
                            f"out: {_and(facts)}.")
    if check:
        thesis_parts.append(f" {_and([r['name'] for r in check])} "
                            f"{_plural(len(check), 'needs', 'need')} a check-in.")
    if any(r["status"] == TOO_EARLY for r in rows):
        thesis_parts.append(" Everything else is still too early to grade.")

    leads, completed = p["total_leads"], p["demos_completed"]
    if report["small_sample"]:
        glance_lead = (f"Only {count(leads)} leads and {count(completed)} completed demos so "
                       "far this month — read every ratio below with that small a sample in mind.")
        glance_reading = _seg(
            ("Reading this:",), f" show rate ({pct(p['show_rate_pct'])}) is "
            f"{count(completed)} of {count(p['qual_demos_booked'])} qualified demos booked by "
            f"day {day} of the month — not a trend yet. Treat every ratio here as a starting "
            "point to watch, not a verdict.")
    else:
        glance_lead = (f"{count(leads)} leads and {count(completed)} completed demos "
                       f"month-to-date across {len(rows)} paid vendors.")
        glance_reading = None

    ahead = [m["label"].lower() for m in report["movers"] if m["beating"]]
    behind = [m for m in report["movers"] if not m["beating"]]
    clauses = []
    if ahead:
        clauses.append(f"{_and(ahead)} {_plural(len(ahead), 'is', 'are')} ahead of target")
    if behind:
        worst = min(behind, key=lambda m: m["gap_pct"])
        tail = (f" — {worst['label'].lower()} by the widest margin ({worst['gap_pct']:+.1f}%)"
                if len(behind) > 1 else f" ({worst['gap_pct']:+.1f}%)")
        clauses.append(f"{_and([m['label'].lower() for m in behind])} "
                       f"{_plural(len(behind), 'is', 'are')} missing "
                       f"{_plural(len(behind), 'its', 'theirs')}{tail}")
    bits = [("; ".join(clauses) + ".") if clauses else "No benchmark could be measured yet."]
    counts = _and([f"{METRICS[k][0].lower()} ({count(p[k])})" for k in COUNT_KEYS])
    bits.append(f"{counts[:1].upper()}{counts[1:]} are raw counts — there is no fixed "
                "percentage benchmark for a count, so they are left out of this chart "
                "rather than compared against a made-up target.")
    for n in report["movers_not_charted"]:
        bits.append(f"{n['label']} is not charted: {n['reason']}.")
    movers_reading = _seg(("Reading this:",), " " + " ".join(bits))
    targets_used = _seg(("Targets used",), f" ({mix['reason']}): ",
                        " · ".join(t["basis"] for t in mix["items"] if t["target"] is not None),
                        ".")

    buckets = report["channels"]["buckets"]
    spent = [b for b in buckets if b["spend"]]
    total_spend = p["total_spend"] or 0
    if spent and total_spend:
        top = max(spent, key=lambda b: b["spend"])
        share = top["spend"] / total_spend * 100
        lead = (f"{share:.0f}% of paid spend this month has gone through {top['channel']}"
                if share < 99.95 else
                f"Every dollar spent this month so far has gone through {top['channel']}")
        idle = [b["channel"] for b in buckets
                if b["channel"] in CHARTED_CHANNELS and not b["spend"]]
        channel_lead = lead + (f" — {_and(idle)} {_plural(len(idle), 'hasn’t', 'haven’t')} "
                               "spent yet." if idle else ".")
    else:
        channel_lead = "No paid spend is recorded in any channel yet."
    with_rev = [r for r in rows if r["projected_revenue"]]
    if len(with_rev) == 1:
        channel_lead += (f" Projected revenue: {with_rev[0]['name']} is the only vendor with any "
                         f"on the books ({money(with_rev[0]['projected_revenue'])}).")
    elif with_rev:
        channel_lead += " Projected revenue on the books: " + "; ".join(
            f"{r['name']} {money(r['projected_revenue'])}" for r in with_rev) + "."
    elif p["projected_revenue"] == 0:
        channel_lead += " No vendor has projected revenue on the books yet."
    channel_reading = None
    if len(spent) == 1:
        channel_reading = _seg(("Reading this:",), " only one channel has recorded spend so far, "
                               "so the spend chart is a single bar — that is the data, not a "
                               "rendering issue.")
    other = report["channels"]["other_vendors"]
    other_note = (_seg(("Other",), " = " + "; ".join(f"{o['vendor']} ({o['channel']})"
                                                     for o in other) + ".") if other else None)

    funded = [r for r in rows if r["budget"]]
    low = [r for r in funded if (r["budget_utilized_pct"] or 0) < 10]
    budget_caption = (f"{len(low)} of {len(funded)} funded vendors are below 10% budget "
                      "utilization." if funded else "No vendor has a budget set.")
    unq = [r for r in rows if (r["demos_booked"] or 0) > (r["qual_demos_booked"] or 0)]
    demos_caption = ("Qualified demos booked (the show-rate denominator) against demos "
                     "completed — raw counts.")
    if unq:
        demos_caption += " Booked but not qualified, so not drawn: " + "; ".join(
            f"{r['name']} {count((r['demos_booked'] or 0) - (r['qual_demos_booked'] or 0))}"
            for r in unq) + "."
    rec = report["reconciliation"]
    if rec["available"]:
        removed = f" after removing {_and(rec['removed'])}" if rec["removed"] else ""
        head = f"Checked against the roll-up tab ({rec['rollup_tab']}, same pull){removed}: "
        if rec["matches"]:
            reconciliation = head + "every figure matches."
        else:
            def signed(d: dict) -> str:
                v = d["difference"]
                body = money(abs(v)) if d["format"] == MONEY else count(abs(v))
                return ("+" if v > 0 else "−") + body
            diffs = ", ".join(f"{d['label'].lower()} {signed(d)}" for d in rec["differences"])
            reconciliation = head + (f"roll-up minus vendor rows — {diffs}" if diffs else "")
            if rec["unmeasured"]:
                reconciliation += (("; " if diffs else "") + f"{_and(rec['unmeasured'])} could "
                                   "not be compared")
            reconciliation += "; every other figure matches."
    else:
        reconciliation = rec["reason"]

    thr = report["rules"]
    status_rules = (
        f"Status rules — Check In: a DNC bad lead logged with $0 spend, a bad-lead rate at or "
        f"above {thr['bad_lead_rate_red']:g}% once a vendor has "
        f"{thr['vendor_check_in_min_leads']:g}+ leads, or {money(thr['spend_no_demo_limit'])}+ "
        f"spent with no demo booked. Strong Start: "
        f"{thr['vendor_strong_start_min_completed']:g}+ completed demos at or above the "
        f"channel's show-rate target, or {thr['vendor_strong_start_min_qualified_leads']:g}+ "
        f"qualified leads. Too Early: everything else; one with "
        f"{thr['vendor_watch_min_qual_booked']:g}+ qualified demos booked gets its own row. "
        "Every threshold is editable in Targets.")

    sweep = report["sweep"]
    by_reason: dict[str, list[str]] = {}
    for e in sweep["excluded"]:
        by_reason.setdefault(str(e.get("reason")), []).append(str(e.get("tab")))
    scope = "Figures reflect paid vendor activity only"
    if by_reason.get("non_media"):
        scope += f" ({_and(by_reason['non_media'])} excluded — non-media)"
    left_out = []
    if by_reason.get("hidden"):
        n = len(by_reason["hidden"])
        left_out.append(f"{n} hidden {_plural(n, 'tab')}")
    if by_reason.get("stale_capture"):
        n = len(by_reason["stale_capture"])
        left_out.append(f"{n} {_plural(n, 'capture')} from earlier runs that day")
    other = [r for r in by_reason if r not in ("non_media", "hidden", "stale_capture")]
    if other:
        n = sum(len(by_reason[r]) for r in other)
        left_out.append(f"{n} other {_plural(n, 'tab')}")
    if left_out:
        scope += f"; {_and(left_out)} left out (listed under data gaps)"
    captured = str(sweep.get("captured_at") or "")
    at = f" ({captured[11:16]} UTC)" if len(captured) >= 16 else ""
    span = ("full month" if report["month_complete"]
            else f"month to date, as of {_short_date(report['as_of'])}")
    goals_by = report["targets"]["show_rate_goals"]
    show_goals = ", ".join(f"{k} {v:g}%" for k, v in goals_by.items() if v is not None)
    cpl = next((t["target"] for t in mix["items"] if t["key"] == "cost_per_lead"), None)
    qlr = next((t["target"] for t in mix["items"] if t["key"] == "ql_ratio_pct"), None)
    bench = []
    if cpl is not None:
        bench.append(f"CPL never >{money(cpl)}")
    if qlr is not None:
        bench.append(f"QL ratio target {qlr:g}%+")
    if show_goals:
        bench.append(f"Show rate goals — {show_goals}")
    footer = _seg((f"Vendor Performance — {report['month_label']} ({span}).",),
                  f" {scope}. All totals recomputed from the {len(rows)} vendor tabs in the "
                  f"{_short_date(report['as_of'])} pull{at}. {reconciliation}"
                  + (f" Benchmarks: {' · '.join(bench)}." if bench else ""))

    left_out = []
    stale = by_reason.get("stale_capture") or []
    if stale:
        n = len(stale)
        left_out.append(
            f"{n} {_plural(n, 'capture')} from earlier in the day "
            f"{_plural(n, 'was', 'were')} left out because "
            f"{_plural(n, 'that tab was', 'those tabs were')} later renamed or removed: "
            f"{', '.join(stale)}.")
    hidden = by_reason.get("hidden") or []
    if hidden:
        n = len(hidden)
        left_out.append(f"{n} hidden {_plural(n, 'tab')} {_plural(n, 'was', 'were')} left "
                        f"out: {', '.join(hidden)}.")
    for e in sweep["excluded"]:
        if e.get("reason") not in ("stale_capture", "hidden"):
            left_out.append(f"{e.get('tab')}: {e.get('reason_text') or e.get('reason')}.")

    return {
        "left_out": left_out,
        "reconciliation": reconciliation,
        "status_rules": status_rules,
        "footer": footer,
        "thesis": _seg(*thesis_parts),
        "glance_lead": glance_lead,
        "glance_sublabel": (f"Funnel & volume — day 1–{day} of {month}" if day > 1
                            else f"Funnel & volume — day 1 of {month}"),
        "glance_reading": glance_reading,
        "movers_lead": _seg(
            f"Percent gap between {month}'s portfolio metrics and their benchmark targets. ",
            ("Bar direction is the sign; color is whether it's beating (green) or missing "
             "(red) the target",),
            " — so a low cost per lead shows right and green, a low show rate shows left "
            "and red."),
        "movers_reading": movers_reading,
        "targets_used": targets_used,
        "budget_caption": budget_caption,
        "demos_caption": demos_caption,
        "channel_lead": channel_lead,
        "channel_reading": channel_reading,
        "other_channels": other_note,
    }


# --- idempotency + the I/O seam ------------------------------------------------------

def cache_key(*, sweep: Mapping, previous: Mapping | None, rollup: Mapping | None,
              targets: Mapping, template: Mapping) -> str:
    """Same sweep capture + same comparison + same targets + same template +
    same generator → same key → the stored run is served instead of re-derived.
    A recapture on the same day moves ``captured_at`` and so the key."""
    def stamp(s: Mapping | None) -> list:
        if not s:
            return []
        docs = s.get("docs") or []
        return [s.get("date"), len(docs),
                max((str(d.get("captured_at") or "") for d in docs), default="")]
    payload = json.dumps({
        "v": GENERATOR_VERSION, "sweep": stamp(sweep), "previous": stamp(previous),
        "rollup": (rollup or {}).get("captured_at"),
        "targets": {"thresholds": targets.get("thresholds"),
                    "channel_goals": targets.get("channel_goals")},
        "template": dict(template),
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EmptyMonth(LookupError):
    """No vendor figures for the requested month. The message is user-facing."""


class TemplateRefused(ValueError):
    """The request named a template this build cannot use. User-facing."""


def empty_month_message(year_month: str) -> str:
    return (f"The last pull has no vendor figures for {_month_name(year_month)}. "
            "Pull the workbook, then build again.")


def resolve_template(requested: Any, active: Mapping | None) -> dict:
    """Which template this build uses, or a refusal — never a silent swap.

    ``requested`` is the request's ``template`` field: ``"builtin"`` forces the
    built-in (the ONLY way a build falls back to it), ``None`` means "the
    workspace's active template". ``active`` is ``runs.active_template()``; its
    ``builtin`` flag (or no stored version at all) means the built-in default,
    whose layout is ``vendor_report_render.DEFAULT_LAYOUT``. Phase 1 renders only
    the built-in, so an active uploaded version is refused with the way out
    named, rather than quietly rendered as something it is not."""
    if requested == "builtin":
        return dict(BUILTIN_TEMPLATE)
    if requested is not None:
        raise TemplateRefused(
            "template must be \"builtin\" or left out — saved template versions are not "
            "selectable by id yet.")
    if active is None or active.get("builtin", True):
        return dict(BUILTIN_TEMPLATE)
    raise TemplateRefused(
        "This workspace's active template is an uploaded version, and this server can only "
        "render the built-in template so far. Build with the built-in template "
        "(template: \"builtin\") to continue.")


def build(*, workspace_id: str, year_month: str | None = None,
          template: Any = None) -> dict:
    """Read, compute, persist (or serve the stored run). Returns the run with
    ``reused`` saying which happened. Raises :class:`EmptyMonth` /
    :class:`TemplateRefused` with caller-safe messages; store failures surface as
    ``snapshots.SnapshotStoreError`` / ``runs.RunStoreError`` (the app maps both
    to 502)."""
    from . import runs, snapshots

    # The active template is read only when the request did not name one.
    tpl = resolve_template(template, runs.active_template(workspace_id)
                           if template is None else None)
    sweep = snapshots.vendor_sweep(year_month)
    if not sweep or not [d for d in (sweep.get("docs") or [])
                         if not _is_rollup(d) and not _is_non_media(d)]:
        ym = year_month or date.today().strftime("%Y-%m")
        raise EmptyMonth(empty_month_message(ym))
    ym = str(sweep["date"])[:7]
    previous = snapshots.previous_month_sweep(ym)
    # The roll-up comes from the SAME bounded read as the vendor docs, or not at
    # all — there is deliberately no wider lookup behind it.
    rollup = _sweep_rollup(sweep)
    targets = goals.get_targets(workspace_id)
    key = cache_key(sweep=sweep, previous=previous, rollup=rollup, targets=targets,
                    template=tpl)
    for run in runs.list_runs(workspace_id, kind=KIND):
        if (run.get("structured") or {}).get("cache_key") == key:
            return {**run, "reused": True}

    structured = compute(sweep, targets=targets, previous=previous, rollup=rollup)
    built_at = datetime.now(timezone.utc).isoformat()
    structured["cache_key"] = key
    structured["build"] = {"built_at": built_at, "sweep_date": sweep["date"],
                           "template": tpl}
    run = {
        "id": runs.new_run_id(),
        "kind": KIND,
        "generated_at": built_at,
        "built_at": built_at,
        "sweep_date": sweep["date"],
        "template": tpl,
        "user_id": workspace_id,
        "agent_id": "a6",
        "sources": [f"mr_snapshots sweep {sweep['date']}"],
        "structured": structured,
        # Honest provenance: no model writes any of this, by design.
        "ai": False,
        "fallback_reason": ("the vendor report is arithmetic over the vendor sweep and fixed "
                            "sentence templates — no model writes any part of it"),
    }
    runs.save_run(run)
    return {**run, "reused": False}


def periods(*, limit: int = 12) -> list[dict]:
    """Months with a vendor sweep, newest first, for the month picker."""
    from . import snapshots

    return [{"year_month": ym, "label": _month_name(ym)}
            for ym in snapshots.sweep_months(limit=limit)]


def iter_scalar_values(report: Mapping) -> Iterable[tuple[str, Any]]:
    """Every scalar the report publishes, ``(name, value)`` — the template
    placeholder vocabulary's data side."""
    for key in METRICS:
        yield key, (report.get("portfolio") or {}).get(key)
    for key in ("title", "month_label", "as_of", "year_month"):
        yield key, report.get(key)
