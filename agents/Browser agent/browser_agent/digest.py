"""Tab monitoring: turn a browsing trail into a short plain-language digest.

Design rule that makes this cheap: the extension NEVER calls us on a tab event.
It keeps a small local ring buffer and posts the whole thing only when the user
asks (or on a schedule they turned on themselves). So watching 40 tabs all day
costs nothing until someone actually wants to know what happened.

Watch rules ("tell me when a Jira ticket about billing shows up") are evaluated
inside the same call — matched by plain substring here, then handed to the model
for the human sentence. A rule that matches nothing says so; we never invent an
alert to look useful.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.services import openrouter

from browser_agent import state

logger = logging.getLogger("agentos.browser")

AGENT_ID = "a11"
CONFIG_DOC = "config-global"

MAX_EVENTS = 200
MAX_RULES = 20
_MAX_TITLE = 120
_INDEX_DOC = "digests-index"
_INDEX_CAP = 60

_SYSTEM = """You summarize a person's browsing trail into a calm, useful digest.

You get a list of pages they visited (title + domain + time) and the tabs open
right now. Write for the person themselves, in plain language.

Reply with ONE JSON object, no markdown fences:
{"headline": "...", "themes": [{"title": "...", "detail": "..."}], "open_loops": ["..."]}

- "headline": one sentence on what this stretch of browsing was mostly about.
- "themes": 2-4 groupings of related pages. "detail" is one sentence each.
- "open_loops": things that look unfinished — a half-read doc, a PR left open,
  a form not submitted. Empty list if nothing stands out. Do not pad it.

Be concrete and specific. Never invent a page that isn't in the list. If the
trail is too thin to say anything useful, say exactly that in the headline and
return empty themes."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict:
    return state.load(CONFIG_DOC) or {"watch_rules": [], "updated_at": None}


def save_config(patch: dict) -> dict:
    cfg = load_config()
    if "watch_rules" in patch:
        rules = []
        for rule in (patch["watch_rules"] or [])[:MAX_RULES]:
            text = str(rule.get("text") or "").strip()[:200]
            if text:
                rules.append({"id": str(rule.get("id") or text[:24]), "text": text,
                              "enabled": bool(rule.get("enabled", True))})
        cfg["watch_rules"] = rules
    cfg["updated_at"] = _now()
    state.save(CONFIG_DOC, cfg)
    return cfg


def _clean_events(events: list[dict]) -> list[dict]:
    """Keep the newest MAX_EVENTS web pages, deduped on consecutive repeats."""
    cleaned: list[dict] = []
    for ev in events[-MAX_EVENTS:]:
        url = str(ev.get("url") or "")
        if not url.startswith("http"):
            continue
        row = {
            "url": url[:400],
            "title": str(ev.get("title") or "")[:_MAX_TITLE],
            "at": str(ev.get("at") or ""),
            "domain": str(ev.get("domain") or ""),
        }
        if cleaned and cleaned[-1]["url"] == row["url"]:
            continue  # same page reloaded/refocused — one entry is enough
        cleaned.append(row)
    return cleaned


def match_rules(events: list[dict], tabs: list[dict], rules: list[dict]) -> list[dict]:
    """Plain substring matching over title+url. Deterministic, no LLM, no guessing."""
    haystack = [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "text": f"{r.get('title', '')} {r.get('url', '')}".lower()}
        for r in [*events, *tabs]
    ]
    alerts: list[dict] = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        needle = str(rule.get("text") or "").strip().lower()
        if not needle:
            continue
        hits = [h for h in haystack if needle in h["text"]]
        if not hits:
            continue
        seen: set[str] = set()
        pages = []
        for hit in hits:
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            pages.append({"title": hit["title"], "url": hit["url"]})
        alerts.append({"rule_id": rule.get("id"), "rule": rule.get("text"),
                       "pages": pages[:10], "count": len(seen)})
    return alerts


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
        raise ValueError("model returned no JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model returned JSON that is not an object")
    return parsed


def _summarize(events: list[dict], tabs: list[dict]) -> dict:
    trail = "\n".join(
        f"- {e['at'][:16]} {e['domain'] or ''} — {e['title'] or e['url']}" for e in events
    )
    open_now = "\n".join(f"- {t.get('title') or t.get('url')}" for t in tabs[:30])
    prompt = (
        f"Pages visited ({len(events)}):\n{trail or '(none)'}\n\n"
        f"Tabs open right now ({len(tabs)}):\n{open_now or '(none)'}\n\n"
        "Write the digest JSON."
    )
    # Fast model: this is summarizing, not reasoning about what to click.
    llm = openrouter.get_llm(temperature=0.3, fast=True, agent_id=AGENT_ID)
    response = llm.invoke([("system", _SYSTEM), ("user", prompt)])
    return _parse(getattr(response, "content", response))


def build_digest(user: dict, events: list[dict], tabs: list[dict]) -> dict:
    """One digest from a browsing trail. Raises ValueError if the model misbehaves."""
    cleaned = _clean_events(events or [])
    tabs = [t for t in (tabs or []) if str(t.get("url", "")).startswith("http")][:30]
    cfg = load_config()
    alerts = match_rules(cleaned, tabs, cfg.get("watch_rules") or [])

    if not cleaned and not tabs:
        summary = {
            "headline": "Nothing to report — no web pages were open or visited.",
            "themes": [],
            "open_loops": [],
        }
    else:
        summary = _summarize(cleaned, tabs)

    now = _now()
    digest = {
        "id": now.replace(":", "-")[:19],
        "at": now,
        "user": str(user.get("email") or ""),
        "user_id": str(user.get("id") or ""),
        "pages_seen": len(cleaned),
        "tabs_open": len(tabs),
        "headline": str(summary.get("headline") or "")[:500],
        "themes": [
            {"title": str(t.get("title") or "")[:120], "detail": str(t.get("detail") or "")[:400]}
            for t in (summary.get("themes") or [])[:6]
            if isinstance(t, dict)
        ],
        "open_loops": [str(x)[:300] for x in (summary.get("open_loops") or [])[:8]],
        "alerts": alerts,
    }
    _persist(digest)
    return digest


def _persist(digest: dict) -> None:
    state.save(f"digest-{digest['id']}", digest)
    index = state.load(_INDEX_DOC) or {"digests": []}
    rows = [r for r in index.get("digests") or [] if r.get("id") != digest["id"]]
    rows.insert(0, {
        "id": digest["id"], "at": digest["at"], "user_id": digest["user_id"],
        "headline": digest["headline"], "pages_seen": digest["pages_seen"],
        "alerts": len(digest["alerts"]),
    })
    state.save(_INDEX_DOC, {"digests": rows[:_INDEX_CAP]})


def list_digests(user_id: str | None = None, limit: int = 30) -> list[dict]:
    rows = (state.load(_INDEX_DOC) or {}).get("digests") or []
    if user_id is not None:
        rows = [r for r in rows if r.get("user_id") == user_id]
    return rows[: max(1, min(limit, _INDEX_CAP))]


def get_digest(digest_id: str) -> dict | None:
    return state.load(f"digest-{digest_id}")
