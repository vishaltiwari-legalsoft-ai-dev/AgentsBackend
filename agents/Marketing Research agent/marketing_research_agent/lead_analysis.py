"""Lead Analysis — demo-level lead-sheet monitoring (requirements doc 2026-08-10).

The team keeps a HubSpot-style lead sheet: one row per booked demo, carrying the
mechanical call status (Meeting Outcome: Completed / No Show / Canceled / Bad
Lead) and the downstream CRM status (Deal Stage: Contract Sent / Hot Leads /
Demo No Show / Lost DNC). The sheet's Campaign column IS the vendor name used on
the report, so rows join back to the tracker's vendor tabs by slug.

The tab is auto-detected by its header row in any connected workbook — the user
just connects the sheet from the Data tab; there is nothing to configure. Five
red-flag rules run per vendor per month, and each flag message names the FIX
direction the requirements doc assigns it (lead-quality fix vs booking/
confirmation fix vs offer-match fix vs SPC-process fix) — the doc's core point
is that these look similar but need different fixes.

Rates are computed over RESOLVED demos only (an outcome is recorded); pending
rows are reported but never counted against a vendor, so a mid-month sheet full
of upcoming demos can't trip a flag.
"""

from __future__ import annotations

from . import goals
from .snapshots import slugify

# Header aliases → canonical field. A tab qualifies as the lead sheet when all
# of _REQUIRED resolve in one header row (the combination is specific enough
# that no tracker/report tab matches it).
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "demo_month": ("demo month",),
    "campaign": ("campaign",),
    "brand": ("brand",),
    "source": ("source",),
    "meeting_outcome": ("meeting outcome",),
    "deal_stage": ("deal stage",),
    "services_sold": ("no. of services sold", "# of services sold", "of services sold", "services sold"),
    "amount": ("$ amount", "amount"),
    "mrr": ("mrr",),
}
_REQUIRED = ("demo_month", "campaign", "meeting_outcome", "deal_stage")

_MONTHS = {m.lower(): i + 1 for i, m in enumerate((
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"))}
_MONTHS.update({m[:3]: n for m, n in list(_MONTHS.items())})

_RESOLVED = ("completed", "no_show", "canceled", "bad_lead")
_STAGES = ("contract_sent", "hot_leads", "demo_no_show", "lost_dnc")


def _norm(s: str) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _match_score(header: str, alias: str) -> int:
    if header == alias:
        return 3
    if header.startswith(alias):
        return 2
    if alias in header:
        return 1
    return 0


def _resolve_columns(header: list[str]) -> dict[str, int]:
    """Best column index per canonical field ("Source" must win over "Original
    Lead Source", so exact > prefix > contains)."""
    cols: dict[str, int] = {}
    normed = [_norm(h) for h in header]
    for field, aliases in _HEADER_ALIASES.items():
        best, best_score = -1, 0
        for idx, h in enumerate(normed):
            if not h:
                continue
            score = max(_match_score(h, a) for a in aliases)
            if score > best_score:
                best, best_score = idx, score
        if best >= 0:
            cols[field] = best
    return cols


def find_lead_tab(rows: list[list[str]]) -> dict | None:
    """Locate the lead sheet's header row in a grid. Returns
    ``{"header_row": i, "cols": {field: index}}`` or None."""
    for i, row in enumerate(rows[:8]):
        cols = _resolve_columns(row)
        if all(f in cols for f in _REQUIRED):
            return {"header_row": i, "cols": cols}
    return None


def _outcome(raw: str) -> str:
    s = _norm(raw)
    if not s:
        return "pending"
    if "completed" in s:
        return "completed"
    if "no show" in s or "no-show" in s or "noshow" in s:
        return "no_show"
    if "cancel" in s:
        return "canceled"
    if "bad lead" in s:
        return "bad_lead"
    return "other"


def _stage(raw: str) -> str:
    s = _norm(raw)
    if not s:
        return "none"
    if "contract sent" in s:
        return "contract_sent"
    if "hot lead" in s:
        return "hot_leads"
    if "demo no show" in s:
        return "demo_no_show"
    if "dnc" in s or "lost" in s:
        return "lost_dnc"
    return "other"


def _month_key(raw: str, year: int) -> str | None:
    s = _norm(raw)
    if not s:
        return None
    parts = s.replace(",", " ").split()
    y = year
    for p in parts:
        if p.isdigit() and len(p) == 4:
            y = int(p)
    for p in parts:
        if p in _MONTHS:
            return f"{y:04d}-{_MONTHS[p]:02d}"
    if len(s) >= 7 and s[:4].isdigit() and s[4] == "-" and s[5:7].isdigit():
        return s[:7]
    return None


def _money(raw: str) -> float:
    s = str(raw or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _int(raw: str) -> int:
    s = str(raw or "").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


def parse_lead_rows(rows: list[list[str]], *, year: int) -> tuple[list[dict], list[str]]:
    """Grid → one record per demo row. Returns (records, gap messages)."""
    found = find_lead_tab(rows)
    if not found:
        return [], ["no lead-analysis header row found"]
    cols = found["cols"]
    gaps: list[str] = []
    records: list[dict] = []
    bad_month = 0

    def cell(row: list[str], field: str) -> str:
        idx = cols.get(field, -1)
        return row[idx] if 0 <= idx < len(row) else ""

    for row in rows[found["header_row"] + 1:]:
        campaign = str(cell(row, "campaign")).strip()
        outcome_raw = str(cell(row, "meeting_outcome")).strip()
        if not campaign and not outcome_raw:
            continue  # blank/padding row
        month = _month_key(cell(row, "demo_month"), year)
        if month is None:
            bad_month += 1
            continue
        records.append({
            "month": month,
            "campaign": campaign or "(no campaign)",
            "brand": str(cell(row, "brand")).strip() or "?",
            "source": str(cell(row, "source")).strip() or "?",
            "outcome": _outcome(outcome_raw),
            "stage": _stage(cell(row, "deal_stage")),
            "services_sold": _int(cell(row, "services_sold")),
            "amount": _money(cell(row, "amount")),
            "mrr": _money(cell(row, "mrr")),
        })
    if bad_month:
        gaps.append(f"{bad_month} row(s) skipped — Demo Month not readable")
    return records, gaps


# --- aggregation + flags ----------------------------------------------------

def _pct(n: int, d: int) -> float | None:
    return round(n * 100 / d, 1) if d else None


def _evaluate(v: dict, t: dict) -> list[dict]:
    """The five lead-quality rules. Levels are all red — these are the doc's
    hard flag lines, and each message names the fix direction."""
    flags: list[dict] = []

    def red(message: str, metric: str) -> None:
        flags.append({"level": "red", "message": message, "metric": metric})

    if v["resolved"]:
        if (v["bad_lead_rate_pct"] or 0) > t["bad_lead_rate_red"]:
            red(f"Bad-lead rate {v['bad_lead_rate_pct']:.0f}% over the {t['bad_lead_rate_red']:.0f}% line — "
                "lead-quality problem (fix targeting/qualification)", "bad_lead_rate")
        if (v["no_show_rate_pct"] or 0) > t["no_show_rate_red"]:
            red(f"No-show rate {v['no_show_rate_pct']:.0f}% over the {t['no_show_rate_red']:.0f}% line — "
                "booking/confirmation problem on the vendor side, not lead quality", "no_show_rate")
        if (v["canceled_rate_pct"] or 0) > t["canceled_rate_red"]:
            red(f"Cancellation rate {v['canceled_rate_pct']:.0f}% over the {t['canceled_rate_red']:.0f}% line — "
                "the outreach offer likely doesn't match what the calendar invite promises", "canceled_rate")

    # "Completed = 0 with 3+ booked", counted over demos that actually reached
    # an outcome so a sheet full of upcoming demos can't trip it early-month.
    if v["completed"] == 0 and v["resolved"] >= t["zero_completed_min_demos"]:
        red(f"{v['booked']} demos booked, {v['resolved']} reached an outcome, none completed — "
            "booking-to-completion breakdown (flags regardless of spend)", "zero_completed")

    tr = v.get("tracker")
    if tr and tr.get("ql_ratio_pct") is not None and tr.get("booking_rate_pct") is not None:
        if tr["ql_ratio_pct"] > t["ql_ratio_great"] and tr["booking_rate_pct"] < t["booking_rate_broken"]:
            red(f"QL ratio {tr['ql_ratio_pct']:.0f}% but booking rate {tr['booking_rate_pct']:.0f}% — "
                "great leads, broken booking process (fix the booking/SPC flow)", "ql_high_booking_low")
    return flags


def _story(v: dict) -> str:
    """One calm plain-language line per vendor for the UI hero."""
    bits = [f"{v['completed']} completed"]
    if v["no_show"]:
        bits.append(f"{v['no_show']} no-show{'s' if v['no_show'] != 1 else ''}")
    if v["canceled"]:
        bits.append(f"{v['canceled']} canceled")
    if v["bad_lead"]:
        bits.append(f"{v['bad_lead']} bad lead{'s' if v['bad_lead'] != 1 else ''}")
    if v["pending"]:
        bits.append(f"{v['pending']} upcoming")
    line = f"{v['booked']} demos booked: " + ", ".join(bits) + "."
    if v["services_sold"]:
        line += f" {v['services_sold']} service{'s' if v['services_sold'] != 1 else ''} sold."
    return line


def _new_bucket(campaign: str) -> dict:
    return {
        "campaign": campaign, "slug": slugify(campaign), "matched_vendor": None,
        "booked": 0, "completed": 0, "no_show": 0, "canceled": 0, "bad_lead": 0,
        "pending": 0, "other": 0,
        "deal_stages": {}, "services_sold": 0, "amount": 0.0, "mrr": 0.0,
        "brands": {}, "sources": {},
    }


def summarize(records: list[dict], *,
              tracker_rollups: dict[str, dict[str, dict]] | None = None,
              thresholds: dict | None = None) -> dict:
    """Per-month, per-vendor aggregation + the five flag rules.

    ``tracker_rollups`` ("YYYY-MM" → slug → {vendor, leads, qualified_leads,
    demos_booked}) joins the tracker's same-month funnel counts in for the
    QL-ratio/booking-rate rule and for naming the matched vendor tab; campaigns
    with no tracker match are surfaced in ``unmatched_campaigns`` — never
    silently dropped.

    ``thresholds`` is the WORKSPACE's resolved threshold map (``goals.thresholds``
    over that workspace's targets). Left out, the verbatim 2026 defaults apply:
    this function does not reach the targets store, because reaching it would
    mean choosing a workspace, and the caller is the only one who knows which."""
    t = goals.default_thresholds()
    t.update(thresholds or {})
    rollups = tracker_rollups or {}

    months: dict[str, dict[str, dict]] = {}
    for r in records:
        bucket = months.setdefault(r["month"], {}).setdefault(r["campaign"], _new_bucket(r["campaign"]))
        bucket["booked"] += 1
        key = r["outcome"] if r["outcome"] in (*_RESOLVED, "pending") else "other"
        bucket[key] += 1
        bucket["deal_stages"][r["stage"]] = bucket["deal_stages"].get(r["stage"], 0) + 1
        bucket["services_sold"] += r["services_sold"]
        bucket["amount"] += r["amount"]
        bucket["mrr"] += r["mrr"]
        bucket["brands"][r["brand"]] = bucket["brands"].get(r["brand"], 0) + 1
        bucket["sources"][r["source"]] = bucket["sources"].get(r["source"], 0) + 1

    out_months: dict[str, dict] = {}
    unmatched: set[str] = set()
    for ym in sorted(months):
        vendors = []
        totals = _new_bucket("Total")
        for campaign, v in months[ym].items():
            v["resolved"] = sum(v[k] for k in _RESOLVED)
            v["completed_rate_pct"] = _pct(v["completed"], v["resolved"])
            v["no_show_rate_pct"] = _pct(v["no_show"], v["resolved"])
            v["canceled_rate_pct"] = _pct(v["canceled"], v["resolved"])
            v["bad_lead_rate_pct"] = _pct(v["bad_lead"], v["resolved"])
            v["amount"] = round(v["amount"], 2)
            v["mrr"] = round(v["mrr"], 2)
            tr = (rollups.get(ym) or {}).get(v["slug"])
            if tr:
                v["matched_vendor"] = tr.get("vendor")
                leads = tr.get("leads") or 0
                v["tracker"] = {
                    "leads": leads,
                    "qualified_leads": tr.get("qualified_leads") or 0,
                    "demos_booked": tr.get("demos_booked") or 0,
                    "ql_ratio_pct": _pct(tr.get("qualified_leads") or 0, leads),
                    "booking_rate_pct": _pct(tr.get("demos_booked") or 0, leads),
                }
            else:
                v["tracker"] = None
                unmatched.add(campaign)
            v["flags"] = _evaluate(v, t)
            v["story"] = _story(v)
            vendors.append(v)
            for k in ("booked", *_RESOLVED, "pending", "other", "services_sold"):
                totals[k] += v[k]
            totals["amount"] = round(totals["amount"] + v["amount"], 2)
            totals["mrr"] = round(totals["mrr"] + v["mrr"], 2)
            for field in ("brands", "sources"):
                for k, n in v[field].items():
                    totals[field][k] = totals[field].get(k, 0) + n
        vendors.sort(key=lambda x: x["booked"], reverse=True)
        totals["resolved"] = sum(totals[k] for k in _RESOLVED)
        totals["completed_rate_pct"] = _pct(totals["completed"], totals["resolved"])
        totals["no_show_rate_pct"] = _pct(totals["no_show"], totals["resolved"])
        totals["canceled_rate_pct"] = _pct(totals["canceled"], totals["resolved"])
        totals["bad_lead_rate_pct"] = _pct(totals["bad_lead"], totals["resolved"])
        for drop in ("campaign", "slug", "matched_vendor", "deal_stages"):
            totals.pop(drop, None)
        out_months[ym] = {
            "vendors": vendors,
            "totals": totals,
            "flag_count": sum(len(v["flags"]) for v in vendors),
        }
    return {
        "months": out_months,
        "latest_month": max(out_months) if out_months else None,
        "unmatched_campaigns": sorted(unmatched),
    }


# --- report/overview surfaces ------------------------------------------------

_FLAG_LABELS = {
    "bad_lead_rate": "over the bad-lead line (lead-quality fix)",
    "no_show_rate": "over the no-show line (booking/confirmation fix)",
    "canceled_rate": "over the cancellation line (offer-match fix)",
    "zero_completed": "with demos resolving but none completed",
    "ql_high_booking_low": "with great leads but a broken booking process",
}


def flag_summary(month_block: dict) -> list[dict]:
    """Grouped one-line-per-rule summary, same shape as the report flag_summary
    rows ({metric, level, count, text})."""
    counts: dict[str, int] = {}
    for v in month_block.get("vendors", []):
        for f in v.get("flags", []):
            counts[f["metric"]] = counts.get(f["metric"], 0) + 1
    out = []
    for metric, label in _FLAG_LABELS.items():
        n = counts.get(metric)
        if n:
            out.append({"metric": metric, "level": "red", "count": n,
                        "text": f"{n} vendor{'s' if n != 1 else ''} {label}"})
    return out


def red_flag_entries(summary: dict, month_keys: list[str] | None = None) -> list[dict]:
    """Lead flags as red_flag_vendors entries ({vendor, reasons}) for the given
    months (default: latest). Vendor is the matched tracker tab name when the
    campaign joined, else the campaign name itself."""
    if not summary or not summary.get("months"):
        return []
    keys = month_keys or ([summary["latest_month"]] if summary.get("latest_month") else [])
    merged: dict[str, list[str]] = {}
    for ym in keys:
        block = summary["months"].get(ym)
        if not block:
            continue
        for v in block["vendors"]:
            if not v["flags"]:
                continue
            name = v.get("matched_vendor") or v["campaign"]
            reasons = merged.setdefault(name, [])
            for f in v["flags"]:
                msg = f["message"] if len(keys) == 1 else f"{ym}: {f['message']}"
                if msg not in reasons:
                    reasons.append(msg)
    return [{"vendor": name, "reasons": reasons} for name, reasons in merged.items()]
