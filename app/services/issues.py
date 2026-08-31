"""Issues — one typed record of what is wrong, built from signals that already exist.

The console has carried every one of these signals for months: ``degraded``
notes on an SEO run doc, engine modes on the GEO status, failure streaks and the
last-completed stamp on the poll config, a plan nobody has ticked. They were
scattered across four panels as raw strings, and the most important one —
"Search Console has never granted access to this property" — rendered as an
``<HttpError 403 …>`` repr inside a brand card. This module reads those signals
and says, in plain words, what is wrong and where to go to fix it.

Pure on purpose: every builder takes documents and never loads them. The router
owns the reads (and the per-source failure handling); this module owns the
vocabulary. That split is what makes each rule a golden test rather than a
Firestore fixture.

Two shapes matter to anyone consuming this:

* :class:`Issue` — ``id`` is a stable ``sha1(area|brand_id|code)[:12]``, so the
  same problem keeps the same id across refreshes and a panel can key on it.
* :class:`Fix` — where the console should send the reader: a workspace slug,
  the brand as ``subject``, and a section id from that workspace's tab list.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, Iterable, TypedDict

from final_geo_agent.geo_poll import DEFAULT_POLL_INTERVAL_DAYS, FAIL_STREAK_LIMIT

SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
AREAS: tuple[str, ...] = ("seo", "geo", "runs")

TITLE_MAX = 80
DETAIL_MAX = 240

#: A plan the team has not touched for this long is a plan that is not being
#: worked — or one that no longer matches the numbers it was written from.
PLAN_UNTOUCHED_DAYS = 14

#: A sweep is overdue once it is this many intervals late: one missed fire is a
#: budget-truncated day, two is a scheduler that has stopped.
STALE_INTERVALS = 2

#: The brand slot for a problem that belongs to the whole workspace rather than
#: one brand (the registry itself could not be read).
WORKSPACE_BRAND: dict[str, str] = {"id": "", "name": "All brands"}

ENGINE_LABELS: dict[str, str] = {
    "perplexity": "Perplexity",
    "gemini": "Gemini",
    "chatgpt": "ChatGPT",
    "aio": "Google AI Overview",
    "ai_mode": "Google AI Mode",
}

_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


class Fix(TypedDict):
    label: str
    workspace: str  # "seo" | "geo"
    subject: str    # brand_id
    section: str    # a section id of that workspace


class Issue(TypedDict):
    id: str
    severity: str   # "high" | "medium" | "low"
    area: str       # "seo" | "geo" | "runs"
    brand_id: str
    brand: str
    code: str
    title: str
    detail: str
    fix: Fix | None
    since: str | None


# ------------------------------------------------------------------ shape ----

def issue_id(area: str, brand_id: str, code: str) -> str:
    return hashlib.sha1(f"{area}|{brand_id}|{code}".encode()).hexdigest()[:12]


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fix(label: str, workspace: str, subject: str, section: str) -> Fix:
    return {"label": label, "workspace": workspace, "subject": subject, "section": section}


def _issue(
    area: str, brand: dict, code: str, severity: str, title: str, detail: str,
    *, fix: Fix | None = None, since: str | None = None,
) -> Issue:
    brand_id = str(brand.get("id") or "")
    return {
        "id": issue_id(area, brand_id, code),
        "severity": severity,
        "area": area,
        "brand_id": brand_id,
        "brand": str(brand.get("name") or brand_id),
        "code": code,
        "title": _clip(title, TITLE_MAX),
        "detail": _clip(detail, DETAIL_MAX),
        "fix": fix,
        "since": since,
    }


def unreadable_issue(area: str, brand: dict, source: str) -> Issue:
    """The issue that stands in for a source that could not be read at all.

    The detail is a fixed sentence: the exception belongs in the server log,
    never in a body that leaves the process. What the reader must not conclude
    from a missing section is that the section is healthy — so it says so.
    """
    code = "unreadable_" + re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")
    return _issue(
        area, brand, code, "low",
        f"{source} could not be read",
        f"{source} could not be read just now, so nothing from it is known — that is "
        "not the same as it being healthy. The reason is in the server log.",
    )


# --------------------------------------------------------------- parsing ----

def _parse_at(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _days_between(earlier: dt.datetime, later: dt.datetime) -> int:
    return max(0, int((later - earlier).total_seconds() // 86400))


def _day_word(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine) or engine.replace("_", " ").title()


# ------------------------------------------------------ Google error text ----

_SOURCES: dict[str, tuple[str, str]] = {
    "search console": ("gsc", "Search Console"),
    "google analytics": ("ga", "Google Analytics"),
    "rank tracking": ("rank", "Rank tracking"),
    "page analytics": ("pages", "Page analytics"),
    "serper": ("serper", "Serper"),
}

_GRANT_HOWTO: dict[str, str] = {
    "gsc": "Add the service account as a user on the property in Search Console, then reconnect.",
    "ga": "Add the service account as a Viewer on the property in Google Analytics, then reconnect.",
}

_HTTPERROR = re.compile(r"<HttpError\b.*?>", re.S)
_RETURNED = re.compile(r'returned\s+"(.*?)"', re.S)
_URL = re.compile(r"https?://\S+")
_STATUS = re.compile(r"HttpError\s+(\d{3})")
_REJECTED = re.compile(r"\brejected\s+(\S+?)(?::\s|$)")
_FOR_SITE = re.compile(r"for (?:site|property) '([^']+)'")


def _source_key(source: str) -> tuple[str, str]:
    """(code prefix, display name) for a ``degraded`` note's source label."""
    known = _SOURCES.get(source.strip().lower())
    if known:
        return known
    prefix = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_") or "seo"
    return prefix, source.strip() or "SEO analysis"


def _human_part(message: str) -> str:
    """The sentence a person can act on, out of a Google client error string.

    The quoted ``returned "…"`` reason is the human part when it exists; the
    surrounding ``<HttpError …>`` repr and any URL are noise for a panel.
    """
    quoted = _RETURNED.search(message)
    text = quoted.group(1) if quoted else _HTTPERROR.sub("", message)
    text = _URL.sub("", text)
    text = re.sub(r"\bSee also:?\s*", "", text)
    return re.sub(r"\s+", " ", text).strip(" :.-")


def _property_in(message: str) -> str:
    for pattern in (_REJECTED, _FOR_SITE):
        found = pattern.search(message)
        if found:
            return found.group(1).rstrip(".,;")
    return ""


def humanize_google_error(source: str, message: str) -> tuple[str, str, str]:
    """``(code, title, detail)`` for one ``"<Source>: <reason>"`` degraded note.

    ``source`` is the prefix ("Search Console", "Google Analytics", …) and
    ``message`` the reason after it — typically a ``CredentialMissing`` text
    wrapping a Google client ``HttpError`` repr.
    """
    prefix, name = _source_key(source)
    prop = _property_in(message)
    where = prop or "this property"
    low = message.lower()
    status = _STATUS.search(message)
    http = int(status.group(1)) if status else None

    if http == 403 or "sufficient permission" in low or "permission" in low or "forbidden" in low:
        return (
            f"{prefix}_no_access",
            f"{name} has not granted access to {where}",
            _GRANT_HOWTO.get(prefix, f"Grant the service account access in {name}, then reconnect."),
        )
    if http == 404 or "not found" in low:
        return (
            f"{prefix}_property_missing",
            f"{name} cannot find {where}",
            f"The property is not in the {name} account this console uses. Check the "
            "property name on the brand, or add the property to that account.",
        )
    if "invalid_grant" in low or "expired" in low or "revoked" in low:
        return (
            f"{prefix}_token_expired",
            f"{name} access has expired for {where}",
            f"The saved {name} connection is no longer valid. Reconnect it for this brand "
            "to renew access.",
        )
    if "not set" in low or "key missing" in low or "no api key" in low or "no key" in low:
        return (
            f"{prefix}_no_key",
            f"{name} has no API key",
            f"No key is configured for {name}, so its data is not collected. Add the key "
            "in Settings → Secrets.",
        )
    human = _human_part(message)[:120]
    return (
        f"{prefix}_unreadable",
        f"{name} could not be read" + (f" for {prop}" if prop else ""),
        human or f"{name} returned an error the console could not interpret.",
    )


# ----------------------------------------------------------------- SEO ----

#: How much a broken source matters to the brand's search picture. Search
#: Console is the primary measurement; Analytics and rank tracking ride along;
#: everything else is a note on the run.
_SEO_SEVERITY: dict[str, str] = {
    "gsc": "high",
    "ga": "medium",
    "rank": "medium",
    "pages": "low",
}


def split_degraded(note: str) -> tuple[str, str]:
    """``"<Source>: <reason>"`` → ``(source, reason)``; no prefix → ``("", note)``."""
    source, sep, reason = note.partition(": ")
    if not sep or not source or len(source) > 40:
        return "", note.strip()
    return source.strip(), reason.strip()


def issues_from_seo(brand: dict, run: dict | None) -> list[Issue]:
    """What the brand's latest SEO run says is wrong, in plain words."""
    brand_id = str(brand.get("id") or "")
    name = str(brand.get("name") or brand_id)
    if not run:
        return [_issue(
            "seo", brand, "seo_never_measured", "medium",
            f"{name} has never been measured for search",
            "No Search Console or ranking data has been collected for this brand yet, so "
            "there is no fix list. Run the first analysis.",
            fix=_fix("Run the first analysis", "seo", brand_id, "fixes"),
        )]

    since = run.get("at") if isinstance(run.get("at"), str) else None
    out: list[Issue] = []
    seen: set[str] = set()
    for note in run.get("degraded") or []:
        if not isinstance(note, str) or not note.strip():
            continue
        source, reason = split_degraded(note)
        if source:
            code, title, detail = humanize_google_error(source, reason)
            severity = _SEO_SEVERITY.get(_source_key(source)[0], "low")
        else:
            code, severity = "seo_note", "low"
            title, detail = "The SEO analysis ran with a limitation", reason[:120]
        if code in seen:
            continue
        seen.add(code)
        out.append(_issue(
            "seo", brand, code, severity, title, detail,
            fix=_fix("Open the fix list", "seo", brand_id, "fixes"), since=since,
        ))
    return out


# ----------------------------------------------------------------- GEO ----

def _interval_days(cfg: dict) -> int:
    raw = cfg.get("poll_interval_days")
    if raw is None:
        return DEFAULT_POLL_INTERVAL_DAYS
    try:
        return max(1, min(int(raw), 30))
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_DAYS


def _calls_for(last_run: dict, engine: str) -> int | None:
    """Calls the last check made to one engine, or ``None`` when the entry
    cannot say — an unknown denominator must not become a failure."""
    calls = last_run.get("calls")
    if isinstance(calls, dict):
        value = calls.get(engine)
        return int(value) if isinstance(value, (int, float)) else None
    engines = last_run.get("engines") or []
    if isinstance(calls, (int, float)) and len(engines) == 1 and engines[0] == engine:
        return int(calls)
    return None


def _engine_off_issues(brand: dict, cfg: dict | None, engine_status: dict) -> list[Issue]:
    out: list[Issue] = []
    last_seen = (cfg or {}).get("engine_last_seen") or {}
    for engine, status in sorted((engine_status or {}).items()):
        if not isinstance(status, dict):
            continue
        if status.get("connected") and status.get("mode") != "off":
            continue
        label = engine_label(engine)
        out.append(_issue(
            "geo", brand, f"engine_off_{engine}", "medium",
            f"{label} is not connected",
            f"No key is configured for {label}, so nothing is measured there and the "
            "brand's visibility numbers leave it out. Add the key in Settings → Secrets.",
            fix=_fix("Connect", "geo", str(brand.get("id") or ""), "settings"),
            since=last_seen.get(engine) if isinstance(last_seen.get(engine), str) else None,
        ))
    return out


def _engine_failed_issues(brand: dict, cfg: dict | None, last_run: dict | None) -> list[Issue]:
    brand_id = str(brand.get("id") or "")
    out: list[Issue] = []
    seen: set[str] = set()
    if last_run:
        errors = last_run.get("errors") or {}
        finished = last_run.get("finished_at") if isinstance(last_run.get("finished_at"), str) else None
        when = f" (finished {finished[:10]})" if finished else ""
        for engine, failed in sorted(errors.items() if isinstance(errors, dict) else []):
            calls = _calls_for(last_run, engine)
            if not isinstance(failed, (int, float)) or failed <= 0 or calls is None or failed < calls:
                continue
            label = engine_label(engine)
            seen.add(engine)
            out.append(_issue(
                "geo", brand, f"engine_failed_{engine}", "high",
                f"{label} failed on every call in the last check",
                f"All {int(calls)} calls to {label} failed in the last sweep{when}, so its "
                "answers are missing from every number. Check the key and the provider's "
                "status, then run a poll.",
                fix=_fix("Open GEO overview", "geo", brand_id, "overview"), since=finished,
            ))
    # The poll's own streak counter says the same thing when no run log entry
    # exists yet; same code, so a brand with both signals shows one issue.
    health = (cfg or {}).get("poll_health") or {}
    day = str(health.get("day") or "")
    stamp = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else ""
    for engine, streak in sorted((health.get("streaks") or {}).items()):
        if engine in seen or not isinstance(streak, (int, float)) or streak < FAIL_STREAK_LIMIT:
            continue
        label = engine_label(engine)
        out.append(_issue(
            "geo", brand, f"engine_failed_{engine}", "high",
            f"{label} failed on every call in the last check",
            f"{label} failed every call in {int(streak)} consecutive batches"
            + (f" on {stamp}" if stamp else "")
            + ", so its answers are missing from every number. Check the key and the "
            "provider's status, then run a poll.",
            fix=_fix("Open GEO overview", "geo", brand_id, "overview"),
            since=stamp or None,
        ))
    return out


def _sweep_issues(brand: dict, cfg: dict, now: dt.datetime) -> list[Issue]:
    brand_id = str(brand.get("id") or "")
    name = str(brand.get("name") or brand_id)
    last = _parse_at(cfg.get("last_poll_completed_at"))
    if last is None:
        return [_issue(
            "geo", brand, "never_swept", "medium",
            f"{name} has never completed an AI answer sweep",
            "No full sweep has finished yet, so the visibility numbers are partial or "
            "missing. Run a poll from the overview, or wait for the scheduled sweep.",
            fix=_fix("Open GEO overview", "geo", brand_id, "overview"),
        )]
    interval = _interval_days(cfg)
    age = _days_between(last, now)
    if age <= STALE_INTERVALS * interval:
        return []
    cause = (
        "Auto-poll is switched off for this brand."
        if not cfg.get("auto_poll", True)
        else "The scheduled poll is not completing — it may be failing or running out of time."
    )
    return [_issue(
        "geo", brand, "sweep_stale", "medium",
        f"AI answers for {name} are {_day_word(age)} old",
        f"The last full sweep finished on {last.date().isoformat()}; one is expected every "
        f"{_day_word(interval)}. {cause}",
        fix=_fix("Open GEO overview", "geo", brand_id, "overview"),
        since=cfg.get("last_poll_completed_at"),
    )]


def _plan_issues(brand: dict, plan: dict | None, now: dt.datetime) -> list[Issue]:
    current = (plan or {}).get("current") or {}
    generated = _parse_at(current.get("generated_at"))
    if not current or generated is None:
        return []
    actions = [
        action
        for wave in current.get("waves") or current.get("pillars") or []
        for action in (wave.get("actions") or [])
        if isinstance(action, dict)
    ]
    if not actions or any(a.get("status") == "done" for a in actions):
        return []
    age = _days_between(generated, now)
    if age < PLAN_UNTOUCHED_DAYS:
        return []
    brand_id = str(brand.get("id") or "")
    name = str(brand.get("name") or brand_id)
    return [_issue(
        "geo", brand, "plan_untouched", "low",
        f"The GEO plan for {name} has had nothing completed in {_day_word(age)}",
        f"The plan was written on {generated.date().isoformat()} and none of its "
        f"{len(actions)} actions is marked done. Work it, or regenerate it so it rests "
        "on this month's numbers.",
        fix=_fix("Open the plan", "geo", brand_id, "plan"),
        since=current.get("generated_at"),
    )]


def issues_from_geo(
    brand: dict,
    cfg: dict | None,
    engine_status: dict | None,
    last_run: dict | None,
    *,
    plan: dict | None = None,
    now: dt.datetime | None = None,
) -> list[Issue]:
    """What the brand's GEO signals say is wrong.

    ``cfg`` is the ``geo-config-{brand}`` document; ``None`` means it could not
    be read, and only the rules that do not need it run — the caller records
    the unreadable config as its own issue. ``last_run`` is the newest run-log
    entry, ``plan`` the strategy document; either may be ``None``.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[Issue] = []
    out.extend(_engine_off_issues(brand, cfg, engine_status or {}))
    out.extend(_engine_failed_issues(brand, cfg, last_run))
    if cfg is not None:
        out.extend(_sweep_issues(brand, cfg, now))
    out.extend(_plan_issues(brand, plan, now))
    return out


# -------------------------------------------------------------- assembly ----

def build_issues(issues: Iterable[Issue]) -> dict:
    """``{"issues": [...], "counts": {...}}`` — high first, then by brand, one
    entry per id."""
    unique: dict[str, Issue] = {}
    for issue in issues:
        unique.setdefault(issue["id"], issue)
    ordered = sorted(
        unique.values(),
        key=lambda i: (_SEVERITY_RANK.get(i["severity"], len(SEVERITIES)),
                       i["brand"].lower(), i["area"], i["title"].lower()),
    )
    counts = {name: 0 for name in SEVERITIES}
    for issue in ordered:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    return {"issues": ordered, "counts": counts}
