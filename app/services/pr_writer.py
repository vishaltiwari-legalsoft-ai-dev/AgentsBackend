"""PR Writer (a13) service: roles, the step machine, and the wire view.

The router (``app/routers/pr_writer.py``) validates the body and calls one
function here per route. This module:

* loads state from the PR store in ``firestore_repo`` (brand cards, runs,
  versions, events, sign-offs — senior-database-engineer's section — plus the
  router's ``flow`` field and step leases);
* decides who may do what (owner / comms lead / brand owner / legal /
  fallback admin / participant — contract §2);
* runs the five steps through ``pr_writer_agent.pipeline`` with a lease, a
  270 s deadline and an honest error per failure;
* turns the stored run into the ``PrRun`` the console renders (contract §4).

Every refusal is a :class:`PrError` carrying the contract's status, stable
``code`` and a plain message; the router turns it into ``{"detail": {...}}``.
Internals (store messages, provider errors) go to the log, never the client.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

from app.config import settings
from app.security import is_geo_only
from app.services import firestore_repo as repo
from pr_writer_agent import cards, export, pipeline
from pr_writer_agent import content_types as cts
from pr_writer_agent import evidence as ev
from seo_geo_agent import insights

logger = logging.getLogger("agentos.pr_writer")

ROLE_ROWS = ("comms_lead", "brand_owner", "legal")
STORE_ROLE = {"comms_lead": "editorial", "brand_owner": "brand", "legal": "compliance"}
ROW_LABEL = {"comms_lead": "Comms lead", "brand_owner": "Brand owner", "legal": "Legal"}
RUN_ID_PREFIX = "pr-"
NEWS_CAP = 600
FACTS_CAP = 8000
NOTE_CAP = 2000
COMMENT_CAP = 1000
PARTICIPANTS_PER_CALL = 20
REVISE_LEASE = "revise"  # flow.steps key for the per-run edit lease; not one of the visible steps
LIST_PAGES = 4
_NAME_LOOKUP_TIMEOUT_S = 10.0

# Tests replace these seams; production uses the real search, fetch and model.
SEARCH: Callable[[str], dict] | None = None
FETCH: Callable[[str], Any] | None = None
INVOKE_BASE: Callable[..., Any] | None = None


class PrError(Exception):
    def __init__(self, http_status: int, code: str, message: str, *, headers: dict | None = None, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message = http_status, code, message
        self.extra, self.headers = extra, headers

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _plain(text: Any) -> str:
    """Human-facing text without the internal evidence ids (``[br-1]``,
    ``[ev-3]``). The ids drive the fact-check; a reader never sees them."""
    return ev.strip_cites(str(text or "")).strip()


def _plain_deep(value: Any) -> Any:
    if isinstance(value, str):
        return _plain(value)
    if isinstance(value, list):
        return [_plain_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain_deep(v) for k, v in value.items()}
    return value


def wire_id(run_id: str) -> str:
    return RUN_ID_PREFIX + run_id


def store_id(wire: str) -> str:
    if not isinstance(wire, str) or not wire.startswith(RUN_ID_PREFIX):
        raise PrError(404, "run_not_found", "This draft doesn't exist or isn't shared with you.")
    return wire[len(RUN_ID_PREFIX):]


def _uid(user: dict) -> str:
    return str(user.get("id") or "")


NOT_FOUND = "This draft doesn't exist or isn't shared with you."


def _store(fn: Callable[[], Any], *, what: str = "this draft") -> Any:
    """Call the store, mapping its exceptions onto the contract."""
    try:
        return fn()
    except PrError:
        raise
    except repo.PrNotFound as exc:
        raise PrError(404, "run_not_found", NOT_FOUND) from exc
    except repo.PrSignoffBlocked as exc:
        # The store's own wording names internal version numbers and role
        # keys; callers that can say it in the screen's words (approve) do.
        raise PrError(409, "approval_blocked", "This draft can't be marked approved yet.", blockers=[]) from exc
    except repo.PrStaleVersion as exc:
        raise PrError(409, "stale_version", "Someone changed this draft after you opened it. "
                      "Reload to see the current version.") from exc
    except repo.PrRunLocked as exc:
        raise PrError(409, "run_locked", "This draft is approved; its approved version can't be changed.") from exc
    except repo.PrCapReached as exc:
        raise PrError(413, "cap_reached", f"This draft has reached a size limit: {exc}") from exc
    except repo.PrConflict as exc:
        if "escalated" in str(exc):
            raise PrError(409, "run_paused", "This draft is paused for comms and legal.") from exc
        raise PrError(409, "conflict", "That change conflicts with the draft's current state. Reload and try again.") from exc
    except repo.PrActorRefused as exc:
        raise PrError(403, "not_a_person", "This action needs a signed-in person.") from exc
    except ValueError as exc:
        logger.warning("pr store refused a value: %s", exc)
        raise PrError(422, "invalid", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Firestore / network
        logger.exception("pr store failure on %s", what)
        raise PrError(502, "store_failed", "PR Writer could not read or save this draft. Nothing was changed.") from exc


# --------------------------------------------------------------------------- #
# Brands, members, names
# --------------------------------------------------------------------------- #

def _brands() -> list[dict]:
    try:
        return [b for b in insights.list_brands() if b.get("enabled", True)]
    except Exception as exc:  # noqa: BLE001
        logger.exception("pr: could not read the brand registry")
        raise PrError(502, "store_failed", "PR Writer could not read the brand list.") from exc


def brand(brand_id: str) -> dict:
    for b in _brands():
        if b.get("id") == brand_id:
            return b
    raise PrError(404, "brand_not_found", "That brand isn't set up. Add it in the SEO Analyst first.")


def _allowed(email: str) -> bool:
    from app.routers.auth import is_allowed_email

    try:
        return bool(email) and is_allowed_email(email) and not is_geo_only(email)
    except Exception:  # noqa: BLE001 — cannot evaluate: not a member
        return False


def members() -> list[dict]:
    users = _store(lambda: repo.list_users(timeout=_NAME_LOOKUP_TIMEOUT_S), what="the member list")
    out = [{"id": u["id"], "name": str(u.get("name") or u.get("email") or ""), "email": str(u.get("email") or "")}
           for u in users if _allowed(str(u.get("email") or ""))]
    return sorted(out, key=lambda m: m["name"].lower())


def _exact_admin_emails() -> set[str]:
    """Addresses listed EXACTLY in ADMIN_EMAILS or CREATOR_EMAILS. ``@domain``
    entries are never expanded: in production ADMIN_EMAILS holds a whole
    domain, so ``is_admin`` alone covers every mailbox at that company."""
    return {e for e in settings.admin_email_set | settings.creator_email_set if not e.startswith("@")}


def _exact_admin_ids() -> list[str]:
    """Rule 5 fallback reviewers: members whose address is an exact admin entry."""
    exact = _exact_admin_emails()
    return [m["id"] for m in members() if m["email"].lower() in exact]


def is_exact_admin(user: dict) -> bool:
    return str(user.get("email") or "").strip().lower() in _exact_admin_emails()


def _names(ids) -> dict[str, dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    try:
        users = repo.get_users_by_ids(ids)
    except Exception:  # noqa: BLE001 — names are labels; degrade, don't fail
        logger.warning("pr: could not read user names", exc_info=True)
        users = {}
    return {i: {"id": i, "name": str((users.get(i) or {}).get("name") or (users.get(i) or {}).get("email")
                                     or "Former member")} for i in ids}


def _ref(names: dict, uid: str | None) -> dict | None:
    return names.get(uid) if uid else None


# --------------------------------------------------------------------------- #
# Roles on a run
# --------------------------------------------------------------------------- #

class Roles:
    def __init__(self, run: dict, uid: str):
        flow = run.get("flow") or {}
        self.uid = uid
        self.owner = run.get("user_id")
        self.reviewers = {r: (flow.get("reviewers") or {}).get(r) for r in ROLE_ROWS}
        self.fallback = list(flow.get("fallback_admins") or [])

    def of(self, uid: str | None = None) -> list[str]:
        uid = uid or self.uid
        out = ["owner"] if uid == self.owner else []
        out += [r for r in ("comms_lead", "legal", "brand_owner") if self.reviewers.get(r) == uid]
        if uid in self.fallback:
            out.append("fallback_admin")
        return out or ["participant"]

    def is_owner(self) -> bool:
        return self.uid == self.owner

    def can_act_row(self, row: str) -> bool:
        assignee = self.reviewers.get(row)
        return self.uid == assignee if assignee else self.uid in self.fallback

    def comms_power(self) -> bool:
        return self.can_act_row("comms_lead")

    def reviewer(self) -> bool:
        return self.is_owner() or self.uid in self.reviewers.values() or self.uid in self.fallback

    def clearer(self) -> tuple[str, str | None]:
        if self.reviewers.get("legal"):
            return "legal", self.reviewers["legal"]
        if self.reviewers.get("comms_lead"):
            return "comms_lead", self.reviewers["comms_lead"]
        return "fallback_admin", None

    def is_clearer(self) -> bool:
        role, uid = self.clearer()
        return self.uid == uid if uid else self.uid in self.fallback

    def comms_or_legal(self) -> bool:
        return self.uid in (self.reviewers.get("comms_lead"), self.reviewers.get("legal")) or self.uid in self.fallback


# --------------------------------------------------------------------------- #
# Reading state
# --------------------------------------------------------------------------- #

def _load(run_wire_id: str, user: dict) -> dict:
    rid = store_id(run_wire_id)
    run = _store(lambda: repo.get_pr_run(rid, _uid(user)))
    if run is None:
        raise PrError(404, "run_not_found", NOT_FOUND)
    return run


def _flow(run: dict) -> dict:
    return run.get("flow") or {}


def _escalation_open(run: dict) -> bool:
    esc = run.get("escalation") or {}
    return bool(esc.get("flagged")) and not esc.get("cleared_by")


def _terminal(run: dict) -> dict | None:
    return _flow(run).get("terminal")


def _intake(run: dict) -> dict:
    flow = _flow(run)
    base = pipeline.brief_to_intake(run.get("brief") or {})
    if flow.get("pending_intake"):
        base = flow["pending_intake"]["intake"]
    clarify = flow.get("clarify")
    chosen = flow.get("content_type")
    if clarify and clarify.get("state") in ("answered", "skipped"):
        base = pipeline.apply_clarify(base, clarify, clarify.get("content_type"))
    elif chosen:
        base = pipeline.apply_clarify(base, None, chosen)
    return pipeline.with_content_type(base)


def _intake_result(run: dict) -> dict:
    meta = _flow(run).get("intake_meta") or {}
    return {"intake": _intake(run), "notes": meta.get("notes", []),
            "injection_notes": meta.get("injection_notes", []), "missing": meta.get("missing", [])}


def _angle(run: dict) -> dict:
    try:
        return json.loads(run.get("angle") or "{}")
    except (TypeError, ValueError):
        return {}


def _card_doc(brand_id: str) -> dict | None:
    stored = _store(lambda: repo.get_brand_card(brand_id), what="the brand card")
    return (stored or {}).get("card") if stored else None


def _lease_alive(entry: dict) -> bool:
    return entry.get("state") == "running" and str(entry.get("lease_until") or "") > _now()


def _step_states(run: dict) -> dict[str, dict]:
    flow = _flow(run)
    table = flow.get("steps") or {}
    store_steps = run.get("steps") or {}
    out: dict[str, dict] = {}
    for sid in pipeline.STEP_IDS:
        entry = dict(table.get(sid) or {})
        state = entry.get("state") or "pending"
        if state == "running" and not _lease_alive(entry):
            state = "failed" if entry.get("error") else "pending"
        if sid == "intake" and ("intake" in store_steps or flow.get("pending_intake")):
            state = "done"
        if sid == "screen" and "intake" in store_steps:
            state = "done"
        if sid == "research" and {"research", "angle"} <= set(store_steps) and state != "running":
            state = "done"
        if sid == "draft" and "qa" in store_steps and state != "running":
            state = "done"
        out[sid] = {"id": sid, "state": state, "started_at": entry.get("started_at"),
                    "finished_at": entry.get("finished_at"), "result": entry.get("result"),
                    "error": entry.get("error") if state == "failed" else None}
    if out["intake"]["state"] == "done" and out["intake"]["result"] is None:
        ct = (_flow(run).get("content_type")) or None
        out["intake"]["result"] = {"content_type": ct,
                                   "content_type_label": cts.CONTENT_TYPES[ct].label if ct in cts.CONTENT_TYPES else None,
                                   "audience": _guess_audience(run)}
    if out["screen"]["state"] == "done" and out["screen"]["result"] is None:
        esc = run.get("escalation") or {}
        out["screen"]["result"] = {"escalated": bool(esc.get("flagged")),
                                   "category": (_flow(run).get("escalation_info") or {}).get("category")}
    return out


def _guess_audience(run: dict) -> str | None:
    value = (_intake(run).get("audience") or {}).get("value")
    return str(value) if value else None


def _next_step(run: dict, states: dict[str, dict]) -> str | None:
    if _terminal(run):
        return None
    for sid in pipeline.STEP_IDS:
        state = states[sid]["state"]
        if state == "done" or (sid == "package" and state == "partial"):
            continue  # a partial package is usable; its retry is a person's click
        if sid == "notes" and state == "failed":
            return None  # reviewer notes failing does not hold the draft back
        if sid in ("research", "draft", "package", "notes"):
            if _escalation_open(run) or ((_flow(run).get("clarify") or {}).get("state") == "open"):
                return None
        return sid
    return None


# --------------------------------------------------------------------------- #
# The wire view
# --------------------------------------------------------------------------- #

def _label_of(run: dict, version: int) -> str:
    for v in _flow(run).get("versions") or []:
        if v.get("version") == version:
            return v["label"]
    return f"0.{version}"


def _version_meta(run: dict, version: int) -> dict:
    for v in _flow(run).get("versions") or []:
        if v.get("version") == version:
            return v
    return {"version": version, "label": _label_of(run, version), "kind": "revision", "what_changed": "",
            "quote_resets": [], "locked": False, "locked_at": None, "created_by": None, "created_at": ""}


def _current_locked(run: dict) -> bool:
    latest = int(run.get("latest_version") or 0)
    return bool(latest) and bool(_version_meta(run, latest).get("locked"))


def _quote_state(run: dict) -> dict[str, dict]:
    return {q["quote_id"]: q for q in run.get("quotes") or []}


def _wire_claims(rows: list[dict], evidence: list[dict], brand_domain: str, names: dict) -> list[dict]:
    from pr_writer_agent import research as pr_research

    idx = {e.get("id"): e for e in evidence}
    out = []
    for row in pipeline.assign_ids(rows):
        items = [idx[i] for i in row.get("evidence_ids") or [] if i in idx]
        first = items[0] if items else {}
        origin = first.get("origin")
        kind = {"brief": "brief", "brand_card": "brand_card", "reviewer": "reviewer"}.get(origin) or (
            pr_research.source_kind(first.get("url", ""), brand_domain) if origin == "web" else "none")
        status = str(row.get("status") or "").strip().lower().replace(" ", "_")
        if status == "verified" and not row.get("verified_by"):
            status = "sourced"
        if status not in ("sourced", "verified", "needs_confirmation", "removed"):
            status = "needs_confirmation"
        verified = None
        if row.get("verified_by"):
            verified = {"by": names.get(row["verified_by"]) or {"id": row["verified_by"],
                                                                "name": row.get("verified_by_name") or ""},
                        "note": row.get("verified_note") or "", "at": row.get("verified_at")}
        out.append({
            "id": pipeline.claim_id(row), "n": int(row.get("n") or len(out) + 1),
            "text": str(row.get("claim") or ""), "block_id": row.get("block_id") or "",
            "placeholder": pipeline.is_placeholder(row),
            "source": {"kind": kind, "label": str(row.get("source") or ""), "url": first.get("url") or None,
                       "date": (row.get("source_date") or None)},
            "date_checked": row.get("date_checked"), "status": status,
            "status_reason": row.get("status_reason") or "",
            "owner_to_confirm": row.get("owner") or None, "verified": verified,
        })
    return out


def _doc_draft(run: dict, doc: dict | None) -> tuple[dict, list[dict]]:
    """A stored version's working draft and evidence (deep copies)."""
    doc = doc or {}
    draft, evidence = pipeline.from_store((doc.get("package") or {}).get("draft") or {}, doc.get("qa"),
                                          run.get("evidence") or [])
    return copy.deepcopy(draft), copy.deepcopy(evidence)


def _wire_version(run: dict, doc: dict, names: dict) -> dict:
    draft, evidence = _doc_draft(run, doc)
    qstate = _quote_state(run)
    current = {q["quote_id"]: q for q in pipeline.store_quotes(draft)}
    approval_by_id: dict[str, str] = {}
    for qid, q in current.items():
        stored = qstate.get(qid) or {}
        same = stored.get("text_hash") == repo.quote_text_hash(q["text"])
        approval_by_id[qid] = "approved" if same and stored.get("status") == "approved" else "pending"
    blocks = pipeline.wire_blocks(draft, approval_by_id)
    rows = doc.get("fact_checks") or []
    claims = _wire_claims(rows, evidence, _flow(run).get("brand_domain") or "",
                          names)
    for b in blocks:
        b["claim_ids"] = [c["id"] for c in claims if c["block_id"] == b["id"] and c["status"] != "removed"]
    markers = pipeline.wire_markers(blocks, claims)
    resets = {}
    for v in _flow(run).get("versions") or []:
        if v.get("version", 0) <= doc.get("version", 0):
            for r in v.get("quote_resets") or []:
                resets[r["quote_id"]] = v["label"]
    quotes = []
    for qid, q in current.items():
        stored = qstate.get(qid) or {}
        approved = approval_by_id[qid] == "approved"
        quotes.append({
            "id": qid, "block_id": qid, "speaker_name": q["speaker"], "speaker_title": q["title"],
            "organization": q["organization"] or None, "text": q["text"],
            "text_hash": repo.quote_text_hash(q["text"]),
            "approval": {"state": "approved" if approved else "pending",
                         "recorded_by": _ref(names, stored.get("attested_by")) if approved else None,
                         "evidence": (stored.get("note") or None) if approved else None,
                         "at": stored.get("attested_at") if approved else None},
            "reset_in_version": None if approved else resets.get(qid),
        })
    headline = _plain(draft.get("headline", ""))
    heads = [{"text": headline, "chars": len(headline), "recommended": True}]
    for o in draft.get("headline_options") or []:
        text = _plain(o.get("text"))
        if text and text != headline and len(heads) < 3:
            heads.append({"text": text, "chars": len(text), "recommended": False})
    subhead = _plain(draft.get("subhead", ""))
    subs = [_plain(s.get("text", "")) for s in draft.get("subhead_options") or []]
    alternate = next((s for s in subs if s and s != subhead), "")
    meta = _version_meta(run, int(doc.get("version") or 0))
    return {
        **_summary_of(meta, names),
        "headlines": heads,
        "subheadline": {"recommended": subhead, "alternate": alternate},
        "news_angle": _plain(_angle(run).get("angle_paragraph", "")),
        "blocks": blocks, "claims": claims, "quotes": quotes, "markers": markers,
    }


def _summary_of(meta: dict, names: dict) -> dict:
    by = meta.get("created_by")
    return {
        "version": meta["label"], "created_at": meta.get("created_at") or "",
        "created_by": None if (not by or by == repo.PR_AGENT_ACTOR) else (names.get(by) or {"id": by, "name": ""}),
        "kind": meta.get("kind") or "revision", "what_changed": _plain(meta.get("what_changed")),
        "quote_resets": [{"quote_id": r["quote_id"], "speaker_name": r.get("speaker_name", "")}
                         for r in meta.get("quote_resets") or []],
        "locked": bool(meta.get("locked")), "locked_at": meta.get("locked_at"),
    }


def _status(run: dict, states: dict, next_step: str | None) -> str:
    term = _terminal(run)
    if term:
        return "withdrawn" if term.get("kind") == "withdrawn" else "closed"
    if run.get("status") == "approved":
        return "approved"
    if _escalation_open(run):
        return "escalated"
    if (_flow(run).get("clarify") or {}).get("state") == "open" and states["screen"]["state"] == "done":
        return "needs_details"
    if next_step and states[next_step]["state"] == "failed":
        return "step_failed"
    if not run.get("latest_version") or states["package"]["state"] not in ("done", "partial") \
            or states["notes"]["state"] in ("pending", "running"):
        return "writing"
    if _current_locked(run):
        return "changes_asked" if run.get("status") == "changes_requested" else "in_review"
    return "draft"


def _signoffs(run: dict, roles: Roles, names: dict, open_verify: int, missing: int) -> dict:
    latest = int(run.get("latest_version") or 0)
    locked = _current_locked(run)
    stored = run.get("signoffs") or {}
    rows = []
    signed = 0
    for row in ROLE_ROWS:
        entry = stored.get(STORE_ROLE[row]) or {}
        live = locked and entry.get("version") == latest
        state = ("signed_off" if entry.get("decision") == "approve" else "changes_asked") if live else "waiting"
        signed += state == "signed_off"
        assignee = roles.reviewers.get(row)
        rows.append({
            "id": row, "kind": "role", "label": ROW_LABEL[row], "assignee": _ref(names, assignee),
            "fallback": not assignee and bool(roles.fallback), "unassigned": not assignee and not roles.fallback,
            "state": state, "version": _label_of(run, latest) if live else None,
            "by": _ref(names, entry.get("actor_id")) if live else None, "at": entry.get("at") if live else None,
            "note": (entry.get("note") or None) if live else None,
            "viewer_can_act": locked and roles.can_act_row(row) and run.get("status") != "approved",
        })
    pending = 0
    for q in run.get("quotes") or []:
        approved = q.get("status") == "approved"
        signed += approved
        pending += not approved
        rows.append({
            "id": f"quote:{q['quote_id']}", "kind": "quote", "label": f"Quote: {q.get('speaker') or ''}",
            "assignee": _ref(names, roles.reviewers.get("comms_lead")),
            "fallback": not roles.reviewers.get("comms_lead") and bool(roles.fallback),
            "unassigned": not roles.reviewers.get("comms_lead") and not roles.fallback,
            "state": "approved" if approved else "pending", "version": None,
            "by": _ref(names, q.get("attested_by")) if approved else None,
            "at": q.get("attested_at") if approved else None, "note": (q.get("note") or None) if approved else None,
            "viewer_can_act": (not approved) and roles.comms_power() and run.get("status") != "approved",
        })
    blockers = {"verify": open_verify, "missing": missing, "pending_quotes": pending}
    all_signed = all(r["state"] == "signed_off" for r in rows if r["kind"] == "role")
    return {
        "version": _label_of(run, latest) if locked else None, "rows": rows,
        "summary": {"signed": signed, "total": len(rows)}, "blockers": blockers,
        "approved": run.get("status") == "approved",
        "can_approve": locked and all_signed and not any(blockers.values()) and roles.comms_power()
        and run.get("status") != "approved",
    }


def _package_view(run: dict) -> dict | None:
    flow = _flow(run)
    sections = flow.get("sections") or {}
    if not sections and not flow.get("package_based_on"):
        return None
    state = {s: {"state": (sections.get(s) or {}).get("state") or "pending",
                 "error": (sections.get(s) or {}).get("error")} for s in pipeline.SECTIONS}
    notes_body = (sections.get("reviewer_notes") or {}).get("body")
    notes = None
    if notes_body:
        notes = ([{"kind": "assumption", "text": _plain(t)} for t in notes_body.get("assumptions", [])]
                 + [{"kind": "open_question", "text": _plain(t)} for t in notes_body.get("open_questions", [])]
                 + [{"kind": "risk", "text": _plain(t)} for t in notes_body.get("risks", [])]
                 + [{"kind": "checklist", "text": _plain(t)} for t in notes_body.get("failed_checks", [])])
    return {
        "based_on_version": _label_of(run, int(flow.get("package_based_on") or 1)),
        "sections": state,
        "seo": _plain_deep((sections.get("seo") or {}).get("body")),
        "distribution": _plain_deep((sections.get("distribution") or {}).get("body")),
        "snippets": _plain_deep((sections.get("snippets") or {}).get("body")),
        "reviewer_notes": notes,
    }


def view(run: dict, user: dict, *, version_doc: dict | None = None, load_version: bool = True) -> dict:
    """The full ``PrRun`` for ``user`` (contract §4.2)."""
    uid = _uid(user)
    flow = _flow(run)
    roles = Roles(run, uid)
    latest = int(run.get("latest_version") or 0)
    if latest and version_doc is None and load_version:
        version_doc = _store(lambda: repo.get_pr_version(run["id"], latest, user_id=uid))
    states = _step_states(run)
    nxt = _next_step(run, states)
    status = _status(run, states, nxt)
    added = {a["user_id"]: a for a in flow.get("added") or []}
    esc_notes = (flow.get("escalation_info") or {}).get("notes") or []
    ids = list(run.get("participants") or []) + [a.get("added_by") for a in added.values()]
    ids += [n.get("by") for n in esc_notes] + [(run.get("escalation") or {}).get("cleared_by")]
    ids += [q.get("attested_by") for q in run.get("quotes") or []]
    ids += [(s or {}).get("actor_id") for s in (run.get("signoffs") or {}).values()]
    ids += [v.get("created_by") for v in flow.get("versions") or []]
    for row in (version_doc or {}).get("fact_checks") or []:
        ids.append(row.get("verified_by"))
    names = _names([i for i in ids if i and i != repo.PR_AGENT_ACTOR])
    draft = _wire_version(run, version_doc, names) if version_doc else None
    open_verify = int(run.get("open_verify_count") or 0)
    missing = pipeline.count_missing(draft["blocks"]) if draft else 0
    pending_quotes = sum(1 for q in run.get("quotes") or [] if q.get("status") != "approved")
    signoffs = _signoffs(run, roles, names, open_verify, missing)
    ct = flow.get("content_type")
    term = _terminal(run)
    terminal = status in ("withdrawn", "closed") or status == "approved"
    locked = _current_locked(run)
    needs_you = None
    if status == "needs_details" and roles.is_owner():
        needs_you = "details"
    elif status == "escalated" and roles.is_clearer():
        needs_you = "clearance"
    elif status in ("in_review", "changes_asked") and any(
            r["viewer_can_act"] and r["state"] in ("waiting", "pending") for r in signoffs["rows"]):
        needs_you = "sign_off"
    elif status in ("in_review", "changes_asked") and signoffs["can_approve"]:
        needs_you = "sign_off"
    can = {
        "run_steps": roles.is_owner() and nxt is not None,
        "answer_clarify": roles.is_owner() and status == "needs_details",
        "add_escalation_note": roles.is_owner() and status == "escalated",
        "withdraw": roles.is_owner() and status == "escalated",
        "clear_escalation": status == "escalated" and roles.is_clearer(),
        "keep_paused": status == "escalated" and roles.comms_or_legal(),
        "close_escalation": status == "escalated" and roles.comms_or_legal(),
        "revise": bool(latest) and not terminal and not _escalation_open(run) and roles.reviewer(),
        "send_for_review": bool(latest) and not terminal and not locked
        and states["package"]["state"] in ("done", "partial") and (roles.is_owner() or roles.comms_power()),
        "record_quote_approval": bool(latest) and not terminal and roles.comms_power(),
        "approve": signoffs["can_approve"],
        "add_people": not term and (roles.is_owner() or roles.comms_power()),
        "export": bool(latest),
    }
    esc = run.get("escalation") or {}
    info = flow.get("escalation_info") or {}
    escalation = None
    if esc.get("flagged") or info:
        notes = [{"kind": n["kind"], "by": names.get(n["by"]) or {"id": n["by"], "name": ""}, "at": n["at"],
                  "text": n["text"]} for n in esc_notes]
        if esc.get("cleared_by"):
            notes.append({"kind": "clear", "by": names.get(esc["cleared_by"]) or {"id": esc["cleared_by"], "name": ""},
                          "at": esc.get("cleared_at"), "text": esc.get("note") or ""})
            notes.sort(key=lambda n: str(n["at"] or ""))
        state = "paused"
        if term:
            state = "withdrawn" if term.get("kind") == "withdrawn" else "closed"
        elif esc.get("cleared_by"):
            state = "cleared"
        crole, cuid = roles.clearer()
        escalation = {
            "state": state, "category": info.get("category") or "", "category_label": info.get("category_label")
            or pipeline.CATEGORY_LABELS[""], "excerpt": info.get("excerpt") or "", "reason": info.get("reason") or "",
            "raised_at": esc.get("flagged_at"), "clearer": {"role": crole, "user": _ref(names, cuid)},
            "notes": notes,
        }
    participants = []
    for pid in run.get("participants") or []:
        a = added.get(pid) or {}
        participants.append({"user": names.get(pid) or {"id": pid, "name": ""}, "roles": roles.of(pid),
                             "added_by": _ref(names, a.get("added_by")), "added_at": a.get("added_at")
                             or run.get("created_at")})
    versions = [_summary_of(v, names) for v in flow.get("versions") or []]
    viewer_roles = roles.of()
    return {
        "id": wire_id(run["id"]), "brand_id": run.get("brand_id"), "brand_name": flow.get("brand_name") or "",
        "brand_domain": flow.get("brand_domain") or "", "has_card": bool(flow.get("has_card")),
        "news": flow.get("news") or run.get("title") or "", "facts": flow.get("facts") or "",
        "owner": names.get(run.get("user_id")) or {"id": run.get("user_id"), "name": ""},
        "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
        "status": status, "next_step": nxt, "steps": [states[s] for s in pipeline.STEP_IDS],
        "content_type": ct, "content_type_label": cts.CONTENT_TYPES[ct].label if ct in cts.CONTENT_TYPES else None,
        "audience": _guess_audience(run),
        "clarify": _clarify_view(flow.get("clarify")),
        "escalation": escalation,
        "reviewers": {r: _ref(names, roles.reviewers.get(r)) for r in ROLE_ROWS},
        "fallback_admins": [names.get(i) or {"id": i, "name": ""} for i in roles.fallback],
        "participants": participants,
        "viewer_role": viewer_roles[0], "viewer_roles": viewer_roles, "needs_you": needs_you, "can": can,
        "current_version": _label_of(run, latest) if latest else None,
        "versions": versions, "draft": draft,
        "markers": {"verify": open_verify, "missing": missing, "pending_quotes": pending_quotes},
        "signoffs": signoffs, "package": _package_view(run),
    }


def _clarify_view(clarify: dict | None) -> dict | None:
    if not clarify:
        return None
    ct = clarify.get("content_type") or pipeline.DEFAULT_CONTENT_TYPE
    return {
        "state": clarify.get("state"),
        "fields": [{"key": f["key"], "label": f["label"], "guess": f.get("guess"), "answer": f.get("answer")}
                   for f in clarify.get("fields") or []],
        "content_type": {"value": ct, "guessed": bool(clarify.get("content_type_guessed")),
                         "options": [{"value": c.id, "label": c.label} for c in cts.CONTENT_TYPES.values()]},
        "answered_at": clarify.get("answered_at"),
    }


def summary(run: dict, user: dict) -> dict:
    full = view(run, user, load_version=False)
    flow = _flow(run)
    return {
        "id": full["id"], "brand_id": full["brand_id"], "brand_name": full["brand_name"], "news": full["news"],
        "headline": (flow.get("current") or {}).get("headline"),
        "content_type_label": full["content_type_label"], "owner": full["owner"],
        "is_owner": run.get("user_id") == _uid(user), "created_at": full["created_at"],
        "updated_at": full["updated_at"], "status": full["status"], "next_step": full["next_step"],
        "running_step": next((s["id"] for s in full["steps"] if s["state"] == "running"), None),
        "failed_step": next((s["id"] for s in full["steps"] if s["state"] == "failed"), None),
        "current_version": full["current_version"],
        "signoff_summary": full["signoffs"]["summary"] if _current_locked(run) else None,
        "needs_you": full["needs_you"], "viewer_role": full["viewer_role"],
    }


def get_run(run_wire_id: str, user: dict) -> dict:
    return view(_load(run_wire_id, user), user)


def get_version(run_wire_id: str, label: str, user: dict) -> dict:
    run = _load(run_wire_id, user)
    number = next((v["version"] for v in _flow(run).get("versions") or [] if v.get("label") == label), None)
    if number is None:
        raise PrError(404, "version_not_found", f"There is no version {label} of this draft.")
    doc = _store(lambda: repo.get_pr_version(run["id"], int(number), user_id=_uid(user)))
    if doc is None:
        raise PrError(404, "version_not_found", f"There is no version {label} of this draft.")
    ids = [r.get("verified_by") for r in doc.get("fact_checks") or []]
    ids += [q.get("attested_by") for q in run.get("quotes") or []] + [doc.get("created_by")]
    return _wire_version(run, doc, _names([i for i in ids if i and i != repo.PR_AGENT_ACTOR]))


def list_runs(user: dict, brand_id: str | None) -> dict:
    if brand_id:
        brand(brand_id)
    uid = _uid(user)
    ids: list[str] = []
    cursor = None
    for _ in range(LIST_PAGES):
        page = _store(lambda: repo.list_pr_runs(uid, limit=repo.PR_LIST_PAGE_MAX, cursor=cursor))
        if page is None:
            raise PrError(502, "store_failed", "PR Writer could not read your drafts.")
        ids += [r["id"] for r in page["runs"] if not brand_id or r.get("brand_id") == brand_id]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    runs = _store(lambda: repo.get_pr_runs_by_ids(ids, user_id=uid)) if ids else []
    return {"runs": [summary(r, user) for r in runs]}


# --------------------------------------------------------------------------- #
# Brand cards and the brand list
# --------------------------------------------------------------------------- #

def list_brands() -> dict:
    out = []
    for b in _brands():
        card = _card_doc(b["id"])
        done = cards.sections(card) if card else {}
        out.append({"id": b["id"], "name": b.get("name", b["id"]), "domain": b.get("domain", ""),
                    "has_card": card is not None, "card_sections_complete": sum(done.values()),
                    "card_sections_total": len(cards.SECTIONS)})
    return {"brands": out}


def _card_editable(user: dict, stored: dict | None) -> bool:
    if user.get("is_admin"):
        return True
    roles = ((stored or {}).get("card") or {}).get("reviewer_roles") or {}
    return bool(stored) and roles.get("comms_lead") == _uid(user)


def card_view(brand_id: str, user: dict, stored: dict | None = None, *, fresh: bool = True) -> dict:
    b = brand(brand_id)
    if fresh:
        stored = _store(lambda: repo.get_brand_card(brand_id), what="the brand card")
    card = (stored or {}).get("card")
    roles = (card or {}).get("reviewer_roles") or {}
    names = _names(list(roles.values()) + [(stored or {}).get("updated_by"),
                                           ((card or {}).get("boilerplate") or {}).get("changed_by")])
    wire = None
    if card:
        bp = card.get("boilerplate") or {}
        wire = {k: card.get(k) for k in ("identity", "messaging", "proof_points", "spokespeople", "product_names",
                                         "avoid", "media_contact", "press_kit", "keywords")}
        wire["reviewers"] = {r: _ref(names, roles.get(r)) for r in cards.ROLES}
        wire["boilerplate"] = {"text": bp.get("text", ""), "approved_by": bp.get("approved_by", ""),
                               "approved_on": bp.get("approved_on"), "changed_by": _ref(names, bp.get("changed_by")),
                               "changed_at": bp.get("changed_at")}
    return {
        "brand_id": brand_id, "brand_name": b.get("name", brand_id), "exists": card is not None,
        "revision": int((stored or {}).get("revision") or 0), "updated_at": (stored or {}).get("updated_at"),
        "updated_by": _ref(names, (stored or {}).get("updated_by")), "can_edit": _card_editable(user, stored),
        "can_edit_reviewers": _card_editable(user, stored) and is_exact_admin(user),
        "sections": cards.sections(card), "card": wire,
    }


def save_card(brand_id: str, user: dict, base_revision: int, card_in: Any, approval_in: Any) -> dict:
    brand(brand_id)
    stored = _store(lambda: repo.get_brand_card(brand_id), what="the brand card")
    if not _card_editable(user, stored):
        raise PrError(403, "cannot_edit_card", "Only this brand's comms lead or a workspace admin can edit its card."
                      if stored else "Only a workspace admin can start a brand card.")
    try:
        clean = cards.clean_input(card_in)
        approval = cards.clean_approval(approval_in)
    except cards.CardInvalid as exc:
        if exc.max_len is not None:
            raise PrError(422, "too_long", str(exc), field=exc.field, max=exc.max_len) from exc
        raise PrError(422, "invalid_card", str(exc), field=exc.field) from exc
    previous_roles = {r: (((stored or {}).get("card") or {}).get("reviewer_roles") or {}).get(r) or None
                      for r in cards.ROLES}
    if clean["reviewer_roles"] != previous_roles and not is_exact_admin(user):
        # Who reviews a brand's drafts decides who reads them and who signs
        # them off. A comms lead naming themselves legal, or any mailbox a
        # domain-wide ADMIN_EMAILS entry makes "admin", must not be able to.
        raise PrError(403, "cannot_assign_reviewers",
                      "Only an admin named individually in the workspace settings can change who reviews this "
                      "brand's drafts. Your other changes weren't saved.")
    reviewer_ids = [i for i in clean["reviewer_roles"].values() if i]
    if reviewer_ids:
        known = {m["id"] for m in members()}
        unknown = [i for i in reviewer_ids if i not in known]
        if unknown:
            raise PrError(422, "unknown_member", "A reviewer is not a member of this workspace.", user_ids=unknown)
    previous = (stored or {}).get("card")
    if cards.boilerplate_changed(clean, previous) and clean["boilerplate"]["text"] and approval is None:
        raise PrError(422, "boilerplate_approval_required",
                      "Say who approved the new boilerplate and when before saving it.")
    doc = cards.to_store(clean, previous, approval, actor_id=_uid(user), now=_now())
    try:
        saved = repo.save_brand_card(brand_id, doc, expected_revision=int(base_revision), actor_id=_uid(user))
    except repo.BrandCardConflict as exc:
        current = card_view(brand_id, user)
        who = (current.get("updated_by") or {}).get("name") or "Someone"
        raise PrError(409, "stale_card", f"{who} saved this card after you opened it. Your changes aren't saved yet.",
                      current=current) from exc
    except Exception as exc:  # noqa: BLE001
        _store(lambda: (_ for _ in ()).throw(exc), what="the brand card")
        raise
    return card_view(brand_id, user, saved | {"brand_id": brand_id}, fresh=False)


# --------------------------------------------------------------------------- #
# Creating a run: intake + screen (≤2 model calls), then the first writes
# --------------------------------------------------------------------------- #

def new_meter() -> pipeline.Meter:
    """A per-request model-spend meter; the router records it on the trail."""
    return pipeline.Meter()


def _invoke(deadline: pipeline.Deadline, meter: pipeline.Meter | None = None):
    return pipeline.deadline_invoke(deadline, INVOKE_BASE, meter)


def _fail(exc: BaseException, step: str) -> PrError:
    failure = pipeline.classify(exc, step=step)
    if failure is None:
        raise exc
    logger.warning("pr step %s failed: %s", step, exc, exc_info=True)
    return PrError(failure.status, failure.code, failure.message, step=step)


def _screen_patch(decision: dict) -> dict:
    if decision.get("escalate"):
        return {"escalation_reason": decision.get("reason") or decision.get("category") or "escalated"}
    return {"status": "in_progress"}


def _escalation_info(decision: dict) -> dict | None:
    if not decision.get("escalate"):
        return None
    return {"category": decision.get("category") or "", "category_label": decision.get("category_label") or "",
            "excerpt": decision.get("excerpt") or "", "reason": decision.get("reason") or "",
            "decided_by": decision.get("decided_by"), "notes": []}


def _small_decision(decision: dict) -> dict:
    return {k: decision.get(k) for k in ("escalate", "categories", "reason", "decided_by", "category")}


def create_run(user: dict, brand_id: str, news: str, facts: str, meter: pipeline.Meter | None = None) -> dict:
    uid = _uid(user)
    b = brand(brand_id)
    news, facts = (news or "").strip(), (facts or "").strip()
    if not news:
        raise PrError(422, "news_required", "Write the news in one sentence. Details can come later.")
    if len(news) > NEWS_CAP:
        raise PrError(422, "too_long", f"The news sentence is longer than {NEWS_CAP} characters.", field="news",
                      max=NEWS_CAP)
    if len(facts) > FACTS_CAP:
        raise PrError(422, "too_long", f"Facts are longer than {FACTS_CAP} characters.", field="facts", max=FACTS_CAP)
    stored_card = _store(lambda: repo.get_brand_card(brand_id), what="the brand card")
    card = (stored_card or {}).get("card")
    card_agent = cards.to_agent(b, card)

    deadline = pipeline.Deadline()
    invoke = _invoke(deadline, meter)
    intake_started = _now()
    try:
        intake_result = pipeline.run_intake(news, facts, b, known_brands=[b.get("name") or brand_id], invoke=invoke,
                                            card_agent=card_agent)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "intake") from exc
    intake_result.pop("usage", None)
    decision, screen_error = None, None
    intake_finished = screen_started = _now()
    try:
        decision = pipeline.run_screen(intake_result["intake"], facts, b, card_agent, invoke=invoke)
    except Exception as exc:  # noqa: BLE001
        screen_error = _fail(exc, "screen")

    roles = (card or {}).get("reviewer_roles") or {}
    reviewers = {r: roles.get(r) for r in ROLE_ROWS}
    fallback = _exact_admin_ids() if not all(reviewers.values()) else []
    fallback = [i for i in fallback if i != uid] if fallback else []
    ct = (intake_result["intake"].get("content_type") or {}).get("value")
    clarify = pipeline.clarify_card(intake_result)
    now = _now()
    run = _store(lambda: repo.create_pr_run(
        owner_id=uid, brand_id=brand_id, content_type=pipeline.content_type_of(intake_result["intake"]),
        title=news, brief=pipeline.intake_to_brief(intake_result["intake"]), participants=fallback))
    rid = run["id"]

    def init(_run: dict, flow: dict) -> dict:
        steps = {"intake": {"state": "done", "started_at": intake_started, "finished_at": intake_finished, "result": {
            "content_type": ct, "content_type_label": cts.CONTENT_TYPES[ct].label if ct in cts.CONTENT_TYPES else None,
            "audience": _guess(intake_result["intake"].get("audience"))}}}
        if decision is not None:
            steps["screen"] = {"state": "done", "started_at": screen_started, "finished_at": now,
                               "result": {"escalated": bool(decision.get("escalate")),
                                          "category": decision.get("category") or None}}
        else:
            steps["screen"] = {"state": "failed", "started_at": screen_started,
                               "error": {"code": screen_error.code, "message": screen_error.message, "at": _now(),
                                         "attempts": 1}}
        return {
            "news": news, "facts": facts, "brand_name": b.get("name", brand_id), "brand_domain": b.get("domain", ""),
            "has_card": card is not None, "reviewers": reviewers, "fallback_admins": fallback,
            "content_type": ct if ct in cts.CONTENT_TYPES else None,
            "intake_meta": {"missing": intake_result.get("missing", []), "notes": intake_result.get("notes", [])[:30],
                            "injection_notes": intake_result.get("injection_notes", [])[:30]},
            "clarify": clarify, "steps": steps, "versions": [], "sections": {}, "added": [],
            "screen": _small_decision(decision) if decision else None,
            "escalation_info": _escalation_info(decision) if decision else None,
            "pending_intake": None if decision is not None else {"intake": intake_result["intake"]},
        }

    _store(lambda: repo.update_pr_flow(rid, actor_id=uid, mutate=init))
    if decision is not None:
        patch = _screen_patch(decision)
        if not decision.get("escalate") and clarify:
            patch["status"] = "needs_input"
        _store(lambda: repo.complete_pr_step(rid, "intake", user_id=uid, output=intake_result, patch=patch))
    return view(_store(lambda: repo.get_pr_run(rid, uid)), user)


def _guess(field: dict | None) -> str | None:
    value = (field or {}).get("value")
    return str(value) if isinstance(value, str) and value else None


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def run_step(run_wire_id: str, step: str, user: dict, meter: pipeline.Meter | None = None) -> dict:
    if step not in pipeline.STEP_IDS:
        raise PrError(404, "step_not_found", f"There is no step called {step!r}.")
    run = _load(run_wire_id, user)
    uid = _uid(user)
    if run.get("user_id") != uid:
        raise PrError(403, "owner_only", "Only the person who started this draft can run its steps.")
    if _terminal(run):
        raise PrError(409, "run_closed", "This draft was closed.", status=view(run, user, load_version=False)["status"])
    states = _step_states(run)
    if states[step]["state"] == "done":
        return view(run, user)
    order = list(pipeline.STEP_IDS)
    blocking = next((s for s in order[: order.index(step)] if states[s]["state"] != "done"
                     and not (s == "package" and states[s]["state"] == "partial")), None)
    if blocking is not None:
        raise PrError(409, "step_out_of_order", f"Finish {blocking} first.", next_step=_next_step(run, states))
    if step in ("research", "draft", "package", "notes"):
        if _escalation_open(run):
            raise PrError(409, "run_paused", "This draft is paused for comms and legal.")
        if (_flow(run).get("clarify") or {}).get("state") == "open":
            raise PrError(409, "needs_details", "Answer or skip the details card first.")
    if step == "intake":
        return view(run, user)  # intake runs inside POST /runs; a run without it has nothing to recover from

    lease = _store(lambda: repo.lease_pr_step(run["id"], step, user_id=uid, lease_s=pipeline.LEASE_S))
    if lease["done"]:
        return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)
    if not lease["acquired"]:
        retry = int(lease["retry_after_s"])
        raise PrError(409, "step_running", "This step is already running. It will finish on its own.",
                      headers={"Retry-After": str(retry)}, retry_after_s=retry)
    run = lease["run"]
    deadline = pipeline.Deadline()
    invoke = _invoke(deadline, meter)
    runner = {"screen": _do_screen, "research": _do_research, "draft": _do_draft, "package": _do_package,
              "notes": _do_notes}[step]
    try:
        state, result, flow_patch, failure = runner(run, user, deadline, invoke)
    except PrError as err:
        _store(lambda: repo.finish_pr_step(run["id"], step, user_id=uid, state="failed",
                                           error={"code": err.code, "message": err.message, "at": _now(),
                                                  "attempts": 1}))
        raise
    except Exception:
        _store(lambda: repo.finish_pr_step(run["id"], step, user_id=uid, state="failed",
                                           error={"code": "internal", "message": "This step hit an unexpected error.",
                                                  "at": _now(), "attempts": 1}))
        raise
    _store(lambda: repo.finish_pr_step(run["id"], step, user_id=uid, state=state, result=result,
                                       flow_patch=flow_patch,
                                       error=({"code": failure.code, "message": failure.message, "at": _now(),
                                               "attempts": 1} if failure and state == "failed" else None)))
    fresh = _store(lambda: repo.get_pr_run(run["id"], uid))
    if failure is not None and state == "failed":
        raise PrError(failure.status, failure.code, failure.message, step=step)
    return view(fresh, user)


def _card_agent(run: dict) -> dict:
    b = {"id": run.get("brand_id"), "name": _flow(run).get("brand_name"), "domain": _flow(run).get("brand_domain")}
    return cards.to_agent(b, _card_doc(run["brand_id"]))


def _do_screen(run, user, deadline, invoke):
    uid = _uid(user)
    flow = _flow(run)
    pending = (flow.get("pending_intake") or {}).get("intake")
    if pending is None:
        return "done", None, {}, None
    b = {"id": run.get("brand_id"), "name": flow.get("brand_name"), "domain": flow.get("brand_domain")}
    try:
        # The intake as it stands now — clarify answers included, if the owner
        # gave them while the screen was failing — not the first extraction.
        decision = pipeline.run_screen(_intake(run), _screen_pasted(flow, _answers_of(flow.get("clarify"))), b,
                                       _card_agent(run), invoke=invoke)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "screen") from exc
    patch = _screen_patch(decision)
    if not decision.get("escalate") and (flow.get("clarify") or {}).get("state") == "open":
        patch["status"] = "needs_input"
    output = {"intake": pending, "missing": (flow.get("intake_meta") or {}).get("missing", [])}
    _store(lambda: repo.complete_pr_step(run["id"], "intake", user_id=uid, output=output, patch=patch))
    result = {"escalated": bool(decision.get("escalate")), "category": decision.get("category") or None}
    return "done", result, {"screen": _small_decision(decision), "escalation_info": _escalation_info(decision),
                            "pending_intake": None}, None


def _do_research(run, user, deadline, invoke):
    uid = _uid(user)
    flow = _flow(run)
    steps = run.get("steps") or {}
    intake = _intake(run)
    card_agent = _card_agent(run)
    b = {"id": run.get("brand_id"), "name": flow.get("brand_name"), "domain": flow.get("brand_domain")}
    if "research" not in steps:
        try:
            gathered = pipeline.gather(flow.get("news") or run.get("title") or "", b, search=SEARCH, fetch=FETCH)
            deadline.check("evidence extraction")
            extracted = pipeline.extract(gathered["pages"], intake, card_agent, invoke=invoke)
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc, "research") from exc
        evidence = pipeline.evidence_for(intake, card_agent, extracted["evidence"])
        result = pipeline.research_result(gathered, extracted, evidence)
        _store(lambda: repo.complete_pr_step(run["id"], "research", user_id=uid, output={"result": result},
                                             patch={"evidence": evidence, "brief": pipeline.intake_to_brief(intake),
                                                    "status": "in_progress"}))
        run = _store(lambda: repo.get_pr_run(run["id"], uid))
    else:
        out = _store(lambda: repo.get_pr_step_output(run["id"], "research", user_id=uid,
                                                     round=int(steps["research"]["round"])))
        result = ((out or {}).get("output") or {}).get("result") or {}
    if "angle" not in (run.get("steps") or {}):
        try:
            angle = pipeline.run_angle(intake, card_agent, run.get("evidence") or [], invoke=invoke)
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc, "research") from exc
        _store(lambda: repo.complete_pr_step(run["id"], "angle", user_id=uid,
                                             patch={"angle": json.dumps(angle, ensure_ascii=False)}))
        if angle.get("audience"):
            result = dict(result, audience=angle["audience"])
    return "done", result, {}, None


def _draft_of(run: dict, uid: str, step: str) -> dict | None:
    entry = (run.get("steps") or {}).get(step)
    if not entry:
        return None
    out = _store(lambda: repo.get_pr_step_output(run["id"], step, user_id=uid, round=int(entry["round"])))
    return copy.deepcopy(((out or {}).get("output")) or None)


def _do_draft(run, user, deadline, invoke):
    uid = _uid(user)
    intake = _intake(run)
    card_agent = _card_agent(run)
    evidence = run.get("evidence") or []
    angle = _angle(run)
    draft = _draft_of(run, uid, "draft")
    if draft is None:
        try:
            draft = pipeline.run_draft(intake, card_agent, evidence, angle, today=_today(), invoke=invoke)
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc, "draft") from exc
        _store(lambda: repo.complete_pr_step(run["id"], "draft", user_id=uid, output=draft))
    if "qa" not in (run.get("steps") or {}) or _draft_of(run, uid, "qa") is None:
        fixed = draft
        if pipeline.needs_qa_fix(draft):
            try:
                fixed = pipeline.run_qa_fix(copy.deepcopy(draft), intake, card_agent, evidence, angle,
                                            today=_today(), invoke=invoke)
            except Exception as exc:  # noqa: BLE001
                raise _fail(exc, "draft") from exc
        _store(lambda: repo.complete_pr_step(run["id"], "qa", user_id=uid, output=fixed))
        draft = fixed
    return "done", pipeline.draft_result(draft), {}, None


def _version_entry(version: int, label: str, *, kind: str, what: str, by: str, locked: bool = False,
                   resets: list | None = None, locked_from: str | None = None) -> dict:
    now = _now()
    return {"version": version, "label": label, "kind": kind, "what_changed": what[:400], "created_by": by,
            "created_at": now, "locked": locked, "locked_at": now if locked else None,
            "quote_resets": resets or [], "locked_from": locked_from}


def _do_package(run, user, deadline, invoke):
    uid = _uid(user)
    flow = _flow(run)
    draft = _draft_of(run, uid, "qa")
    if draft is None:
        raise PrError(409, "step_out_of_order", "Finish the draft first.", next_step="draft")
    latest = int(run.get("latest_version") or 0)
    done_sections = {s for s, v in (flow.get("sections") or {}).items() if (v or {}).get("state") == "done"}
    evidence = run.get("evidence") or []
    try:
        out = pipeline.run_package(draft=draft, intake=_intake(run), angle=_angle(run), card_agent=_card_agent(run),
                                   evidence=evidence, today=_today(), invoke=invoke, need_fact_check=not latest,
                                   done_sections=done_sections)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "package") from exc
    sections = dict(flow.get("sections") or {})
    sections.update(out["sections"])
    patch: dict[str, Any] = {"sections": sections}
    if not latest and out["fact_rows"] is not None:
        rows = [dict(r, n=i + 1) for i, r in enumerate(pipeline.assign_ids(out["fact_rows"]))]
        content, extras = pipeline.for_store(draft, evidence)
        package = {"draft": content, "kind": "draft", "locked": False, "what_changed": "First draft"}
        _store(lambda: repo.create_pr_version(
            run["id"], user_id=uid, version=1, package=package, fact_checks=rows, qa=extras,
            changes=["First draft"], quotes=pipeline.store_quotes(draft), created_by=repo.PR_AGENT_ACTOR,
            label="v0.1", close_step="fact_check"))
        patch["versions"] = list(flow.get("versions") or []) + [
            _version_entry(1, "0.1", kind="draft", what="First draft", by=repo.PR_AGENT_ACTOR)]
        patch["current"] = {"version": 1, "label": "0.1", "headline": draft.get("headline"), "locked": False}
        patch["package_based_on"] = 1
        latest = 1
    failures = out["failures"]
    if not latest:
        failure = failures.get("fact_check") or next(iter(failures.values()), None)
        return "failed", None, patch, failure or pipeline.Failure(502, "model_output_invalid", pipeline.MSG_INVALID)
    built = ("seo", "distribution", "snippets")
    state = "partial" if any((sections.get(s) or {}).get("state") != "done" for s in built) else "done"
    return state, {"sections": {s: {"state": (sections.get(s) or {}).get("state") or "pending",
                                    "error": (sections.get(s) or {}).get("error")} for s in built}}, patch, None


def _do_notes(run, user, deadline, invoke):
    uid = _uid(user)
    flow = _flow(run)
    latest = int(run.get("latest_version") or 0)
    if not latest:
        raise PrError(409, "step_out_of_order", "Build the package first.", next_step="package")
    doc = _store(lambda: repo.get_pr_version(run["id"], latest, user_id=uid))
    draft, _ = _doc_draft(run, doc)
    sections = dict(flow.get("sections") or {})
    try:
        notes = pipeline.run_notes(intake_result=_intake_result(run), screen=flow.get("screen") or {}, draft=draft,
                                   fact_rows=(doc or {}).get("fact_checks") or [], invoke=invoke)
    except Exception as exc:  # noqa: BLE001
        failure = pipeline.classify(exc, step="notes")
        if failure is None:
            raise
        logger.warning("pr notes step failed: %s", exc, exc_info=True)
        sections["reviewer_notes"] = {"state": "failed", "error": failure.message, "body": None}
        return "failed", None, {"sections": sections}, failure
    sections["reviewer_notes"] = {"state": "done", "error": None, "body": notes}
    return "done", {"notes": sum(len(notes.get(k) or []) for k in ("assumptions", "open_questions", "risks"))}, \
        {"sections": sections}, None


# --------------------------------------------------------------------------- #
# Clarify and escalation
# --------------------------------------------------------------------------- #

def _answers_of(card: dict | None) -> dict[str, str]:
    return {f["key"]: f["answer"] for f in (card or {}).get("fields") or [] if f.get("answer")}


def _screen_pasted(flow: dict, answers: dict[str, str]) -> str:
    """The requester's own free text the screen reads beside the intake: the
    facts box and every clarify answer, whatever field it went into."""
    return "\n".join([str(flow.get("facts") or "")] + [v for v in answers.values() if v]).strip()


def clarify(run_wire_id: str, user: dict, mode: str, answers: dict[str, str], content_type: str | None,
            meter: pipeline.Meter | None = None) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    if run.get("user_id") != uid:
        raise PrError(403, "owner_only", "Only the person who started this draft can add these details.")
    if content_type is not None and content_type not in cts.CONTENT_TYPES:
        raise PrError(422, "invalid", "Unknown content type.", field="content_type")
    for key, value in (answers or {}).items():
        if key not in pipeline.CLARIFY_KEYS:
            raise PrError(422, "invalid", f"Unknown field {key!r}.", field=key)
        if len(value or "") > NOTE_CAP:
            raise PrError(422, "too_long", f"{key} is longer than {NOTE_CAP} characters.", field=key, max=NOTE_CAP)

    flow = _flow(run)
    if flow.get("terminal"):
        raise PrError(409, "run_closed", "This draft was closed.", status="closed")
    current = flow.get("clarify")
    if not current or current.get("state") != "open":
        raise PrError(409, "clarify_closed", "These details were already answered.")

    def answered(card: dict) -> dict:
        card = copy.deepcopy(card)
        for f in card["fields"]:
            text = str((answers or {}).get(f["key"]) or "").strip()
            f["answer"] = text if mode == "answers" and text else None
        card["state"] = "answered" if mode == "answers" else "skipped"
        card["content_type"] = content_type or card.get("content_type")
        card["answered_at"] = _now()
        return card

    # Answers become STATED requirements of the draft, so they are screened
    # exactly like the brief was: "acquired by Acme" typed into key facts must
    # pause the run as surely as it would have in the one-sentence news. A
    # screen that cannot run refuses the answers (nothing saved) — never "safe".
    decision = None
    prospective = answered(current)
    new_text = _answers_of(prospective)
    if new_text:
        b = {"id": run.get("brand_id"), "name": flow.get("brand_name"), "domain": flow.get("brand_domain")}
        intake = pipeline.apply_clarify(_intake(run), prospective, prospective["content_type"])
        try:
            decision = pipeline.run_screen(intake, _screen_pasted(flow, new_text), b, _card_agent(run),
                                           invoke=_invoke(pipeline.Deadline(), meter))
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc, "screen") from exc
    escalate = bool(decision and decision.get("escalate"))
    if escalate:
        # The flag first: if the second write fails the run is paused with the
        # answers unsaved — the safe way round.
        _store(lambda: repo.flag_pr_escalation(run["id"], actor_id=uid,
                                               reason=decision.get("reason") or decision.get("category") or "escalated"))

    def mutate(r: dict, flow: dict) -> dict:
        if flow.get("terminal"):
            raise PrError(409, "run_closed", "This draft was closed.", status="closed")
        card = flow.get("clarify")
        if not card or card.get("state") != "open":
            raise PrError(409, "clarify_closed", "These details were already answered.")
        flow["clarify"] = answered(card)
        flow["content_type"] = flow["clarify"]["content_type"]
        if decision is not None:
            flow["screen"] = _small_decision(decision)
        if escalate:
            flow["escalation_info"] = _escalation_info(decision)
        return flow

    fresh = _store(lambda: repo.update_pr_flow(run["id"], actor_id=uid, mutate=mutate, event_kind="clarify_answered",
                                               event_data={"mode": mode, "escalated": escalate}))
    return view(fresh, user)


def _note(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise PrError(422, "note_required", "Write why. It stays on the draft's record.")
    if len(text) > NOTE_CAP:
        raise PrError(422, "too_long", f"The note is longer than {NOTE_CAP} characters.", field="note", max=NOTE_CAP)
    return text


def _require_open_escalation(run: dict) -> None:
    if _terminal(run) or not _escalation_open(run):
        raise PrError(409, "escalation_not_open", "There is no open escalation on this draft.")


def escalation_action(run_wire_id: str, user: dict, action: str, note: str | None) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    roles = Roles(run, uid)
    _require_open_escalation(run)
    if action == "clear":
        if not roles.is_clearer():
            role, who = roles.clearer()
            names = _names([who])
            raise PrError(403, "not_the_clearer", "Only the designated reviewer can clear this.",
                          clearer=_ref(names, who))
        text = _note(note or "")
        _store(lambda: repo.clear_pr_escalation(run["id"], actor_id=uid, note=text))
        return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)
    if action in ("keep_paused", "close"):
        if not roles.comms_or_legal():
            raise PrError(403, "not_comms_or_legal", "Only comms, legal or a fallback admin can do this.")
        text = _note(note or "")
    elif action in ("withdraw", "note"):
        if not roles.is_owner():
            raise PrError(403, "owner_only", "Only the person who started this draft can do this.")
        text = _note(note or "") if action == "note" else (note or "").strip()[:NOTE_CAP]
    else:
        raise PrError(404, "not_found", "Unknown escalation action.")
    kind = {"keep_paused": "keep_paused", "close": "close", "withdraw": "withdraw", "note": "requester"}[action]

    def mutate(r: dict, flow: dict) -> dict:
        _require_open_escalation(r)
        info = dict(flow.get("escalation_info") or {"notes": []})
        if text:
            info["notes"] = list(info.get("notes") or []) + [{"kind": kind, "by": uid, "at": _now(), "text": text}]
        flow["escalation_info"] = info
        if action in ("close", "withdraw"):
            flow["terminal"] = {"kind": "closed" if action == "close" else "withdrawn", "by": uid, "at": _now(),
                                "note": text}
        return flow

    fresh = _store(lambda: repo.update_pr_flow(run["id"], actor_id=uid, mutate=mutate,
                                               event_kind=f"escalation_{kind}", event_data={"text": text}))
    return view(fresh, user)


# --------------------------------------------------------------------------- #
# Draft edits: each writes a new, immutable version
# --------------------------------------------------------------------------- #

def _editable(run: dict, user: dict, version: str) -> tuple[Roles, int, dict]:
    uid = _uid(user)
    roles = Roles(run, uid)
    if _terminal(run):
        raise PrError(409, "run_closed", "This draft was closed.", status="closed")
    if run.get("status") == "approved":
        raise PrError(409, "run_locked", "This draft is approved; its approved version can't be changed.")
    if _escalation_open(run):
        raise PrError(409, "run_paused", "This draft is paused for comms and legal.")
    latest = int(run.get("latest_version") or 0)
    if not latest:
        raise PrError(409, "no_draft", "There is no draft yet.")
    if latest >= repo.PR_VERSIONS_CAP:
        # Refused before any model call: the store would refuse the version
        # after the call had already been paid for.
        raise PrError(413, "cap_reached", f"This draft has {repo.PR_VERSIONS_CAP} versions, the most one draft "
                      "can hold. Start a new draft from the current text.")
    current = _label_of(run, latest)
    if version != current:
        raise PrError(409, "stale_version", "Someone changed this draft after you opened it.",
                      current_version=current)
    doc = _store(lambda: repo.get_pr_version(run["id"], latest, user_id=uid))
    if doc is None:
        raise PrError(502, "store_failed", "PR Writer could not read this draft. Nothing was changed.")
    return roles, latest, doc


def _new_version(run: dict, user: dict, latest: int, doc: dict, *, draft: dict, evidence: list[dict],
                 rows: list[dict], kind: str, what: str, label: str, locked: bool = False,
                 locked_from: str | None = None) -> dict:
    uid = _uid(user)
    prev_quotes = {q["quote_id"]: q for q in run.get("quotes") or []}
    quotes = pipeline.store_quotes(draft)
    resets = []
    for q in quotes:
        old = prev_quotes.get(q["quote_id"])
        if old and old.get("status") == "approved" and old.get("text_hash") != repo.quote_text_hash(q["text"]):
            resets.append({"quote_id": q["quote_id"], "speaker_name": q["speaker"]})
    if resets:
        what = what + " (" + ", ".join(f"{r['speaker_name']}'s quote changed: approval reset" for r in resets) + ")"
    content, extras = pipeline.for_store(draft, evidence)
    package = {"draft": content, "kind": kind, "locked": locked, "what_changed": what}
    number = latest + 1
    _store(lambda: repo.create_pr_version(
        run["id"], user_id=uid, version=number, package=package, fact_checks=rows, qa=extras,
        changes=[what], quotes=quotes, created_by=uid, label=f"v{label}"))
    entry = _version_entry(number, label, kind=kind, what=what, by=uid, locked=locked, resets=resets,
                           locked_from=locked_from)

    def mutate(r: dict, flow: dict) -> dict:
        flow["versions"] = [v for v in flow.get("versions") or [] if v.get("version") != number] + [entry]
        flow["current"] = {"version": number, "label": label, "headline": draft.get("headline"), "locked": locked}
        return flow

    fresh = _store(lambda: repo.update_pr_flow(run["id"], actor_id=uid, mutate=mutate))
    return view(fresh, user)


def revise(run_wire_id: str, block_id: str, user: dict, comment: str, version: str,
           meter: pipeline.Meter | None = None) -> dict:
    comment = (comment or "").strip()
    if not comment:
        raise PrError(422, "comment_required", "Say what to change.")
    if len(comment) > COMMENT_CAP:
        raise PrError(422, "too_long", f"The comment is longer than {COMMENT_CAP} characters.", field="comment",
                      max=COMMENT_CAP)
    run = _load(run_wire_id, user)
    roles, latest, doc = _editable(run, user, version)
    if not roles.reviewer():
        raise PrError(403, "not_a_reviewer", "Only the author and the named reviewers can change this draft.")
    return _revise_with(run, user, latest, doc, block_id, comment, removing=None, meter=meter)


def _revise_with(run: dict, user: dict, latest: int, doc: dict, block_id: str, comment: str,
                 removing: dict | None, meter: pipeline.Meter | None = None) -> dict:
    draft, evidence = _doc_draft(run, doc)
    if not any(b["id"] == block_id for b in draft.get("blocks", [])):
        known = {b["id"] for b in pipeline.wire_blocks(draft, {})}
        if block_id in known:
            raise PrError(422, "block_not_revisable", "This part of the draft is written by code from the brand "
                          "card and the brief; change it there.")
        raise PrError(404, "block_not_found", "That block isn't in the current version.")
    uid = _uid(user)
    # One model-backed edit per run at a time: without this, two people
    # revising the same version each pay for a call and only one version lands.
    lease = _store(lambda: repo.lease_pr_step(run["id"], REVISE_LEASE, user_id=uid, lease_s=pipeline.LEASE_S))
    if not lease["acquired"]:
        retry = int(lease["retry_after_s"]) or 1
        raise PrError(409, "revision_running", "Someone is already revising this draft. Try again when it lands.",
                      headers={"Retry-After": str(retry)}, retry_after_s=retry)
    try:
        if int(lease["run"].get("latest_version") or 0) != latest:
            raise PrError(409, "stale_version", "Someone changed this draft after you opened it.",
                          current_version=_label_of(lease["run"], int(lease["run"].get("latest_version") or 0)))
        return _revise_locked(run, user, latest, doc, draft, evidence, block_id, comment, removing, meter)
    finally:
        try:
            repo.finish_pr_step(run["id"], REVISE_LEASE, user_id=uid, state="pending")
        except Exception:  # noqa: BLE001 — the lease lapses on its own after LEASE_S
            logger.warning("pr: could not release the revise lease on %s", run["id"], exc_info=True)


def _revise_locked(run: dict, user: dict, latest: int, doc: dict, draft: dict, evidence: list[dict], block_id: str,
                   comment: str, removing: dict | None, meter: pipeline.Meter | None = None) -> dict:
    uid = _uid(user)
    commenter = (_names([uid]).get(uid) or {}).get("name") or str(user.get("email") or "reviewer")
    deadline = pipeline.Deadline()
    try:
        draft, change = pipeline.run_revise(draft, block_id, comment, intake=_intake(run), card_agent=_card_agent(run),
                                            evidence=evidence, angle=_angle(run), today=_today(),
                                            invoke=_invoke(deadline, meter), commenter=commenter)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "revise") from exc
    if removing is not None:
        block = next(b for b in draft["blocks"] if b["id"] == block_id)
        if pipeline._norm(removing["claim"]) in pipeline._norm(pipeline.qa.scan_text(block["text"])):
            raise PrError(502, "model_output_invalid", "The rewrite kept the claim, so nothing was changed. Try again.")
    rows = pipeline.carry_fact_rows(doc.get("fact_checks") or [], draft, evidence, revised_block=block_id,
                                    today=_today())
    kind = "claim_removed" if removing is not None else "revision"
    what = f"Removed “{removing['claim'][:80]}”" if removing is not None else change
    label = pipeline.next_minor(_label_of(run, latest))
    return _new_version(run, user, latest, doc, draft=draft, evidence=evidence, rows=rows, kind=kind, what=what,
                        label=label)


def _claim_row(doc: dict, claim: str) -> dict:
    row = next((r for r in pipeline.assign_ids(doc.get("fact_checks") or []) if pipeline.claim_id(r) == claim
                and str(r.get("status") or "").lower() != "removed"), None)
    if row is None:
        raise PrError(404, "claim_not_found", "That claim isn't in the current version.")
    return row


def remove_claim(run_wire_id: str, claim: str, user: dict, version: str,
                 meter: pipeline.Meter | None = None) -> dict:
    run = _load(run_wire_id, user)
    roles, latest, doc = _editable(run, user, version)
    if not roles.reviewer():
        raise PrError(403, "not_a_reviewer", "Only the author and the named reviewers can change this draft.")
    row = _claim_row(doc, claim)
    if pipeline.is_placeholder(row) or not row.get("block_id"):
        raise PrError(422, "claim_not_removable", "This item isn't a sentence in the body. Fill it in with "
                      "Mark verified, or change the brand card.")
    return _revise_with(run, user, latest, doc, row["block_id"], pipeline.remove_comment(row["claim"]),
                        removing=row, meter=meter)


def verify_claim(run_wire_id: str, claim: str, user: dict, version: str, note: str, value: str | None) -> dict:
    run = _load(run_wire_id, user)
    roles, latest, doc = _editable(run, user, version)
    if not roles.reviewer():
        raise PrError(403, "not_a_reviewer", "Only the author and the named reviewers can verify facts.")
    note = (note or "").strip()
    if not note:
        raise PrError(422, "note_required", "Say where this comes from or who confirmed it.")
    if len(note) > COMMENT_CAP or len(value or "") > COMMENT_CAP:
        raise PrError(422, "too_long", f"Keep it under {COMMENT_CAP} characters.", field="note", max=COMMENT_CAP)
    _claim_row(doc, claim)
    draft, evidence = _doc_draft(run, doc)
    name = (_names([_uid(user)]).get(_uid(user)) or {}).get("name") or str(user.get("email") or "")
    try:
        rows, what = pipeline.apply_verification(draft, copy.deepcopy(doc.get("fact_checks") or []), claim,
                                                 note=note, value=(value or "").strip() or None,
                                                 actor_id=_uid(user), actor_name=name, now=_now())
    except KeyError as exc:
        raise PrError(404, "claim_not_found", "That claim isn't in the current version.") from exc
    except ValueError as exc:
        raise PrError(422, "value_required", "This is a placeholder: write the confirmed fact that replaces it.") from exc
    pipeline.refresh(draft, _intake(run), _card_agent(run), evidence, _angle(run), today=_today())
    rows = pipeline.carry_fact_rows(rows, draft, evidence, revised_block=None, today=_today())
    label = pipeline.next_minor(_label_of(run, latest))
    return _new_version(run, user, latest, doc, draft=draft, evidence=evidence, rows=rows, kind="verification",
                        what=what, label=label)


# --------------------------------------------------------------------------- #
# Review, sign-off, approval
# --------------------------------------------------------------------------- #

def send_for_review(run_wire_id: str, user: dict, version: str) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    roles = Roles(run, uid)
    if not (roles.is_owner() or roles.comms_power()):
        raise PrError(403, "not_a_reviewer", "Only the author or the comms lead can send this for review.")
    latest = int(run.get("latest_version") or 0)
    meta = _version_meta(run, latest) if latest else {}
    if meta.get("locked") and meta.get("locked_from") == version:
        return view(run, user)  # replay of the lock that already happened
    roles, latest, doc = _editable(run, user, version)
    if meta.get("locked"):
        raise PrError(409, "already_locked", "This version is already in review.")
    if _step_states(run)["package"]["state"] not in ("done", "partial"):
        raise PrError(409, "package_not_ready", "Finish the package first.")
    draft, evidence = _doc_draft(run, doc)
    label = pipeline.next_major(_label_of(run, latest))
    return _new_version(run, user, latest, doc, draft=draft,
                        evidence=evidence, rows=doc.get("fact_checks") or [], kind="review_lock",
                        what="Sent for review", label=label, locked=True, locked_from=version)


def _locked_current(run: dict, version: str) -> int:
    if _terminal(run):
        raise PrError(409, "run_closed", "This draft was closed.", status="closed")
    latest = int(run.get("latest_version") or 0)
    if not latest:
        raise PrError(409, "no_draft", "There is no draft yet.")
    if not _current_locked(run):
        raise PrError(409, "not_in_review", "Send this draft for review first.")
    current = _label_of(run, latest)
    if version != current:
        raise PrError(409, "stale_version", "A newer version exists.", current_version=current)
    return latest


def signoff(run_wire_id: str, row: str, user: dict, version: str, decision: str, note: str | None) -> dict:
    if row not in ROLE_ROWS:
        raise PrError(404, "row_not_found", "There is no such sign-off row.")
    run = _load(run_wire_id, user)
    uid = _uid(user)
    roles = Roles(run, uid)
    if not roles.can_act_row(row):
        raise PrError(403, "not_your_row", "This sign-off belongs to someone else.", row_id=row)
    text = _note(note or "") if decision == "reject" else (note or "").strip()[:NOTE_CAP]
    latest = _locked_current(run, version)
    if run.get("status") == "approved":
        raise PrError(409, "run_locked", "This draft is already approved.")
    _store(lambda: repo.record_pr_signoff(run["id"], role=STORE_ROLE[row], actor_id=uid, version=latest,
                                          decision=decision, note=text))
    return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)


def approve(run_wire_id: str, user: dict, version: str) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    roles = Roles(run, uid)
    if not roles.comms_power():
        raise PrError(403, "comms_lead_only", "Only the comms lead marks a draft approved.")
    if run.get("status") == "approved":
        return view(run, user)
    latest = _locked_current(run, version)
    doc = _store(lambda: repo.get_pr_version(run["id"], latest, user_id=uid))
    missing = pipeline.count_missing(pipeline.wire_blocks(_doc_draft(run, doc)[0], {}))
    blockers = approval_blockers(run, missing)
    if blockers:
        raise PrError(409, "approval_blocked", "This draft can't be marked approved yet.", blockers=blockers)
    try:
        repo.record_pr_signoff(run["id"], role="final", actor_id=uid, version=latest, decision="approve")
    except repo.PrSignoffBlocked as exc:  # something moved since the read: say it in the screen's words
        fresh = _store(lambda: repo.get_pr_run(run["id"], uid)) or run
        raise PrError(409, "approval_blocked", "This draft can't be marked approved yet.",
                      blockers=approval_blockers(fresh, missing) or ["The draft changed. Reload and try again."]) from exc
    except Exception as exc:  # noqa: BLE001
        _store(lambda: (_ for _ in ()).throw(exc))
    return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)


def approval_blockers(run: dict, missing: int = 0) -> list[str]:
    """What stops approval, in the screen's words: the version label the UI
    shows (v1.0, not the store's internal number), the reviewer rows by their
    labels and each pending quote by its speaker. Mirrors
    ``firestore_repo.pr_final_blockers``, which stays the enforcing check."""
    latest = int(run.get("latest_version") or 0)
    if not latest:
        return ["There is no draft yet."]
    label = _label_of(run, latest)
    out: list[str] = []
    open_verify = int(run.get("open_verify_count") or 0)
    if open_verify:
        out.append(f"{open_verify} fact{'s' if open_verify != 1 else ''} still to verify in v{label}.")
    if missing:
        out.append(f"{missing} missing detail{'s' if missing != 1 else ''} still open in v{label}.")
    for q in run.get("quotes") or []:
        if q.get("status") != "approved":
            speaker = str(q.get("speaker") or "")
            out.append("The quote from the speaker still to be named is waiting for approval."
                       if not speaker or speaker.startswith("[") else f"{speaker}'s quote is waiting for approval.")
    if _escalation_open(run):
        out.append("The comms and legal check of this news isn't cleared.")
    signoffs = run.get("signoffs") or {}
    for row in ROLE_ROWS:
        entry = signoffs.get(STORE_ROLE[row]) or {}
        if entry.get("decision") != "approve" or entry.get("version") != latest:
            out.append(f"{ROW_LABEL[row]} hasn't signed off v{label}.")
    return out


def approve_quote(run_wire_id: str, quote_id: str, user: dict, evidence: str, text_hash: str) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    if not Roles(run, uid).comms_power():
        raise PrError(403, "comms_lead_only", "Only the comms lead records a quote approval.")
    if _terminal(run):
        raise PrError(409, "run_closed", "This draft was closed.", status="closed")
    if not run.get("latest_version"):
        raise PrError(409, "no_draft", "There is no draft yet.")
    evidence = (evidence or "").strip()
    if not evidence:
        raise PrError(422, "note_required", "Say how the speaker approved it.")
    if len(evidence) > COMMENT_CAP:
        raise PrError(422, "too_long", f"Keep it under {COMMENT_CAP} characters.", field="evidence", max=COMMENT_CAP)
    if not any(q.get("quote_id") == quote_id for q in run.get("quotes") or []):
        raise PrError(404, "quote_not_found", "That quote isn't in the current version.")
    _store(lambda: repo.attest_pr_quote(run["id"], quote_id, actor_id=uid, text_hash=text_hash,
                                        decision="approved", note=evidence))
    return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)


# --------------------------------------------------------------------------- #
# People and export
# --------------------------------------------------------------------------- #

def add_participants(run_wire_id: str, user: dict, user_ids: list[str]) -> dict:
    run = _load(run_wire_id, user)
    uid = _uid(user)
    roles = Roles(run, uid)
    if not (roles.is_owner() or roles.comms_power()):
        raise PrError(403, "not_a_reviewer", "Only the author or the comms lead can add people.")
    if _terminal(run):
        raise PrError(409, "run_closed", "This draft was closed.", status="closed")
    ids = [i for i in dict.fromkeys(user_ids or []) if i]
    if not ids or len(ids) > PARTICIPANTS_PER_CALL:
        raise PrError(422, "invalid", f"Add between 1 and {PARTICIPANTS_PER_CALL} people.", field="user_ids")
    known = {m["id"] for m in members()}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise PrError(422, "unknown_member", "Someone you picked is not a member of this workspace.", user_ids=unknown)
    new = [i for i in ids if i not in (run.get("participants") or [])]
    for pid in new:
        _store(lambda pid=pid: repo.add_pr_participant(run["id"], actor_id=uid, user_id=pid))
    if new:
        now = _now()

        def mutate(r: dict, flow: dict) -> dict:
            flow["added"] = list(flow.get("added") or []) + [
                {"user_id": pid, "added_by": uid, "added_at": now} for pid in new]
            return flow

        _store(lambda: repo.update_pr_flow(run["id"], actor_id=uid, mutate=mutate))
    return view(_store(lambda: repo.get_pr_run(run["id"], uid)), user)


EXPORT_TYPES = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "md": ("text/markdown; charset=utf-8", "md"),
}


def export_run(run_wire_id: str, user: dict, fmt: str, label: str | None) -> tuple[bytes, str, str]:
    if fmt not in EXPORT_TYPES:
        raise PrError(422, "bad_format", "Export as docx or md.")
    run = _load(run_wire_id, user)
    if not run.get("latest_version"):
        raise PrError(409, "no_draft", "There is no draft to export yet.")
    full = view(run, user)
    version = full["draft"] if not label or label == full["current_version"] else get_version(run_wire_id, label, user)
    media, ext = EXPORT_TYPES[fmt]
    data = export.to_docx(full, version) if fmt == "docx" else export.to_markdown(full, version).encode("utf-8")
    return data, media, export.filename(full, version, ext, _today().isoformat())
