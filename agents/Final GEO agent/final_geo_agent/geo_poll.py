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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from typing import Any, Callable

from seo_geo_agent import state
from final_geo_agent import geo_engines, geo_history, geo_prompts, geo_window
from seo_geo_agent.sources import CredentialMissing, llm_json

logger = logging.getLogger("agentos.geo.poll")

DEFAULT_DAILY_CAP = 2000
DEFAULT_RUNS = 3
DEFAULT_BATCH = 10  # tasks (engine calls) per step
# A full sweep is ~40 prompts x 3 engines x 3 runs + 40 AIO = ~400 engine calls.
# Run sequentially at ~5s each that is half an hour with a browser tab held
# open, which is what made polling feel unusable. The calls are independent
# network waits, so they overlap; only the bookkeeping stays serial.
# Kept modest on purpose — provider rate limits, not CPU, are the ceiling here.
POLL_CONCURRENCY = 6
# Scheduled polling: the cron fires daily and this decides whether a brand is
# actually due. A day-of-month cron step (``*/2``) would silently double-fire
# across month boundaries (31st -> 1st); an interval measured from the last
# completed sweep does not.
DEFAULT_POLL_INTERVAL_DAYS = 2
# Wall clock one cron invocation may spend on a single brand. Cloud Run kills
# long requests, so a sweep that does not finish leaves its remaining tasks
# pending and the next fire resumes them — ``_pending_tasks`` already derives
# what is left from what is stored.
DEFAULT_CRON_BUDGET_SECONDS = 240
# Google AIO runs on SerpAPI's free ~250 searches/month: one run per prompt
# (SERP content, low variance) under a hard monthly credit guard.
AIO_MONTHLY_CAP = 200
# How many consecutive batches an engine may fail every single call in before
# the poll is declared terminal. 1 batch = a blip worth retrying; 3 in a row on
# the same engine is a dead key or an outage, and its tasks can never complete,
# so the loop can never end on its own — stop and name the engine.
FAIL_STREAK_LIMIT = 3
# Firestore hard-caps docs at 1 MB; day-docs are trimmed to this.
DOC_TRIM_BYTES = 900_000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
        fresh = {
            "brand_id": brand["id"],
            "aliases": {"self": _default_aliases(brand)},
            "competitors": [],  # [{key, name, aliases: [...]}]
            "daily_cap": DEFAULT_DAILY_CAP,
            "poll_interval_days": DEFAULT_POLL_INTERVAL_DAYS,
            "auto_poll": True,
            "counters": {},
            "updated_at": _now(),
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
    per_engine: dict[str, list[tuple[str, dict, int]]] = {}
    for engine, doc in docs.items():
        # only SUCCESSFUL answers complete a task — an errored run (quota,
        # outage) stays pending so the next poll retries it instead of
        # writing the whole day off. ("no AIO shown" counts as completed.)
        done = {
            (a["prompt_id"], a["run"])
            for a in doc.get("answers", []) if not a.get("error")
        }
        engine_runs = 1 if engine == geo_engines.AIO_ENGINE else runs
        per_engine[engine] = [
            (engine, prompt, run)
            for prompt in prompts
            for run in range(1, engine_runs + 1)
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


# --------------------------------------------------------------- atomicity ----
# Offline state is plain JSON files, so a process-local lock is the whole
# guarantee there; that is enough for tests and single-process local dev.
_LOCAL_LOCK = threading.Lock()


def _mutate(doc_id: str, change: Callable[[dict], tuple[dict, Any]]) -> Any:
    """Atomic read-modify-write of one state doc; returns ``change``'s result.

    ``change(current) -> (new_doc, result)`` runs INSIDE the transaction and may
    be retried on contention, so it must be a pure function of ``current``.

    Read-then-write without this is a lost update: two overlapping poll steps
    both read ``used=1990`` against a 2000 cap, each fires ten paid calls, and
    both write 2000 — ten calls billed, zero counted, repeatable forever. The
    same shape drops answer records from a day-doc when two steps overlap.
    """
    if state.use_cloud():
        from google.cloud import firestore

        from app.services import firestore_repo

        # state owns the collection naming — reuse its ref builder rather than
        # keep a second copy of it here. Same cached client the txn runs on.
        ref = state._firestore_doc(doc_id)
        transaction = firestore_repo._db().transaction()

        @firestore.transactional
        def _apply(txn) -> Any:
            snap = ref.get(transaction=txn)
            current = snap.to_dict() if snap.exists else {}
            new_doc, result = change(current or {})
            # same JSON round-trip state.save uses: no dataclasses/dates/sets
            txn.set(ref, json.loads(json.dumps(new_doc, default=str)))
            return result

        return _apply(transaction)

    with _LOCAL_LOCK:
        new_doc, result = change(state.load(doc_id) or {})
        state.save(doc_id, new_doc)
        return result


def _trim_counters(cfg: dict) -> dict:
    """Trailing 30 day-counters / 3 month-counters, so the config never grows."""
    if cfg.get("counters"):
        cfg["counters"] = dict(sorted(cfg["counters"].items())[-30:])
    if cfg.get("counters_aio"):
        cfg["counters_aio"] = dict(sorted(cfg["counters_aio"].items())[-3:])
    if cfg.get("answer_counts"):
        cfg["answer_counts"] = dict(
            sorted(cfg["answer_counts"].items())[-geo_window.MAX_DAYS:]
        )
    return cfg


def _reserve_calls(brand_id: str, day: str, want: int) -> tuple[int, int, int]:
    """Claim up to ``want`` engine calls against the daily cap, atomically.

    Returns ``(granted, used_before, cap)``. The claim happens BEFORE the calls
    are made on purpose: counting them afterwards — however atomically — still
    lets two concurrent steps both decide there is room and both spend it.
    """

    def change(cfg: dict) -> tuple[dict, tuple[int, int, int]]:
        cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)
        used = int((cfg.get("counters") or {}).get(day, 0))
        granted = max(0, min(want, cap - used))
        cfg = dict(cfg)
        cfg["counters"] = dict(cfg.get("counters") or {}) | {day: used + granted}
        return _trim_counters(cfg), (granted, used, cap)

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
) -> dict:
    """Hand back the unspent reservation, bank AIO credits, update fail streaks.

    One atomic write closing out the step. Returns
    ``{"used": int, "aio_month": int, "streaks": {engine: consecutive_fails}}``.

    ``engine_totals`` is ``{engine: answers now stored in today's day-doc}``,
    straight out of the merge transactions that just ran. Banking it here costs
    nothing — this write was happening anyway — and it is what lets the brand
    listing answer "how many answers this week" without fetching a week of
    answer text to take a ``len``.
    """

    def change(cfg: dict) -> tuple[dict, dict]:
        cfg = dict(cfg)
        counters = dict(cfg.get("counters") or {})
        counters[day] = max(0, int(counters.get(day, 0)) - (granted - spent))
        cfg["counters"] = counters
        if aio_credits:
            aio = dict(cfg.get("counters_aio") or {})
            aio[_month()] = int(aio.get(_month(), 0)) + aio_credits
            cfg["counters_aio"] = aio
        health = cfg.get("poll_health") or {}
        # streaks are per-day: a new day starts everyone clean
        streaks = dict(health.get("streaks") or {}) if health.get("day") == day else {}
        for engine, failed in engine_failed.items():
            streaks[engine] = streaks.get(engine, 0) + 1 if failed else 0
        cfg["poll_health"] = {"day": day, "streaks": streaks}
        # When an engine last produced a usable answer. The report window is
        # short and AIO runs once per prompt where chat engines run three
        # times, so AIO ages out first and used to vanish from the panel
        # entirely -- which reads as "this engine is broken" rather than "this
        # engine was last measured on the 11th".
        seen = dict(cfg.get("engine_last_seen") or {})
        for engine, stored in (engine_stored or {}).items():
            if stored:
                seen[engine] = _now()
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
        }

    return _mutate(config_doc_id(brand_id), change)


def _merge_answers(brand_id: str, engine: str, day: str, records: list[dict]) -> int:
    """Append this step's records to the engine's day-doc, atomically; return
    how many answers the doc holds afterwards.

    Re-reads inside the transaction so a concurrent step's answers survive
    instead of being overwritten by our stale in-memory copy.

    The returned count is the day-doc's true post-merge length — after retry
    replacement and after overflow trimming — which is the only place that
    number is known for free. ``_settle_calls`` banks it on the config so the
    brand listing can report ``recent_answers`` without hydrating the corpus.
    """

    def change(doc: dict) -> tuple[dict, int]:
        doc = dict(doc or {})
        doc.setdefault("brand_id", brand_id)
        doc.setdefault("engine", engine)
        doc.setdefault("date", day)
        answers = list(doc.get("answers") or [])
        for record in records:
            # a retry replaces the task's earlier error record — the doc keeps
            # one honest latest attempt per (prompt, run), not a pile of 429s
            answers = [
                a for a in answers
                if not (
                    a.get("prompt_id") == record["prompt_id"]
                    and a.get("run") == record["run"]
                    and a.get("error")
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
        return doc, len(doc["answers"])

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
) -> dict:
    """The one response shape ``poll_step`` returns, on every path.

    ``terminal``/``terminal_reason`` are a cross-agent contract with the console
    poll loop — always present, never renamed.
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
    }


def _usable_engines(engines: list[str] | None) -> list[str]:
    """Engines we may actually call: requested (or all), minus unconfigured."""
    availability = geo_engines.available_engines()
    wanted = engines or [e for e, ok in availability.items() if ok]
    return [e for e in wanted if availability.get(e)]


def _total_tasks(prompts: list[dict], usable: list[str], runs: int) -> int:
    """Engine calls a complete sweep costs. AIO is SERP content with low
    variance, so it runs once per prompt while chat engines run ``runs`` times."""
    return sum(
        len(prompts) * (1 if e == geo_engines.AIO_ENGINE else runs) for e in usable
    )


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
    how much of today's sweep is done, and when the next one is due."""
    cfg = ensure_config(brand)
    usable = _usable_engines(engines)
    prompts = geo_prompts.enabled_prompts(brand["id"])
    docs = _load_day_docs(brand["id"], usable, _today())
    pending = _pending_tasks(prompts, docs, runs)
    total = _total_tasks(prompts, usable, runs)
    due, reason = poll_due(cfg)
    return {
        "brand_id": brand["id"],
        "pending": len(pending),
        "done": max(total - len(pending), 0),
        "total": total,
        "auto_poll": bool(cfg.get("auto_poll", True)),
        "interval_days": _interval_days(cfg),
        "last_completed_at": cfg.get("last_poll_completed_at"),
        "next_due_at": next_due_at(cfg),
        "due_now": due,
        "due_reason": reason,
    }


def _interval_days(cfg: dict) -> int:
    """Days between scheduled sweeps for this brand.

    NOT ``geo_window.clamp_days``, despite sharing its 1..30 bounds — this is a
    cadence, that is a measured window, and the day they need different ceilings
    is the day a shared helper silently breaks one of them.
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
    return (last + dt.timedelta(days=_interval_days(cfg))).isoformat(timespec="seconds")


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
    due_at = last + dt.timedelta(days=_interval_days(cfg))
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
    same honest reasons a step does — terminal engine failure, daily cap — and
    otherwise for the wall clock, leaving the remainder pending for the next
    fire. Only a genuinely finished sweep stamps the schedule.
    """
    deadline = clock() + budget_seconds
    steps = 0
    progress: dict | None = None
    stop = "completed"
    while True:
        if clock() >= deadline:
            stop = "budget exhausted — remaining tasks resume on the next run"
            break
        progress = poll_step(brand, engines=engines, runs=runs, batch_size=batch_size)
        steps += 1
        if progress.get("terminal"):
            stop = progress.get("terminal_reason") or "terminal"
            break
        if progress.get("done", 0) >= progress.get("total", 0):
            break
    if progress is None:  # budget was zero or negative — nothing was attempted
        progress = poll_status(brand, engines=engines, runs=runs)
        stop = "no time budget"
    finished = progress.get("done", 0) >= progress.get("total", 0) and not progress.get("terminal")
    if finished:
        mark_poll_completed(brand["id"])
    elif steps:
        # ran out of wall clock rather than work: the answers collected are
        # still today's measurement, so the chart gets its point either way
        _record_history(brand, ensure_config(brand), _today())
    return {
        "brand_id": brand["id"],
        "steps": steps,
        "done": progress.get("done", 0),
        "total": progress.get("total", 0),
        "completed": finished,
        "stopped_because": stop,
        "terminal_reason": progress.get("terminal_reason"),
        "calls_used_today": progress.get("calls_used_today"),
    }


def poll_step(
    brand: dict,
    engines: list[str] | None = None,
    runs: int = DEFAULT_RUNS,
    batch_size: int = DEFAULT_BATCH,
) -> dict:
    """Execute up to ``batch_size`` engine calls; return progress for the UI loop."""
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

    # monthly credit guard for the SerpAPI free tier: when the month's AIO
    # budget is spent, AIO simply leaves the panel until next month — honestly
    # reported, never a surprise bill or a silent hole
    aio_cap = int(cfg.get("aio_monthly_cap") or AIO_MONTHLY_CAP)
    aio_capped = geo_engines.AIO_ENGINE in usable and aio_used_month(cfg) >= aio_cap
    if aio_capped:
        usable = [e for e in usable if e != geo_engines.AIO_ENGINE]
        if not usable:
            raise ValueError("AIO monthly credit budget is spent — resumes next month")

    day = _today()
    docs = _load_day_docs(brand["id"], usable, day)
    tasks = _pending_tasks(prompts, docs, runs)
    total = _total_tasks(prompts, usable, runs
    )
    done_before = total - len(tasks)
    cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)

    if not tasks:  # the day is complete — nothing to reserve, nothing to spend
        return _progress(
            done=done_before, total=total, used=used_today(cfg), cap=cap,
            engines=usable, day=day, aio_capped=aio_capped,
            aio_credits_month=aio_used_month(cfg),
        )

    # Claim the budget before spending it, so overlapping steps cannot both
    # decide there is room for a full batch. ``granted`` is what we may bill.
    granted, used, cap = _reserve_calls(brand["id"], day, min(batch_size, len(tasks)))
    if granted <= 0:
        return _progress(
            done=done_before, total=total, used=used, cap=cap, engines=usable,
            day=day, aio_capped=aio_capped, aio_credits_month=aio_used_month(cfg),
            capped=True, terminal=True,
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
    aio_credits = 0
    spent = 0
    try:
        # The network waits overlap; everything that touches a counter stays on
        # this thread, in submission order, so budgets and streaks behave
        # exactly as they did when the loop was serial.
        for (engine, prompt, run), answer in zip(batch, _answers_for(batch)):
            spent += 1  # billed the moment the call goes out, success or not
            attempts[engine] = attempts.get(engine, 0) + 1
            if engine == geo_engines.AIO_ENGINE:
                aio_credits += getattr(answer, "credits", 1)
            record = answer.to_dict() | {
                "prompt_id": prompt["id"],
                "prompt_text": prompt["text"],
                "intent": prompt.get("intent", ""),
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
        stored_totals: dict[str, int] = {}
        for engine, engine_records in records.items():
            stored_totals[engine] = _merge_answers(
                brand["id"], engine, day, engine_records
            )
        settled = _settle_calls(
            brand["id"], day, granted, spent, aio_credits,
            {e: len(failures.get(e, [])) == n for e, n in attempts.items()},
            # "no AI Overview shown" is a successful observation, so it counts
            # as the engine having been measured
            {e: sum(1 for r in recs if not r.get("error")) for e, recs in records.items()},
            engine_totals=stored_totals,
        )

    terminal, terminal_reason = _terminal_signal(
        len(batch), failures, settled["streaks"]
    )
    done_now = done_before + len(batch)
    # Bank the day's trend point whenever the loop is about to STOP — finished,
    # or stopped honestly. Waiting for a completed sweep meant waiting for
    # something that has never happened in production: the cron's wall clock is
    # smaller than a full sweep, so the chart would only ever hold reconstructed
    # points. A truncated day is a real measurement of a smaller sample, and it
    # is already marked `partial` on the chart.
    if done_now >= total or terminal:
        _record_history(brand, cfg, day)
    return _progress(
        done=done_now, total=total, used=settled["used"], cap=cap,
        engines=usable, day=day, aio_capped=aio_capped,
        aio_credits_month=settled["aio_month"],
        terminal=terminal, terminal_reason=terminal_reason,
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
