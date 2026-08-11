"""Learned flows: do it once by reasoning, then repeat it by memory.

Today the agent rediscovers Gmail's compose button every single time. That is
slow, expensive, and a fresh chance to get lost. A person doesn't do this — they
learn a route once and then walk it. Skills are that memory.

A skill is a sequence of steps that worked, saved against the site it worked on.
Replay costs no model calls at all, which is both the speed win and the
reliability win: a replayed step cannot hallucinate.

Two decisions worth stating:

**Saving is never automatic.** The user approves each skill (their call). A run
that limped to a finish is exactly the run you don't want to enshrine, and no
heuristic reliably tells "worked" from "worked eventually".

**Replay is trusted only as far as it verifies.** Every step is found by NAME,
never by position, and the moment one doesn't match, replay stops and the model
takes over from that point. A half-remembered route is worse than none, so the
failure path is the important half of this file.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from browser_agent import state

_INDEX_DOC = "skills-index"
MAX_SKILLS = 200
MAX_STEPS = 40
MIN_MATCH = 0.45

# Steps worth replaying. Anything else (waiting, scrolling, looking) is noise
# that the live page will tell us about better than a recording will.
REPLAYABLE = frozenset({"navigate", "click", "type", "select", "key", "open_tab"})

_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about which task this is.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "at", "my",
    "me", "please", "then", "with", "from", "into", "it", "this", "that",
    "go", "open", "get", "do", "make", "give",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower().replace("www.", "")
    except ValueError:
        return ""


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def similarity(a: str, b: str) -> float:
    """How alike two task descriptions are, 0..1 (Jaccard over content words)."""
    x, y = _tokens(a), _tokens(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


# --------------------------------------------------------------------------- #
# Building a skill out of a finished run
# --------------------------------------------------------------------------- #

def steps_from_run(run: dict) -> list[dict]:
    """The replayable spine of a run: the steps that changed something and worked.

    A step without a name is dropped rather than saved with its index — indexes
    are re-assigned every observation, so a remembered number means nothing on
    the next visit.
    """
    kept: list[dict] = []
    for step in run.get("steps") or []:
        action = step.get("action") or {}
        kind = action.get("kind")
        if kind not in REPLAYABLE:
            continue
        result = step.get("result") or {}
        if result.get("ok") is False:
            continue
        if kind in ("click", "type", "select") and not action.get("expect"):
            continue
        kept.append({
            k: v
            for k, v in {
                "kind": kind,
                "expect": action.get("expect"),
                "role": action.get("role"),
                "text": action.get("text"),
                "value": action.get("value"),
                "url": action.get("url"),
                "why": action.get("why"),
                "expect_after": action.get("expect_after"),
            }.items()
            if v is not None
        })
        if len(kept) >= MAX_STEPS:
            break
    return kept


def _clean_steps(raw: list[dict]) -> list[dict]:
    """Trim recorded or run-derived steps to the replayable shape."""
    out: list[dict] = []
    for item in (raw or [])[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in REPLAYABLE:
            continue
        step = {"kind": kind}
        for field in ("expect", "role", "text", "value", "url", "why", "expect_after"):
            value = item.get(field)
            if value not in (None, ""):
                step[field] = str(value)[:400]
        if kind in ("click", "type", "select") and not step.get("expect"):
            continue
        if kind in ("navigate", "open_tab") and not step.get("url"):
            continue
        out.append(step)
    return out


def save_skill(user: dict, name: str, goal: str, host: str, steps: list[dict],
               *, source: str = "run") -> dict:
    """Store an approved skill. Raises ValueError when there is nothing usable."""
    clean = _clean_steps(steps)
    if not clean:
        raise ValueError(
            "There are no repeatable steps here — a skill needs actions that "
            "name what they act on."
        )
    skill = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or goal or "Untitled").strip()[:80],
        "goal": (goal or "").strip()[:300],
        "host": (host or "").lower().replace("www.", "")[:120],
        "steps": clean,
        "source": source,  # "run" (learned) or "recording" (demonstrated)
        "user_id": str(user.get("id") or ""),
        "user": str(user.get("email") or ""),
        "created_at": _now(),
        "uses": 0,
        "last_used": None,
        "last_ok": None,
    }
    state.save(f"skill-{skill['id']}", skill)
    index = state.load(_INDEX_DOC) or {"skills": []}
    rows = [s for s in index.get("skills") or [] if s.get("id") != skill["id"]]
    rows.insert(0, _row(skill))
    state.save(_INDEX_DOC, {"skills": rows[:MAX_SKILLS]})
    return skill


def _row(skill: dict) -> dict:
    return {
        "id": skill["id"], "name": skill["name"], "goal": skill["goal"],
        "host": skill["host"], "steps": len(skill["steps"]), "source": skill["source"],
        "user_id": skill["user_id"], "created_at": skill["created_at"],
        "uses": skill.get("uses", 0), "last_ok": skill.get("last_ok"),
    }


def get_skill(skill_id: str) -> dict | None:
    return state.load(f"skill-{skill_id}")


def list_skills(user_id: str | None = None) -> list[dict]:
    rows = (state.load(_INDEX_DOC) or {}).get("skills") or []
    if user_id is not None:
        rows = [r for r in rows if r.get("user_id") == user_id]
    return rows


def delete_skill(skill_id: str) -> bool:
    found = get_skill(skill_id)
    if not found:
        return False
    state.delete(f"skill-{skill_id}")
    index = state.load(_INDEX_DOC) or {"skills": []}
    state.save(_INDEX_DOC, {
        "skills": [s for s in index.get("skills") or [] if s.get("id") != skill_id]
    })
    return True


def record_use(skill_id: str, *, ok: bool) -> None:
    """Keep an honest record of whether a skill is still working."""
    skill = get_skill(skill_id)
    if not skill:
        return
    skill["uses"] = int(skill.get("uses", 0)) + 1
    skill["last_used"] = _now()
    skill["last_ok"] = bool(ok)
    state.save(f"skill-{skill_id}", skill)
    index = state.load(_INDEX_DOC) or {"skills": []}
    rows = [_row(skill) if s.get("id") == skill_id else s for s in index.get("skills") or []]
    state.save(_INDEX_DOC, {"skills": rows})


# --------------------------------------------------------------------------- #
# Finding one to use
# --------------------------------------------------------------------------- #

def find_match(goal: str, user_id: str, *, start_url: str | None = None) -> dict | None:
    """The best saved skill for this task, or None.

    Deliberately conservative: a wrong skill sends the agent confidently down
    the wrong path, which is worse than thinking from scratch. Host must agree
    when we know it, and the wording has to genuinely overlap.
    """
    host = host_of(start_url or "")
    best: tuple[float, dict] | None = None
    for row in list_skills(user_id=user_id):
        if host and row.get("host") and row["host"] != host:
            continue
        score = max(similarity(goal, row.get("goal", "")), similarity(goal, row.get("name", "")))
        if score < MIN_MATCH:
            continue
        # A skill that failed last time is a worse bet than one that worked.
        if row.get("last_ok") is False:
            score -= 0.15
        if not best or score > best[0]:
            best = (score, row)
    if not best:
        return None
    full = get_skill(best[1]["id"])
    if full:
        full["match_score"] = round(best[0], 2)
    return full
