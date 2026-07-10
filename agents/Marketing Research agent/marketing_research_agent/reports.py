"""Report builders — the seven deliverables (requirements §4).

Each ``build`` produces a Report dict with structured data + Markdown + HTML and
persists it as a run. The structured layer comes from the feature modules; the
narrative layer comes from the analysis brain (LLM online, deterministic
offline).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import analysis, goals, runs
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
    """Blended KPIs across all channels for the report's hero strip."""
    spend = sum(a["spend"] for a in agg.values())
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


def _enrich(channel: str, agg: dict) -> dict:
    agg["goal"] = goals.goal_dict(channel)
    agg["status"] = goals.status_for(channel, agg)
    return agg


# Plain-language summary per flagged metric (so the report shows "14 campaigns
# over the $600 ceiling", not 14 near-identical lines). Built per call because
# every threshold figure is user-editable.
def _flag_labels() -> dict[str, str]:
    t = goals.thresholds()
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


def _flag_summary(flags: list[dict]) -> list[dict]:
    """Group raw flags by metric into one summarized line each."""
    groups: dict[tuple, dict] = {}
    for f in flags:
        key = (f.get("metric"), f["level"])
        g = groups.setdefault(key, {"metric": f.get("metric"), "level": f["level"], "count": 0, "worst": 0.0})
        g["count"] += 1
        g["worst"] = max(g["worst"], _money_in(f["message"]))
    labels = _flag_labels()
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


def _period_label(start: date, end: date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
    return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"


def _clip_to_period(metrics: list, start: date, end: date) -> list:
    """Keep the months the window touches (the tracker is a monthly grid).
    Falls back to the latest month on/before the window's end so a report never
    silently aggregates pre-filled future retainer months or goes empty."""
    lo, hi = (start.year, start.month), (end.year, end.month)
    kept = [m for m in metrics if lo <= (m.date.year, m.date.month) <= hi]
    if not kept and metrics:
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


def _vendor_red_flags(vendors: list[dict]) -> list[dict]:
    """Which vendors are on a red flag and exactly why (editable thresholds)."""
    t = goals.thresholds()
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


def _fallback_vendor_insights(vendors: list[dict], red_map: dict[str, list[str]]) -> list[dict]:
    """Deterministic 3-insights / 3-actions per vendor (offline or LLM failure)."""
    t = goals.thresholds()
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


def _vendor_insights(vendors: list[dict], red_flags: list[dict]) -> list[dict]:
    """3 concise insights + 3 action points per vendor: LLM online, deterministic
    fallback offline — output shape is identical either way."""
    if not vendors:
        return []
    red_map = {r["vendor"]: r["reasons"] for r in red_flags}
    prompt = analysis.load_prompt("vendor_insights").replace(
        "{data}", json.dumps({"vendors": vendors, "red_flags": red_flags}, default=str))
    raw = analysis.llm_json(prompt)
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
            return sorted(rows, key=lambda r: [v["vendor"] for v in vendors].index(r["vendor"]))
    return _fallback_vendor_insights(vendors, red_map)


def _campaign_structured(ds: dict) -> dict:
    metrics = ds.get("metrics", [])
    previous = ds.get("previous_metrics")
    current_agg = cr.aggregate_by_channel(metrics)
    # A consolidated "Total" block (from an "All" roll-up view) is the totals,
    # not a channel — pull it out so it isn't double-counted in the KPI strip.
    total_block = current_agg.pop("Total", None)
    for channel, agg in current_agg.items():
        _enrich(channel, agg)
    totals = _enrich("Total", total_block) if total_block is not None else _totals(current_agg)
    flags = [f.__dict__ for f in cr.flag_all(metrics, ds.get("prior"))]
    structured = {
        "channels": current_agg,
        "totals": totals,
        "top_utm": cr.top_utm_sources(metrics),
        "flags": flags,
        "flag_summary": _flag_summary(flags),
    }
    # Per-vendor layer (present when the dataset keeps vendor identity).
    vendor_metrics = ds.get("vendor_metrics")
    if vendor_metrics:
        vendors = _vendor_rollup(vendor_metrics)
        red = _vendor_red_flags(vendors)
        structured["vendors"] = vendors
        structured["red_flag_vendors"] = red
        if ds.get("with_vendor_insights"):
            structured["vendor_insights"] = _vendor_insights(vendors, red)
    if previous is not None:
        structured["week_over_week"] = cr.week_over_week(
            current_agg, cr.aggregate_by_channel(previous)
        )
    return structured


def _structured(kind: str, ds: dict) -> dict:
    if kind in CAMPAIGN_KINDS:
        return _campaign_structured(ds)
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


def _markdown(kind: str, structured: dict) -> str:
    title = kind.replace("_", " ").title()
    narrative = analysis.narrate(kind, _narration_input(kind, structured))
    return f"# {title}\n\n{narrative}"


def build(kind: str, dataset: dict, user_id: str) -> dict:
    """Build, persist, and return one report deliverable."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind: {kind}")
    if kind in CAMPAIGN_KINDS:
        today = dataset.get("today") or date.today()
        start, end = _period_window(kind, today)
        dataset = {
            **dataset,
            "metrics": _clip_to_period(dataset.get("metrics", []), start, end),
            "vendor_metrics": {
                v: _clip_to_period(ms, start, end)
                for v, ms in (dataset.get("vendor_metrics") or {}).items()
            } or None,
            "with_vendor_insights": True,
        }
        structured = _structured(kind, dataset)
        structured["period"] = {
            "start": start.isoformat(), "end": end.isoformat(),
            "label": _period_label(start, end),
            "basis": "Tracker figures are month-to-date cumulatives; the report reads the months this window touches.",
        }
    else:
        structured = _structured(kind, dataset)
    markdown = _markdown(kind, structured)
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
    }
    runs.save_run(report)
    return report


def overview(ds: dict) -> dict:
    """Live dashboard state for /mr/overview — latest-month KPIs vs goals.

    Anchored to the latest month NOT after today: vendor tabs pre-fill retainer
    fees into future months (spend, no activity), which would otherwise make the
    dashboard land on an empty September. Pure read: reuses the campaign
    aggregation but never persists a run."""
    metrics = ds.get("metrics", [])
    sources = ds.get("sources", [])
    if not metrics:
        return {"has_data": False, "month": None, "totals": None,
                "channels": {}, "flag_summary": [], "sources": sources}
    today = ds.get("today") or date.today()
    months = {(m.date.year, m.date.month) for m in metrics}
    current = {ym for ym in months if ym <= (today.year, today.month)}
    latest = max(current) if current else min(months)
    month_metrics = [m for m in metrics if (m.date.year, m.date.month) == latest]
    s = _campaign_structured({**ds, "metrics": month_metrics, "vendor_metrics": None})
    return {
        "has_data": True,
        "month": f"{latest[0]:04d}-{latest[1]:02d}",
        "totals": s["totals"],
        "channels": s["channels"],
        "flag_summary": s["flag_summary"],
        "sources": sources,
    }
