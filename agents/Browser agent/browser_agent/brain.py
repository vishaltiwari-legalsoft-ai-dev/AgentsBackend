"""The a11 brain: one LLM call per step → exactly one validated Action.

``decide`` never raises on model garbage — after one retry it returns an honest
``fail`` action (never a guessed one). The DOM block is defensively re-truncated
here even though the extension already caps it, so a misbehaving client can't
blow the context window.
"""
from __future__ import annotations

import json
import logging

from app.services import openrouter

from browser_agent import actions

AGENT_ID = "a11"

logger = logging.getLogger("agentos.browser")

_MAX_DOM_CHARS = 16_000
_MAX_TEXT_EXCERPT = 3_000
_MAX_FINDINGS_CHARS = 6_000
_MAX_HISTORY_STEPS = 10
_MAX_TABS = 20

_SYSTEM = """You are the AgentOS Browser Agent. You control the user's real, \
logged-in Chrome browser through a fixed set of actions. You see one \
observation per step (active tab, open tabs, a distilled index of interactive \
elements) and must reply with EXACTLY ONE action as a single JSON object — no \
markdown fences, no prose outside the JSON.

Actions (field "kind" plus the listed required fields):
- {"kind":"navigate","url":...}            go to a URL in the current tab
- {"kind":"click","index":N}               click element [N] from the DOM index
- {"kind":"type","index":N,"text":...}     focus element [N] and type text
- {"kind":"select","index":N,"value":...}  choose an option in a <select>
- {"kind":"key","text":"Enter"}            press a key (Enter, Escape, Tab)
- {"kind":"scroll","direction":"down"}     scroll the page (or "up")
- {"kind":"switch_tab","tab_id":N}         focus another open tab
- {"kind":"open_tab","url":...}            open a URL in a new tab
- {"kind":"wait"}                          wait ~1.5s for the page to settle
- {"kind":"extract"}                       get the full page text (next step)
- {"kind":"ask_user","text":...}           ask the user a question and pause
- {"kind":"done","summary":...,"extracted":...}  finish with your findings
- {"kind":"fail","reason":...}             give up honestly — never fake success

Every action also takes "why": one short plain-language line shown to the user.

Rules:
1. Use ONLY element indexes present in the DOM index. If what you need is not
   there, scroll or extract — never invent an index.
2. If the DOM index says truncated:true, the page has more than shown — scroll
   before concluding something is missing.
3. In monitor mode you are READ-ONLY: navigate/click/type/select/key/open_tab
   are forbidden; observe, extract and summarize instead.
4. Sensitive steps (pay, buy, delete, send, transfer, checkout) require the
   user's confirmation — the harness handles that; still avoid them unless the
   goal clearly asks for it.
5. When the goal is achieved, reply "done" with a concise summary (and
   "extracted" for structured data). If you are stuck after trying, reply
   "fail" with the honest reason. Do not loop on the same failing action.
"""


def _dom_block(dom: dict | None) -> str:
    if not dom:
        return "(no DOM captured for this step)"
    lines: list[str] = []
    for el in dom.get("elements") or []:
        i = el.get("i")
        tag = el.get("tag", "?")
        role = el.get("role") or ""
        text = str(el.get("text") or el.get("name") or "")[:80]
        extra = f" {role}" if role and role != tag else ""
        lines.append(f'[{i}] <{tag}{extra}> "{text}"')
    block = "\n".join(lines)
    if len(block) > _MAX_DOM_CHARS:
        block = block[:_MAX_DOM_CHARS] + "\n…(re-truncated by server)"
    excerpt = str(dom.get("text_excerpt") or "")[:_MAX_TEXT_EXCERPT]
    truncated = bool(dom.get("truncated"))
    return (
        f"Interactive elements (truncated: {str(truncated).lower()}):\n{block}\n\n"
        f"Page text excerpt:\n{excerpt}"
    )


def _history_block(run: dict) -> str:
    steps = (run.get("steps") or [])[-_MAX_HISTORY_STEPS:]
    if not steps:
        return "(first step)"
    lines = []
    for s in steps:
        act = s.get("action") or {}
        result = s.get("result") or {}
        outcome = "ok" if result.get("ok") else (result.get("error") or "pending")
        lines.append(f'{s.get("seq")}. {act.get("kind")} — {act.get("why", "")} → {outcome}')
    return "\n".join(lines)


def _findings_block(run: dict) -> str:
    findings = run.get("findings") or []
    if not findings:
        return "(none yet)"
    text = "\n---\n".join(str(f) for f in findings)
    if len(text) > _MAX_FINDINGS_CHARS:
        text = text[-_MAX_FINDINGS_CHARS:]
    return text


def _user_prompt(run: dict, observation: dict) -> str:
    tab = observation.get("tab") or {}
    tabs = (observation.get("tabs") or [])[:_MAX_TABS]
    tabs_lines = "\n".join(
        f'- tab_id {t.get("id")}: {str(t.get("title") or "")[:60]} ({t.get("url", "")})'
        + (" [active]" if t.get("active") else "")
        for t in tabs
    )
    last = observation.get("last_result")
    last_line = json.dumps(last, ensure_ascii=False)[:2000] if last else "(none)"
    reply = observation.get("user_reply")
    reply_line = f"\nUser replied to your question: {reply}" if reply else ""
    remaining = max(0, int(run.get("step_cap", 0)) - int(run.get("steps_used", 0)))
    return (
        f"GOAL: {run.get('goal', '')}\n"
        f"MODE: {run.get('mode', 'act')}\n"
        f"STEPS REMAINING: {remaining}\n\n"
        f"Recent steps:\n{_history_block(run)}\n\n"
        f"Collected findings:\n{_findings_block(run)}\n\n"
        f"Active tab: {str(tab.get('title') or '')[:80]} — {tab.get('url', '')}\n"
        f"Open tabs:\n{tabs_lines or '(none reported)'}\n\n"
        f"{_dom_block(observation.get('dom'))}\n\n"
        f"Result of your previous action: {last_line}{reply_line}\n\n"
        "Reply with exactly one action JSON object."
    )


def _parse_action(content: object) -> actions.Action:
    if isinstance(content, list):  # some providers return content parts
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = str(content).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model returned no JSON object")
    return actions.validate_action(json.loads(text[start : end + 1]))


def decide(run: dict, observation: dict) -> actions.Action:
    """One LLM call (plus one retry on garbage) → one validated Action."""
    system = _SYSTEM
    user = _user_prompt(run, observation)
    llm = openrouter.get_llm(temperature=0.2, agent_id=AGENT_ID)

    last_error = ""
    for attempt in range(2):
        prompt = user if not last_error else (
            f"{user}\n\nYour previous reply was invalid: {last_error}. "
            "Reply again with exactly one valid action JSON object."
        )
        response = llm.invoke([("system", system), ("user", prompt)])
        try:
            return _parse_action(getattr(response, "content", response))
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            logger.warning("browser brain parse failure (attempt %d): %s", attempt + 1, exc)

    return actions.Action(
        kind="fail",
        reason=f"model returned an unparseable action: {last_error}",
        why="The model could not produce a valid action.",
    )
