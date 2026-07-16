"""Monthly trend rollups + deterministic desk insights (desk-board spec 2026-07-08).

Pure functions over rehydrated CampaignMetric lists — no I/O, no LLM. Insight
sentences carry exact figures so the panel is auditable against the charts."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime

PACE_WARN_PCT = -15.0
MOVER_PCT = 30.0
CPQL_OUTLIER_X = 2.0
MAX_INSIGHTS = 6


def _mkey(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def _mname(key: str) -> str:
    return datetime.strptime(key, "%Y-%m").strftime("%B")


def _blk() -> dict:
    return {"spend": 0.0, "leads": 0, "qualified_leads": 0, "demos_booked": 0, "demos_completed": 0}


def _add(b: dict, m) -> None:
    b["spend"] += m.spend or 0
    b["leads"] += int(m.leads or 0)
    b["qualified_leads"] += int(m.qualified_leads or 0)
    b["demos_booked"] += int(m.demos_booked or 0)
    b["demos_completed"] += int(m.demos_completed or 0)


def build(vendor_datasets: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    cutoff = (today.year, today.month)
    cur_key = _mkey(*cutoff)

    monthly: dict[str, dict] = defaultdict(_blk)
    channels: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_blk))
    vendors: list[dict] = []
    any_metrics = False

    for vd in vendor_datasets:
        vseries: dict[str, dict] = defaultdict(_blk)
        v_cur = _blk()
        for m in vd.get("metrics", []):
            if (m.date.year, m.date.month) > cutoff:
                continue  # pre-filled future retainer months
            any_metrics = True
            k = _mkey(m.date.year, m.date.month)
            _add(monthly[k], m)
            _add(channels[m.channel][k], m)
            _add(vseries[k], m)
            if k == cur_key:
                _add(v_cur, m)
        vendors.append({
            "vendor": vd["vendor"],
            "spend_mtd": round(v_cur["spend"], 2),
            "leads": v_cur["leads"],
            "qualified_leads": v_cur["qualified_leads"],
            "cpql": round(v_cur["spend"] / v_cur["qualified_leads"], 2) if v_cur["qualified_leads"] else None,
            "spend_series": [{"month": k, "spend": round(v["spend"], 2)} for k, v in sorted(vseries.items())],
        })

    if not any_metrics:
        return {"has_data": False, "month": None, "monthly": [], "channels": {}, "vendors": [], "insights": []}

    monthly_list = []
    for k in sorted(monthly):
        b = monthly[k]
        monthly_list.append({
            "month": k, "spend": round(b["spend"], 2), "leads": b["leads"],
            "qualified_leads": b["qualified_leads"], "demos_booked": b["demos_booked"],
            "demos_completed": b["demos_completed"],
            "cpql": round(b["spend"] / b["qualified_leads"], 2) if b["qualified_leads"] else None,
        })
    channels_out = {
        ch: [{"month": k, "spend": round(v["spend"], 2), "leads": v["leads"], "qualified_leads": v["qualified_leads"]}
             for k, v in sorted(per.items())]
        for ch, per in channels.items()
    }
    vendors.sort(key=lambda v: v["spend_mtd"], reverse=True)
    return {
        "has_data": True,
        "month": cur_key if cur_key in monthly else monthly_list[-1]["month"],
        "monthly": monthly_list,
        "channels": channels_out,
        "vendors": vendors,
        "insights": _insights(monthly_list, vendors, today),
    }


def _insights(monthly_list: list[dict], vendors: list[dict], today: date) -> list[dict]:
    out: list[dict] = []
    by_month = {m["month"]: m for m in monthly_list}
    cur = by_month.get(_mkey(today.year, today.month))
    py, pm = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    prev = by_month.get(_mkey(py, pm))

    # 1 — month-end pace vs last month's actual
    if cur and prev and today.day >= 3 and prev["spend"]:
        days = calendar.monthrange(today.year, today.month)[1]
        proj = cur["spend"] / today.day * days
        pct = (proj - prev["spend"]) / prev["spend"] * 100
        text = (f"{_mname(cur['month'])} pace: ${proj:,.0f} spend by month-end "
                f"vs ${prev['spend']:,.0f} in {_mname(prev['month'])} ({pct:+.0f}%)")
        qpct = None
        if prev["qualified_leads"]:
            qproj = cur["qualified_leads"] / today.day * days
            qpct = (qproj - prev["qualified_leads"]) / prev["qualified_leads"] * 100
            text += f"; qualified leads tracking {qproj:.0f} vs {prev['qualified_leads']} ({qpct:+.0f}%)"
        level = "warn" if pct <= PACE_WARN_PCT or (qpct is not None and qpct <= PACE_WARN_PCT) else "info"
        out.append({"kind": "pace", "level": level, "text": text})

    # 2 — vendor efficiency (current month)
    ranked = sorted((v for v in vendors if v["cpql"]), key=lambda v: v["cpql"])
    if len(ranked) >= 2:
        best, worst = ranked[0], ranked[-1]
        out.append({"kind": "efficiency", "level": "good",
                    "text": f"Best cost per qualified lead: {best['vendor']} at ${best['cpql']:,.0f} MTD"})
        if worst["cpql"] >= CPQL_OUTLIER_X * best["cpql"]:
            out.append({"kind": "efficiency", "level": "warn",
                        "text": (f"{worst['vendor']} pays ${worst['cpql']:,.0f} per qualified lead — "
                                 f"{worst['cpql'] / best['cpql']:.1f}x the best desk rate")})

    # 3 — MoM movers in the last complete month
    if prev:
        ppy, ppm = (py, pm - 1) if pm > 1 else (py - 1, 12)
        pp = by_month.get(_mkey(ppy, ppm))
        if pp:
            for field, label, money in (("spend", "Spend", True), ("qualified_leads", "Qualified leads", False)):
                a, b = pp[field], prev[field]
                if a and abs(b - a) / a * 100 >= MOVER_PCT:
                    pct = (b - a) / a * 100
                    fmt = (lambda n: f"${n:,.0f}") if money else (lambda n: f"{n:,.0f}")
                    out.append({
                        "kind": "mover",
                        "level": "warn" if pct < 0 else "info",
                        "text": f"{label} moved {pct:+.0f}% in {_mname(prev['month'])} ({fmt(a)} → {fmt(b)})",
                    })
    return out[:MAX_INSIGHTS]
