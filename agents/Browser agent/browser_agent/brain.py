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
- {"kind":"next_subtask"}                  the current sub-task's done_when is
                                           satisfied — move to the next one
- {"kind":"tool","tool":"<name>",...}      use a direct API instead of the page
- {"kind":"done","summary":...,"extracted":...}  finish with your findings
- {"kind":"fail","reason":...}             give up honestly — never fake success

Every action also takes "why": one short plain-language line shown to the user.

Any action with an "index" MUST also carry "expect": the text shown for that
element in the list, copied exactly. The element is found BY THAT NAME — the
number is only a tie-breaker, because numbers are re-assigned every step and a
dropdown opening renumbers the whole page. Add "role" (button, link, textbox…)
when several things share a name. If the name isn't found, or matches several
things, the step is refused and you get to look again rather than acting on
whatever inherited the number.

Every action should also carry "expect_after": one short sentence saying what
will be observably true once it works — "the To field shows the address", "the
compose window is open". You will be shown, on the next step, whether it came
true. This is how you notice a step that silently did nothing, instead of
building on an assumption.

When a screenshot is attached, it is the current viewport. Use it to work out
why the text view isn't matching what you expect — an overlay or dialog covering
the control, a element that looks clickable but isn't the real input, a field
that is already filled, a menu that never opened. Then pick the element index
from the list that matches what you SEE. The indexes are not on the image; map
by the element's text and position.

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
6. If a step has already been tried and the page did not change, do NOT repeat
   it. Try something else: a different element, scroll, wait, or say why you are
   stuck. Repeating an ineffective action is the single most common way these
   runs fail.
7. Prefer a tool over the browser whenever one fits. A tool answers in seconds
   and cannot get stuck; clicking through a UI for something an API can do is
   how these runs fail. Google Sheets in particular is ONLY reachable by tool —
   its cells are painted pixels, so no click or keystroke will ever touch them.
8. Work ONLY on the current sub-task shown below. Its plan lists the micro-steps
   and the edge cases someone already thought through — when the page matches a
   listed risk, apply that handling instead of improvising. When its "done_when"
   is visibly true, reply "next_subtask". Reply "done" only when the LAST
   sub-task is finished.
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
    warning = ""
    if dom.get("canvas_app"):
        warning = (
            "\nNOTE: this page paints its real content onto a <canvas> (Google "
            "Sheets, Docs, Slides, Figma and similar). The cells, text and "
            "shapes are pixels — they are NOT elements, and no element index "
            "will ever refer to them. You can use the surrounding chrome "
            "(menus, toolbars, dialogs) but you cannot read or type into the "
            "document body. If the task needs that, reply \"fail\" and say so "
            "plainly instead of hunting for elements that do not exist.\n"
        )
    return (
        f"Interactive elements (truncated: {str(truncated).lower()}):\n{block}\n"
        f"{warning}\n"
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
        target = f' [{act.get("index")}]' if act.get("index") is not None else ""
        lines.append(
            f'{s.get("seq")}. {act.get("kind")}{target} — {act.get("why", "")} → {outcome}'
        )
    return "\n".join(lines)


def _last_claim(run: dict) -> str:
    """Confront the model with the claim it made about its previous step.

    The model asserts what should be true afterwards; here it is asked to check
    that against the page it is now looking at. Verification for the price of a
    few lines in a prompt we were already sending.
    """
    steps = run.get("steps") or []
    if not steps:
        return ""
    last = steps[-1]
    claim = (last.get("action") or {}).get("expect_after")
    if not claim:
        return ""
    result = last.get("result") or {}
    landed = "the step reported success" if result.get("ok") else (
        f"the step FAILED: {result.get('error')}"
    )
    return (
        f'\nYou expected after your last step: "{claim}"\n'
        f"({landed}.) Check that against the page below BEFORE doing anything "
        "else. If it did not come true, do not build on it — say so and change "
        "approach.\n"
    )


def _tried_here(run: dict, page_fp: str) -> str:
    """Steps already attempted on THIS screen — the antidote to the retry loop."""
    tried: list[str] = []
    for step in reversed(run.get("steps") or []):
        if step.get("page_fp") != page_fp:
            break
        act = step.get("action") or {}
        label = f'{act.get("kind")} [{act.get("index")}]' if act.get("index") is not None \
            else str(act.get("kind"))
        if label not in tried:
            tried.append(label)
    if not tried:
        return ""
    return (
        "\nAlready tried on this exact screen, with NO change to the page: "
        + ", ".join(reversed(tried))
        + ". Do not repeat these — try something different.\n"
    )


def _findings_block(run: dict) -> str:
    findings = run.get("findings") or []
    if not findings:
        return "(none yet)"
    text = "\n---\n".join(str(f) for f in findings)
    if len(text) > _MAX_FINDINGS_CHARS:
        text = text[-_MAX_FINDINGS_CHARS:]
    return text


def _plan_block(run: dict) -> str:
    """The current sub-task, its micro-steps and its pre-thought edge cases."""
    from browser_agent import planner  # local import: avoids a cycle

    plan = run.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    if not subtasks:
        return ""
    active = planner.current(plan)
    done, total = planner.progress(plan)

    outline = "\n".join(
        f'  {"[x]" if s.get("status") == "done" else "[ ]"} {i + 1}. {s.get("title")}'
        for i, s in enumerate(subtasks)
    )
    if not active:
        return f"PLAN ({done}/{total} done):\n{outline}\n\nAll sub-tasks are done — reply \"done\".\n"

    rail = active.get("rail", "browser")
    rail_note = {
        "data": (
            "  RAIL: data — do this with a tool, NOT the browser. Reach for the "
            "API; opening a tab for this is the wrong move."
        ),
        "think": (
            "  RAIL: think — no tool needed. Reason over what you already have, "
            'then reply "next_subtask" or "done".'
        ),
        "browser": "  RAIL: browser — only the browser can do this one.",
    }.get(rail, "")
    steps = "\n".join(f"  - {s}" for s in active.get("steps") or []) or "  (none listed)"
    edges = (
        "\n".join(f'  - If {e["risk"]} → {e["handle"]}' for e in active.get("edge_cases") or [])
        or "  (none listed)"
    )
    notes = f"\nOverall notes: {plan['notes']}" if plan.get("notes") else ""
    return (
        f"PLAN ({done}/{total} sub-tasks done):\n{outline}\n\n"
        f"CURRENT SUB-TASK: {active.get('title')}\n"
        f"{rail_note}\n"
        f"  Goal: {active.get('goal')}\n"
        f"  Finished when: {active.get('done_when')}\n"
        f"  Micro-steps:\n{steps}\n"
        f"  Edge cases to watch for:\n{edges}{notes}\n"
    )


def _tools_block() -> str:
    from browser_agent import tools  # local import: keeps the module cycle-free

    ready = tools.availability()
    usable = [n for n in tools.TOOLS if ready.get(n)]
    if not usable:
        return "\nTOOLS: none are available right now — the browser is all you have.\n"
    lines = "\n".join(
        f'- {n}: {tools.TOOLS[n]["help"]}' for n in usable
    )
    missing = [n for n in tools.TOOLS if not ready.get(n)]
    note = f"\nUnavailable right now: {', '.join(missing)}." if missing else ""
    return (
        f"\nTOOLS (use with {{\"kind\":\"tool\", ...}} — faster and far more "
        f"reliable than clicking):\n{lines}{note}\n"
        f"Share sheets with {ready['service_account']} (Editor if I must write).\n"
    )


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
    from browser_agent import runs as _runs  # local import: avoids a cycle

    return (
        f"GOAL: {run.get('goal', '')}\n"
        f"MODE: {run.get('mode', 'act')}\n"
        f"STEPS REMAINING: {remaining}\n\n"
        f"{_plan_block(run)}"
        f"{_tools_block()}\n"
        f"Recent steps:\n{_history_block(run)}\n"
        f"{_last_claim(run)}"
        f"{_tried_here(run, _runs.page_fingerprint(observation))}\n"
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


def _user_message(text: str, screenshot: str | None) -> tuple[str, object]:
    """A plain text turn, or a multimodal one when the model should look."""
    if not screenshot:
        return ("user", text)
    return (
        "user",
        [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": screenshot}},
        ],
    )


def decide(run: dict, observation: dict) -> actions.Action:
    """One LLM call (plus one retry on garbage) → one validated Action."""
    system = _SYSTEM
    user = _user_prompt(run, observation)
    screenshot = observation.get("screenshot") or None
    llm = openrouter.get_llm(temperature=0.2, agent_id=AGENT_ID)

    last_error = ""
    for attempt in range(2):
        prompt = user if not last_error else (
            f"{user}\n\nYour previous reply was invalid: {last_error}. "
            "Reply again with exactly one valid action JSON object."
        )
        response = llm.invoke([("system", system), _user_message(prompt, screenshot)])
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
