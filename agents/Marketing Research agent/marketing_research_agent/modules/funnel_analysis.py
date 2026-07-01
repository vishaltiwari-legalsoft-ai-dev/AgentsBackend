"""Lead Channel & Funnel Analysis (requirements §3.3).

Maps leads to UTM attribution, computes conversion by channel, ranks practice
areas, finds funnel drop-off points, and flags high-volume/low-booking channels.
"""

from __future__ import annotations

from collections import defaultdict

from ..schemas import Lead

STAGE_ORDER = ["visit", "form_fill", "booked", "qualified", "completed"]
_RANK = {s: i for i, s in enumerate(STAGE_ORDER)}


def _reached(lead: Lead, stage: str) -> bool:
    return _RANK.get(lead.stage, 0) >= _RANK[stage]


def attribution(leads: list[Lead]) -> dict:
    total = len(leads)
    attributed = sum(1 for l in leads if l.utm_source)
    pct = round(attributed / total * 100, 1) if total else 0.0
    return {"attributed": attributed, "total": total, "pct": pct}


def conversion_by_channel(leads: list[Lead]) -> dict[str, dict]:
    acc: dict[str, dict] = defaultdict(lambda: {s: 0 for s in STAGE_ORDER})
    counts: dict[str, int] = defaultdict(int)
    for l in leads:
        counts[l.channel] += 1
        for s in STAGE_ORDER:
            if _reached(l, s):
                acc[l.channel][s] += 1
    out: dict[str, dict] = {}
    for ch, c in acc.items():
        base = c["form_fill"] or c["visit"] or counts[ch]
        out[ch] = dict(c)
        out[ch]["booked_rate"] = round(c["booked"] / base, 3) if base else None
        out[ch]["qualified_rate"] = round(c["qualified"] / base, 3) if base else None
    return out


def best_practice_areas(leads: list[Lead]) -> list[dict]:
    acc: dict[str, dict] = defaultdict(lambda: {"total": 0, "qualified": 0})
    for l in leads:
        acc[l.practice_area]["total"] += 1
        if _reached(l, "qualified"):
            acc[l.practice_area]["qualified"] += 1
    rows = [
        {
            "practice_area": pa,
            "total": v["total"],
            "qualified_rate": round(v["qualified"] / v["total"], 3) if v["total"] else 0.0,
        }
        for pa, v in acc.items()
    ]
    rows.sort(key=lambda r: r["qualified_rate"], reverse=True)
    return rows


def dropoff_points(leads: list[Lead]) -> dict[str, int]:
    """Cumulative count of leads reaching each stage (page visit -> booked)."""
    return {s: sum(1 for l in leads if _reached(l, s)) for s in STAGE_ORDER}


def low_booking_channels(leads: list[Lead], min_leads: int = 5, max_rate: float = 0.1) -> list[str]:
    """Channels with high lead volume but low booking rate (optimization review)."""
    conv = conversion_by_channel(leads)
    counts: dict[str, int] = defaultdict(int)
    for l in leads:
        counts[l.channel] += 1
    out = []
    for ch, n in counts.items():
        rate = conv[ch]["booked_rate"] or 0.0
        if n >= min_leads and rate <= max_rate:
            out.append(ch)
    return out
