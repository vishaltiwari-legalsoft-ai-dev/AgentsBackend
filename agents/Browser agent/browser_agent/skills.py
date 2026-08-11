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

from browser_agent import actions, state

_INDEX_DOC = "skills-index"
MAX_SKILLS_PER_USER = 200
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
    """Site a route belongs to. Anchored strip — an unanchored replace would
    turn "a.wwww.com" into "a.wcom" and quietly mismatch two real hosts."""
    return re.sub(r"^www\.", "", actions.domain_of(url))


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
        # Only a step we SAW succeed. A step still carrying result=None was
        # never confirmed — the run may have been stopped, or the worker died
        # before the next step reported back. Enshrining it would repeat an
        # action nobody ever verified.
        if (step.get("result") or {}).get("ok") is not True:
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
    """Trim steps to the replayable shape, keeping only ones that can replay.

    Validation is delegated to ``actions.validate_action`` rather than
    re-checked here: a second, weaker copy of the rules is how you end up
    saving a step that is guaranteed to abandon the route on first use.
    """
    out: list[dict] = []
    for item in (raw or [])[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in REPLAYABLE:
            continue
        step: dict = {"kind": kind}
        for field in ("expect", "role", "text", "value", "url", "why", "expect_after"):
            value = item.get(field)
            if value is not None:
                step[field] = str(value)[:400]
        try:
            actions.validate_action(step)
        except ValueError:
            continue  # unusable on replay, so not worth remembering
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
    state.save(_INDEX_DOC, {"skills": _capped(rows)})
    return skill


def _capped(rows: list[dict]) -> list[dict]:
    """Trim per owner, not globally — a busy colleague must not silently evict
    everyone else's learned routes from the only list anything reads."""
    kept_per_user: dict[str, int] = {}
    out: list[dict] = []
    for row in rows:
        owner = str(row.get("user_id") or "")
        seen = kept_per_user.get(owner, 0)
        if seen >= MAX_SKILLS_PER_USER:
            continue
        kept_per_user[owner] = seen + 1
        out.append(row)
    return out


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

# Text that is clearly a specific value rather than a fixed part of the route:
# an address, a number, a reference. Typing one of these from memory is how a
# remembered route sends the right message to the wrong person.
_LITERAL_RE = re.compile(r"[\w.+-]+@[\w.-]+|\b\d[\d,./-]{2,}\b")


def literals(steps: list[dict]) -> list[str]:
    """Specific values a saved route would type from memory."""
    found: list[str] = []
    for step in steps or []:
        if step.get("kind") != "type":
            continue
        for match in _LITERAL_RE.findall(str(step.get("text") or "")):
            if match not in found:
                found.append(match)
    return found


def parameters_still_apply(goal: str, steps: list[dict]) -> bool:
    """Would replaying this route type values the new task never mentioned?

    The matcher is fuzzy on purpose — "send a hello email" and "please send
    hello email to my friend" are the same job. But the saved steps are literal,
    so the second task would silently reuse the first one's recipient. Any
    specific value in the route must appear in the new goal, or the route is not
    safe to replay as-is.
    """
    lowered = (goal or "").lower()
    return all(value.lower() in lowered for value in literals(steps))


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
        # A skill that failed last time is a worse bet — weigh that BEFORE the
        # threshold, or a known-broken route can still scrape in.
        if row.get("last_ok") is False:
            score -= 0.15
        if score < MIN_MATCH:
            continue
        if not best or score > best[0]:
            best = (score, row)
    if not best:
        return None

    full = get_skill(best[1]["id"])
    if not full:
        return None
    if not parameters_still_apply(goal, full.get("steps") or []):
        # Same shape of task, different details. Thinking it through is slower
        # than replaying, and enormously better than acting on stale values.
        return None
    full["match_score"] = round(best[0], 2)
    return full
