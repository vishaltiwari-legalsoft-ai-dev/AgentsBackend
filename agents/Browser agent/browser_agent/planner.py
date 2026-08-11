"""Think before clicking: turn a task into sub-tasks, micro-steps and edge cases.

Runs ONCE per run, on a premium model, before the browser is touched. The act
loop then works one sub-task at a time instead of holding the whole goal in its
head — which is what produced the Gmail failure where it clicked the same field
twenty times with no idea where it was in the job.

The edge cases matter as much as the steps. "The compose window might open as a
popout" costs nothing to write down here, and saves a run that would otherwise
stall the moment it happens.

A run without a plan is still a run: planning failures degrade to a single
sub-task carrying the raw goal, and say so, rather than aborting.
"""
from __future__ import annotations

import json
import logging

from app.services import openrouter, runtime_config

from browser_agent import actions

logger = logging.getLogger("agentos.browser")

AGENT_ID = "a11"

RAILS = ("data", "browser", "think")

MAX_SUBTASKS = 8
MAX_STEPS_PER_SUBTASK = 8
MAX_EDGE_CASES = 6
_MAX_CHARS = 300

_SYSTEM = """You plan work for an agent that has BOTH a real logged-in Chrome \
browser and a set of direct APIs. You do NOT do the work — you produce the plan \
the agent follows. Reply with ONE JSON object, no markdown fences.

{"subtasks": [
   {"title": "short name",
    "goal": "what this sub-task must achieve, in one sentence",
    "rail": "data | browser | think",
    "steps": ["micro-step", "micro-step"],
    "edge_cases": [{"risk": "what might go wrong", "handle": "what to do then"}],
    "done_when": "the observable condition that proves this sub-task finished"}
 ],
 "notes": "anything the agent should know throughout, or empty"}

CHOOSING THE RAIL — the most important judgement you make:
- "data": the job can be done through an API. Reading or writing a Google
  Sheet, searching the web, reading an article. ALWAYS prefer this. It takes
  seconds, cannot get stuck, and does not break when a site is redesigned.
- "browser": only the browser can do it — a logged-in app with no API, a
  vendor portal, a site that blocks fetching, or a UI the user specifically
  asked to be driven.
- "think": no tool at all, just reasoning over what has already been gathered.

Rules that follow from this:
- **Google Sheets is ALWAYS "data", never "browser".** Sheets, Docs and Slides
  paint their content onto a canvas; the agent physically cannot read cells or
  type into the grid by clicking. Plan sheet work through the API or it will
  fail every time.
- Ordinary research is "data" too: search and read directly rather than
  driving Google in a tab.
- The agent can only ADD rows to a sheet, in its own tab. It cannot edit or
  delete existing cells — never plan a step that depends on changing one.

Rules for a good plan:
- 2 to 6 sub-tasks. Each is a milestone with a visible result, not a single
  click. "Open the compose window" is a sub-task; "click Compose" is a step.
- "done_when" must be checkable by LOOKING at the page — "the To field contains
  the address", not "the address was entered".
- Micro-steps are concrete and in order. Name the control you expect to use.
- Edge cases are the point of this exercise. For each sub-task, think about what
  actually goes wrong in real web apps: the control is off screen, a dialog or
  cookie banner covers it, the app opens a popout instead of an inline panel,
  the field is a combobox that needs a click before it accepts typing, an
  autocomplete dropdown swallows the Enter key, the page is still loading, the
  user is signed out, there are several matching controls. Give each a concrete
  handle.
- Never plan to send money, delete anything, or send a message unless the task
  explicitly asks for it. If the task does, make that its own final sub-task so
  it can be confirmed separately.
- If the task is a single simple action, one sub-task is the right answer. Do
  not pad the plan.

Be specific to the site named in the task. A plan that would fit any website is
not a plan."""


def _parse(content: object) -> dict:
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    text = str(content).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("planner returned no JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("planner returned JSON that is not an object")
    return parsed


def _clean(raw: dict, goal: str) -> dict:
    """Trim the model's plan to the shape the executor relies on."""
    subtasks: list[dict] = []
    for item in (raw.get("subtasks") or [])[:MAX_SUBTASKS]:
        if not isinstance(item, dict):
            continue
        sub_goal = str(item.get("goal") or item.get("title") or "").strip()[:_MAX_CHARS]
        if not sub_goal:
            continue
        rail = str(item.get("rail") or "browser").strip().lower()
        subtasks.append(
            {
                "id": f"s{len(subtasks) + 1}",
                "title": str(item.get("title") or sub_goal)[:80],
                "goal": sub_goal,
                "rail": rail if rail in RAILS else "browser",
                "steps": [
                    str(s)[:_MAX_CHARS]
                    for s in (item.get("steps") or [])[:MAX_STEPS_PER_SUBTASK]
                    if str(s).strip()
                ],
                "edge_cases": [
                    {
                        "risk": str(e.get("risk") or "")[:_MAX_CHARS],
                        "handle": str(e.get("handle") or "")[:_MAX_CHARS],
                    }
                    for e in (item.get("edge_cases") or [])[:MAX_EDGE_CASES]
                    if isinstance(e, dict) and str(e.get("risk") or "").strip()
                ],
                "done_when": str(item.get("done_when") or "")[:_MAX_CHARS],
                "status": "pending",
            }
        )
    if not subtasks:
        raise ValueError("planner produced no usable sub-tasks")
    return {
        "subtasks": subtasks,
        "notes": str(raw.get("notes") or "")[:_MAX_CHARS],
        "planned": True,
        "goal": goal,
    }


def fallback_plan(goal: str, reason: str) -> dict:
    """One sub-task carrying the raw goal — honest about not being a real plan."""
    return {
        "subtasks": [
            {
                "id": "s1",
                "title": "Complete the task",
                "goal": goal,
                "rail": "browser",
                "steps": [],
                "edge_cases": [],
                "done_when": "the task is complete",
                "status": "pending",
            }
        ],
        "notes": "",
        "planned": False,
        "plan_error": reason,
        "goal": goal,
    }


def build_plan(goal: str, *, start_url: str | None = None) -> dict:
    """Decompose a task before any clicking. Never raises — a failed plan
    degrades to the raw goal, because a run without a plan still beats no run."""
    from browser_agent import tools  # local import: keeps the module cycle-free

    model = runtime_config.get_for_agent(AGENT_ID, "browser_planner_model")
    where = f"\nThe agent starts on: {start_url}" if start_url else ""
    ready = tools.availability()
    offline = [name for name in tools.TOOLS if not ready.get(name)]
    unavailable = (
        f"\nNOT available right now, do not plan around them: {', '.join(offline)}."
        if offline else ""
    )
    prompt = (
        f"TASK: {goal}{where}\n\n"
        f"In the browser it can: {', '.join(actions.ACTION_KINDS)}. It sees a page "
        "as a numbered list of interactive elements and can look at a screenshot "
        "when stuck.\n\n"
        f"Its direct APIs (the 'data' rail):\n{tools.grammar()}{unavailable}\n\n"
        "Produce the plan JSON."
    )
    try:
        llm = openrouter.get_llm(temperature=0.3, model=model, agent_id=AGENT_ID)
        response = llm.invoke([("system", _SYSTEM), ("user", prompt)])
        return _clean(_parse(getattr(response, "content", response)), goal)
    except Exception as exc:  # noqa: BLE001 — planning must never kill the run
        logger.warning("browser planner failed: %s", exc)
        return fallback_plan(goal, str(exc)[:200])


# --------------------------------------------------------------------------- #
# Progress through the plan
# --------------------------------------------------------------------------- #

def current(plan: dict) -> dict | None:
    for sub in plan.get("subtasks") or []:
        if sub.get("status") != "done":
            return sub
    return None


def advance(plan: dict) -> dict | None:
    """Mark the current sub-task done; return the next one (None when finished)."""
    active = current(plan)
    if active:
        active["status"] = "done"
    return current(plan)


def progress(plan: dict) -> tuple[int, int]:
    subtasks = plan.get("subtasks") or []
    return sum(1 for s in subtasks if s.get("status") == "done"), len(subtasks)
