"""Reasoning-model adapter for the Blog Writer — every call stamps agent_id="a9".

Deep research and long-form writing run on the *reasoning* slot (the creator's
``openrouter_model`` override for a9), unlike ``seo_geo_agent.sources`` which
uses the fast slot. Raises ``CredentialMissing`` when offline or the provider
fails, so callers surface an honest message instead of fabricating output.
"""
from __future__ import annotations

import json
import re

from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import state

AGENT_ID = "a9"


def llm_text(system: str, prompt: str, *, temperature: float = 0.4) -> str:
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=temperature, agent_id=AGENT_ID).invoke(
            [("system", system), ("user", prompt)]
        ).content
        return str(raw).strip()
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"LLM unavailable: {exc}") from exc


def parse_json(raw) -> dict | list:
    """Tolerant parse: strip code fences, then take the first balanced JSON
    value even when the model wraps it in prose or trails extra text."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value


def llm_json(system: str, prompt: str, *, temperature: float = 0.2):
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=temperature, agent_id=AGENT_ID).invoke(
            [("system", system), ("user", prompt)]
        ).content
        return parse_json(raw)
    except Exception as exc:  # noqa: BLE001 — bad JSON, no key, provider down
        raise CredentialMissing(f"LLM unavailable: {exc}") from exc
