"""GEO agent — step-wise engine polling, parsing, and per-brand config.

Polling is deliberately chunked (the Blog Writer ``research_step`` precedent):
one ``poll_step`` call executes a small batch of (engine × prompt × run) tasks
and returns progress, so the UI loops instead of one giant request. Results
append to one Firestore doc per engine per day
(``geo-poll-{brand}-{engine}-{YYYYMMDD}``) — bounded well under the 1 MB doc
limit by the answer-text cap.

Cost control: a per-brand daily engine-call counter with a hard cap lives in
``geo-config-{brand}``; when the cap is hit the step reports it honestly and
does nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from seo_geo_agent import state
from final_geo_agent import geo_engines, geo_prompts
from seo_geo_agent.sources import CredentialMissing, llm_json

DEFAULT_DAILY_CAP = 2000
DEFAULT_RUNS = 3
DEFAULT_BATCH = 10  # tasks (engine calls) per step
# Google AIO runs on SerpAPI's free ~250 searches/month: one run per prompt
# (SERP content, low variance) under a hard monthly credit guard.
AIO_MONTHLY_CAP = 200


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
        "counters": {},
        "updated_at": _now(),
    }
    state.save(config_doc_id(brand["id"]), doc)
    return doc


def save_config(brand_id: str, patch: dict) -> dict:
    doc = state.load(config_doc_id(brand_id)) or {}
    for key in ("aliases", "competitors", "daily_cap", "aio_monthly_cap"):
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


def poll_step(
    brand: dict,
    engines: list[str] | None = None,
    runs: int = DEFAULT_RUNS,
    batch_size: int = DEFAULT_BATCH,
) -> dict:
    """Execute up to ``batch_size`` engine calls; return progress for the UI loop."""
    cfg = ensure_config(brand)
    availability = geo_engines.available_engines()
    wanted = engines or [e for e, ok in availability.items() if ok]
    usable = [e for e in wanted if availability.get(e)]
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
    total = sum(
        len(prompts) * (1 if e == geo_engines.AIO_ENGINE else runs) for e in usable
    )
    done_before = total - len(tasks)

    cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)
    used = used_today(cfg)
    if used >= cap:
        return {
            "done": done_before, "total": total, "calls_used_today": used,
            "daily_cap": cap, "capped": True, "engines": usable, "date": day,
        }

    budget = min(batch_size, cap - used)
    batch = tasks[:budget]
    aliases = alias_map(cfg)
    calls = 0
    aio_credits = 0
    for engine, prompt, run in batch:
        answer = geo_engines.poll_engine(engine, prompt["text"])
        calls += 1
        if engine == geo_engines.AIO_ENGINE:
            aio_credits += getattr(answer, "credits", 1)
        record = answer.to_dict() | {
            "prompt_id": prompt["id"],
            "prompt_text": prompt["text"],
            "intent": prompt.get("intent", ""),
            "run": run,
            "at": _now(),
        }
        if not answer.error:
            mentions = detect_mentions(answer.text, aliases)
            record["mentions"] = mentions
            record["brand_mentioned"] = "self" in mentions
            record["brand_position"] = mentions.get("self")
            own_domain = (brand.get("domain") or "").lower().removeprefix("www.")
            record["brand_cited"] = bool(own_domain) and any(
                (c.get("domain") or "").endswith(own_domain)
                for c in record["citations"]
            )
            # Sentiment only on run 1 of answers that mention the brand — one
            # cheap LLM call per prompt/engine instead of per run.
            if record["brand_mentioned"] and run == 1:
                record["sentiment"] = _sentiment(
                    prompt["text"], answer.text, brand.get("name", "")
                )
        # a retry replaces the task's earlier error record — the doc keeps one
        # honest latest attempt per (prompt, run), not a pile of stale 429s
        docs[engine]["answers"] = [
            a for a in docs[engine]["answers"]
            if not (a["prompt_id"] == prompt["id"] and a["run"] == run and a.get("error"))
        ]
        docs[engine]["answers"].append(record)

    for engine in {engine for engine, _p, _r in batch}:
        doc = docs[engine]
        # Firestore hard-caps docs at 1 MB. Records are slimmed at the source
        # (text + citation caps in EngineAnswer.to_dict); this is the honest
        # last resort if a doc still balloons: drop oldest answers, say so.
        while len(json.dumps(doc["answers"])) > 900_000 and doc["answers"]:
            doc["answers"].pop(0)
            doc["overflow_trimmed"] = int(doc.get("overflow_trimmed", 0)) + 1
        state.save(poll_doc_id(brand["id"], engine, day), doc)

    cfg.setdefault("counters", {})[day] = used + calls
    # keep only the trailing 30 day-counters so the config doc never grows
    cfg["counters"] = dict(sorted(cfg["counters"].items())[-30:])
    if aio_credits:
        cfg.setdefault("counters_aio", {})[_month()] = aio_used_month(cfg) + aio_credits
        cfg["counters_aio"] = dict(sorted(cfg["counters_aio"].items())[-3:])
    state.save(config_doc_id(brand["id"]), cfg)

    return {
        "done": done_before + len(batch), "total": total,
        "calls_used_today": used + calls, "daily_cap": cap, "capped": False,
        "engines": usable, "date": day, "aio_capped": aio_capped,
        "aio_credits_month": aio_used_month(cfg),
    }


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
