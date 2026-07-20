"""LLM pass: group statistical terms into meaningful topics/entities/questions.

Honesty rule (standing feedback): if the LLM call fails, callers get the raw
statistical terms labeled ai=False + fallback_reason — never dressed up as AI.
"""

from __future__ import annotations

import json
import logging
import re

from seo_agent.schemas import TermTarget, Topic

logger = logging.getLogger("agentos.seo.topics")

_PROMPT = """You are an SEO content strategist. For the target keyword "{keyword}",
group the following competitor-derived terms into 3-7 meaningful topics.

Terms (importance-ordered): {terms}
People-Also-Ask questions from Google: {paa}

Return ONLY JSON:
{{"topics": [{{"name": str, "terms": [str], "questions": [str]}}],
  "questions": [str]}}
"questions" is the deduplicated union of all questions an article should answer."""

_BRAND_BLOCK = """

The article is being written FOR this business — make every topic name and
question relevant to it, not generic SEO advice:
- Brand: {name} ({domain})
- What they do: {category}
- Competitors to be aware of: {competitors}"""


def build_brand_context(brand_cfg: dict) -> str:
    return _BRAND_BLOCK.format(
        name=brand_cfg.get("name", ""),
        domain=brand_cfg.get("domain", ""),
        category=brand_cfg.get("category", "this industry"),
        competitors=", ".join(brand_cfg.get("competitors", [])) or "(none listed)",
    )


def _default_llm():
    from app.services import openrouter, runtime_config

    model = runtime_config.get_for_agent("a2", "openrouter_fast_model")
    return openrouter.get_llm(temperature=0.2, model=model or None, fast=True)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object in LLM reply")
    return json.loads(match.group(0))


def _fallback(term_targets: list[TermTarget], paa: list[str], reason: str):
    topic = Topic(
        name="Key terms (statistical)",
        terms=[t.term for t in term_targets],
        questions=[],
    )
    return [topic], list(paa), False, reason


def group_topics(
    term_targets: list[TermTarget],
    paa_questions: list[str],
    keyword: str,
    llm=None,
    brand_context: str | None = None,
) -> tuple[list[Topic], list[str], bool, str | None]:
    try:
        model = llm or _default_llm()
        prompt = _PROMPT.format(
            keyword=keyword,
            terms=", ".join(t.term for t in term_targets),
            paa="; ".join(paa_questions) or "(none)",
        ) + (brand_context or "")
        reply = model.invoke(prompt)
        data = _extract_json(str(reply.content))
        topics = [Topic(**t) for t in data.get("topics", [])]
        questions = [q for q in data.get("questions", []) if isinstance(q, str)]
        if not topics:
            raise ValueError("LLM returned no topics")
        return topics, questions or list(paa_questions), True, None
    except Exception as exc:
        logger.warning("SEO topic grouping fell back to statistical terms: %s", exc)
        return _fallback(term_targets, paa_questions, str(exc))
