"""Report builders — the seven deliverables (requirements §4).

Each ``build`` produces a Report dict with structured data + Markdown + HTML and
persists it as a run. The structured layer comes from the feature modules; the
narrative layer comes from the analysis brain (LLM online, deterministic
offline).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from . import analysis, board_report as br, config, goals, lead_analysis, runs
from .modules import campaign_reporting as cr
from .modules import funnel_analysis as fa
from .modules import opportunity_research as orr

KINDS = [
    "daily_summary",
    "weekly_summary",
    "monthly_summary",
    "quarterly_summary",
    "threshold_alert",
    "competitor_digest",
    "opportunity_report",
    "utm_attribution",
    "icp_signal",
    "daily_movement",
    # The board deliverable, in two shapes. They are report kinds like any
    # other - same run store, same ``/mr/runs`` listing, same ownership check
    # on read back - and the only two that are not narrated. See BOARD_KINDS.
    "board_report",
    "board_report_comparison",
]

# Campaign-performance kinds share the aggregation pipeline and the
# period-window logic (the tracker holds monthly cumulative figures).
CAMPAIGN_KINDS = (
    "daily_summary", "weekly_summary", "monthly_summary",
    "quarterly_summary", "threshold_alert",
)


def _md_to_html(md: str) -> str:
    """Minimal, dependency-free Markdown -> HTML (headers + paragraphs)."""
    out: list[str] = []
    for line in md.splitlines():
        if line.startswith("# "):
            out.append(f"<h2>{line[2:]}</h2>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    return "<div class='mr-report'>" + "".join(out) + "</div>"


def _totals(agg: dict[str, dict]) -> dict:
    """Blended KPIs across all channels for the report's hero strip.

    Spend (and the costs derived from it) is media channels only, matching the
    tracker sheet's own total; funnel counts still include every channel."""
    media = {ch: a for ch, a in agg.items() if ch not in config.NON_MEDIA_CHANNELS}
    spend = sum(a["spend"] for a in media.values())
    leads = sum(a["leads"] for a in agg.values())
    qualified = sum(a["qualified_leads"] for a in agg.values())
    booked = sum(a["demos_booked"] for a in agg.values())
    completed = sum(a["demos_completed"] for a in agg.values())
    div = lambda n, d: round(n / d, 2) if d else None
    return {
        "spend": round(spend, 2),
        "leads": leads,
        "qualified_leads": qualified,
        "demos_booked": booked,
        "demos_completed": completed,
        "cost_per_demo_booked": div(spend, booked),
        "cost_per_demo_completed": div(spend, completed),
    }


def _enrich(channel: str, agg: dict, targets: dict) -> dict:
    agg["goal"] = goals.goal_dict(channel, targets)
    agg["status"] = goals.status_for(channel, agg, targets)
    return agg


# Plain-language summary per flagged metric (so the report shows "14 campaigns
# over the $600 ceiling", not 14 near-identical lines). Built per call because
# every threshold figure is user-editable.
def _flag_labels(targets: dict) -> dict[str, str]:
    t = goals.thresholds(targets)
    return {
        "cost_per_qualified_lead": f"over the ${t['cost_per_qualified_lead_red']:,.0f} cost-per-qualified-lead ceiling",
        "cac": f"over the ${t['cac_red']:,.0f} CAC ceiling",
        "spend_no_demo": f"spending ${t['spend_no_demo_limit']:,.0f}+ with no demo booked",
        "cost_per_booking": f"over the ${t['cost_per_booking_flag']:,.0f} cost-per-booking target",
        "conversion_drop": f"with a {t['conversion_drop_pct'] * 100:.0f}%+ drop in conversion",
        "channel_goal": "over the channel cost-per-demo-booked goal",
    }


def _money_in(text: str) -> float:
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", text)]
    return max(nums) if nums else 0.0


def _flag_summary(flags: list[dict], targets: dict) -> list[dict]:
    """Group raw flags by metric into one summarized line each."""
    groups: dict[tuple, dict] = {}
    for f in flags:
        key = (f.get("metric"), f["level"])
        g = groups.setdefault(key, {"metric": f.get("metric"), "level": f["level"], "count": 0, "worst": 0.0})
        g["count"] += 1
        g["worst"] = max(g["worst"], _money_in(f["message"]))
    labels = _flag_labels(targets)
    out = []
    for (metric, level), g in groups.items():
        label = labels.get(metric, (metric or "issue").replace("_", " "))
        worst = f" (worst ${g['worst']:,.0f})" if g["worst"] else ""
        out.append({
            "metric": metric,
            "level": level,
            "count": g["count"],
            "text": f"{g['count']} campaign{'s' if g['count'] != 1 else ''} {label}{worst}",
        })
    out.sort(key=lambda x: (x["level"] != "red", -x["count"]))
    return out


# --- reporting period ---------------------------------------------------------

def _period_window(kind: str, today: date) -> tuple[date, date]:
    """The window a campaign report covers. Reports always run through
    YESTERDAY (a July 9 daily report covers July 1–8): today's sheet state is
    still moving while the day is in progress."""
    end = today - timedelta(days=1)
    if kind == "weekly_summary":
        return end - timedelta(days=6), end
    if kind == "quarterly_summary":
        return date(end.year, ((end.month - 1) // 3) * 3 + 1, 1), end
    # daily / monthly / threshold_alert: month-to-date
    return end.replace(day=1), end


class PeriodError(ValueError):
    """A user-facing problem with an explicitly requested report period."""


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def _month_end(year: int, month: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return nxt - timedelta(days=1)


def _explicit_window(kind: str, period: str, today: date) -> tuple[date, date, str]:
    """Window + human name for an explicitly requested period ('2026-07' /
    '2026-Q2'). The end is clamped to yesterday while the period is still in
    progress (reports always run through yesterday)."""
    yesterday = today - timedelta(days=1)
    if kind == "monthly_summary":
        m = _MONTH_RE.match(period)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise PeriodError(f"'{period}' is not a month (expected YYYY-MM).")
        y, mo = int(m.group(1)), int(m.group(2))
        start, end = date(y, mo, 1), _month_end(y, mo)
        name = start.strftime("%B %Y")
    elif kind == "quarterly_summary":
        q = _QUARTER_RE.match(period)
        if not q:
            raise PeriodError(f"'{period}' is not a quarter (expected YYYY-Q1..Q4).")
        y, qn = int(q.group(1)), int(q.group(2))
        start, end = date(y, 3 * qn - 2, 1), _month_end(y, 3 * qn)
        name = f"Q{qn} {y}"
    else:
        raise PeriodError(f"'{kind}' reports don't take a period.")
    if start > yesterday:
        raise PeriodError(f"No tracker data for {name} yet.")
    return start, min(end, yesterday), name


def _period_label(start: date, end: date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
    return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"


def _clip_to_period(metrics: list, start: date, end: date, fallback: bool = True) -> list:
    """Keep the months the window touches (the tracker is a monthly grid).
    With ``fallback`` (default reports only), falls back to the latest month
    on/before the window's end so a report never silently aggregates pre-filled
    future retainer months or goes empty; explicit periods pass fallback=False
    and handle emptiness honestly upstream."""
    lo, hi = (start.year, start.month), (end.year, end.month)
    kept = [m for m in metrics if lo <= (m.date.year, m.date.month) <= hi]
    if not kept and fallback and metrics:
        past = {(m.date.year, m.date.month) for m in metrics
                if (m.date.year, m.date.month) <= hi}
        if past:
            ym = max(past)
            kept = [m for m in metrics if (m.date.year, m.date.month) == ym]
    return kept


# --- per-vendor rollups, red flags, insights -----------------------------------

def _vendor_rollup(vendor_metrics: dict[str, list]) -> list[dict]:
    div = lambda n, d: round(n / d, 2) if d else None
    out = []
    for vendor, ms in (vendor_metrics or {}).items():
        spend = round(sum(m.spend for m in ms), 2)
        leads = sum(m.leads for m in ms)
        ql = sum(m.qualified_leads for m in ms)
        booked = sum(m.demos_booked for m in ms)
        completed = sum(m.demos_completed for m in ms)
        out.append({
            "vendor": vendor,
            "spend": spend,
            "leads": leads,
            "qualified_leads": ql,
            "demos_booked": booked,
            "demos_completed": completed,
            "cost_per_qualified_lead": div(spend, ql),
            "cost_per_demo_booked": div(spend, booked),
            "cost_per_demo_completed": div(spend, completed),
        })
    out.sort(key=lambda v: v["spend"], reverse=True)
    return out


def _vendor_red_flags(vendors: list[dict], targets: dict) -> list[dict]:
    """Which vendors are on a red flag and exactly why (editable thresholds)."""
    t = goals.thresholds(targets)
    out = []
    for v in vendors:
        reasons = []
        if v["spend"] >= t["spend_no_demo_limit"] and v["demos_booked"] == 0:
            reasons.append(f"${v['spend']:,.0f} spent with no demo booked")
        if v["spend"] > 0 and v["leads"] == 0:
            reasons.append(f"${v['spend']:,.0f} spent with zero leads")
        cpql = v["cost_per_qualified_lead"]
        if cpql is not None and cpql >= t["cost_per_qualified_lead_red"]:
            reasons.append(
                f"cost per qualified lead ${cpql:,.0f} at/above the "
                f"${t['cost_per_qualified_lead_red']:,.0f} red line")
        cac = v["cost_per_demo_completed"]
        if cac is not None and cac >= t["cac_red"]:
            reasons.append(f"CAC ${cac:,.0f} at/above the ${t['cac_red']:,.0f} red line")
        if reasons:
            out.append({"vendor": v["vendor"], "reasons": reasons})
    return out


def _fallback_vendor_insights(vendors: list[dict], red_map: dict[str, list[str]],
                              targets: dict) -> list[dict]:
    """Deterministic 3-insights / 3-actions per vendor (offline or LLM failure)."""
    t = goals.thresholds(targets)
    lo, hi = t["cost_per_qualified_lead_target_low"], t["cost_per_qualified_lead_target_high"]
    out = []
    for v in vendors:
        cpql = v["cost_per_qualified_lead"]
        show = round(v["demos_completed"] / v["demos_booked"] * 100) if v["demos_booked"] else None
        insights = [
            f"${v['spend']:,.0f} spend produced {v['qualified_leads']} qualified leads "
            f"from {v['leads']} total leads.",
            (f"Cost per qualified lead is ${cpql:,.0f} vs the ${lo:,.0f}–${hi:,.0f} target."
             if cpql is not None else "No qualified leads yet, so cost per qualified lead can't be measured."),
            (f"{v['demos_booked']} demos booked and {v['demos_completed']} completed"
             + (f" ({show}% show rate)." if show is not None else ".")),
        ]
        actions = []
        if red_map.get(v["vendor"]):
            actions.append(f"Address the red flag: {red_map[v['vendor']][0]}.")
        if cpql is not None and cpql > hi:
            actions.append(f"Rework the worst ad sets to pull CPQL back under ${hi:,.0f}.")
        elif cpql is None and v["spend"] > 0:
            actions.append("Audit targeting and lead capture — spend is running without qualified leads.")
        else:
            actions.append("Keep the current mix; efficiency is inside target.")
        if v["demos_booked"] == 0:
            actions.append("Investigate the lead-to-demo handoff; nothing is being booked.")
        elif show is not None and show < 60:
            actions.append("Tighten demo reminders/follow-ups to lift the show rate.")
        else:
            actions.append("Hold demo follow-up cadence; booking flow is working.")
        actions.append("Review spend pacing against the monthly budget before the next pull.")
        out.append({"vendor": v["vendor"], "insights": insights[:3], "actions": actions[:3]})
    return out


def _vendor_insights(vendors: list[dict], red_flags: list[dict],
                     targets: dict) -> tuple[list[dict], str | None]:
    """3 concise insights + 3 action points per vendor: LLM online, deterministic
    fallback offline — output shape is identical either way.

    Returns ``(rows, fallback_reason)``. Because the shapes are identical, the
    reason is the *only* thing that distinguishes a model read from the canned
    one downstream, so it is never dropped: None means genuinely model-written.
    """
    if not vendors:
        return [], None
    red_map = {r["vendor"]: r["reasons"] for r in red_flags}
    prompt = analysis.load_prompt("vendor_insights").replace(
        "{data}", json.dumps({"vendors": vendors, "red_flags": red_flags}, default=str))
    raw, reason = analysis.llm_json_result(prompt)
    if isinstance(raw, list):
        known = {v["vendor"] for v in vendors}
        rows = []
        for r in raw:
            if not isinstance(r, dict) or r.get("vendor") not in known:
                continue
            ins = [str(s) for s in (r.get("insights") or []) if str(s).strip()][:3]
            act = [str(s) for s in (r.get("actions") or []) if str(s).strip()][:3]
            if len(ins) == 3 and len(act) == 3:
                rows.append({"vendor": r["vendor"], "insights": ins, "actions": act})
        if len(rows) == len(vendors):
            return sorted(rows, key=lambda r: [v["vendor"] for v in vendors].index(r["vendor"])), None
        reason = (f"model output rejected by validation: {len(rows)} of "
                  f"{len(vendors)} vendors came back with 3 insights + 3 actions")
    return _fallback_vendor_insights(vendors, red_map, targets), (
        reason or "the model returned no usable vendor insights")


# Official fields the headline strip may take from the sheet's Overall tab
# (decision 2026-07-27, extended same day to every team-level KPI: the console
# must show the cells the team reads — the roll-up aggregates ledger/raw
# sources with no vendor tab, so a vendor-tab sum can never reproduce them).
_OFFICIAL_TOTAL_FIELDS = (
    "spend", "leads", "qualified_leads", "demos_booked", "demos_completed",
    "qual_demos_booked", "budget", "services_sold",
)


def _official_totals_for(ds: dict, metrics: list) -> dict[str, float] | None:
    """Per-field official sums for the months these metrics cover. A field is
    returned only when every covered month reports it, so a partial figure is
    never passed off as the official one. Falls back to a spend-only map from
    runs persisted before the full-totals read existed."""
    official = ds.get("official_totals") or {}
    if not official:
        official = {k: {"spend": v} for k, v in (ds.get("official_spend") or {}).items()}
    if not official or not metrics:
        return None
    keys = {f"{m.date.year:04d}-{m.date.month:02d}" for m in metrics}
    if not keys <= set(official):
        return None
    out = {}
    for field in _OFFICIAL_TOTAL_FIELDS:
        if all(field in official[k] for k in keys):
            out[field] = round(sum(official[k][field] for k in keys), 2)
    return out or None


def _apply_official_totals(totals: dict, off: dict[str, float]) -> dict:
    """Swap the headline figures to the sheet's official ones; keep each
    vendor-tab sum alongside so any drift is visible, never silent. Derived
    costs are recomputed from the official inputs — which makes them equal the
    sheet's own derived rows by construction."""
    if "spend" in off:
        totals["spend_computed"] = totals.get("spend")
        totals["spend"] = off["spend"]
        totals["spend_delta"] = round(off["spend"] - (totals["spend_computed"] or 0), 2)
        totals["spend_source"] = "sheet_overall"
    for field in ("leads", "qualified_leads", "demos_booked", "demos_completed"):
        if field in off:
            totals[f"{field}_computed"] = totals.get(field)
            totals[field] = int(off[field])
    div = lambda n, d: round(n / d, 2) if d else None
    totals["cost_per_demo_booked"] = div(totals.get("spend"), totals.get("demos_booked"))
    totals["cost_per_demo_completed"] = div(totals.get("spend"), totals.get("demos_completed"))
    return totals


def _apply_lead_quality(structured: dict, ds: dict) -> None:
    """Fold the lead sheet's per-vendor outcome picture into a campaign report:
    a ``lead_quality`` section for the window's months, and the lead flags
    merged into ``red_flag_vendors`` (matched campaigns under their tracker
    vendor name, unmatched ones under the campaign name itself)."""
    lead = ds.get("lead_summary")
    if not lead or not lead.get("months"):
        return
    keys = ds.get("lead_month_keys")
    if keys is None:
        keys = [lead["latest_month"]] if lead.get("latest_month") else []
    keys = [k for k in keys if k in lead["months"]]
    if not keys:
        return
    structured["lead_quality"] = {
        "months": {k: lead["months"][k] for k in keys},
        "unmatched_campaigns": lead.get("unmatched_campaigns") or [],
    }
    extra = lead_analysis.red_flag_entries(lead, keys)
    if extra:
        merged = {r["vendor"]: r for r in structured.get("red_flag_vendors") or []}
        for e in extra:
            r = merged.setdefault(e["vendor"], {"vendor": e["vendor"], "reasons": []})
            r["reasons"].extend(m for m in e["reasons"] if m not in r["reasons"])
        structured["red_flag_vendors"] = list(merged.values())


def _month_keys(start: date, end: date) -> list[str]:
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


def _campaign_structured(ds: dict, targets: dict) -> dict:
    # ``targets`` is resolved ONCE per report, by the caller, for the workspace
    # the report belongs to. Every threshold/goal read below takes it as an
    # argument; each one used to re-read the mr_config/targets document — and
    # that document was the same one for every workspace.
    metrics = ds.get("metrics", [])
    previous = ds.get("previous_metrics")
    current_agg = cr.aggregate_by_channel(metrics)
    # A consolidated "Total" block (from an "All" roll-up view) is the totals,
    # not a channel — pull it out so it isn't double-counted in the KPI strip.
    total_block = current_agg.pop("Total", None)
    for channel, agg in current_agg.items():
        _enrich(channel, agg, targets)
    totals = _enrich("Total", total_block, targets) if total_block is not None else _totals(current_agg)
    official = _official_totals_for(ds, metrics)
    if official is not None:
        totals = _apply_official_totals(totals, official)
    flags = [f.__dict__ for f in cr.flag_all(metrics, ds.get("prior"), targets)]
    structured = {
        "channels": current_agg,
        "totals": totals,
        "top_utm": cr.top_utm_sources(metrics),
        "flags": flags,
        "flag_summary": _flag_summary(flags, targets),
    }
    # Per-vendor layer (present when the dataset keeps vendor identity).
    vendor_metrics = ds.get("vendor_metrics")
    if vendor_metrics:
        vendors = _vendor_rollup(vendor_metrics)
        red = _vendor_red_flags(vendors, targets)
        structured["vendors"] = vendors
        structured["red_flag_vendors"] = red
        if ds.get("with_vendor_insights"):
            rows, vi_reason = _vendor_insights(vendors, red, targets)
            structured["vendor_insights"] = rows
            # Identical shape either way — these two are the only tell.
            structured["vendor_insights_ai"] = vi_reason is None
            structured["vendor_insights_fallback_reason"] = vi_reason
    _apply_lead_quality(structured, ds)
    if previous is not None:
        structured["week_over_week"] = cr.week_over_week(
            current_agg, cr.aggregate_by_channel(previous)
        )
    return structured


def _structured(kind: str, ds: dict, targets: dict) -> dict:
    if kind in CAMPAIGN_KINDS:
        return _campaign_structured(ds, targets)
    if kind == "utm_attribution":
        leads = ds.get("leads", [])
        return {
            "attribution": fa.attribution(leads),
            "conversion": fa.conversion_by_channel(leads),
            "best_practice_areas": fa.best_practice_areas(leads),
            "dropoff": fa.dropoff_points(leads),
            "low_booking_channels": fa.low_booking_channels(leads),
        }
    if kind == "competitor_digest":
        return {
            "competitors": [
                {"competitor": r["competitor"], "changed": r["changed"], "summary": r["summary"]}
                for r in ds.get("competitor_results", [])
            ]
        }
    if kind in ("opportunity_report", "icp_signal"):
        opps = ds.get("opportunities", [])
        today = ds.get("today", date.today())
        return {
            "ranked": [o.__dict__ for o in orr.rank(opps)],
            "stale": [o.name for o in orr.stale_outreach(opps, today)],
            "placement_issues": [o.name for o in orr.placement_issues(opps)],
        }
    if kind == "daily_movement":
        return {"vendors": ds.get("snapshot_deltas", [])}
    return {}


def _narration_input(kind: str, s: dict) -> dict:
    """Compact, relevant slice of the structured data for the LLM — keeps the
    read focused and cheap (no giant raw-flag list)."""
    if kind in CAMPAIGN_KINDS:
        keep = ("spend", "demos_booked", "demos_completed",
                "cost_per_demo_booked", "cost_per_demo_completed",
                "cost_per_qualified_lead", "cac", "goal")
        return {
            "period": s.get("period"),
            "totals": {k: (s.get("totals") or {}).get(k) for k in ("spend", "demos_completed", "cost_per_demo_completed", "qualified_leads")},
            "channels": {ch: {k: a.get(k) for k in keep} for ch, a in (s.get("channels") or {}).items()},
            "issues": s.get("flag_summary", []),
            "red_flag_vendors": s.get("red_flag_vendors", []),
        }
    if kind == "daily_movement":
        return {
            "vendors": [
                {
                    "vendor": v.get("vendor"),
                    "days": v.get("days"),
                    "corrected": v.get("corrected"),
                    "moves": {p: f.get("delta")
                              for p, f in (v.get("blocks", {}).get("team_overall", {}).get("additive") or {}).items()
                              if f.get("delta")},
                }
                for v in s.get("vendors", [])
            ]
        }
    return s


def _markdown(kind: str, structured: dict) -> tuple[str, dict]:
    """``(markdown, narration)`` where ``narration`` is the honest-failure pair
    from :func:`analysis.narrate_result`. The markdown is what downloads as a
    client-facing PDF, so the caller must stamp the pair onto the report."""
    title = kind.replace("_", " ").title()
    narration = analysis.narrate_result(kind, _narration_input(kind, structured))
    return f"# {title}\n\n{narration['text']}", narration


def build(kind: str, dataset: dict, user_id: str, period: str | None = None) -> dict:
    """Build, persist, and return one report deliverable.

    ``period`` (monthly/quarterly only) pins the report to an explicit month
    ('2026-07') or quarter ('2026-Q2') instead of today's default window.
    Explicit periods never substitute another month's data — an empty window
    raises :class:`PeriodError`.

    Every threshold in the report is judged against ``user_id``'s OWN targets,
    resolved once here and handed down. The report is stamped with, persisted
    under and only ever read back by that user, so anyone else's figures would
    flag campaigns nobody can explain."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind: {kind}")
    if kind in BOARD_KINDS:
        # Not a "not implemented": this function's contract is one period and
        # a narrative, and the board report has two periods and no narrative.
        # Falling through here would have produced a report carrying an empty
        # ledger and an LLM paragraph about it.
        raise ValueError(
            f"'{kind}' is not built here - use build_board_report(), which takes "
            "two periods and keys the result on board_report.cache_key()")
    targets = goals.get_targets(user_id)
    if period is not None and kind not in ("monthly_summary", "quarterly_summary"):
        raise PeriodError(f"'{kind}' reports don't take a period.")
    if kind in CAMPAIGN_KINDS:
        today = dataset.get("today") or date.today()
        if period is not None:
            try:
                start, end, name = _explicit_window(kind, period, today)
            except PeriodError:
                raise
            except ValueError:
                # date() overflow for degenerate years (0000, 9999-12) — same
                # user answer as any malformed period.
                raise PeriodError(f"'{period}' is not a valid period.")
            metrics = _clip_to_period(dataset.get("metrics", []), start, end, fallback=False)
            if not metrics:
                raise PeriodError(f"No tracker data for {name}.")
            vendor_metrics = {
                v: kept
                for v, ms in (dataset.get("vendor_metrics") or {}).items()
                if (kept := _clip_to_period(ms, start, end, fallback=False))
            }
        else:
            start, end = _period_window(kind, today)
            metrics = _clip_to_period(dataset.get("metrics", []), start, end)
            # Per VENDOR the fallback is a lie, not a kindness: a vendor with no
            # activity in the window was shown its latest earlier month under
            # the window's heading. On 2026-08-15 two vendor cards printed June
            # figures beneath "Aug 1–13, 2026". Absent from the window means
            # absent from the table; the report-level fallback above still keeps
            # the deliverable from coming out empty.
            vendor_metrics = {
                v: kept
                for v, ms in (dataset.get("vendor_metrics") or {}).items()
                if (kept := _clip_to_period(ms, start, end, fallback=False))
            }
        dataset = {
            **dataset,
            "metrics": metrics,
            "vendor_metrics": vendor_metrics or None,
            "with_vendor_insights": True,
            "lead_month_keys": _month_keys(start, end),
        }
        structured = _structured(kind, dataset, targets)
        structured["period"] = {
            "start": start.isoformat(), "end": end.isoformat(),
            "label": _period_label(start, end),
            "basis": "Tracker figures are month-to-date cumulatives; the report reads the months this window touches.",
        }
    else:
        structured = _structured(kind, dataset, targets)
    markdown, narration = _markdown(kind, structured)
    # Provenance for the whole deliverable. The narrative and the vendor
    # insights are both LLM paths that degrade to hand-written templates; if
    # EITHER degraded the report is not AI-written, and the PDF/UI must say so.
    reasons = [r for r in (
        narration["fallback_reason"],
        structured.get("vendor_insights_fallback_reason"),
    ) if r]
    report = {
        "id": runs.new_run_id(),
        "kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "agent_id": "a6",
        "sources": dataset.get("sources", []),
        "structured": structured,
        "markdown": markdown,
        "html": _md_to_html(markdown),
        "ai": not reasons,
        "fallback_reason": "; ".join(reasons) or None,
        "narrative_ai": narration["ai"],
        "narrative_fallback_reason": narration["fallback_reason"],
    }
    runs.save_run(report)
    return report


def overview(ds: dict, user_id: object) -> dict:
    """Live dashboard state for /mr/overview — latest-month KPIs vs goals.

    Anchored to the latest month NOT after today: vendor tabs pre-fill retainer
    fees into future months (spend, no activity), which would otherwise make the
    dashboard land on an empty September. Pure read: reuses the campaign
    aggregation but never persists a run.

    ``user_id`` is the workspace whose targets the traffic lights and flag lines
    are judged against — the same workspace ``ds`` was loaded for."""
    metrics = ds.get("metrics", [])
    sources = ds.get("sources", [])
    if not metrics:
        return {"has_data": False, "month": None, "totals": None,
                "channels": {}, "flag_summary": [], "lead_quality": None,
                "sources": sources}
    today = ds.get("today") or date.today()
    months = {(m.date.year, m.date.month) for m in metrics}
    current = {ym for ym in months if ym <= (today.year, today.month)}
    latest = max(current) if current else min(months)
    month_key = f"{latest[0]:04d}-{latest[1]:02d}"
    month_metrics = [m for m in metrics if (m.date.year, m.date.month) == latest]
    s = _campaign_structured({**ds, "metrics": month_metrics, "vendor_metrics": None,
                              "lead_month_keys": [month_key]},
                             goals.get_targets(user_id))
    lead_block = ((s.get("lead_quality") or {}).get("months") or {}).get(month_key)
    flag_summary = s["flag_summary"]
    if lead_block:
        flag_summary = flag_summary + lead_analysis.flag_summary(lead_block)
    return {
        "has_data": True,
        "month": month_key,
        "totals": s["totals"],
        "channels": s["channels"],
        "flag_summary": flag_summary,
        "lead_quality": lead_block,
        "sources": sources,
    }


# --- picker periods -------------------------------------------------------------

def available_periods(dataset: dict) -> dict:
    """Months/quarters the Reports picker can offer: metric months on/before the
    month containing yesterday (vendor tabs pre-fill future retainer months),
    newest first. 'Current' is the month/quarter containing yesterday."""
    today = dataset.get("today") or date.today()
    yesterday = today - timedelta(days=1)
    cur_ym = (yesterday.year, yesterday.month)
    cur_q = (yesterday.year, (yesterday.month - 1) // 3 + 1)
    months = sorted(
        {(m.date.year, m.date.month) for m in dataset.get("metrics", [])
         if (m.date.year, m.date.month) <= cur_ym},
        reverse=True,
    )
    quarters = sorted({(y, (mo - 1) // 3 + 1) for y, mo in months}, reverse=True)
    return {
        "months": [
            {"period": f"{y:04d}-{mo:02d}", "label": date(y, mo, 1).strftime("%B %Y"),
             "current": (y, mo) == cur_ym}
            for y, mo in months
        ],
        "quarters": [
            {"period": f"{y:04d}-Q{q}", "label": f"Q{q} {y}",
             "current": (y, q) == cur_q}
            for y, q in quarters
        ],
    }

# --- board report ------------------------------------------------------------
# Where board_report.py's catalog and roll-up meet the data the agent actually
# holds. That module is pure and does no I/O; this is the only place that hands
# it the sheet's parsed figures, and the only place that persists what it
# returns.

#: The two kinds :func:`build` does not narrate. They sit in :data:`KINDS` so
#: they list, persist and read back through the one run rail, but the board
#: report is the sheet's own arithmetic and a model sentence over it would be a
#: second, unreconcilable account of the same numbers.
BOARD_KINDS = ("board_report", "board_report_comparison")

#: The kinds :func:`build` does narrate. A test sweeping "every report kind"
#: wants this, not :data:`KINDS`.
NARRATED_KINDS = tuple(k for k in KINDS if k not in BOARD_KINDS)

#: Stated on every board report, because a reader who knows the template will
#: look for its channel table and needs to know why it is not there.
#:
#: ``board_report.roll_up`` computes the "Other / untracked" row from a
#: per-channel ``{spend, revenue_amount_sold, revenue_clients}`` feed. Three
#: candidates were checked on 2026-09-05 and none can supply one:
#:
#: * ``schemas.CampaignMetric`` - the vendor-tab shape every report reads - has
#:   spend and funnel counts, and no revenue or client field at all.
#: * The roll-up tab DOES carry a channel sub-block, and exactly one: "WEBSITE",
#:   rows 83-148, with its own revenue and revenue-client rows.
#:   ``sheets_source.fetch_official_totals`` reads ``rows[:120]``, so the report
#:   path never sees them.
#: * ``snapshots.py``'s ``_CHANNEL_MAP`` already maps those rows - and that
#:   module's routes are WORKSPACE_SHARED, so importing it would pull that
#:   scoping into this TENANT_SCOPED path.
#:
#: The last is why this is a note and not a fix, but it is not the strongest
#: reason. Wiring the one available block gives, on live Q1 data, an untracked
#: row reading 89.19% of spend and 100.00% of revenue untracked - because
#: Websites is a single non-media channel (``config.NON_MEDIA_CHANNELS``), not
#: the tracked-channel set the row is the residual of. The documented figures
#: are 7.0% and 12.0%. Publishing 100% beside a row that should read 7% is the
#: plausible-wrong-number failure this module exists to refuse, so the row stays
#: absent - never zero - until a real per-channel feed exists.
CHANNEL_SOURCE_STATUS = (
    "absent - a6 has no per-channel revenue/client feed this path can read. "
    "CampaignMetric carries spend and funnel counts only, and the roll-up tab's "
    "one channel sub-block (Websites) sits past the 120-row window "
    "fetch_official_totals reads. A spend-only channel table would make the "
    "residual read 100% untracked against a documented 7%, so the row is "
    "withheld rather than zeroed."
)

_YEAR_RE = re.compile(r"^(\d{4})$")


def board_report_enabled() -> bool:
    """Kill switch, **default off** - the board report ships dark.

    Mirrors ``sources_registry.multi_sheet_enabled()`` (env read on every call,
    so a deployment flips it without a code change and a test can set it),
    inverted: this one is opt-IN. Off, the route answers 404 - the same answer
    an unknown path gives, so a deployment that has not enabled the feature does
    not advertise that it exists.
    """
    return os.environ.get("MR_BOARD_REPORT", "0").strip().lower() in ("1", "true", "on")


def board_period(text: str) -> br.PeriodSpec:
    """``'2026-07'`` / ``'2026-Q2'`` / ``'2026'`` -> a :class:`PeriodSpec`.

    Deliberately wider than ``_explicit_window``'s month-or-quarter vocabulary.
    The board's two columns are only "column A" and "column B" - a month against
    a month and a year against a year both have to be expressible, and anything
    that hardcodes a quarter forecloses them. Anything unparseable is a
    user-facing error; no period is ever substituted for another.
    """
    t = (text or "").strip().upper()
    m = _MONTH_RE.match(t)
    if m and 1 <= int(m.group(2)) <= 12:
        return br.month_period(int(m.group(1)), int(m.group(2)))
    q = _QUARTER_RE.match(t)
    if q:
        return br.quarter_period(int(q.group(1)), int(q.group(2)))
    y = _YEAR_RE.match(t)
    if y:
        return br.year_period(int(y.group(1)))
    raise PeriodError(
        f"'{text}' is not a board-report period (expected YYYY-MM, YYYY-Qn or YYYY).")


def _capture_date(ds: dict) -> date:
    """The day ``ds``'s official figures were pulled from the sheet.

    Part of the idempotency key, so a fresh pull re-derives instead of serving
    the roll-up of a capture that has since been replaced. A run predating the
    stamp falls back to today, which is still idempotent within the day and
    never stale across one.
    """
    stamp = str(ds.get("official_captured_at") or "")[:10]
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return date.today()


def _absent_reason(metric, rollup) -> str:
    """Why one catalog row has no number for this period, in the reader's terms
    - so "absent" never has to be guessed at as "zero"."""
    if metric.formula is not None:
        missing = sorted(c for c in metric.formula.components()
                         if rollup.components.get(c) is None)
        if missing:
            return "the roll-up tab does not report " + ", ".join(missing)
        return f"its denominator ({metric.formula.denominator}) is zero for this period"
    return f"the roll-up tab does not report '{metric.source}' for this period"


def _coverage(columns: list) -> dict:
    """Which catalog rows each column could fill, and why the rest could not.
    ``columns`` is ``[(label, PeriodRollup), ...]``.

    Computed per request on purpose: "absent" is a property of the capture
    sitting in the store right now, not of the catalog. A capture pulled before
    the roll-up parser learned the revenue rows fills 13 of 38 - the 13 it
    publishes are correct and the other 25 are honestly missing, and without
    this block a thin capture reads exactly like a thin quarter.
    """
    out = []
    for label, r in columns:
        filled, reasons = [], {}
        for m in br.CATALOG:
            if r.value(m.key) is None:
                reasons[m.key] = _absent_reason(m, r)
            else:
                filled.append(m.key)
        out.append({
            "column": label,
            "period": r.period.key,
            "months": list(r.period.months),
            "filled": filled,
            "absent": sorted(reasons),
            "absent_reasons": reasons,
            "filled_count": len(filled),
            "metric_count": len(br.CATALOG),
        })
    return {"columns": out, "metric_count": len(br.CATALOG),
            "channel_reconciliation": CHANNEL_SOURCE_STATUS}


def _single_column(r, key: str) -> dict:
    """One period's board report: the ledger shape with one value column.

    Deliberately not ``compare(r, r)``. Comparing a period with itself prints a
    delta column of zeros, and a zero delta is a claim - "nothing moved" - that
    nobody made. One column is one column.
    """
    return {
        "generator": br.GENERATOR_VERSION,
        "cache_key": key,
        "columns": [r.period.label],
        "periods": [r.period.as_dict()],
        "groups": [g for g in br.GROUPS if any(m.group == g for m in br.CATALOG)],
        "rows": [
            {"key": m.key, "label": m.label, "group": m.group, "format": m.format,
             "polarity": m.polarity, "basis": m.basis, "value": r.value(m.key)}
            for m in br.CATALOG
        ],
        "components": dict(r.components),
        "channels": [c.as_dict() for c in r.channels],
        "untracked": [r.untracked.as_dict() if r.untracked else None],
        "gaps": list(r.gaps),
    }


def _cached_board_run(user_id: str, kind: str, key: str) -> dict | None:
    """A board run this workspace already derived for exactly this key.

    Scoped to ``user_id`` at the query, which is why the key itself does not
    have to carry the workspace: two tenants asking for the same quarter of the
    same capture hash identically and still cannot reach each other's run. Both
    filters are equality, so Firestore serves them from the automatic
    single-field indexes - no composite index, and no unbounded scan.
    """
    for run in runs.list_runs(user_id, kind=kind):
        if (run.get("structured") or {}).get("cache_key") == key:
            return run
    return None


def build_board_report(dataset: dict, user_id: str, *, period: str,
                       compare_to: str | None = None) -> dict:
    """Build - or serve from the store - one board report, persisted as a run.

    Same envelope, same ``mr_runs`` collection and the same ``user_id`` stamp as
    every other deliverable, so ``/mr/runs`` lists these and ``/mr/runs/{id}``
    reads them back through the ownership check already in place.

    The one thing the campaign kinds have no notion of, and this needs, is
    idempotency. The report is a pure function of (periods, capture date,
    generator version), so :func:`board_report.cache_key` is computed FIRST and
    a run already carrying that key is returned untouched instead of re-derived.
    A ``GENERATOR_VERSION`` bump changes the key, which makes it a cache
    invalidation rather than a stale read.

    ``compare_to`` present -> the two-column comparison; absent -> one column.
    Nothing here is quarter-shaped: any two of month / quarter / year compare.
    """
    official = dataset.get("official_totals") or {}
    if not official:
        raise PeriodError(
            "No official roll-up figures have been pulled yet - the board report "
            "reads the Overall tab, so run a sheet pull first.")

    spec_a = board_period(period)
    spec_b = board_period(compare_to) if compare_to else None
    if spec_b is not None and spec_b.key == spec_a.key:
        raise PeriodError(
            f"A board comparison needs two different periods (both were {spec_a.key}).")
    kind = "board_report_comparison" if spec_b is not None else "board_report"

    captured_on = _capture_date(dataset)
    key = br.cache_key([s for s in (spec_a, spec_b) if s is not None], captured_on)
    cached = _cached_board_run(user_id, kind, key)
    if cached is not None:
        return {**cached, "reused": True}

    # ``channels=`` is deliberately not passed: see CHANNEL_SOURCE_STATUS. The
    # roll-up then leaves ``untracked`` None and the channel table empty, which
    # is the absent state and not a zeroed one.
    rollup_a = br.roll_up(spec_a, official)
    columns = [(spec_a.label, rollup_a)]
    if spec_b is not None:
        rollup_b = br.roll_up(spec_b, official)
        columns.append((spec_b.label, rollup_b))
        ledger = br.compare(rollup_a, rollup_b, captured_on=captured_on)
        structured = {**ledger.as_dict(), "r_array": ledger.to_r_array()}
    else:
        structured = _single_column(rollup_a, key)
    structured["captured_on"] = captured_on.isoformat()
    structured["coverage"] = _coverage(columns)

    report = {
        "id": runs.new_run_id(),
        "kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "agent_id": "a6",
        "sources": dataset.get("sources", []),
        "structured": structured,
        # The honest-provenance pair, and this is not a degradation: no model
        # writes any part of this deliverable, so ai=True would be the lie. The
        # reason names the design rather than a failure.
        "ai": False,
        "fallback_reason": (
            "the board report is the roll-up tab's own figures - no model writes "
            "any part of it"),
    }
    runs.save_run(report)
    return {**report, "reused": False}
