"""Run lifecycle: create → step (idempotent) → stop. The extension owns the loop.

Each ``step`` call carries one observation and returns one decided action. The
``seq`` counter makes replays safe: a re-POST of the current seq returns the
cached decision (that is how the extension resumes after MV3 kills its service
worker mid-run), and anything else out of order is an honest 409.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from browser_agent import actions, brain, planner, state

TERMINAL = frozenset({"completed", "failed", "stopped"})

_INDEX_DOC = "runs-index"
_INDEX_CAP = 200
_MAX_FINDING_CHARS = 8_000
_MAX_FINDINGS = 40
# Identical failing steps in a row mean the page isn't responding the way the
# model thinks. Burning the whole step cap on that helps nobody.
_STUCK_AFTER = 4
# Steps allowed on one unchanging screen before we call it. The repeat guard
# above only catches the SAME action twice; real stuck-ness usually looks like
# variety — Escape, click, reload, Escape — none of which move the page. Counting
# steps-without-progress catches the cycle the repeat guard walks straight past.
_NO_PROGRESS_AFTER = 8
# Repeats before we ask the extension for a screenshot. Text alone can't explain
# why a click does nothing on an app like Gmail — an overlay, a control that only
# looks focusable, an element the model is reading as the wrong thing. One repeat
# is noise; two means the text view isn't enough, so let the model look.
_LOOK_AFTER = 2


class ProtocolMismatch(ValueError):
    """Extension protocol version differs from the server's."""


class OutOfSync(ValueError):
    """Step seq is neither a replay of the current step nor the next one."""


def disabled() -> bool:
    return os.environ.get("BROWSER_AGENT_DISABLED", "0") == "1"


def policy() -> dict:
    """The safety policy handed to the extension at run start (and enforced here)."""
    blocked = actions.parse_domains(
        os.environ.get("BROWSER_BLOCKED_DOMAINS", actions.DEFAULT_BLOCKED_DOMAINS)
    )
    allowed = actions.parse_domains(os.environ.get("BROWSER_ALLOWED_DOMAINS", ""))
    try:
        step_cap = int(os.environ.get("BROWSER_MAX_STEPS", "40"))
    except ValueError:
        step_cap = 40
    return {
        "step_cap": max(1, min(step_cap, 200)),
        "allowed": sorted(allowed),
        "blocked": sorted(blocked),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(user: dict, goal: str, mode: str, start_url: str | None) -> dict:
    pol = policy()
    # Plan before touching the browser: a bad first move costs every step after it.
    plan = planner.build_plan(goal.strip(), start_url=start_url)
    run = {
        "id": uuid.uuid4().hex[:12],
        "protocol": actions.PROTOCOL,
        "goal": goal.strip(),
        "plan": plan,
        "mode": mode,
        "start_url": start_url,
        "status": "running",
        "user": str(user.get("email") or ""),
        "user_id": str(user.get("id") or ""),
        "created_at": _now(),
        "updated_at": _now(),
        "step_cap": pol["step_cap"],
        "allowed": pol["allowed"],
        "blocked": pol["blocked"],
        "sensitive_confirm": True,
        "steps_used": 0,
        "seq": 0,
        "steps": [],
        "findings": [],
        "pending": None,
        "last_decision": None,
        "tracked_end": False,
    }
    _persist(run)
    return run


def get_run(run_id: str) -> dict | None:
    return state.load(f"run-{run_id}")


def list_runs(user_id: str | None = None, limit: int = 50) -> list[dict]:
    index = state.load(_INDEX_DOC) or {}
    rows = index.get("runs") or []
    if user_id is not None:
        rows = [r for r in rows if r.get("user_id") == user_id]
    return rows[: max(1, min(limit, _INDEX_CAP))]


def _index_entry(run: dict) -> dict:
    return {
        "id": run["id"],
        "goal": str(run.get("goal") or "")[:160],
        "mode": run.get("mode"),
        "status": run.get("status"),
        "steps_used": run.get("steps_used", 0),
        "step_cap": run.get("step_cap", 0),
        "user": run.get("user"),
        "user_id": run.get("user_id"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "summary": str(run.get("summary") or "")[:200],
    }


def _persist(run: dict) -> None:
    state.save(f"run-{run['id']}", run)
    index = state.load(_INDEX_DOC) or {"runs": []}
    rows = [r for r in index.get("runs") or [] if r.get("id") != run["id"]]
    rows.insert(0, _index_entry(run))
    state.save(_INDEX_DOC, {"runs": rows[:_INDEX_CAP]})


def _response(
    run: dict,
    action: dict | None,
    *,
    requires_confirmation: bool = False,
    want_screenshot: bool = False,
) -> dict:
    return {
        "protocol": actions.PROTOCOL,
        "action": action,
        "done": run["status"] in TERMINAL,
        "status": run["status"],
        "requires_confirmation": requires_confirmation,
        # Asks the extension to attach a screenshot to the NEXT observation, so
        # the model can look at the page when the text view stops explaining it.
        "want_screenshot": want_screenshot,
        "subtask": (planner.current(run.get("plan") or {}) or {}).get("title"),
        "subtask_progress": list(planner.progress(run.get("plan") or {})),
        "steps_remaining": max(0, int(run["step_cap"]) - int(run["steps_used"])),
        "summary": run.get("summary"),
        "fail_reason": run.get("fail_reason"),
    }


def _fingerprint(action: dict) -> str:
    return f"{action.get('kind')}:{action.get('index')}:{action.get('url')}:{action.get('text')}"


def page_fingerprint(body: dict) -> str:
    """A cheap "is this still the same screen?" signature.

    Clicking a control that does nothing still reports success — the click was
    dispatched, it just achieved nothing — so counting failures alone misses the
    worst loop: the same click landing forever on an unchanged page. Comparing
    the screen instead catches it, while leaving genuine repetition (scrolling a
    long page changes which elements are in view) alone.

    Hashes EVERY label, not the first handful: a dropdown opening near the
    bottom of a compose window is real progress, and a fingerprint that ignored
    it would call a working run stuck.
    """
    dom = body.get("dom") or {}
    elements = dom.get("elements") or []
    labels = "|".join(str(e.get("text") or "")[:32] for e in elements)
    digest = hashlib.md5(labels.encode("utf-8", "replace")).hexdigest()[:12]
    url = (body.get("tab") or {}).get("url") or ""
    return f"{url}#{len(elements)}#{digest}"


def repeat_count(run: dict, action: actions.Action, page_fp: str) -> int:
    """How many times in a row this exact action has been tried on this screen."""
    fingerprint = _fingerprint(action.model_dump(exclude_none=True))
    repeats = 1
    for step in reversed(run.get("steps") or []):
        if _fingerprint(step.get("action") or {}) != fingerprint:
            break
        if step.get("page_fp") != page_fp:
            break  # the screen moved on, so this isn't the same attempt again
        repeats += 1
    return repeats


def steps_without_progress(run: dict, page_fp: str) -> int:
    """Consecutive steps taken while the screen stayed exactly the same."""
    count = 0
    for step in reversed(run.get("steps") or []):
        if step.get("page_fp") != page_fp:
            break
        count += 1
    return count


def stuck_on(
    run: dict, action: actions.Action, page_fp: str, *, canvas_app: bool = False
) -> str | None:
    """Why this run should stop, if flailing is all that is left."""
    # Whichever guard fires, the canvas explanation is the one that actually
    # tells the user what to do differently — so it rides along with both.
    canvas_note = (
        " This page draws its content on a canvas, which I can't read or click "
        "into — the cells and text simply aren't in the page for me to work with."
        if canvas_app
        else ""
    )

    repeats = repeat_count(run, action, page_fp)
    if repeats >= _STUCK_AFTER:
        return (
            f'tried the same "{action.kind}" step {repeats} times and the page '
            f"never changed.{canvas_note or ' It is not responding the way I expected.'}"
        )
    stalled = steps_without_progress(run, page_fp)
    if stalled >= _NO_PROGRESS_AFTER:
        return (
            f"{stalled} steps on the same screen with no progress."
            f"{canvas_note or ' Nothing I try changes this screen.'}"
        )
    return None


def _veto(run: dict, action: actions.Action) -> str | None:
    """Policy check on a decided action; returns the honest refusal or None."""
    if run.get("mode") == "monitor" and action.kind in actions.MUTATING_KINDS:
        return f'monitor mode is read-only — "{action.kind}" is not allowed'
    if action.kind in ("navigate", "open_tab"):
        return actions.check_url(
            action.url or "", set(run.get("allowed") or []), set(run.get("blocked") or [])
        )
    return None


def step(run: dict, body: dict) -> tuple[dict, dict]:
    """Advance the run by one decision. Returns (run, response)."""
    if run["status"] in TERMINAL:
        return run, _response(run, None)

    if int(body.get("protocol") or 0) != actions.PROTOCOL:
        raise ProtocolMismatch(
            f"extension protocol {body.get('protocol')!r} unsupported "
            f"(server speaks {actions.PROTOCOL}) — please update the extension"
        )

    seq = int(body.get("seq") or 0)

    # Replay of the current step — cached decision (worker-death resume) and
    # the confirmation handshake both land here.
    if seq == run["seq"] and run.get("last_decision"):
        decision = run["last_decision"]
        if run.get("pending") and body.get("confirmed"):
            run["pending"] = None
            run["status"] = "running"
            decision = dict(decision, requires_confirmation=False, status="running")
            run["last_decision"] = decision
            run["updated_at"] = _now()
            _persist(run)
        return run, decision

    if seq != run["seq"] + 1:
        raise OutOfSync(f"expected seq {run['seq']} (replay) or {run['seq'] + 1}, got {seq}")

    # Attach the result of the previous action; harvest extracted page text.
    last = body.get("last_result")
    if isinstance(last, dict) and run["steps"]:
        run["steps"][-1]["result"] = {"ok": bool(last.get("ok")), "error": last.get("error")}
        extracted = last.get("extracted")
        if extracted and len(run["findings"]) < _MAX_FINDINGS:
            run["findings"].append(str(extracted)[:_MAX_FINDING_CHARS])

    run["seq"] = seq

    if run["steps_used"] >= run["step_cap"]:
        run["status"] = "failed"
        run["fail_reason"] = f"step cap reached ({run['step_cap']})"
        response = _response(run, None)
        run["last_decision"] = response
        run["updated_at"] = _now()
        _persist(run)
        return run, response

    action = brain.decide(run, body)

    # "next_subtask" never leaves the server: mark the milestone done and ask
    # again for a real action on the new sub-task, so the extension still gets
    # exactly one executable action per step.
    advanced: list[str] = []
    while action.kind == "next_subtask" and len(advanced) < len(
        (run.get("plan") or {}).get("subtasks") or []
    ):
        finished = planner.current(run.get("plan") or {})
        following = planner.advance(run.get("plan") or {})
        advanced.append(str((finished or {}).get("title") or ""))
        if not following:
            action = actions.Action(
                kind="done",
                summary=f"Completed every step of the plan for: {run.get('goal', '')}",
                why="The last sub-task is finished.",
            )
            break
        action = brain.decide(run, body)

    veto = _veto(run, action)
    if veto:
        retry = dict(body, last_result={"ok": False, "error": veto})
        action = brain.decide(run, retry)
        veto = _veto(run, action)
        if veto:
            action = actions.Action(kind="fail", reason=veto, why="Blocked by policy.")

    page_fp = page_fingerprint(body)
    canvas_app = bool((body.get("dom") or {}).get("canvas_app"))
    stuck = stuck_on(run, action, page_fp, canvas_app=canvas_app)
    if stuck:
        action = actions.Action(kind="fail", reason=stuck, why="Going in circles.")

    # Ask for a look once repetition starts, unless we already had one this step.
    want_screenshot = (
        not body.get("screenshot")
        and action.kind not in ("done", "fail")
        and repeat_count(run, action, page_fp) >= _LOOK_AFTER
    )

    elements = (body.get("dom") or {}).get("elements") or []
    sensitive = bool(run.get("sensitive_confirm", True)) and actions.is_sensitive(
        action, elements
    )
    act_dict = action.model_dump(exclude_none=True)

    active = planner.current(run.get("plan") or {})
    run["steps_used"] += 1
    run["steps"].append(
        {"seq": seq, "action": act_dict, "at": _now(), "sensitive": sensitive,
         "page_fp": page_fp, "saw_page": bool(body.get("screenshot")),
         "subtask": (active or {}).get("title"), "completed": advanced, "result": None}
    )

    if action.kind == "done":
        run["status"] = "completed"
        run["summary"] = action.summary
        if action.extracted is not None:
            run["extracted"] = action.extracted
        response = _response(run, act_dict)
    elif action.kind == "fail":
        run["status"] = "failed"
        run["fail_reason"] = action.reason
        response = _response(run, act_dict)
    elif sensitive and not body.get("confirmed"):
        run["status"] = "awaiting_confirmation"
        run["pending"] = act_dict
        response = _response(run, act_dict, requires_confirmation=True)
    elif action.kind == "ask_user":
        run["status"] = "awaiting_user"
        response = _response(run, act_dict)
    else:
        run["status"] = "running"
        response = _response(run, act_dict, want_screenshot=want_screenshot)

    run["last_decision"] = response
    run["updated_at"] = _now()
    _persist(run)
    return run, response


def stop_run(run: dict) -> dict:
    if run["status"] not in TERMINAL:
        run["status"] = "stopped"
        run["updated_at"] = _now()
        run["last_decision"] = _response(run, None)
        _persist(run)
    return run
