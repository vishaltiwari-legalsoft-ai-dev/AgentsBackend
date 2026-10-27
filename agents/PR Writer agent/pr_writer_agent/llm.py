"""Model adapter for PR Writer — every call stamps agent_id="a13".

One step = one generation call, then at most one repair call. That is the
whole budget: the frontend relay cuts a request at 300 s and ``get_llm``
retries a timed-out call twice (three attempts), so the plan in
``prompts.STEPS`` sizes each call's timeout so ``3 x timeout`` for the
generation plus ``3 x REPAIR_TIMEOUT_S`` for the repair stays under the relay.

Failure is always loud and specific:

- ``LLMUnavailable``   no key, provider error, timeout. Subclasses the shared
                       ``CredentialMissing`` so the router's existing honest
                       424 path applies, as it does for a9.
- ``LLMTruncated``     output hit ``max_tokens``. Not retried: a repair call
                       cannot recover text the model never wrote.
- ``LLMRefused``       the provider declined (content filter / refusal).
- ``LLMOutputInvalid`` still not schema-valid JSON after the repair call.

Nothing here ever substitutes a canned payload for a failed call.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from seo_geo_agent.sources import CredentialMissing

AGENT_ID = "a13"
REPAIR_TIMEOUT_S = 30


class LLMUnavailable(CredentialMissing):
    pass


class LLMOutputInvalid(RuntimeError):
    pass


class LLMTruncated(LLMOutputInvalid):
    pass


class LLMRefused(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    finish_reason: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    #: What the provider billed for this call, from OpenRouter's ``usage.cost``.
    #: ``None`` when the provider did not report it — never estimated here.
    cost_usd: float | None = None


def total_cost(replies) -> float | None:
    """Summed provider cost, or ``None`` unless EVERY reply reported one: a
    sum over only some calls would understate the spend and read as real."""
    costs = [getattr(r, "cost_usd", None) for r in replies]
    if not costs or any(c is None for c in costs):
        return None
    return round(sum(costs), 6)


@dataclass
class Result:
    payload: dict
    calls: list[Reply] = field(default_factory=list)

    @property
    def usage(self) -> dict:
        return {"calls": len(self.calls),
                "input_tokens": sum(c.input_tokens for c in self.calls),
                "output_tokens": sum(c.output_tokens for c in self.calls),
                "cost_usd": total_cost(self.calls),
                "models": sorted({c.model for c in self.calls if c.model})}


# invoke(system, user, *, slot, timeout_s, max_tokens, temperature) -> Reply
Invoke = Callable[..., Reply]


def default_invoke(system: str, user: str, *, slot: str, timeout_s: float, max_tokens: int,
                   temperature: float) -> Reply:
    try:
        from app.services.openrouter import get_llm

        llm = get_llm(temperature, fast=(slot == "fast"), agent_id=AGENT_ID,
                      timeout=timeout_s, max_tokens=max_tokens)
        msg = llm.invoke([("system", system), ("user", user)], extra_body=request_extras(llm))
    except Exception as exc:  # noqa: BLE001 — no key, provider down, timeout
        raise LLMUnavailable(f"model call failed ({slot} slot): {type(exc).__name__}: {exc}") from exc
    return reply_from_message(msg, fallback_model=str(getattr(llm, "model_name", "")))


#: OpenRouter usage accounting: the response's ``usage`` then carries ``cost``
#: (USD actually billed for the call). Sent on every call so the cost is
#: reported, not inferred from a price table.
USAGE_ACCOUNTING = {"usage": {"include": True}}


def request_extras(llm) -> dict:
    """The model's own request extras (e.g. the fast slot's reasoning effort)
    plus usage accounting. A per-call ``extra_body`` replaces the model's
    whole ``extra_body`` in LangChain, so the two are merged here."""
    return {**(getattr(llm, "extra_body", None) or {}), **USAGE_ACCOUNTING}


def provider_cost(meta: dict) -> float | None:
    """``usage.cost`` from a LangChain ``response_metadata``; ``None`` when the
    provider did not report a number."""
    cost = ((meta or {}).get("token_usage") or {}).get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost)


def reply_from_message(msg, *, fallback_model: str = "") -> Reply:
    meta = getattr(msg, "response_metadata", None) or {}
    usage = getattr(msg, "usage_metadata", None) or {}
    return Reply(
        text=str(msg.content),
        finish_reason=meta.get("finish_reason"),
        model=str(meta.get("model_name") or fallback_model),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=provider_cost(meta),
    )


# --------------------------------------------------------------------------- #
# Parsing and schema validation (the subset of JSON Schema prompts.py uses)
# --------------------------------------------------------------------------- #

def parse_json(raw: str) -> Any:
    """Strip code fences, then take the first balanced JSON value."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", str(raw or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "anyOf" in schema:
        if all(validate(value, s, path) for s in schema["anyOf"]):
            errors.append(f"{path}: matches none of the allowed shapes")
        return errors
    types = schema.get("type")
    if types:
        names = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in names):
            return [f"{path}: expected {'/'.join(names)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                errors.append(f"{path}: unexpected key(s) {extra}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s), got {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: at most {schema['maxItems']} item(s), got {len(value)}")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value[:40]!r} does not match {schema['pattern']}")
    return errors


def _check(raw: str, schema: dict) -> tuple[Any, list[str]]:
    try:
        payload = parse_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"not JSON: {exc}"]
    return payload, validate(payload, schema)


_REPAIR_SYSTEM = (
    "You repair malformed JSON. The text you are given was meant to be one JSON "
    "object matching the schema below; the listed problems stop it from parsing or "
    "validating. Return only the corrected JSON object: no prose, no code fences. "
    "Fix structure only. Do not add, remove or reword any content, facts, citation "
    "markers like [ev-3], or [VERIFY: ...] placeholders. A required value that is "
    "absent becomes null, an empty string or an empty list as the schema allows — "
    "never invented content."
)


def call_json(step, system: str, user: str, *, invoke: Invoke | None = None) -> Result:
    """One generation call, local repair, at most one model repair, then raise."""
    invoke = invoke or default_invoke
    calls: list[Reply] = []

    def _guard(reply: Reply, label: str) -> None:
        if reply.finish_reason == "length":
            raise LLMTruncated(
                f"{step.name}: {label} output hit max_tokens={step.max_tokens} and was cut off; "
                "a retry would be cut the same way")
        if reply.finish_reason in ("content_filter", "refusal"):
            raise LLMRefused(f"{step.name}: the model declined to answer ({reply.finish_reason})")

    first = invoke(system, user, slot=step.slot, timeout_s=step.timeout_s,
                   max_tokens=step.max_tokens, temperature=step.temperature)
    calls.append(first)
    _guard(first, "generation")
    payload, errors = _check(first.text, step.schema)
    if not errors:
        return Result(payload, calls)

    repair_user = (
        f"SCHEMA:\n{json.dumps(step.schema)}\n\nPROBLEMS:\n- " + "\n- ".join(errors[:12])
        + f"\n\nTEXT TO REPAIR:\n{first.text}"
    )
    second = invoke(_REPAIR_SYSTEM, repair_user, slot="fast", timeout_s=REPAIR_TIMEOUT_S,
                    max_tokens=step.max_tokens, temperature=0.0)
    calls.append(second)
    _guard(second, "repair")
    payload, errors2 = _check(second.text, step.schema)
    if errors2:
        raise LLMOutputInvalid(
            f"{step.name}: model output was not valid JSON for the schema after one repair "
            f"attempt: {'; '.join(errors2[:5])}")
    return Result(payload, calls)
