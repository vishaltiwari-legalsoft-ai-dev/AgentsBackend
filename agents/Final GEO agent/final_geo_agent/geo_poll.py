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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from seo_geo_agent import state
from final_geo_agent import geo_engines, geo_prompts
from seo_geo_agent.sources import CredentialMissing, llm_json

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


def poll_doc_id(brand_id: str, engine: str, day: str) -> str:
    return f"geo-poll-{brand_id}-{engine}-{day}"


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
    """Load the brand's GEO config, creating a sensible default on first use."""
    doc = state.load(config_doc_id(brand["id"]))
    if doc:
        return doc
    doc = {
        "brand_id": brand["id"],
        "aliases": {"self": _default_aliases(brand)},
        "competitors": [],  # [{key, name, aliases: [...]}]
        "daily_cap": DEFAULT_DAILY_CAP,
        "poll_interval_days": DEFAULT_POLL_INTERVAL_DAYS,
        "auto_poll": True,
        "counters": {},
        "updated_at": _now(),
    }
    state.save(config_doc_id(brand["id"]), doc)
    return doc


def save_config(brand_id: str, patch: dict) -> dict:
    doc = state.load(config_doc_id(brand_id)) or {}
    for key in ("aliases", "competitors", "daily_cap", "aio_monthly_cap",
                "poll_interval_days", "auto_poll"):
        if key in patch:
            doc[key] = patch[key]
    doc["brand_id"] = brand_id
    doc["updated_at"] = _now()
    state.save(config_doc_id(brand_id), doc)
    return doc


def alias_map(cfg: dict) -> dict[str, list[str]]:
    aliases = {"self": list((cfg.get("aliases") or {}).get("self") or [])}
    for comp in cfg.get("competitors") or []:
        key = comp.get("key") or comp.get("name", "")
        if key:
            aliases[key] = list(comp.get("aliases") or [comp.get("name", "")])
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
    docs = {}
    for engine in engines:
        doc = state.load(poll_doc_id(brand_id, engine, day)) or {
            "brand_id": brand_id,
            "engine": engine,
            "date": day,
            "answers": [],
        }
        docs[engine] = doc
    return docs


def _pending_tasks(
    prompts: list[dict], docs: dict[str, dict], runs: int
) -> list[tuple[str, dict, int]]:
    tasks: list[tuple[str, dict, int]] = []
    for engine, doc in docs.items():
        # only SUCCESSFUL answers complete a task — an errored run (quota,
        # outage) stays pending so the next poll retries it instead of
        # writing the whole day off. ("no AIO shown" counts as completed.)
        done = {
            (a["prompt_id"], a["run"])
            for a in doc.get("answers", []) if not a.get("error")
        }
        engine_runs = 1 if engine == geo_engines.AIO_ENGINE else runs
        for prompt in prompts:
            for run in range(1, engine_runs + 1):
                if (prompt["id"], run) not in done:
                    tasks.append((engine, prompt, run))
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
) -> dict:
    """Hand back the unspent reservation, bank AIO credits, update fail streaks.

    One atomic write closing out the step. Returns
    ``{"used": int, "aio_month": int, "streaks": {engine: consecutive_fails}}``.
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
        cfg = _trim_counters(cfg)
        return cfg, {
            "used": counters[day],
            "aio_month": int((cfg.get("counters_aio") or {}).get(_month(), 0)),
            "streaks": streaks,
        }

    return _mutate(config_doc_id(brand_id), change)


def _merge_answers(brand_id: str, engine: str, day: str, records: list[dict]) -> None:
    """Append this step's records to the engine's day-doc, atomically.

    Re-reads inside the transaction so a concurrent step's answers survive
    instead of being overwritten by our stale in-memory copy.
    """

    def change(doc: dict) -> tuple[dict, None]:
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
        return doc, None

    _mutate(poll_doc_id(brand_id, engine, day), change)


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
        for engine, engine_records in records.items():
            _merge_answers(brand["id"], engine, day, engine_records)
        settled = _settle_calls(
            brand["id"], day, granted, spent, aio_credits,
            {e: len(failures.get(e, [])) == n for e, n in attempts.items()},
        )

    terminal, terminal_reason = _terminal_signal(
        len(batch), failures, settled["streaks"]
    )
    return _progress(
        done=done_before + len(batch), total=total, used=settled["used"], cap=cap,
        engines=usable, day=day, aio_capped=aio_capped,
        aio_credits_month=settled["aio_month"],
        terminal=terminal, terminal_reason=terminal_reason,
    )


def recent_answers(brand_id: str, days: int = 7) -> list[dict]:
    """All stored answers for the brand across engines over the last N days."""
    answers: list[dict] = []
    today = dt.datetime.now(dt.timezone.utc).date()
    for offset in range(days):
        day = (today - dt.timedelta(days=offset)).strftime("%Y%m%d")
        for engine in geo_engines.ALL_ENGINES:
            doc = state.load(poll_doc_id(brand_id, engine, day))
            if doc:
                answers.extend(doc.get("answers", []))
    return answers
