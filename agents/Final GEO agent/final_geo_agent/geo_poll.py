"""GEO agent — step-wise engine polling, parsing, and per-brand config.

Polling is deliberately chunked (the Blog Writer ``research_step`` precedent):
one ``poll_step`` call executes a small batch of (engine × prompt × run) tasks
and returns progress, so the UI loops instead of one giant request. Results
append to one Firestore doc per engine per day
(``geo-poll-{brand}-{engine}-{YYYYMMDD}``) — bounded well under the 1 MB doc
limit by the answer-text cap.

Cost control: a per-brand daily engine-call counter with a hard cap lives in
``geo-config-{brand}``; when the cap is hit the step reports it honestly and
does nothing. Calls are RESERVED against that counter before they are made and
the unspent remainder is handed back afterwards — see ``_reserve_calls``.

Work assignment is a SEPARATE guarantee from budget, and the two are claimed
together. The reservation protects the spend; it does not stop two overlapping
sweeps from being handed the same tasks. A sweep therefore also takes a LEASE
on the brand in the same transaction — see ``POLL_LEASE_TTL_SECONDS`` and
``_reserve_calls``.

Termination: a step that cannot make progress says so. ``terminal`` /
``terminal_reason`` in the result are the UI's stop signal — without them a
dead key or a 5xx-ing provider keeps every task pending forever (errored runs
never complete, by design) while each poll still burns real paid calls, so the
only thing that ever ended the loop was the daily budget running out.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from typing import Any, Callable, NamedTuple

from seo_geo_agent import state
from final_geo_agent import (
    geo_engines, geo_history, geo_prompts, geo_runlog, geo_store, geo_window,
)
from seo_geo_agent.sources import CredentialMissing, llm_json

logger = logging.getLogger("agentos.geo.poll")

DEFAULT_DAILY_CAP = 2000
DEFAULT_RUNS = geo_engines.CHAT_RUNS_PER_PROMPT
DEFAULT_BATCH = 10  # tasks (engine calls) per step
# A full sweep is ~40 prompts x 3 engines x 3 runs + 40 AIO = ~400 engine calls.
# Run sequentially at ~5s each that is half an hour with a browser tab held
# open, which is what made polling feel unusable. The calls are independent
# network waits, so they overlap; only the bookkeeping stays serial.
# Kept modest on purpose — provider rate limits, not CPU, are the ceiling here.
# Measured, not guessed: the stored answers give median 5.2s (perplexity),
# 9.1s (gemini) and 32.8s (chatgpt via the OpenRouter stand-in). At 6 a single
# brand's sweep needs ~1030s, which does not fit the service's 900s request
# timeout -- so no sweep has ever completed, the poll interval has never taken
# effect, and both brands were re-polled from scratch every day. At 10 one
# brand finishes in ~620s, and with two brands swept stalest-first that lands
# exactly on the intended every-other-day cadence.
POLL_CONCURRENCY = 10
# Scheduled polling: the cron fires daily and this decides whether a brand is
# actually due. A day-of-month cron step (``*/2``) would silently double-fire
# across month boundaries (31st -> 1st); an interval measured from the last
# completed sweep does not.
DEFAULT_POLL_INTERVAL_DAYS = 2
# The cadence a SELF-SERVE brand is created with — see ``init_config``. Weekly,
# not the 2 days above: that number was chosen for two hand-picked brands whose
# spend somebody watches, and a panel where anyone can add a brand needs a
# default that is cheap to leave switched on. It is only a starting value; the
# switch is off at creation and the interval is editable from the config route.
SELF_SERVE_POLL_INTERVAL_DAYS = 7
# Wall clock one cron invocation may spend, across every brand it sweeps. Must
# stay under the service's request timeout (900s in production) — Cloud Run
# kills the request at that point, and because a day-doc is keyed by TODAY a
# sweep cut short does not resume tomorrow, it restarts. So this number decides
# whether a sweep can ever finish at all, not merely how fast it does.
DEFAULT_CRON_BUDGET_SECONDS = 800
# The SERP engines (Google AI Overview, AI Mode) are billed per call — a few
# tenths of a cent each on DataForSEO — so this is a spend ceiling of a few
# dollars per brand per month, not a vendor quota. It applies to every SERP
# engine JOINTLY, under the counter and override keys the AIO-only guard
# already stored (``counters_aio`` / ``aio_monthly_cap``): the key names are
# storage-stable and stay as they are.
SERP_MONTHLY_CAP = 2000
AIO_MONTHLY_CAP = SERP_MONTHLY_CAP
# How many consecutive batches an engine may fail every single call in before
# the poll is declared terminal. 1 batch = a blip worth retrying; 3 in a row on
# the same engine is a dead key or an outage, and its tasks can never complete,
# so the loop can never end on its own — stop and name the engine.
FAIL_STREAK_LIMIT = 3
# How long one sweep owns a brand. ``_reserve_calls`` is transactional and
# protects the daily BUDGET; it does not assign WORK. Two overlapping steps
# both read the same day-docs, both build the same ordered task list, both
# reserve ten calls against a cap with room for twenty, and both then execute
# ``tasks[:granted]`` -- the SAME ten (engine, prompt, run) triples. Twenty
# paid provider calls for ten measurements, and the day's mention rate then
# averaged over a doubled sample.
#
# So a sweep also claims a lease on the brand, INSIDE the same transaction as
# the reservation -- a second transaction to claim it would simply move the
# race rather than close it. 120s because a batch is bounded by
# ``geo_engines.REQUEST_TIMEOUT`` (45s), so this covers a slow batch without
# stranding the brand for long if a holder dies mid-sweep.
POLL_LEASE_TTL_SECONDS = 120
#: The ``trigger`` value that means "the scheduler drove this, not a person".
#: The once-a-day manual gate keys off THIS, never off who the holder is: an
#: email is a label, and a spend guard must not depend on how a label is spelt.
SCHEDULED_TRIGGER = "cron"
# Firestore hard-caps docs at 1 MB; day-docs are trimmed to this.
DOC_TRIM_BYTES = 900_000


#: Why a step stopped, as a value rather than a sentence. Four refusals now
#: exist and the console has to word all four differently, so each one is
#: identifiable without matching on prose that translation or a reword breaks.
STOP_DAILY_CAP = "daily_cap"                    # today's engine-call budget is spent
STOP_LEASE_HELD = "lease_held"                  # a check is running right now
STOP_CHECKED_TODAY = "already_checked_today"    # this brand's manual check is used
STOP_ENGINE_FAILED = "engine_failed"            # the providers are not answering
#: The fifth refusal, the joint SERP monthly cap, never reaches this shape: it
#: raises and the router answers 409. Recorded here so the set reads as complete.


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _next_day_boundary() -> str:
    """When the day-keyed guards reset — the next UTC midnight.

    The same boundary :func:`_today` draws, derived from it rather than stated
    again: a refusal that says "unlocks at" and a counter that rolls over must
    never disagree about when the day ends.
    """
    today = dt.datetime.strptime(_today(), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    return (today + dt.timedelta(days=1)).isoformat()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")


def config_doc_id(brand_id: str) -> str:
    return f"geo-config-{brand_id}"


# The day-doc id shape belongs to ``geo_window`` — it is the module every
# reader goes through, and a second copy of the shape here is exactly how a
# reader and a writer drift apart. Re-exported because this module writes those
# documents and its callers already import the name from here.
poll_doc_id = geo_window.poll_doc_id


def _default_aliases(brand: dict) -> list[str]:
    aliases = [brand.get("name", "")]
    domain = brand.get("domain", "")
    if domain:
        aliases.append(domain)
        stem = domain.split(".")[0]
        if len(stem) > 3:
            aliases.append(stem)
    return [a for a in aliases if a]


def _default_config(brand: dict) -> dict:
    """The config document a brand starts life with.

    One definition, two entry points: :func:`ensure_config` (a brand that exists
    but has never had a config) and :func:`init_config` (a brand being created
    right now, which gets to choose its schedule). A second literal here is how
    a brand created one way ends up with a field a brand created the other way
    does not have.
    """
    return {
        "brand_id": brand["id"],
        "aliases": {"self": _default_aliases(brand)},
        "competitors": [],  # [{key, name, aliases: [...]}]
        "daily_cap": DEFAULT_DAILY_CAP,
        "poll_interval_days": DEFAULT_POLL_INTERVAL_DAYS,
        "auto_poll": True,
        "counters": {},
        "updated_at": _now(),
    }


def ensure_config(brand: dict) -> dict:
    """Load the brand's GEO config, creating a sensible default on first use.

    The create half goes through :func:`_mutate`: this document also holds the
    spend counters, so a default written over a config an overlapping caller
    just created would hand back budget that was already reserved. The common
    read stays a plain read — no write, no transaction.
    """
    doc = state.load(config_doc_id(brand["id"]))
    if doc:
        return doc

    def change(cfg: dict) -> tuple[dict, dict]:
        if cfg:  # created between the read above and this transaction
            return cfg, cfg
        fresh = _default_config(brand)
        return fresh, fresh

    return _mutate(config_doc_id(brand["id"]), change)


def init_config(
    brand: dict, *, auto_poll: bool, poll_interval_days: int
) -> dict:
    """Seed a brand-new brand's config, with its schedule chosen by the creator.

    Two things this does that :func:`ensure_config` deliberately does not.

    **The schedule is an argument.** ``ensure_config``'s default is ``auto_poll:
    True`` every 2 days, which is right for the brands that predate the flag and
    wrong for one somebody just typed in: a brand created self-serve starts
    un-watched and costs nothing until a human turns it on.

    **``answer_counts_at`` is stamped.** The listing calls
    :func:`recent_answer_count`, which treats a missing stamp as "this brand
    polled before the counter existed" and runs :func:`_rebuild_answer_counts` —
    a 30-day x 5-engine window read, ~150 document fetches, landing on whoever
    opens the panel first. For a brand created seconds ago the reconstruction is
    guaranteed to find nothing, so it is pure cost: twelve new brands is ~1,800
    reads to compute twelve zeroes. An empty counter with today's stamp is the
    same answer, honestly, for free.

    Refuses (``ValueError``) if a config already exists — a brand id that has
    been used before still owns its spend counters and its measured history, and
    stamping a fresh document over them would hand back budget and erase counts.
    """
    def change(cfg: dict) -> tuple[dict, dict]:
        if cfg:
            raise ValueError(f"GEO config already exists for '{brand['id']}'")
        fresh = _default_config(brand) | {
            "auto_poll": bool(auto_poll),
            "poll_interval_days": max(1, min(int(poll_interval_days), 30)),
            "answer_counts": {},
            "answer_counts_at": _now(),
        }
        return fresh, fresh

    return _mutate(config_doc_id(brand["id"]), change)


def save_config(brand_id: str, patch: dict) -> dict:
    """Patch the brand's GEO config — transactionally, like every other write
    to this document.

    ``geo-config-{brand}`` carries the daily engine-call counters that
    :func:`_reserve_calls` claims inside a real transaction. A plain
    read-modify-write here silently restores the ``counters`` map to whatever it
    was when this request loaded the doc, giving back calls a poll had already
    reserved and spent — and that cap is the only thing between a dead provider
    key and the whole daily budget.
    """

    def change(cfg: dict) -> tuple[dict, dict]:
        doc = dict(cfg)
        for key in ("aliases", "competitors", "daily_cap", "aio_monthly_cap",
                    "poll_interval_days", "auto_poll"):
            if key in patch:
                doc[key] = patch[key]
        doc["brand_id"] = brand_id
        doc["updated_at"] = _now()
        return doc, doc

    return _mutate(config_doc_id(brand_id), change)


def competitor_aliases(
    name: str, domain: str = "", extra: list[str] | None = None
) -> list[str]:
    """Every written form of a competitor worth searching an answer for.

    Mentions are matched on word boundaries against the exact string, so a
    rival typed as "smith ai" is found in NONE of the answers that write it
    "Smith.ai" — measured: 32 of 200 stored answers named them, 0 were
    detected. The brand's own aliases have always been derived from name +
    domain + stem (:func:`_default_aliases`); the competitor was the only
    entity we expected to be typed exactly the way the engines write it.

    Anything the team typed themselves comes first and is never dropped.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = (value or "").strip()
        if not value or value.lower() in seen:
            return
        seen.add(value.lower())
        out.append(value)

    for value in extra or []:
        add(value)
    add(name)
    host = (
        (domain or "").strip().lower()
        .removeprefix("https://").removeprefix("http://").removeprefix("www.")
        .split("/")[0]
    )
    if host:
        add(host)
        stem = host.split(".")[0]
        # same guard as the brand's own stem: below this it matches noise
        if len(stem) > 3:
            add(stem)
    return out


def alias_map(cfg: dict) -> dict[str, list[str]]:
    """entity key -> the strings we search answers for.

    Competitor aliases are derived on READ, not baked in at save time, so a
    competitor tracked before this derivation existed is matched correctly on
    the next rescan instead of needing a migration.
    """
    aliases = {"self": list((cfg.get("aliases") or {}).get("self") or [])}
    for comp in cfg.get("competitors") or []:
        key = comp.get("key") or comp.get("name", "")
        if key:
            aliases[key] = competitor_aliases(
                comp.get("name", ""), comp.get("domain", ""), comp.get("aliases"),
            )
    return aliases


def detect_mentions(text: str, aliases: dict[str, list[str]]) -> dict[str, int]:
    """First-occurrence index per entity → 1-based mention order.

    Word-boundary, case-insensitive match so 'Ramp' doesn't fire inside
    'rampant'. Domains match with or without the TLD dot intact.
    """
    lowered = text.lower()
    first_index: dict[str, int] = {}
    for entity, names in aliases.items():
        best = -1
        for name in names:
            needle = name.lower().strip()
            if len(needle) < 3:
                continue
            pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
            match = re.search(pattern, lowered)
            if match and (best == -1 or match.start() < best):
                best = match.start()
        if best >= 0:
            first_index[entity] = best
    ranked = sorted(first_index.items(), key=lambda kv: kv[1])
    return {entity: rank for rank, (entity, _idx) in enumerate(ranked, start=1)}


def _sentiment(prompt_text: str, answer_text: str, brand_name: str) -> str | None:
    """LLM-judge sentiment for the brand within one answer. Best-effort: any
    failure (offline, keyless) returns None — never fabricated, never fatal."""
    try:
        data = llm_json(
            "Classify how the brand is portrayed in this AI answer. Return "
            'STRICT JSON: {"sentiment": "positive"|"neutral"|"negative"}',
            f"Brand: {brand_name}\nQuestion: {prompt_text}\nAnswer:\n{answer_text[:2000]}",
            agent_id=geo_prompts.GEO_AGENT_ID,
        )
        value = data.get("sentiment")
        return value if value in ("positive", "neutral", "negative") else None
    except (CredentialMissing, Exception):  # noqa: BLE001
        return None


def _load_day_docs(brand_id: str, engines: list[str], day: str) -> dict[str, dict]:
    """One day's docs for the engines the POLL PLANNER may call — the one
    legitimate exception to ``geo_window.ENGINES``.

    Every *reader* must scan ``ALL_ENGINES`` or it loses stored AIO answers;
    this is not a reader. It decides what to poll next, so it deliberately
    iterates only the engines that have a key right now, and an unconfigured
    engine's stored answers are none of its business. Stated here rather than
    inferred, because the two lists diverging silently is the bug the window
    module exists to prevent.

    Missing docs are filled in with an empty shell: ``_pending_tasks`` walks
    every engine it was asked about, so absence has to look like "no answers
    yet" rather than a gap in the mapping.
    """
    docs = geo_window.load_docs(brand_id, [day], engines)
    return {
        engine: docs.get((day, engine)) or {
            "brand_id": brand_id,
            "engine": engine,
            "date": day,
            "answers": [],
        }
        for engine in engines
    }


def _engine_prompts(engine: str, prompts: list[dict]) -> list[dict]:
    """The prompts this engine is asked: every one, unless its spec restricts
    it to the discovery intents (the billed SERP engines). A prompt with no
    recorded intent counts as category."""
    spec = geo_engines.ENGINE_SPECS.get(engine)
    intents = spec.intents if spec is not None else None
    if intents is None:
        return prompts
    return [p for p in prompts if (p.get("intent") or "category") in intents]


def aio_prompts(prompts: list[dict]) -> list[dict]:
    """The subset of the universe Google AI Overview is polled for."""
    return _engine_prompts(geo_engines.AIO_ENGINE, prompts)


def _engine_runs(engine: str, runs: int) -> int:
    """Runs per prompt for this engine in a sweep asking for ``runs``.

    A SERP engine is pinned to its spec: the content is a snapshot, not a
    sample, and each call is billed, so the caller's sample size does not
    multiply it. Chat engines sample ``runs`` times.
    """
    spec = geo_engines.ENGINE_SPECS.get(engine)
    if spec is not None and spec.kind == "serp":
        return spec.runs_per_prompt
    return runs


def search_credit_state(cfg: dict, engines: list[str] | None = None) -> dict:
    """The billed-Google-engine budget, as any READ surface reports it.

    One shape, so the cold-open report and the status poll cannot describe the
    same brand differently. Names say what the fact is rather than which engine
    it started life attached to: the guard has covered both Google engines
    since AI Mode arrived, and ``aio_capped`` reads as though AI Mode were fine.
    The STORED keys keep their old names (``counters_aio`` /
    ``aio_monthly_cap`` / the ``aio_*`` fields ``poll_step`` has always
    returned) — renaming those is a document migration, and this is not it.

    ``serp_capped_since`` is gated on the live condition on purpose: raising the
    cap un-pauses the engines with no poll at all, and a date that outlived its
    own condition is worse than no date.
    """
    usable = _usable_engines(engines)
    spent = serp_capped(cfg, usable)
    return {
        "search_credit_spent": spent,
        "search_credit_used": aio_used_month(cfg),
        "search_credit_limit": serp_monthly_cap(cfg),
        "serp_capped_since": cfg.get("serp_capped_since") if spent else None,
    }


def expected_answers(
    prompts: list[dict],
    engines: list[str] | tuple[str, ...],
    runs: int = DEFAULT_RUNS,
) -> dict[str, int]:
    """``engine -> answers ONE complete sweep of that engine should produce``.

    Per sweep, not per report window: it is prompts x runs for this engine and
    nothing else, so it is the same number whether the window is one day or
    thirty, and a reader may divide a window's stored count by it.

    The whole reason it exists is that the two halves are engine-specific and
    the product never said so. The team filed "different engines return
    different numbers of answers" as a bug; it is
    :func:`_engine_prompts` (a billed SERP engine is asked only the discovery
    questions -- the ones that do not already name the brand) composed with
    :func:`_engine_runs` (a SERP snapshot is fetched once, a chat engine is
    sampled three times to measure variance). Composed, never restated: this is
    also what :func:`_total_tasks` sums, so a report that says an engine owes 41
    answers and a sweep that plans 41 calls can never disagree.
    """
    return {
        engine: len(_engine_prompts(engine, prompts)) * _engine_runs(engine, runs)
        for engine in engines
    }


def representative_order(prompts: list[dict]) -> list[dict]:
    """Round-robin the universe across intents so a TRUNCATED sweep still
    samples every question kind.

    Tasks used to be built in universe order, and generated universes open
    with the brand-intent questions — so a budget-starved brand measured ONLY
    those. legal soft banked five straight daily points of score 96 with
    mention_rate 1.0 on n_prompts=3 while its full-window rate was 0.23: not a
    math bug, a sampling bug. Category first and brand last, because brand
    questions nearly always name the brand and are the least informative
    spend of a scarce call.
    """
    groups: dict[str, list[dict]] = {}
    for prompt in prompts:
        groups.setdefault(prompt.get("intent") or "category", []).append(prompt)
    order = [g for g in ("category", "problem", "brand") if g in groups]
    order += [g for g in groups if g not in order]
    return [
        prompt
        for row in zip_longest(*(groups[g] for g in order))
        for prompt in row
        if prompt is not None
    ]


def _pending_tasks(
    prompts: list[dict], docs: dict[str, dict], runs: int
) -> list[tuple[str, dict, int]]:
    """Everything still owed for this day, INTERLEAVED across engines.

    A step bills ``tasks[:granted]``, and a full sweep (~410 calls) has never
    once fitted in the cron's wall clock — so whatever sits at the end of this
    list is never measured at all. Grouped by engine, that end was always Google
    AIO: a 250-call day bought perplexity 123/123, gemini 123/123, chatgpt 4/123
    and **aio 0/41**, every day, which is why the panel showed no AI Overview
    data despite a working SerpAPI key.

    Round-robin instead. The same budget now buys a proportionate slice of every
    engine, and AIO — one run per prompt where the chat engines take three —
    finishes first rather than never.
    """
    prompts = representative_order(prompts)
    per_engine: dict[str, list[tuple[str, dict, int]]] = {}
    for engine, doc in docs.items():
        # only SUCCESSFUL answers complete a task — an errored run (quota,
        # outage) stays pending so the next poll retries it instead of
        # writing the whole day off. ("no AIO shown" counts as completed.)
        done = {
            (a["prompt_id"], a["run"])
            for a in doc.get("answers", []) if not a.get("error")
        }
        per_engine[engine] = [
            (engine, prompt, run)
            for prompt in _engine_prompts(engine, prompts)
            for run in range(1, _engine_runs(engine, runs) + 1)
            if (prompt["id"], run) not in done
        ]
    tasks: list[tuple[str, dict, int]] = []
    for row in zip_longest(*per_engine.values()):
        tasks.extend(task for task in row if task is not None)
    return tasks


def used_today(cfg: dict) -> int:
    return int((cfg.get("counters") or {}).get(_today(), 0))


def _month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m")


def aio_used_month(cfg: dict) -> int:
    return int((cfg.get("counters_aio") or {}).get(_month(), 0))


def serp_monthly_cap(cfg: dict) -> int:
    """This brand's billed-SERP ceiling for the month.

    Stored under ``aio_monthly_cap`` because that is the key the AIO-only guard
    already wrote; it governs every SERP engine jointly. Read through here so
    the poll step, the status endpoint and the settle transaction cannot end up
    comparing the counter against three different numbers.
    """
    return int(cfg.get("aio_monthly_cap") or SERP_MONTHLY_CAP)


def serp_capped(cfg: dict, engines: list[str] | tuple[str, ...]) -> bool:
    """Are the billed Google engines out of budget for this month?

    ``engines`` is the list we may actually call: a brand with no DataForSEO
    credential is not "capped", it is unconfigured, and the panel words those
    two differently. The condition is stated ONCE here because a sweep drops the
    engines on it and a status read explains the drop with it — two copies is
    how a panel ends up saying "paused" while the sweep keeps polling.
    """
    return (
        any(engine in geo_engines.SERP_ENGINES for engine in engines)
        and aio_used_month(cfg) >= serp_monthly_cap(cfg)
    )


# --------------------------------------------------------------- atomicity ----
# The transactional primitive lives in ``geo_store`` so modules this one imports
# (``geo_prompts``) can share it. Kept under the private name here because the
# call sites below were written against it.
_LOCAL_LOCK = geo_store.LOCAL_LOCK
_mutate = geo_store.mutate


def _trim_counters(cfg: dict) -> dict:
    """Trailing 30 day-counters / 3 month-counters, so the config never grows."""
    if cfg.get("counters"):
        cfg["counters"] = dict(sorted(cfg["counters"].items())[-30:])
    # Same window as the spend counters, and keyed by the same ``_today()``
    # string, so "one manual check per day" rolls over on exactly the boundary
    # the budget does. A second notion of "today" is a second thing to get
    # wrong at midnight UTC.
    if cfg.get("manual_checks"):
        cfg["manual_checks"] = dict(sorted(cfg["manual_checks"].items())[-30:])
    if cfg.get("counters_aio"):
        cfg["counters_aio"] = dict(sorted(cfg["counters_aio"].items())[-3:])
    if cfg.get("answer_counts"):
        cfg["answer_counts"] = dict(
            sorted(cfg["answer_counts"].items())[-geo_window.MAX_DAYS:]
        )
    return cfg


# ------------------------------------------------------------ sweep lease ----
# ``poll_lease`` on ``geo-config-{brand}`` is ``{holder, run_id, expires_at}``.
# ``run_id`` identifies one SWEEP (the cron's whole loop, or a single manual
# step); ``holder`` is the human label the console shows when it has to say who
# already has the brand.


def _new_run_id() -> str:
    return uuid.uuid4().hex


#: Longest client loop token we will use. Longer is truncated, never rejected.
POLL_TOKEN_MAX = 64
#: Everything outside this is dropped from a client token. The value ends up in
#: a Firestore document and in a refusal message shown to another person, so it
#: is reduced to characters that cannot be mistaken for either.
_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def loop_run_id(scope: str, token: Any) -> str | None:
    """A caller's loop token turned into a lease run id, or ``None``.

    ``None`` means "this caller named no loop", and the step then holds the
    lease for its own duration only — today's behaviour, unchanged. A token
    that survives cleaning holds the lease across the WHOLE loop instead, which
    is what makes "a check is already running for this brand" a stable answer
    rather than something two callers trade back and forth between batches.

    Never raises and never rejects. A malformed, oversized or hostile token
    degrades to ``None`` — a client that sends nonsense gets the old behaviour,
    not a 422 in the middle of somebody's poll loop.

    ``scope`` is the authenticated caller, and it is PREFIXED rather than
    trusted alongside: a token is a plain string chosen by a client, so without
    this two people who both send ``"1"`` would share one lease and both drive
    the same sweep — the exact collision this closes.
    """
    if not isinstance(token, str):
        return None
    cleaned = _TOKEN_UNSAFE.sub("", token.strip())[:POLL_TOKEN_MAX]
    if not cleaned:
        return None
    return f"{_TOKEN_UNSAFE.sub('', scope)[:POLL_TOKEN_MAX]}:{cleaned}"


def _live_lease(cfg: dict, now: dt.datetime) -> dict | None:
    """The brand's poll lease if it is still live, else ``None``.

    An absent, malformed or undateable ``expires_at`` reads as EXPIRED on
    purpose. This is a cost guard, not a correctness lock: a lease nobody can
    date would be a brand nobody could ever poll again, which is a worse
    outcome than the duplicate batch it was protecting against.
    """
    lease = cfg.get("poll_lease")
    if not isinstance(lease, dict):
        return None
    expires = _parse_at(lease.get("expires_at"))
    if expires is None or expires <= now:
        return None
    return lease


def _lease_doc(holder: str, run_id: str, now: dt.datetime) -> dict:
    return {
        "holder": holder or "another sweep",
        "run_id": run_id,
        "expires_at": (
            now + dt.timedelta(seconds=POLL_LEASE_TTL_SECONDS)
        ).isoformat(),
    }


def _release_lease(brand_id: str, run_id: str) -> None:
    """Hand the brand back — but only if the lease is still OURS.

    Run-id matched because release is called from more than one place (the step
    that ends a sweep, the cron loop's ``finally``) and because a lease that
    expired and was re-claimed now belongs to somebody else. Clearing it blindly
    would hand a second caller's sweep to a third.

    Best-effort, in the manner of ``_record_history``: the answers are already
    paid for and stored, so a failed release must not turn a finished sweep into
    an error. It is logged with its traceback, and the TTL is the backstop.
    """

    def change(cfg: dict) -> tuple[dict, None]:
        lease = cfg.get("poll_lease")
        if not isinstance(lease, dict) or lease.get("run_id") != run_id:
            return cfg, None
        cfg = dict(cfg)
        cfg.pop("poll_lease", None)
        return cfg, None

    try:
        _mutate(config_doc_id(brand_id), change)
    except Exception:  # noqa: BLE001 — the TTL expires it anyway
        logger.exception("geo: could not release the poll lease for %s", brand_id)


class Reservation(NamedTuple):
    """What one claim attempt got, and if it got nothing, precisely why.

    ``stop_code`` is the machine-readable half of a refusal, ``refused_by``
    names the person or sweep responsible where there is one, and
    ``unlocks_at`` is when the caller may try again.
    """

    granted: int
    used: int
    cap: int
    stop_code: str | None = None
    refused_by: str | None = None
    unlocks_at: str | None = None


def _release_manual_check(brand_id: str, day: str, run_id: str) -> None:
    """Give today's manual check back, if it is ours and it measured nothing.

    A dead provider key must not cost somebody their one check for the day: the
    sweep was claimed, it stored no answer, so nothing was measured and there is
    nothing to have used up. Run-id matched like every other release, so a
    later, genuine check by somebody else is never handed back on their behalf.
    """

    def change(cfg: dict) -> tuple[dict, None]:
        checks = cfg.get("manual_checks") or {}
        mine = checks.get(day)
        if not isinstance(mine, dict) or mine.get("run_id") != run_id:
            return cfg, None
        cfg = dict(cfg)
        cfg["manual_checks"] = {k: v for k, v in checks.items() if k != day}
        return cfg, None

    try:
        _mutate(config_doc_id(brand_id), change)
    except Exception:  # noqa: BLE001 — the day rolls over anyway
        logger.exception("geo: could not release the manual check for %s", brand_id)


def _reserve_calls(
    brand_id: str, day: str, want: int, *, run_id: str, holder: str,
    manual: bool = False,
) -> Reservation:
    """Claim up to ``want`` engine calls AND the brand's sweep lease, atomically.

Three guards, one transaction, in the order a caller needs to hear them:
    a check already RUNNING (the lease), a check already RUN today (``manual``),
    then the budget. Each refuses with its own ``stop_code`` and an
    ``unlocks_at``, because "somebody is running this, try in a minute",
    "today's check is done, back tomorrow" and "the budget is spent" are three
    different situations with three different answers, and a caller that cannot
    tell them apart gives the wrong one.

    ``manual`` marks a person-driven check, which is limited to once per brand
    per day. The scheduler passes ``False``: it is metered by its own interval
    and must neither be blocked by a manual check nor consume one.

    Budget and lease are claimed in ONE transaction on purpose. Reserving first
    and claiming second leaves exactly the window this exists to close — both
    callers reserve, both then find the lease free in turn, and the second bills
    a batch of work the first is already doing.

    The claim happens BEFORE the calls are made, for the reason it always did:
    counting them afterwards — however atomically — still lets two concurrent
    steps both decide there is room and both spend it.
    """

    def change(cfg: dict) -> tuple[dict, Reservation]:
        now = dt.datetime.now(dt.timezone.utc)
        cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)
        used = int((cfg.get("counters") or {}).get(day, 0))

        held = _live_lease(cfg, now)
        if held is not None and held.get("run_id") != run_id:
            # Somebody else has the brand right now. Reserve nothing.
            return cfg, Reservation(
                0, used, cap, STOP_LEASE_HELD,
                str(held.get("holder") or "another sweep"),
                str(held.get("expires_at") or "") or None,
            )

        check = (cfg.get("manual_checks") or {}).get(day)
        checked_by_someone_else = (
            manual and isinstance(check, dict) and check.get("run_id") != run_id
        )
        if checked_by_someone_else:
            # One manual check per brand per day, whoever clicks. A fresh UTC
            # day buys ONE ~440-call sweep, not one per person per click.
            return cfg, Reservation(
                0, used, cap, STOP_CHECKED_TODAY,
                str(check.get("by") or "somebody in this workspace"),
                _next_day_boundary(),
            )

        granted = max(0, min(want, cap - used))
        cfg = dict(cfg)
        cfg["counters"] = dict(cfg.get("counters") or {}) | {day: used + granted}
        if granted:
            # Only a step that will actually execute takes the lease or spends
            # the day's check: a step the budget grants nothing to polls
            # nothing, so it must not lock anyone out of anything.
            cfg["poll_lease"] = _lease_doc(holder, run_id, now)
            if manual and not isinstance(check, dict):
                # Claimed HERE, inside the transaction that reserves the calls,
                # for the reason the lease is: two people clicking together
                # would otherwise both find the day unclaimed and both start a
                # full sweep.
                cfg["manual_checks"] = dict(cfg.get("manual_checks") or {}) | {
                    day: {"at": _now(), "by": holder or "a console", "run_id": run_id},
                }
        return _trim_counters(cfg), Reservation(granted, used, cap)

    return _mutate(config_doc_id(brand_id), change)


def _settle_calls(
    brand_id: str,
    day: str,
    granted: int,
    spent: int,
    aio_credits: int,
    engine_failed: dict[str, bool],
    engine_stored: dict[str, int] | None = None,
    engine_totals: dict[str, int] | None = None,
    lease_run_id: str = "",
) -> dict:
    """Hand back the unspent reservation, bank SERP credits, update fail streaks.

    One atomic write closing out the step. Returns
    ``{"used": int, "aio_month": int, "streaks": {engine: consecutive_fails},
    "steps": int}`` — ``steps`` being how many steps today's sweep has taken
    so far, which only this transaction can count exactly.

    ``engine_totals`` is ``{engine: answers now stored in today's day-doc}``,
    straight out of the merge transactions that just ran. Banking it here costs
    nothing — this write was happening anyway — and it is what lets the brand
    listing answer "how many answers this week" without fetching a week of
    answer text to take a ``len``.

    It is also where the sweep lease is REFRESHED, for the same reason: the step
    just finished a batch, this write was happening anyway, and a lease that
    only ever expired would drop a long cron sweep on the floor mid-run.
    """

    def change(cfg: dict) -> tuple[dict, dict]:
        cfg = dict(cfg)
        # ONE instant for everything this transaction stamps. Two `_now()` calls
        # in the same write are microseconds apart, and the panel reads two of
        # these as one sentence — "AI Overview last measured X, paused since Y".
        # Y landing before X on the very step that did both is a sentence that
        # reads as a bug in the clock.
        stamp = _now()
        if lease_run_id:
            # Only if the lease is still ours. A step slow enough to have lost
            # it must not steal it back from whoever legitimately claimed it.
            lease = cfg.get("poll_lease")
            if isinstance(lease, dict) and lease.get("run_id") == lease_run_id:
                cfg["poll_lease"] = _lease_doc(
                    str(lease.get("holder") or ""), lease_run_id,
                    dt.datetime.now(dt.timezone.utc),
                )
        counters = dict(cfg.get("counters") or {})
        counters[day] = max(0, int(counters.get(day, 0)) - (granted - spent))
        cfg["counters"] = counters
        if aio_credits:
            aio = dict(cfg.get("counters_aio") or {})
            aio[_month()] = int(aio.get(_month(), 0)) + aio_credits
            cfg["counters_aio"] = aio
        # WHEN the billed Google engines ran out of month, stamped once at the
        # transition. The step already recomputes "capped" from the counter, and
        # that is enough to DROP the engines from the sweep -- it is not enough
        # to say when they stopped, because nothing anywhere records the moment.
        # Their counts then simply freeze, and a frozen number with no date on
        # it reads as a measurement bug rather than a spent budget. Evaluated on
        # every settle, not only a SERP one, so the month rolling over clears it
        # on the next chat-only step instead of leaving last month's date up.
        if aio_used_month(cfg) >= serp_monthly_cap(cfg):
            if not cfg.get("serp_capped_since"):
                cfg["serp_capped_since"] = stamp
        else:
            cfg.pop("serp_capped_since", None)
        health = cfg.get("poll_health") or {}
        # streaks and the step count are per-day: a new day starts clean
        same_day = health.get("day") == day
        streaks = dict(health.get("streaks") or {}) if same_day else {}
        for engine, failed in engine_failed.items():
            streaks[engine] = streaks.get(engine, 0) + 1 if failed else 0
        steps = (int(health.get("steps") or 0) if same_day else 0) + 1
        cfg["poll_health"] = {"day": day, "streaks": streaks, "steps": steps}
        # When an engine last produced a usable answer. The report window is
        # short and AIO runs once per prompt where chat engines run three
        # times, so AIO ages out first and used to vanish from the panel
        # entirely -- which reads as "this engine is broken" rather than "this
        # engine was last measured on the 11th".
        seen = dict(cfg.get("engine_last_seen") or {})
        for engine, stored in (engine_stored or {}).items():
            if stored:
                seen[engine] = stamp
        if seen:
            cfg["engine_last_seen"] = seen
        if engine_totals:
            # per-engine, because a step only ever touches the engines it
            # polled: a day total could not be maintained without re-reading
            # the engines this step left alone.
            counts = dict(cfg.get("answer_counts") or {})
            counts[day] = (counts.get(day) or {}) | {
                engine: int(total) for engine, total in engine_totals.items()
            }
            cfg["answer_counts"] = counts
        cfg = _trim_counters(cfg)
        return cfg, {
            "used": counters[day],
            "aio_month": int((cfg.get("counters_aio") or {}).get(_month(), 0)),
            "streaks": streaks,
            "steps": steps,
        }

    return _mutate(config_doc_id(brand_id), change)


def _merge_answers(
    brand_id: str, engine: str, day: str, records: list[dict]
) -> list[dict]:
    """Append this step's records to the engine's day-doc, atomically; return
    the answers the doc holds afterwards.

    Re-reads inside the transaction so a concurrent step's answers survive
    instead of being overwritten by our stale in-memory copy.

    What comes back is the day-doc's true post-merge content — after retry
    replacement and after overflow trimming — which is the only place it is
    known for free. Its length is what ``_settle_calls`` banks on the config so
    the brand listing can report ``recent_answers`` without hydrating the
    corpus, and the run log summarises the sweep from it without re-reading
    the day.
    """

    def change(doc: dict) -> tuple[dict, list[dict]]:
        doc = dict(doc or {})
        doc.setdefault("brand_id", brand_id)
        doc.setdefault("engine", engine)
        doc.setdefault("date", day)
        answers = list(doc.get("answers") or [])
        for record in records:
            # ONE record per (prompt_id, run), unconditionally. The rule used to
            # be "a retry replaces the task's earlier ERROR record", which kept
            # the doc free of 429 piles but let two successful answers for the
            # same task both land — and ``geo_metrics.mention_stats`` counts
            # ``n_answers`` per record while grouping by prompt, so the day's
            # rate was then averaged over a doubled sample and the banked
            # ``answer_counts`` were permanently wrong.
            #
            # The sweep lease is what stops a task being executed twice; this is
            # the defence in depth that stops a duplicate that slips through
            # from corrupting the measurement. No legitimate caller is affected:
            # ``poll_step`` is the only writer here and it only ever polls tasks
            # ``_pending_tasks`` reports as still owed, so a second successful
            # record for one (prompt_id, run) has no honest way to exist.
            answers = [
                a for a in answers
                if not (
                    a.get("prompt_id") == record["prompt_id"]
                    and a.get("run") == record["run"]
                )
            ]
            answers.append(record)
        doc["answers"] = answers
        # Records are slimmed at the source (text + citation caps in
        # EngineAnswer.to_dict); this is the honest last resort if a doc still
        # balloons past Firestore's 1 MB: drop oldest answers, say so.
        while len(json.dumps(doc["answers"])) > DOC_TRIM_BYTES and doc["answers"]:
            doc["answers"].pop(0)
            doc["overflow_trimmed"] = int(doc.get("overflow_trimmed", 0)) + 1
        return doc, list(doc["answers"])

    return _mutate(poll_doc_id(brand_id, engine, day), change)


# ``geo_history`` stores its per-sweep points in a state doc of the same shape
# and needs the same atomic read-modify-write. Public alias rather than a
# second copy of the helper — two implementations of "mutate a GEO doc" is how
# one of them ends up non-transactional.
mutate = _mutate


# --------------------------------------------------------------- terminal ----

def _failure_summary(failures: dict[str, list[str]]) -> str:
    """Name the engine and quote its actual failure — never a generic string."""
    return "; ".join(
        f"{engine} ({len(errors)}x): {errors[0][:200]}"
        for engine, errors in sorted(failures.items())
    )


def _terminal_signal(
    batch_size: int, failures: dict[str, list[str]], streaks: dict[str, int]
) -> tuple[bool, str | None]:
    """Should the UI stop polling, and why (in words a human can act on)?"""
    failed_calls = sum(len(errors) for errors in failures.values())
    if batch_size and failed_calls >= batch_size:
        return True, (
            f"every one of the {batch_size} engine calls in this batch failed — "
            f"{_failure_summary(failures)}"
        )
    stuck = sorted(e for e, streak in streaks.items() if streak >= FAIL_STREAK_LIMIT)
    if stuck:
        return True, "; ".join(
            f"{engine} has failed every call in {streaks[engine]} consecutive "
            f"batches — {_failure_summary({engine: failures[engine]})}"
            if failures.get(engine)
            else f"{engine} has failed every call in {streaks[engine]} consecutive batches"
            for engine in stuck
        )
    return False, None


def _progress(
    *,
    done: int,
    total: int,
    used: int,
    cap: int,
    engines: list[str],
    day: str,
    aio_capped: bool,
    aio_credits_month: int,
    capped: bool = False,
    terminal: bool = False,
    terminal_reason: str | None = None,
    lease_held_by: str | None = None,
    stop_code: str | None = None,
    unlocks_at: str | None = None,
) -> dict:
    """The one response shape ``poll_step`` returns, on every path.

    ``terminal``/``terminal_reason`` are a cross-agent contract with the console
    poll loop — always present, never renamed.

``stop_code`` says WHICH refusal this is (``STOP_*``), ``unlocks_at`` when
    it clears, and ``lease_held_by`` who is responsible where somebody is. All
    ``None`` on a healthy step. They are fields rather than something to parse
    out of ``terminal_reason`` because the console has to word four refusals
    differently — a check running, a check already done today, the daily budget,
    a dead engine — and prose is not an API.

    They describe the REFUSAL, not the sweep that caused it: nothing here
    exposes a running sweep's state. ``done``/``total`` are the day's stored
    progress, the same numbers ``poll_status`` gives any reader.
    """
    return {
        "done": done,
        "total": total,
        "calls_used_today": used,
        "daily_cap": cap,
        "capped": capped,
        "engines": engines,
        "date": day,
        "aio_capped": aio_capped,
        "aio_credits_month": aio_credits_month,
        "terminal": terminal,
        "terminal_reason": terminal_reason,
        "lease_held_by": lease_held_by,
        "stop_code": stop_code,
        "unlocks_at": unlocks_at,
    }


def _usable_engines(engines: list[str] | None) -> list[str]:
    """Engines we may actually call: requested (or all), minus unconfigured."""
    availability = geo_engines.available_engines()
    wanted = engines or [e for e, ok in availability.items() if ok]
    return [e for e in wanted if availability.get(e)]


def _total_tasks(prompts: list[dict], usable: list[str], runs: int) -> int:
    """Engine calls a complete sweep costs.

    Each engine's spec decides its prompts and its runs: SERP engines are
    snapshots, once per prompt and only over the discovery prompts, so billed
    calls are not spent on questions that already name us; chat engines are
    sampled ``runs`` times over everything. This MUST agree with
    :func:`_pending_tasks`, or a sweep can never report itself finished.
    """
    return sum(expected_answers(prompts, usable, runs).values())


def _answers_for(batch: list[tuple[str, dict, int]]) -> list[Any]:
    """Poll every task in the batch concurrently, results in submission order.

    Order matters: the caller settles budget and failure streaks by walking
    this list, and a batch that settled in completion order would make those
    counters depend on provider latency. A single-task batch skips the pool
    entirely so the common test path stays synchronous.
    """
    if len(batch) <= 1:
        return [geo_engines.poll_engine(e, p["text"]) for e, p, _ in batch]
    workers = min(POLL_CONCURRENCY, len(batch))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="geo-poll") as pool:
        # ``map`` preserves input order and re-raises in order; poll_engine is
        # documented never to raise, so this is belt-and-braces.
        return list(pool.map(lambda task: geo_engines.poll_engine(task[0], task[1]["text"]), batch))


def poll_status(brand: dict, *, engines: list[str] | None = None,
                runs: int = DEFAULT_RUNS) -> dict:
    """What the panel needs to say when nobody is watching a progress bar:
    how much of today's sweep is done, when the next one is due, and whether
    the billed Google engines are paused on a spent budget."""
    cfg = ensure_config(brand)
    usable = _usable_engines(engines)
    credit = search_credit_state(cfg, engines)
    prompts = geo_prompts.enabled_prompts(brand["id"])
    docs = _load_day_docs(brand["id"], usable, _today())
    pending = _pending_tasks(prompts, docs, runs)
    total = _total_tasks(prompts, usable, runs)
    due, reason = poll_due(cfg)
    check = (cfg.get("manual_checks") or {}).get(_today())
    return {
        "brand_id": brand["id"],
        "pending": len(pending),
        "done": max(total - len(pending), 0),
        "total": total,
        # What "Check now" may do right now. Without these the button can only
        # discover it is blocked by being pressed, which is the dead-button
        # experience this exists to avoid.
        "manual_check_used": isinstance(check, dict),
        "manual_check_by": (check or {}).get("by") if isinstance(check, dict) else None,
        "manual_check_unlocks_at": _next_day_boundary() if isinstance(check, dict) else None,
        # Why the two Google engines stopped moving, WITHOUT pressing anything.
        # ``poll_step`` has always returned this, so a spent SERP budget was
        # discoverable only by STARTING a check -- meanwhile the panel showed
        # frozen AI Overview counts beside live chat counts, which is
        # indistinguishable from the engine being broken. The limit rides along
        # so the copy can read "2,000 of 2,000" rather than a bare number the
        # reader has to already know the budget to interpret, and the date joins
        # to ``engine_last_seen[engine]`` on the report: "AI Overview last
        # measured 31 Aug, paused since 1 Sep".
        **credit,
        # The same three facts under the names ``poll_step`` has always used.
        # Duplicated rather than renamed: the console's poll loop reads the
        # ``aio_*`` vocabulary today and the stored keys are ``counters_aio`` /
        # ``aio_monthly_cap``, so the honest names cannot replace them without a
        # document migration. ``search_credit_*`` above is the name to build
        # against; these are the compatibility half, and the whole reason they
        # are computed once above is that two spellings must never be able to
        # disagree about one brand.
        "aio_capped": credit["search_credit_spent"],
        "aio_credits_month": credit["search_credit_used"],
        "aio_monthly_cap": credit["search_credit_limit"],
        "auto_poll": bool(cfg.get("auto_poll", True)),
        "interval_days": interval_days(cfg),
        "last_completed_at": cfg.get("last_poll_completed_at"),
        "next_due_at": next_due_at(cfg),
        "due_now": due,
        "due_reason": reason,
    }


def interval_days(cfg: dict) -> int:
    """Days between scheduled sweeps for this brand.

    NOT ``geo_window.clamp_days``, despite sharing its 1..30 bounds — this is a
    cadence, that is a measured window, and the day they need different ceilings
    is the day a shared helper silently breaks one of them.

    Public (it was ``_interval_days``) because the brand listing shows the
    cadence next to the scheduled-check switch, and a router reaching into a
    private name is how a second, drifting copy of this clamp gets written.
    """
    # `or DEFAULT` would be wrong here: an explicit 0 is out of range and must
    # clamp to "every day", not silently become the two-day default
    raw = cfg.get("poll_interval_days")
    if raw is None:
        return DEFAULT_POLL_INTERVAL_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_DAYS
    return max(1, min(days, 30))


def _parse_at(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def next_due_at(cfg: dict) -> str | None:
    """When this brand's next scheduled sweep may start. ``None`` = never
    polled, so it is due immediately rather than at some invented date."""
    last = _parse_at(cfg.get("last_poll_completed_at"))
    if last is None:
        return None
    return (last + dt.timedelta(days=interval_days(cfg))).isoformat(timespec="seconds")


def staleness_rank(cfg: dict) -> float:
    """How overdue this brand is, as a sortable number (higher = staler).

    A cron fire has a finite budget, so when it cannot reach every due brand the
    order decides whose data rots. Never-polled brands outrank everything, then
    brands whose last sweep never completed, then oldest-completed first.
    """
    last = _parse_at(cfg.get("last_poll_completed_at"))
    if last is None:
        return float("inf")
    return -last.timestamp()


def poll_due(cfg: dict, *, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Is a scheduled sweep due for this brand, and why (or why not).

    The reason is returned rather than logged because it is what the cron's
    response body shows a human debugging a sweep that "did not run".
    """
    if not cfg.get("auto_poll", True):
        return False, "auto-poll is off for this brand"
    last = _parse_at(cfg.get("last_poll_completed_at"))
    if last is None:
        return True, "never polled"
    now = now or dt.datetime.now(dt.timezone.utc)
    due_at = last + dt.timedelta(days=interval_days(cfg))
    if now >= due_at:
        return True, f"last completed {last.date().isoformat()}, due {due_at.date().isoformat()}"
    return False, f"next due {due_at.date().isoformat()}"


def mark_poll_completed(brand_id: str) -> None:
    """Stamp the end of a full sweep. Only a sweep that actually finished may
    move the schedule — a budget-truncated run must stay due so the next fire
    resumes it instead of skipping two days of data."""
    def change(doc: dict) -> tuple[dict, None]:
        doc = dict(doc or {})
        doc["brand_id"] = brand_id
        doc["last_poll_completed_at"] = _now()
        doc["updated_at"] = _now()
        return doc, None

    _mutate(config_doc_id(brand_id), change)


def poll_until_done(
    brand: dict,
    *,
    engines: list[str] | None = None,
    runs: int = DEFAULT_RUNS,
    batch_size: int = DEFAULT_BATCH,
    budget_seconds: float = DEFAULT_CRON_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Run steps back-to-back until the sweep finishes or the budget runs out.

    This is the unattended path: no browser, no progress bar. It stops for the
    same honest reasons a step does — terminal engine failure, daily cap, a
    sweep somebody else is already running — and otherwise for the wall clock,
    leaving the remainder pending for the next fire. Only a genuinely finished
    sweep stamps the schedule.

    It holds ONE sweep lease across every step (see :func:`_reserve_calls`), so
    its own steps never lock each other out and a console poll that arrives
    mid-sweep is refused instead of re-billing the batch this loop is running.
    """
    run_id = _new_run_id()
    holder = f"scheduled sweep {run_id[:8]}"
    deadline = clock() + budget_seconds
    steps = 0
    progress: dict | None = None
    stop = "completed"
    try:
        while True:
            if clock() >= deadline:
                stop = "budget exhausted — remaining tasks resume on the next run"
                break
            progress = poll_step(
                brand, engines=engines, runs=runs, batch_size=batch_size,
                trigger="cron", holder=holder, lease_run_id=run_id,
            )
            steps += 1
            if progress.get("terminal"):
                stop = progress.get("terminal_reason") or "terminal"
                break
            if progress.get("done", 0) >= progress.get("total", 0):
                break
        if progress is None:  # budget was zero or negative — nothing was attempted
            progress = poll_status(brand, engines=engines, runs=runs)
            stop = "no time budget"
        finished = (
            progress.get("done", 0) >= progress.get("total", 0)
            and not progress.get("terminal")
        )
        if finished:
            mark_poll_completed(brand["id"])
        elif steps and not progress.get("lease_held_by"):
            # ran out of wall clock rather than work: the answers collected are
            # still today's measurement, so the chart gets its point either way.
            # A refused sweep polled nothing, so it has nothing to bank — the
            # sweep that holds the lease will record the day's point itself.
            _record_history(brand, ensure_config(brand), _today())
        return {
            "brand_id": brand["id"],
            "steps": steps,
            "done": progress.get("done", 0),
            "total": progress.get("total", 0),
            "completed": finished,
            "stopped_because": stop,
            "terminal_reason": progress.get("terminal_reason"),
            "lease_held_by": progress.get("lease_held_by"),
            "stop_code": progress.get("stop_code"),
            "unlocks_at": progress.get("unlocks_at"),
            "calls_used_today": progress.get("calls_used_today"),
        }
    finally:
        # Whatever stopped this loop — a finished sweep, the wall clock, a dead
        # engine, an exception on the way out — the brand goes back on the shelf
        # HERE. An unreleased cron lease blocks every poll of this brand until
        # the TTL expires, and one leaked at the end of an 800s budget run is
        # precisely the failure that gets discovered by the morning being wrong.
        # Run-id matched, so refusing to claim somebody else's lease above does
        # not release it here either.
        _release_lease(brand["id"], run_id)


def poll_step(
    brand: dict,
    engines: list[str] | None = None,
    runs: int = DEFAULT_RUNS,
    batch_size: int = DEFAULT_BATCH,
    *,
    trigger: str = "manual",
    holder: str = "",
    lease_run_id: str | None = None,
) -> dict:
    """Execute up to ``batch_size`` engine calls; return progress for the UI loop.

    ``trigger`` names who is driving the loop — ``"manual"`` from the console,
    ``"cron"`` from :func:`poll_until_done` — and is recorded on the run log
    entry when this step turns out to be the one that ends the sweep.

    ``holder`` is the human label another caller sees if it collides with this
    one — the signed-in account for a console poll, the sweep id for the cron.

    ``trigger`` also decides whether the once-a-day manual gate applies:
    anything but :data:`SCHEDULED_TRIGGER`, driven by a NAMED loop, is a
    person-driven check, and a brand allows one per UTC day whoever clicks. The
    scheduler is metered by its own interval instead, so a manual check neither
    blocks it nor uses up its turn.

    ``lease_run_id`` names the LOOP this step belongs to, and it is what makes
    "a check is already running for this brand" a stable answer. A caller that
    names its loop — the cron via :func:`poll_until_done`, a console via the
    client token :func:`loop_run_id` builds — holds the brand from its first
    step to its last, so a second person is refused consistently every time
    they try. A caller that names no loop holds the lease for the duration of
    one step: still no duplicated work, but two such callers interleave
    batch-by-batch and neither can be told who is running the check.

    Held across a loop the lease is still bounded by ``POLL_LEASE_TTL_SECONDS``
    — it is only ever refreshed by a step that actually ran, so a client that
    walks away frees the brand on the TTL and a token buys nobody a longer
    hold than a sweep that is genuinely in progress.
    """
    cfg = ensure_config(brand)
    usable = _usable_engines(engines)
    if not usable:
        raise CredentialMissing(
            "No engine keys configured — add a Perplexity/Gemini/OpenAI key in "
            "Settings → Secrets"
        )

    prompts = geo_prompts.enabled_prompts(brand["id"])
    if not prompts:
        raise ValueError("No enabled prompts — generate the prompt universe first")

    # joint monthly spend guard for the billed SERP engines: when the month's
    # budget is spent they simply leave the panel until next month — honestly
    # reported, never a surprise bill or a silent hole
    aio_capped = serp_capped(cfg, usable)
    if aio_capped:
        usable = [e for e in usable if e not in geo_engines.SERP_ENGINES]
        if not usable:
            raise ValueError("SERP monthly call budget is spent — resumes next month")

    day = _today()
    docs = _load_day_docs(brand["id"], usable, day)
    tasks = _pending_tasks(prompts, docs, runs)
    total = _total_tasks(prompts, usable, runs)
    done_before = total - len(tasks)
    cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)

    if not tasks:  # the day is complete — nothing to reserve, nothing to spend
        if lease_run_id:
            # A named loop reaching a finished day IS that loop finishing, and
            # the brand must not stay locked until the TTL over a step that
            # found nothing left to do.
            _release_lease(brand["id"], lease_run_id)
        return _progress(
            done=done_before, total=total, used=used_today(cfg), cap=cap,
            engines=usable, day=day, aio_capped=aio_capped,
            aio_credits_month=aio_used_month(cfg),
        )

    # Claim the budget AND the brand, in one transaction, before spending
    # anything — so overlapping steps can neither both decide there is room for
    # a full batch nor both be handed the same batch. ``granted`` is what we may
    # bill; ``lease_held_by`` is set only when somebody else already has this
    # brand.
    owns_lease = lease_run_id is None
    run_id = lease_run_id or _new_run_id()
    # The once-a-day gate needs a loop it can recognise across steps, or a
    # caller would refuse its own second batch. The router ALWAYS names one
    # (client token, else the session, else the account), so every Check Now
    # arrives gated; a direct library call from a script or a test names none
    # and is not what this limit is about.
    manual = trigger != SCHEDULED_TRIGGER and lease_run_id is not None
    res = _reserve_calls(
        brand["id"], day, min(batch_size, len(tasks)),
        run_id=run_id, holder=holder or f"{trigger} poll", manual=manual,
    )
    used, cap = res.used, res.cap
    if res.stop_code == STOP_LEASE_HELD:
        # Refused, plainly. Nothing was reserved, nothing was billed, and the
        # caller is told a check is already running, by whom, and when it frees
        # up. Terminal so the caller stops rather than spinning on an endpoint
        # that will keep refusing it.
        return _progress(
            done=done_before, total=total, used=used, cap=cap, engines=usable,
            day=day, aio_capped=aio_capped, aio_credits_month=aio_used_month(cfg),
            terminal=True, stop_code=res.stop_code, unlocks_at=res.unlocks_at,
            lease_held_by=res.refused_by,
            terminal_reason=(
                f"a check is already running for this brand ({res.refused_by})"
                f" — you cannot start another one until it finishes"
            ),
        )
    if res.stop_code == STOP_CHECKED_TODAY:
        return _progress(
            done=done_before, total=total, used=used, cap=cap, engines=usable,
            day=day, aio_capped=aio_capped, aio_credits_month=aio_used_month(cfg),
            terminal=True, stop_code=res.stop_code, unlocks_at=res.unlocks_at,
            terminal_reason=(
                f"this brand has already been checked today (started by "
                f"{res.refused_by}) — one check per brand per day; the next one "
                f"unlocks at {res.unlocks_at}"
            ),
        )
    granted = res.granted
    if granted <= 0:
        if lease_run_id:
            # Out of budget ends this loop, so hand the brand back now instead
            # of holding it for a TTL over a sweep that cannot continue.
            _release_lease(brand["id"], lease_run_id)
        return _progress(
            done=done_before, total=total, used=used, cap=cap, engines=usable,
            day=day, aio_capped=aio_capped, aio_credits_month=aio_used_month(cfg),
            capped=True, terminal=True, stop_code=STOP_DAILY_CAP,
            unlocks_at=_next_day_boundary(),
            terminal_reason=(
                f"daily engine-call cap reached — {used} of {cap} calls used "
                f"today; polling resumes tomorrow or after raising the cap"
            ),
        )

    batch = tasks[:granted]
    aliases = alias_map(cfg)
    records: dict[str, list[dict]] = {}
    attempts: dict[str, int] = {}
    failures: dict[str, list[str]] = {}
    serp_credits = 0
    spent = 0
    # engine -> the day-doc's answers as they stand after this step's merge;
    # engines this step did not touch keep the copy loaded above
    stored: dict[str, list[dict]] = {}
    try:
        # The network waits overlap; everything that touches a counter stays on
        # this thread, in submission order, so budgets and streaks behave
        # exactly as they did when the loop was serial.
        for (engine, prompt, run), answer in zip(batch, _answers_for(batch)):
            spent += 1  # billed the moment the call goes out, success or not
            attempts[engine] = attempts.get(engine, 0) + 1
            if engine in geo_engines.SERP_ENGINES:
                serp_credits += getattr(answer, "credits", 1)
            record = answer.to_dict() | {
                "prompt_id": prompt["id"],
                "prompt_text": prompt["text"],
                "intent": prompt.get("intent", ""),
                # the buyer the prompt is written as; optional on the prompt,
                # always present on the record so readers need no default
                "persona": str(prompt.get("persona") or ""),
                "run": run,
                "at": _now(),
            }
            if answer.error:
                failures.setdefault(engine, []).append(str(answer.error))
            else:
                mentions = detect_mentions(answer.text, aliases)
                record["mentions"] = mentions
                record["brand_mentioned"] = "self" in mentions
                record["brand_position"] = mentions.get("self")
                own_domain = (brand.get("domain") or "").lower().removeprefix("www.")
                record["brand_cited"] = bool(own_domain) and any(
                    (c.get("domain") or "").endswith(own_domain)
                    for c in record["citations"]
                )
                # Sentiment only on run 1 of answers that mention the brand —
                # one cheap LLM call per prompt/engine instead of per run.
                if record["brand_mentioned"] and run == 1:
                    record["sentiment"] = _sentiment(
                        prompt["text"], answer.text, brand.get("name", "")
                    )
            records.setdefault(engine, []).append(record)
    finally:
        # Whatever happened, store the answers we already paid for and hand
        # back the part of the reservation we never spent — an exception must
        # not leave the day's budget charged for calls that never went out.
        try:
            for engine, engine_records in records.items():
                stored[engine] = _merge_answers(brand["id"], engine, day, engine_records)
            settled = _settle_calls(
                brand["id"], day, granted, spent, serp_credits,
                {e: len(failures.get(e, [])) == n for e, n in attempts.items()},
                # "no AI Overview shown" is a successful observation, so it counts
                # as the engine having been measured
                {e: sum(1 for r in recs if not r.get("error")) for e, recs in records.items()},
                engine_totals={e: len(answers) for e, answers in stored.items()},
                lease_run_id=run_id,
            )
        finally:
            # Nested so the brand is handed back even if the bookkeeping above
            # raises: a lease leaked here blocks every poll of this brand until
            # the TTL expires, which is a far worse failure than the one that
            # caused it.
            if owns_lease:
                _release_lease(brand["id"], run_id)

    terminal, terminal_reason = _terminal_signal(
        len(batch), failures, settled["streaks"]
    )
    done_now = done_before + len(batch)
    completed = done_now >= total and not terminal
    # Bank the day's trend point whenever the loop is about to STOP — finished,
    # or stopped honestly. Waiting for a completed sweep meant waiting for
    # something that has never happened in production: the cron's wall clock is
    # smaller than a full sweep, so the chart would only ever hold reconstructed
    # points. A truncated day is a real measurement of a smaller sample, and it
    # is already marked `partial` on the chart.
    day_answers_now = [
        a for e in docs
        for a in (stored[e] if e in stored else docs[e].get("answers") or [])
    ]
    if done_now >= total or terminal:
        if not owns_lease:
            # The sweep is over. Hand the brand back now rather than making the
            # next caller wait out a TTL for a holder that has already stopped.
            _release_lease(brand["id"], run_id)
        if manual and not any(not a.get("error") for a in day_answers_now):
            # Claimed the day's check, measured nothing at all — a dead key or
            # an outage must not cost somebody their one check. Give it back and
            # say so; the terminal reason already names the engine that failed.
            _release_manual_check(brand["id"], day, run_id)
        _record_history(brand, cfg, day)
        # A sweep the console drove to the end is as complete as one the cron
        # drove; the schedule moves on either, or the brand stays "due" and
        # the next cron fire re-polls a day that is already fully measured.
        if completed:
            mark_poll_completed(brand["id"])
        _record_run(
            brand, day, day_answers_now,
            trigger=trigger, steps=settled["steps"], done=done_now, total=total,
            completed=completed, terminal_reason=terminal_reason,
        )
    return _progress(
        done=done_now, total=total, used=settled["used"], cap=cap,
        engines=usable, day=day, aio_capped=aio_capped,
        aio_credits_month=settled["aio_month"],
        terminal=terminal, terminal_reason=terminal_reason,
        stop_code=STOP_ENGINE_FAILED if terminal else None,
    )


# The three brand-id-only readers below are thin adapters over
# ``geo_window``: existing callers (and their tests) pass a brand id and want a
# list back, while the window is keyed on the brand dict so it can also resolve
# the config and the alias map. Anything that needs more than raw answers —
# every read path in the router — should open the window itself and get the
# report for free instead of re-assembling it.
def recent_answers(brand_id: str, days: int = geo_window.DEFAULT_DAYS) -> list[dict]:
    """All stored answers for the brand across engines over the last N days."""
    return geo_window.open_window({"id": brand_id}, days).answers


def day_answers(brand_id: str, day: str) -> list[dict]:
    """Every stored answer for one UTC day, across every engine."""
    return geo_window.open_day({"id": brand_id}, day).day(day)


def answers_by_day(
    brand_id: str, days: int = geo_window.MAX_DAYS
) -> dict[str, list[dict]]:
    """day (YYYYMMDD) -> that day's answers, for days that have any."""
    return geo_window.open_window({"id": brand_id}, days).by_day


# ------------------------------------------------- stored-answer counter ----
# The brand listing wants ONE integer per brand — "how many answers this week"
# — and used to get it by hydrating the whole window and taking a ``len``: 28
# day-doc fetches per brand, each up to DOC_TRIM_BYTES, megabytes deserialised
# to produce a number. The count is now maintained on the config document (the
# document the listing already reads) by the same ``_settle_calls`` transaction
# that already maintains the spend counters, the failure streaks and
# ``engine_last_seen`` — same kind of fact, same owner, no second document to
# keep in sync.


def _rebuild_answer_counts(brand: dict) -> dict[str, dict[str, int]]:
    """One-time reconstruction of the counter from the day-docs themselves.

    A brand that was already polling before the counter existed has no stored
    counts, and the two dishonest ways out are both unacceptable: showing zero
    would tell a brand with a month of data that it has never been polled, and
    re-counting the window on every listing is the cost this exists to remove.

    So it is reconstructed exactly once — the same move ``geo_history.backfill``
    makes for the trend line, with the same stamp guarding it. One batched
    window read, then the listing is cheap forever.
    """
    fresh = geo_window.open_window(brand, geo_window.MAX_DAYS).answer_counts()

    def change(cfg: dict) -> tuple[dict, dict]:
        cfg = dict(cfg)
        if cfg.get("answer_counts_at"):  # another request got there first
            return cfg, dict(cfg.get("answer_counts") or {})
        stored = dict(cfg.get("answer_counts") or {})
        # A poll that landed while we were reading knows better than the
        # reconstruction, so its per-engine entries win.
        cfg["answer_counts"] = {
            day: (fresh.get(day) or {}) | (stored.get(day) or {})
            for day in sorted(set(fresh) | set(stored))
        }
        cfg["answer_counts_at"] = _now()
        cfg = _trim_counters(cfg)
        return cfg, dict(cfg["answer_counts"])

    return _mutate(config_doc_id(brand["id"]), change)


def recent_answer_count(
    brand: dict, cfg: dict | None, days: int = geo_window.DEFAULT_DAYS
) -> int:
    """How many answers are stored for this brand over the last ``days`` days.

    Exact, not an estimate: the counter holds the day-doc lengths themselves, so
    this is the same integer ``len(recent_answers(...))`` returns — for the cost
    of a document the caller already had.

    ``cfg=None`` means the config could not be read at all. That is the one case
    where the window is counted the expensive way, because the alternative is
    inventing a number for a brand we currently know nothing about.
    """
    if cfg is None:
        return len(geo_window.open_window(brand, days).answers)
    counts = (
        dict(cfg.get("answer_counts") or {})
        if cfg.get("answer_counts_at")
        else _rebuild_answer_counts(brand)
    )
    return sum(
        int(n)
        for day in geo_window.day_ids(days)
        for n in (counts.get(day) or {}).values()
    )


def _rescore_record(record: dict, aliases: dict[str, list[str]], own_domain: str) -> bool:
    """Re-derive one stored answer's mentions from its stored text.

    Returns whether anything changed. Errored answers have no text to read and
    are left exactly as they are.
    """
    if record.get("error"):
        return False
    mentions = detect_mentions(record.get("text") or "", aliases)
    cited = bool(own_domain) and any(
        (c.get("domain") or "").endswith(own_domain)
        for c in record.get("citations") or []
    )
    before = (record.get("mentions"), record.get("brand_cited"))
    record["mentions"] = mentions
    record["brand_mentioned"] = "self" in mentions
    record["brand_position"] = mentions.get("self")
    record["brand_cited"] = cited
    return before != (mentions, cited)


def rescan_mentions(brand: dict, days: int = geo_window.DEFAULT_DAYS) -> dict:
    """Re-read stored answers for a competitor tracked after they were polled.

    Mentions are detected when an answer is stored, so adding a rival normally
    means waiting two days for the next sweep before a single number about them
    exists — which makes "compare us to them" useless in the meeting where
    somebody asks for it. The answer TEXT is already on disk, so their name can
    be found in it now.

    Costs zero engine calls: nothing is re-asked, only re-read. The one honest
    limit is the stored text cap (``geo_engines.ANSWER_TEXT_CAP``) — a name that
    only appeared past the cap in the original answer is not recoverable, and
    the next real sweep is what fixes that.

    **Reads are one batch, writes are serial and only where something changed.**
    This path used to be the odd one out: a nested ``day x engine`` loop doing
    ``state.load`` then ``_mutate`` per pair — 120 serial probes and up to 120
    serial transactions at 30 days — because the batching its three sibling
    readers got was an idiom rather than a module. It now reads one window, and
    then:

    * a doc whose stored scoring already matches the current alias map is
      **not written at all**. Re-running the transaction would rewrite it byte
      for byte and spend a round trip proving nothing changed, and after adding
      one competitor that is nearly every doc in the window.
    * the docs that do change are written **one at a time, on this thread**.
      Not for safety of the documents — each ``_mutate`` is an independent
      transaction on a distinct doc — but because parallel writes buy nothing
      here and cost something: offline, every ``_mutate`` serialises on one
      process-global lock anyway; in cloud mode, firing N transactions at once
      multiplies contention with the live poll writing today's doc, and a
      contended transaction re-runs ``change`` rather than merely waiting. The
      counters (``scanned``, ``changed``, ``touched``) also stay single-threaded,
      which is the rule ``poll_step`` already follows for its budget bookkeeping.
      With the no-op writes gone, writes are no longer the cost anyway.
    """
    window = geo_window.open_window(brand, days)
    cfg = window.cfg
    aliases = window.aliases
    own_domain = window.own_domain

    scanned = changed = 0
    # day -> engine -> the answers as they now stand on disk, so the trend
    # rebuild below does not re-fetch a window we are already holding
    current: dict[str, dict[str, list[dict]]] = {}
    touched_days: list[str] = []

    for (day, engine), doc in window.docs.items():
        rescored = [dict(a) for a in doc.get("answers") or []]
        # Dry run against the copy first: this is the same pure function the
        # transaction applies, so "would change nothing" is a fact, not a guess.
        hits = sum(1 for a in rescored if _rescore_record(a, aliases, own_domain))
        if not hits:
            scanned += len(rescored)
            current.setdefault(day, {})[engine] = rescored
            continue

        def change(stored: dict) -> tuple[dict, tuple[int, int, list[dict]]]:
            stored = dict(stored or {})
            answers = [dict(a) for a in stored.get("answers") or []]
            fresh = sum(
                1 for a in answers if _rescore_record(a, aliases, own_domain)
            )
            stored["answers"] = answers
            if fresh:
                stored["rescanned_at"] = _now()
            return stored, (len(answers), fresh, answers)

        seen, applied, answers = _mutate(poll_doc_id(brand["id"], engine, day), change)
        scanned += seen
        current.setdefault(day, {})[engine] = answers
        if applied:
            changed += applied
            if day not in touched_days:
                touched_days.append(day)

    # The trend's rival lines are computed from the same answers, so leaving
    # them alone would make the Dashboard contradict the Competitors tab. Fed
    # from what we just wrote — re-reading the day here would put four fetches
    # per touched day back on top of the batch we already paid for.
    for day in sorted(touched_days):
        by_engine = current.get(day) or {}
        _record_history(
            brand, cfg, day,
            answers=[a for engine in geo_window.ENGINES
                     for a in by_engine.get(engine, [])],
            preserve_source=True,
        )

    return {
        "brand_id": brand["id"],
        "days": window.days,
        "answers_scanned": scanned,
        "answers_updated": changed,
        "days_updated": sorted(touched_days),
        "entities": sorted(aliases),
        "text_cap": geo_engines.ANSWER_TEXT_CAP,
    }


def _record_history(brand: dict, cfg: dict, day: str, *,
                    answers: list[dict] | None = None,
                    preserve_source: bool = False) -> None:
    """Bank the trend point for a sweep that just completed.

    Best-effort by design: the answers are already paid for and stored, and a
    failure to derive a chart point must not turn a finished sweep into an
    error. It is logged with its traceback, never swallowed silently.

    Pass ``answers`` when the day's records are already in hand (a rescan that
    just wrote them) — otherwise the day is fetched, which is one round trip per
    engine that the caller may already have paid for.
    """
    try:
        # ``open_day`` fetches nothing on its own — it is here for the alias map
        # and the normalised domain. The answers still come through
        # ``day_answers`` (itself a one-batch window read) so the day remains a
        # named, stubbable seam rather than an attribute lookup on an object.
        window = geo_window.open_day(brand, day, cfg=cfg)
        geo_history.record_sweep(
            brand["id"], day,
            day_answers(brand["id"], day) if answers is None else answers,
            window.entities, window.own_domain,
            preserve_source=preserve_source,
        )
    except Exception:  # noqa: BLE001 — derived artifact, never fatal to a sweep
        logger.exception("geo: could not record history point for %s on %s",
                         brand.get("id"), day)


def _run_summary(
    day: str, answers: list[dict], *, trigger: str, steps: int, done: int,
    total: int, completed: bool, terminal_reason: str | None,
    score: float | None, plan_progress: dict | None,
) -> dict:
    """One run-log entry from the day's answers as they now stand on disk.

    ``answers`` spans every engine the sweep could call, so the timings are the
    sweep's — the earliest record is when it started, whichever step wrote
    it — and the counts are the day-doc's, retry replacement included.
    """
    finished = dt.datetime.now(dt.timezone.utc)
    stamps = [t for a in answers if (t := _parse_at(a.get("at"))) is not None]
    started = min(stamps) if stamps else None
    errors: dict[str, int] = {}
    measured: set[str] = set()
    for a in answers:
        if a.get("error"):
            errors[a["engine"]] = errors.get(a["engine"], 0) + 1
        else:
            measured.add(a.get("engine", ""))
    return {
        "day": day,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds(), 1) if started else None,
        "trigger": trigger,
        "steps": steps,
        "done": done,
        "total": total,
        "completed": completed,
        "stopped_because": "completed" if completed else (terminal_reason or "terminal"),
        "terminal_reason": terminal_reason,
        "engines": [e for e in geo_engines.ALL_ENGINES if e in measured],
        "calls": len(answers),
        "errors": errors,
        "no_aio": sum(1 for a in answers if a.get("no_aio")),
        "score": score,
        "plan_progress": plan_progress,
    }


def _record_run(
    brand: dict, day: str, answers: list[dict], *, trigger: str, steps: int,
    done: int, total: int, completed: bool, terminal_reason: str | None,
) -> None:
    """Log the sweep that just ended. Same discipline as ``_record_history``:
    the answers are paid for and stored, so a failure here is logged with its
    traceback and never turns a finished sweep into an error."""
    try:
        point = next(
            (p for p in geo_history.load_points(brand["id"]) if p.get("date") == day),
            None,
        )
        geo_runlog.record_run(brand["id"], _run_summary(
            day, answers, trigger=trigger, steps=steps, done=done, total=total,
            completed=completed, terminal_reason=terminal_reason,
            score=(point or {}).get("score"),
            plan_progress=geo_runlog.plan_progress(brand["id"]),
        ))
    except Exception:  # noqa: BLE001 — derived artifact, never fatal to a sweep
        logger.exception("geo: could not record run log for %s on %s",
                         brand.get("id"), day)
