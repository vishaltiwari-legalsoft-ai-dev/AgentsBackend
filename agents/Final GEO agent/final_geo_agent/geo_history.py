"""GEO agent — the composite GEO score and its movement over time.

The team's second ask from the 18 Aug review: "is the score going up or down?"
Nothing in the agent answered that, because every number was computed live
from a rolling window and then thrown away.

Two halves live here:

* **the score** — one 0-100 number blended from the rates we already measure,
  with its components and their weights returned alongside it. A score whose
  recipe is hidden is a number nobody can act on, so the panel gets the parts.
* **the series** — one document per brand (``geo-history-{brand}``) holding one
  point per completed sweep. Appending on sweep completion is what makes the
  trend real rather than a re-derivation of whatever happens to still be in
  the 30-day answer window.

Honesty rules: a component that cannot be measured is **dropped and the
remaining weights are renormalised** — never silently scored as 0, which would
punish a brand for an engine that returns no citations. A point built from too
few answers is marked ``partial`` instead of being drawn as a cliff.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import mean

from final_geo_agent import geo_metrics
from final_geo_agent.geo_store import mutate
from seo_geo_agent import state

# What the score is made of. Presence dominates because being named at all is
# the thing the whole agent exists to move; citation and share of voice split
# the middle; breadth is the tie-breaker that stops one runaway question from
# carrying the number.
WEIGHTS: dict[str, float] = {
    "presence": 0.40,
    "citation": 0.25,
    "sov": 0.25,
    "breadth": 0.10,
}

COMPONENT_LABELS: dict[str, str] = {
    "presence": "Named in answers",
    "citation": "Your site cited",
    "sov": "Share of voice",
    "breadth": "Questions covered",
}

# Below this a sweep is a sample, not a measurement, and it does not earn a
# point on the chart.
MIN_POINT_ANSWERS = 10
# A point measured on far fewer answers than its neighbours is real but thin —
# flagged so the panel can draw it as provisional instead of as a crash.
PARTIAL_RATIO = 0.6
# Roughly a year of two-day sweeps. The doc stays far under Firestore's 1 MB.
MAX_POINTS = 200
# How far back a one-time backfill reads day-docs for a brand that was already
# polling before this document existed. Deliberately bounded: each day costs
# one state read per engine, and the answer docs do not live forever anyway.
# Must stay <= geo_window.MAX_DAYS — a window wider than the readable window
# would silently measure less than it claims (pinned by test_geo_window).
BACKFILL_DAYS = 30

# The SERIES window: how far back the chart is drawn. A different quantity from
# the answer window (``geo_window`` — how far back day-docs are read) and from
# the poll interval (``geo_poll``): points are banked per sweep and kept for
# roughly a year, so the chart outlives the answers it was derived from. The
# floor exists because a trend needs at least a week to be a trend.
MIN_SERIES_DAYS = 7
MAX_SERIES_DAYS = 365
# A quarter of weekly bars: enough to see a plan's 90-day horizon end to end.
DEFAULT_ROLLUP_WEEKS = 12


def clamp_series_days(days: object) -> int:
    """The trend window, in days, forced into ``MIN..MAX_SERIES_DAYS``."""
    try:
        value = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 90
    return max(MIN_SERIES_DAYS, min(value, MAX_SERIES_DAYS))


def history_doc_id(brand_id: str) -> str:
    return f"geo-history-{brand_id}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ------------------------------------------------------------------ score


def score_components(
    block: dict, rollup: list[dict], *, has_competitors: bool
) -> dict[str, float]:
    """The 0..1 parts of the score that are actually measurable here.

    A key missing from the result means "we could not measure this", which is
    different from measuring zero — the caller renormalises around it.
    """
    parts: dict[str, float] = {}

    mention = block.get("mention") or {}
    if mention.get("rate") is not None:
        parts["presence"] = float(mention["rate"])

    citation = block.get("citation") or {}
    # no answers carried citations (or no domain on record) -> unknowable
    if citation.get("rate") is not None:
        parts["citation"] = float(citation["rate"])

    sov = (block.get("sov") or {}).get("share") or {}
    # with nobody tracked, share of voice is 100% of a one-horse race
    if has_competitors and sov.get("self") is not None:
        parts["sov"] = float(sov["self"])

    if rollup:
        parts["breadth"] = sum(1 for r in rollup if r.get("self_rate", 0) > 0) / len(rollup)

    return parts


def geo_score(
    block: dict, rollup: list[dict], *, has_competitors: bool
) -> dict:
    """0-100 with the recipe attached. ``score`` is None when nothing is
    measurable — an empty window scores nothing, it does not score zero."""
    parts = score_components(block, rollup, has_competitors=has_competitors)
    used = {k: WEIGHTS[k] for k in parts if k in WEIGHTS}
    total_weight = sum(used.values())
    if not total_weight:
        return {"score": None, "components": {}, "weights": {}, "missing": list(WEIGHTS)}
    score = sum(parts[k] * w for k, w in used.items()) / total_weight
    return {
        "score": round(score * 100, 1),
        "components": {k: round(parts[k], 4) for k in used},
        # renormalised, so the panel's bars add up to the number above them
        "weights": {k: round(w / total_weight, 4) for k, w in used.items()},
        "missing": [k for k in WEIGHTS if k not in used],
    }


# ------------------------------------------------------------------ points


def build_point(
    day: str,
    answers: list[dict],
    entities: list[str],
    own_domain: str,
    *,
    source: str = "sweep",
) -> dict | None:
    """One chart point from one sweep's answers, or ``None`` if too thin."""
    measured = geo_metrics.measurable(answers)
    if len(measured) < MIN_POINT_ANSWERS:
        return None
    report = geo_metrics.engine_report(answers, entities, own_domain)
    block = report["blended"]
    rollup = report.get("prompt_rollup") or []
    rivals = [e for e in entities if e != "self"]
    scored = geo_score(block, rollup, has_competitors=bool(rivals))
    return {
        "date": day,
        "at": _now(),
        "source": source,
        "score": scored["score"],
        "components": scored["components"],
        "weights": scored["weights"],
        "missing": scored["missing"],
        "mention_rate": (block["mention"] or {}).get("rate"),
        "citation_rate": (block["citation"] or {}).get("rate"),
        "sov_self": (block["sov"]["share"] or {}).get("self"),
        "n_measured": len(measured),
        "n_answers": len(answers),
        "n_prompts": block["mention"].get("n_prompts", 0),
        # The three states one answer can be in, as counts over the SAME
        # denominator (``n_measured``).
        #
        # ``mention_rate`` and ``citation_rate`` cannot be combined into these:
        # the citation rate is computed over answers that carry citations at
        # all, which is a different and smaller population. Multiplying both by
        # ``n_measured`` and subtracting produced "named, no link: -9" on a real
        # brand — a count that cannot exist. So the split is counted here, from
        # the answers themselves, and the panel either has all three or draws
        # none of them.
        "n_named": sum(1 for a in measured if "self" in (a.get("mentions") or {})),
        "n_named_cited": sum(
            1 for a in measured
            if "self" in (a.get("mentions") or {}) and a.get("brand_cited")
        ),
        "engines": {
            engine: (b["mention"] or {}).get("rate")
            for engine, b in report["engines"].items()
        },
        # rivals on the same axis as us — the review asked for competitor
        # numbers *alongside* our own, over time, not on a separate screen
        "competitors": {
            key: (stats or {}).get("rate") for key, stats in report["competitors"].items()
        },
    }


def mark_partial(points: list[dict]) -> list[dict]:
    """Flag points measured on far fewer answers than the series' best."""
    best = max((p.get("n_measured") or 0) for p in points) if points else 0
    for point in points:
        point["partial"] = bool(
            best and (point.get("n_measured") or 0) < best * PARTIAL_RATIO
        )
    return points


def merge_points(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Newest-wins by date, sorted oldest→newest, capped at ``MAX_POINTS``.

    A resumed sweep can finish on the day after it started, so a date is
    written more than once — the later measurement is the fuller one and
    replaces the earlier, rather than adding a second point for the same day.
    """
    by_date: dict[str, dict] = {p["date"]: p for p in existing if p.get("date")}
    for point in incoming:
        if not point or not point.get("date"):
            continue
        by_date[point["date"]] = point
    ordered = [by_date[d] for d in sorted(by_date)]
    return ordered[-MAX_POINTS:]


def load_points(brand_id: str) -> list[dict]:
    doc = state.load(history_doc_id(brand_id)) or {}
    return list(doc.get("points") or [])


def save_points(brand_id: str, points: list[dict]) -> list[dict]:
    """Merge ``points`` into the brand's history document, atomically."""

    def change(doc: dict) -> tuple[dict, list[dict]]:
        doc = dict(doc or {})
        doc["brand_id"] = brand_id
        merged = mark_partial(merge_points(list(doc.get("points") or []), points))
        doc["points"] = merged
        doc["updated_at"] = _now()
        return doc, merged

    return mutate(history_doc_id(brand_id), change)


def needs_backfill(brand_id: str) -> bool:
    """Has this brand's trend ever been reconstructed from its day-docs?

    Checked before doing it, because the reconstruction costs one state read
    per engine per day and must happen once, not on every panel load. The
    stamp is set even when the reconstruction yields no points — a brand with
    no history to recover must not re-scan 30 days on every request.
    """
    doc = state.load(history_doc_id(brand_id)) or {}
    return not doc.get("backfilled_at")


def backfill(
    brand_id: str,
    days_map: dict[str, list[dict]],
    entities: list[str],
    own_domain: str,
) -> list[dict]:
    """One-time reconstruction of the trend from answers already on disk.

    Without it a brand that has been polling for weeks would show an empty
    chart until its next sweep. Points are marked ``source: "backfill"`` so the
    panel can be explicit that they were derived after the fact.
    """
    built = [
        point
        for day in sorted(days_map)
        if (point := build_point(day, days_map[day], entities, own_domain,
                                 source="backfill")) is not None
    ]

    def change(doc: dict) -> tuple[dict, list[dict]]:
        doc = dict(doc or {})
        if doc.get("backfilled_at"):  # another request got there first
            return doc, list(doc.get("points") or [])
        # Reconstructed points are the BASE and anything already recorded wins
        # over them: a point banked live by a completed sweep is the real
        # measurement, and a same-day reconstruction must not relabel it.
        merged = mark_partial(merge_points(built, list(doc.get("points") or [])))
        doc["brand_id"] = brand_id
        doc["points"] = merged
        doc["backfilled_at"] = _now()
        doc["updated_at"] = _now()
        return doc, merged

    return mutate(history_doc_id(brand_id), change)


def record_sweep(
    brand_id: str,
    day: str,
    answers: list[dict],
    entities: list[str],
    own_domain: str,
    *,
    preserve_source: bool = False,
) -> dict | None:
    """Build and store the point for a sweep that just finished.

    ``preserve_source`` is for a rebuild after a rescan: the answers changed
    but the way they were collected did not, so a point that was banked live
    must not start claiming it was reconstructed.
    """
    point = build_point(day, answers, entities, own_domain)
    if point is None:
        return None
    if preserve_source:
        previous = next((p for p in load_points(brand_id) if p.get("date") == day), None)
        if previous:
            point["source"] = previous.get("source", point["source"])
    save_points(brand_id, [point])
    return point


# ------------------------------------------------------------------ trend


def _movement(current: float | None, earlier: float | None) -> dict:
    if current is None or earlier is None:
        return {"change": None, "direction": "unknown"}
    change = round(current - earlier, 1)
    direction = "flat" if abs(change) < 0.5 else ("up" if change > 0 else "down")
    return {"change": change, "direction": direction}


def trend(points: list[dict]) -> dict:
    """Latest score, the move since the sweep before it, and since the window
    opened. ``None`` everywhere there is nothing to compare against — the panel
    says "first measurement" rather than drawing a 0% change."""
    scored = [p for p in points if p.get("score") is not None]
    if not scored:
        return {
            "current": None, "previous": None, "first": None,
            "since_last": {"change": None, "direction": "unknown"},
            "since_start": {"change": None, "direction": "unknown"},
            "n_points": len(points),
        }
    current = scored[-1]
    previous = scored[-2] if len(scored) > 1 else None
    first = scored[0] if len(scored) > 1 else None
    return {
        "current": current,
        "previous": previous,
        "first": first,
        "since_last": _movement(current["score"], (previous or {}).get("score")),
        "since_start": _movement(current["score"], (first or {}).get("score")),
        "n_points": len(scored),
    }


# ------------------------------------------------------------------ weekly


def _iso_week(day: object) -> tuple[str, str] | None:
    """``"20260827"`` -> ``("2026-W35", "2026-08-24")``; unreadable -> None."""
    try:
        date = dt.datetime.strptime(str(day), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None
    year, week, _weekday = date.isocalendar()
    return f"{year}-W{week:02d}", dt.date.fromisocalendar(year, week, 1).isoformat()


def _mean_or_none(values: list, digits: int) -> float | None:
    present = [float(v) for v in values if v is not None]
    return round(mean(present), digits) if present else None


def weekly_rollup(points: list[dict], *, weeks: int = DEFAULT_ROLLUP_WEEKS) -> list[dict]:
    """Points bucketed by ISO week, oldest→newest, the last ``weeks`` of them.

    Derived on read and never stored: the per-sweep points are the record, and
    a banked rollup would drift from them the moment a same-day point was
    replaced by a fuller measurement. A week with no sweep is absent rather
    than drawn as zero, so ``delta_score`` is against the previous week that
    actually has a score — the same rule ``trend`` uses for "since last".

    ``all_partial`` is the week-level version of a point's ``partial`` flag:
    every sweep that week was thin, so the bar is provisional, not a cliff.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    starts: dict[str, str] = {}
    for point in points:
        stamped = _iso_week(point.get("date"))
        if stamped is None:
            continue
        week, start = stamped
        buckets[week].append(point)
        starts[week] = start

    out: list[dict] = []
    previous_score: float | None = None
    for week in sorted(buckets):
        bucket = buckets[week]
        score = _mean_or_none([p.get("score") for p in bucket], 1)
        out.append({
            "week": week,
            "start": starts[week],
            "score": score,
            "mention_rate": _mean_or_none([p.get("mention_rate") for p in bucket], 4),
            "citation_rate": _mean_or_none([p.get("citation_rate") for p in bucket], 4),
            "n_sweeps": len(bucket),
            "all_partial": all(p.get("partial") for p in bucket),
            "delta_score": (
                round(score - previous_score, 1)
                if score is not None and previous_score is not None else None
            ),
        })
        if score is not None:
            previous_score = score
    return out[-max(1, int(weeks)):]
