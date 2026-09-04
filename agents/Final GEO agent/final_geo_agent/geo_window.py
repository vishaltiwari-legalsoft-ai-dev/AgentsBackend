"""GEO agent — the measured window: one brand, one clamped day range, one fetch.

Every GEO read path answers the same question — "what did the engines say about
this brand over the last N days?" — and before this module each call site
assembled that answer by hand: clamp the day count, build the day-doc ids, pick
an engine constant, fetch, then re-derive the config, the alias map and the
report. Six sites re-typed the sequence, and they did not agree:

* Of the four ``(days x engines)`` fan-outs, three were given a batched fetch
  when the panel was found waiting ~11.5s on serial round trips.
  ``rescan_mentions`` was not, because the pattern was an idiom rather than a
  module, so it stayed at one round trip per ``(day, engine)`` — 120 of them at
  30 days, plus a transaction each.
* One reader scanned ``ENGINE_KEY_FIELDS`` instead of ``ALL_ENGINES``, which made
  every stored Google AI Overview answer invisible to the whole product. The fix
  was a *comment* in ``geo_engines`` telling the next reader which constant to
  use — load-bearing prose, the weakest kind of guarantee.
* ``generate_strategy`` opened the same 7-day window twice in one request and
  paid for both: 56 day-doc fetches and two full ``engine_report`` computations
  over byte-identical data.

So the window is a value now. A caller asks for one and gets the answers, the
config, the alias map and the report already assembled. What that makes
impossible to get wrong:

* **the engine constant** — :data:`ENGINES` is the one place a reader's engine
  list is written down, and no caller names engines at all;
* **the fan-out** — the only fetch is :func:`load_many`, so a new read path is
  batched by construction rather than by remembering;
* **the clamp** — :func:`clamp_days` runs inside the constructor and the clamped
  value is what ``window.days`` reports, so the number in the response is the
  number that was actually measured;
* **paying twice** — answers, config, aliases and report are each computed at
  most once per window, so "one request, one window" is the default rather than
  something to catch in review.

Fetching is **lazy**: constructing a window costs nothing, so a path that turns
out not to need the answers (``/history`` when its points are already banked)
pays nothing, and a path that reads them twice pays once.
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property

from final_geo_agent import geo_engines, geo_metrics
from seo_geo_agent import state

# The engine list a READER scans. Deliberately ``ALL_ENGINES`` and deliberately
# stated once: ``ENGINE_KEY_FIELDS`` covers only the chat engines (the SERP
# engines are keyed per vendor, not per engine), and a reader that picked that
# up made every stored AI Overview answer invisible on 2026-08-11. The poll
# PLANNER is the one legitimate exception — it iterates only the engines it may
# actually call — and it says so at its own call site.
ENGINES: tuple[str, ...] = geo_engines.ALL_ENGINES

#: Bounds of the answer window, in days. This is the *measured* window — how far
#: back stored day-docs are read. It is not the poll interval (``geo_poll``) and
#: not the trend-series window (``geo_history``); those are different quantities
#: that merely happen to be counted in days, and folding them in here would be a
#: false unification.
MIN_DAYS = 1
MAX_DAYS = 30
DEFAULT_DAYS = 7

# Every read here is (days x engines) INDEPENDENT document fetches, each one a
# network round trip. In sequence a 7-day report is 28 serial round trips —
# measured at ~12s, during which the whole panel sits empty. They are
# independent waits, so they overlap; the ceiling is the datastore's, not ours.
READ_CONCURRENCY = 12


def clamp_days(days: object) -> int:
    """The answer window, in days, forced into ``MIN_DAYS..MAX_DAYS``.

    Non-numeric input clamps to :data:`DEFAULT_DAYS` rather than raising: this
    runs on a query parameter, and a report that quietly measures a week is a
    better answer than a 500 for ``?days=lots``.
    """
    try:
        value = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(value, MAX_DAYS))


def poll_doc_id(brand_id: str, engine: str, day: str) -> str:
    """The day-doc id. Written down HERE and nowhere else — a second copy of
    this shape is how a reader ends up looking for documents no writer makes."""
    return f"geo-poll-{brand_id}-{engine}-{day}"


def day_ids(days: int) -> list[str]:
    """The window's UTC day keys, newest first."""
    today = dt.datetime.now(dt.timezone.utc).date()
    return [
        (today - dt.timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(clamp_days(days))
    ]


def load_many(doc_ids: list[str]) -> dict[str, dict | None]:
    """Fetch several state docs in ONE parallel batch, keyed by doc id.

    The single fetch primitive for the whole rail. A single-id request skips the
    pool so the common test path stays synchronous and easy to reason about.
    """
    if len(doc_ids) < 2:
        return {doc_id: state.load(doc_id) for doc_id in doc_ids}
    with ThreadPoolExecutor(max_workers=min(READ_CONCURRENCY, len(doc_ids))) as pool:
        return dict(zip(doc_ids, pool.map(state.load, doc_ids)))


def load_docs(
    brand_id: str, days: list[str], engines: tuple[str, ...] | list[str] = ENGINES
) -> dict[tuple[str, str], dict]:
    """``(day, engine) -> day-doc`` for the docs that exist, in one batch.

    Missing docs are simply absent: "this engine wrote nothing that day" and
    "this engine wrote an empty day" are the same fact to every reader, and a
    synthesised empty doc only invites a writer to save it back.
    """
    ids = {
        (day, engine): poll_doc_id(brand_id, engine, day)
        for day in days
        for engine in engines
    }
    loaded = load_many(list(ids.values()))
    return {key: doc for key, doc_id in ids.items() if (doc := loaded.get(doc_id))}


class MeasuredWindow:
    """Every stored answer for one brand across one clamped day range.

    Built by :func:`open_window`. Construction is free; the first property that
    needs the datastore triggers the one batched fetch, and every property is
    computed at most once.
    """

    def __init__(self, brand: dict, days: int, *, cfg: dict | None = None) -> None:
        self.brand = brand
        #: already clamped — this is the number the response should report
        self.days = clamp_days(days)
        self.day_ids: tuple[str, ...] = tuple(day_ids(self.days))
        self._cfg = cfg

    def __repr__(self) -> str:  # keep megabytes of answer text out of tracebacks
        return f"<MeasuredWindow {self.brand.get('id')} days={self.days}>"

    # ------------------------------------------------------------- identity

    @property
    def brand_id(self) -> str:
        return self.brand["id"]

    @property
    def own_domain(self) -> str:
        """The brand's domain, normalised the way every scorer normalises it.

        ``geo_metrics`` applies the same ``lower().removeprefix("www.")``
        internally, so handing it this is byte-identical to handing it the raw
        value — and it cannot be ``None``, which the raw one can.
        """
        return (self.brand.get("domain") or "").lower().removeprefix("www.")

    @cached_property
    def cfg(self) -> dict:
        """The brand's GEO config, read (and defaulted) at most once.

        Imported inside the property rather than at module scope: ``geo_poll``
        owns this document *and* reads windows, so a top-level import back would
        be a cycle. ``geo_history`` resolves the same knot the same way.
        """
        if self._cfg is None:
            from final_geo_agent import geo_poll

            self._cfg = geo_poll.ensure_config(self.brand)
        return self._cfg

    @cached_property
    def aliases(self) -> dict[str, list[str]]:
        """entity key -> the strings an answer is searched for."""
        from final_geo_agent import geo_poll

        return geo_poll.alias_map(self.cfg)

    @cached_property
    def entities(self) -> list[str]:
        """Tracked entity keys, ``self`` first — the report's share-of-voice axis."""
        return list(self.aliases)

    # ---------------------------------------------------------------- reads

    @cached_property
    def docs(self) -> dict[tuple[str, str], dict]:
        """``(day, engine) -> day-doc``, for the docs that exist. ONE fetch."""
        return load_docs(self.brand_id, list(self.day_ids))

    def doc(self, day: str, engine: str) -> dict | None:
        return self.docs.get((day, engine))

    @cached_property
    def by_day(self) -> dict[str, list[dict]]:
        """day (YYYYMMDD) -> that day's answers, newest day first, empty days
        omitted."""
        out: dict[str, list[dict]] = {}
        for day in self.day_ids:
            rows = [
                answer
                for engine in ENGINES
                if (doc := self.docs.get((day, engine)))
                for answer in doc.get("answers", [])
            ]
            if rows:
                out[day] = rows
        return out

    @cached_property
    def answers(self) -> list[dict]:
        """Every stored answer in the window, flattened newest day first.

        Treat as read-only — it is shared by every caller of this window. Copy
        it if you intend to sort or filter in place.
        """
        return [answer for rows in self.by_day.values() for answer in rows]

    def day(self, day: str) -> list[dict]:
        """One day's answers across every engine — the span of one sweep."""
        return self.by_day.get(day, [])

    def answer_counts(self) -> dict[str, dict[str, int]]:
        """``day -> engine -> stored answer count``, from the docs already read.

        The cheap fact behind ``recent_answers`` on the brand listing: counting
        was all the listing ever wanted, and hydrating megabytes of answer text
        to take a ``len`` was the whole finding.
        """
        counts: dict[str, dict[str, int]] = {}
        for (day, engine), doc in self.docs.items():
            answers = doc.get("answers") or []
            if answers:
                counts.setdefault(day, {})[engine] = len(answers)
        return counts

    # --------------------------------------------------------------- report

    @cached_property
    def expected(self) -> dict[str, int]:
        """``engine -> answers one complete sweep of that engine owes``.

        The brand's CURRENT question list, priced per engine by
        ``geo_poll.expected_answers`` — so it answers "why does AI Overview
        return fewer answers than ChatGPT" for the questions being asked now,
        which is the question the panel is asked. One extra document read (the
        prompt universe), taken lazily by ``report`` and never by a caller that
        only wants the answers.

        Over :data:`ENGINES`, the reader's list, not the engines that happen to
        hold a key: an engine that is off has no stored answers and therefore no
        block in the report, and gating this on availability as well would only
        hide the number on the day a key is pulled.

        ``geo_poll.DEFAULT_RUNS`` is the sample size, because it is the one the
        schedule polls at. A caller that hand-drove a sweep at ``runs=1`` will
        see a window that owes more than it bought — correctly: it bought a
        smaller sample.
        """
        from final_geo_agent import geo_poll, geo_prompts

        return geo_poll.expected_answers(
            geo_prompts.enabled_prompts(self.brand_id), ENGINES,
        )

    @cached_property
    def n_sweeps(self) -> int:
        """How many of this window's days the brand actually ran a check on.

        The multiplier that turns the report's per-sweep ``n_expected`` into a
        window figure — and the reason it is per BRAND rather than per engine.
        A per-engine count would collapse to zero for an engine that was paused
        on a spent search budget, so its expectation would collapse too and the
        shortfall this window exists to show would vanish into "owed nothing,
        delivered nothing". The brand's count keeps the hole visible and
        attributable: five checks x 34 owed = 170, AI Overview answered on two
        of them = 68, and ``serp_capped_since`` says when it stopped.

        Zero is a real answer — a brand with no checks in this period — and it
        is the panel's cue to say so rather than to divide by it.
        """
        from final_geo_agent import geo_runlog

        return len(geo_runlog.sweep_days(self.brand_id, self.day_ids))

    @cached_property
    def report(self) -> dict:
        """The full per-engine + blended report over this window.

        Computed once. ``generate_strategy`` used to build this twice per
        request over identical data; asking the window means it cannot.
        Callers that add keys must copy first — ``dict(window.report) | {...}``.
        """
        return geo_metrics.engine_report(
            self.answers, self.entities, self.own_domain, expected=self.expected,
        )


def open_window(
    brand: dict, days: int = DEFAULT_DAYS, *, cfg: dict | None = None
) -> MeasuredWindow:
    """The brand's measured window over the last ``days`` days.

    ``days`` is clamped here, so callers hand through whatever the query string
    said. Pass ``cfg`` when the config document is already in hand (a poll step
    settling up, a rescan) to save the read.
    """
    return MeasuredWindow(brand, days, cfg=cfg)


def open_day(brand: dict, day: str, *, cfg: dict | None = None) -> MeasuredWindow:
    """A window pinned to ONE stored day — the span of exactly one sweep.

    A sweep lives entirely inside one UTC day (``_pending_tasks`` reads the
    day-doc for today), so "one day" and "one sweep" are the same window, which
    is what a trend point is measured over.
    """
    window = MeasuredWindow(brand, MIN_DAYS, cfg=cfg)
    window.day_ids = (day,)
    return window
