"""Weekly GEO capture: for each configured brand × engine, ask the question set
through OpenRouter and score the answers. `ask` is injectable for tests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import httpx

from seo_agent import store
from seo_agent.config import effective_config
from seo_agent.geo import prompts, scoring
from seo_agent.schemas import GeoAnswer, GeoRun

logger = logging.getLogger("agentos.seo.geo")

AskFn = Callable[[str, str], tuple[str, list[str]]]  # (model, question) -> (text, citations)


def _cfg() -> dict:
    return effective_config(store.load_config())


def _ask_openrouter(model: str, question: str) -> tuple[str, list[str]]:
    from app.config import settings
    from app.services import runtime_config

    response = httpx.post(
        f"{settings.openrouter_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {runtime_config.require('openrouter_api_key')}"},
        json={"model": model, "messages": [{"role": "user", "content": question}]},
        timeout=90,
    )
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    text = message.get("content") or ""
    citations = [
        ann["url_citation"]["url"]
        for ann in message.get("annotations", [])
        if ann.get("type") == "url_citation" and ann.get("url_citation", {}).get("url")
    ]
    return text, citations


def _iso_week(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def run_geo_capture(ask: AskFn | None = None, serp_provider=None) -> list[GeoRun]:
    cfg = _cfg()
    ask_fn = ask or _ask_openrouter
    now = datetime.now(timezone.utc)
    runs: list[GeoRun] = []

    for slug, brand in (cfg.get("brands") or {}).items():
        answers: list[GeoAnswer] = []
        questions = prompts.question_set(brand)
        for engine, model in (cfg.get("geo_engines") or {}).items():
            if not model or model == "serpapi":
                continue  # ai_overview engine lands in a later slice
            for question in questions:
                try:
                    text, citations = ask_fn(model, question)
                    answer = GeoAnswer(engine=engine, question=question,
                                       answer_text=text, citations=citations)
                    answers.append(scoring.evaluate_answer(
                        answer, brand.get("name", ""), brand.get("domain", "")))
                except Exception as exc:
                    logger.warning("GEO %s/%s failed: %s", slug, engine, exc)
                    answers.append(GeoAnswer(engine=engine, question=question, error=str(exc)))

        score, components, engine_scores, no_data = scoring.score_run(
            answers, brand.get("competitors", []), cfg)
        run = GeoRun(
            id=store.new_id(), brand=slug, week=_iso_week(now),
            captured_at=now.isoformat(), answers=answers, score=score,
            components=components, engine_scores=engine_scores,
            no_data_engines=no_data,
        )
        store.save_geo_run(run.model_dump())
        runs.append(run)
    return runs
