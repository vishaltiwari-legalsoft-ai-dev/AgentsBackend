"""One message in, one :class:`Verdict` or :class:`Rejected` out, through the
hub's OpenRouter seam as agent ``a12``.

The contract — prompt, hygiene, validation — is :mod:`triage`; this module
only moves text to the model and back. A reply that fails validation is asked
for once more with the reason attached (the reasons name the check, never the
content), and a second failure is the :class:`Rejected` the caller writes as
``needs_review``. A call that raises is different from a reply that is wrong:
it is :class:`ModelCallFailed`, so the pipeline can tell "this email confused
the model" from "the model is down".

Logging: message id, token counts when the provider reports them, and the
rejection reason. Never the subject, sender, body or reply.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.services.openrouter import get_llm

from . import AGENT_ID, refuse_if_offline, triage
from .gmail_client import Message
from .triage import Rejected, Verdict

logger = logging.getLogger("agentos.inbox.summarise")

#: Per-call deadline. A valid reply is ~70 tokens; ``get_llm``'s 120s default
#: is sized for a long reasoning turn, and two of those would eat a fire.
OPENROUTER_TIMEOUT_SECONDS = 45

RE_ASK = (
    "That reply was not accepted: {reason} Return exactly the JSON object "
    "described in the instructions, and nothing else."
)


class ModelUnavailable(RuntimeError):
    """No model can be built: no key, or the agent is offline. Systemic."""


class ModelCallFailed(RuntimeError):
    """One call raised (timeout, provider error). Per message, retried later."""


def build_llm():
    """The model for one fire, built once and reused for every message."""
    refuse_if_offline("The triage model")
    try:
        return get_llm(
            temperature=triage.TEMPERATURE,
            fast=True,
            agent_id=AGENT_ID,
            timeout=OPENROUTER_TIMEOUT_SECONDS,
            max_tokens=triage.MAX_TOKENS,
        )
    except RuntimeError as exc:  # runtime_config.require: the key is missing
        raise ModelUnavailable(str(exc)) from exc


def summarise(message: Message, *, llm=None) -> Verdict | Rejected:
    model = llm if llm is not None else build_llm()
    nonce = triage.make_nonce()
    system, user = triage.build_prompt(
        from_header=message.from_,
        to_header=message.to,
        date_header=message.date_header,
        subject=message.subject,
        body=triage.clean_body(message.body_text),
        nonce=nonce,
    )
    email_date = triage.email_date_ist(message.date_header, fallback=message.received_at.date())
    conversation = [SystemMessage(content=system), HumanMessage(content=user)]

    reply = _ask(model, conversation, message.id, attempt=1)
    verdict = triage.validate(reply, email_date=email_date)
    if isinstance(verdict, Verdict):
        return verdict
    logger.info("a12 %s: reply rejected (%s); asking once more", message.id, verdict.reason)

    conversation = conversation + [
        AIMessage(content=reply),
        HumanMessage(content=RE_ASK.format(reason=verdict.reason)),
    ]
    reply = _ask(model, conversation, message.id, attempt=2)
    verdict = triage.validate(reply, email_date=email_date)
    if isinstance(verdict, Rejected):
        logger.info("a12 %s: reply rejected twice (%s); needs review", message.id, verdict.reason)
    return verdict


def _ask(model, conversation: list, message_id: str, *, attempt: int) -> str:
    try:
        response = model.invoke(conversation)
    except Exception as exc:  # noqa: BLE001 — every failure shape is one honest class
        logger.warning(
            "a12 %s: model call failed on attempt %d: %s", message_id, attempt, type(exc).__name__
        )
        raise ModelCallFailed(f"The model call failed ({type(exc).__name__}).") from exc
    usage = getattr(response, "usage_metadata", None) or {}
    if usage:
        logger.info(
            "a12 %s: attempt %d, %s in / %s out tokens",
            message_id, attempt, usage.get("input_tokens"), usage.get("output_tokens"),
        )
    content = getattr(response, "content", response)
    if isinstance(content, list):  # some providers answer in parts
        content = " ".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)
