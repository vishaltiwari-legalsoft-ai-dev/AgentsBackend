"""Brand cards: the wire shape the console edits, its validation, the
completeness rule, and the mapping onto the shape the AI core reads.

The HTTP contract (``docs/pr-writer-api.md`` §5.1) groups the card the way the
editor shows it (identity, boilerplate, messaging, ...). The AI core
(``evidence.normalize_card``) reads a flat card. This module is the one place
the two meet, so a field renamed on one side is renamed here and nowhere else.

Pure: no persistence, no HTTP. The store keeps the wire card plus a flat
``reviewers`` id list (who ``create_pr_run`` adds as participants) and
``reviewer_roles`` (who holds which row).
"""
from __future__ import annotations

import re
from typing import Any

ROLES = ("comms_lead", "brand_owner", "legal")

SECTIONS = ("reviewers", "identity", "boilerplate", "messaging", "proof_points",
            "spokespeople", "product_names", "avoid", "media_contact", "press_kit", "keywords")

STR_CAP = 2000
BOILERPLATE_CAP = 3000
LIST_CAP = 50
KEY_MESSAGES_CAP = 3

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CardInvalid(ValueError):
    """A card field is the wrong shape or too long. ``field`` names it."""

    def __init__(self, field: str, message: str, *, max_len: int | None = None):
        super().__init__(message)
        self.field = field
        self.max_len = max_len


def _s(value: Any, field: str, cap: int = STR_CAP) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CardInvalid(field, f"{field} must be text")
    value = value.strip()
    if len(value) > cap:
        raise CardInvalid(field, f"{field} is longer than {cap} characters", max_len=cap)
    return value


def _list(value: Any, field: str, cap: int = LIST_CAP) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CardInvalid(field, f"{field} must be a list")
    if len(value) > cap:
        raise CardInvalid(field, f"{field} has more than {cap} entries", max_len=cap)
    return value


def _obj(value: Any, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CardInvalid(field, f"{field} must be an object")
    return value


def _rows(value: Any, field: str, keys: tuple[str, ...]) -> list[dict[str, str]]:
    out = []
    for i, row in enumerate(_list(value, field)):
        row = _obj(row, f"{field}[{i}]")
        cleaned = {k: _s(row.get(k), f"{field}[{i}].{k}") for k in keys}
        if any(cleaned.values()):
            out.append(cleaned)
    return out


def _strings(value: Any, field: str, cap: int = LIST_CAP) -> list[str]:
    return [s for s in (_s(v, f"{field}[{i}]") for i, v in enumerate(_list(value, field, cap))) if s]


def clean_input(card: Any) -> dict[str, Any]:
    """The PUT body's ``card`` validated and trimmed, reviewers as ids.
    Raises :class:`CardInvalid`."""
    card = _obj(card, "card")
    reviewers = _obj(card.get("reviewers"), "reviewers")
    identity = _obj(card.get("identity"), "identity")
    messaging = _obj(card.get("messaging"), "messaging")
    avoid = _obj(card.get("avoid"), "avoid")
    contact = _obj(card.get("media_contact"), "media_contact")
    bp = _obj(card.get("boilerplate"), "boilerplate")
    roles = {r: (_s(reviewers.get(r), f"reviewers.{r}", 128) or None) for r in ROLES}
    if roles["legal"] and roles["legal"] == roles["comms_lead"]:
        # Legal clears escalations and holds its own sign-off; one person in
        # both rows could clear and sign a sensitive draft alone.
        raise CardInvalid("reviewers.legal", "The legal reviewer must be a different person from the comms lead.")
    return {
        "reviewer_roles": roles,
        "identity": {k: _s(identity.get(k), f"identity.{k}") for k in
                     ("legal_name", "short_name", "website", "newsroom_url", "hq_city", "hq_state", "dateline")},
        "boilerplate": {"text": _s(bp.get("text"), "boilerplate.text", BOILERPLATE_CAP)},
        "messaging": {
            "mission": _s(messaging.get("mission"), "messaging.mission"),
            "positioning": _s(messaging.get("positioning"), "messaging.positioning"),
            "key_messages": _strings(messaging.get("key_messages"), "messaging.key_messages", KEY_MESSAGES_CAP),
        },
        "proof_points": _rows(card.get("proof_points"), "proof_points", ("claim", "source", "date")),
        "spokespeople": _rows(card.get("spokespeople"), "spokespeople",
                              ("name", "title", "topics", "quote_style", "approval_contact")),
        "product_names": _strings(card.get("product_names"), "product_names"),
        "avoid": {"words": _strings(avoid.get("words"), "avoid.words"),
                  "competitor_policy": _s(avoid.get("competitor_policy"), "avoid.competitor_policy")},
        "media_contact": {k: _s(contact.get(k), f"media_contact.{k}") for k in ("name", "title", "email", "phone")},
        "press_kit": _rows(card.get("press_kit"), "press_kit", ("label", "url")),
        "keywords": _strings(card.get("keywords"), "keywords"),
    }


def clean_approval(value: Any) -> dict[str, str] | None:
    """``boilerplate_approval`` from the PUT body, or None when absent."""
    if value is None:
        return None
    value = _obj(value, "boilerplate_approval")
    by = _s(value.get("approved_by"), "boilerplate_approval.approved_by", 200)
    on = _s(value.get("approved_on"), "boilerplate_approval.approved_on", 10)
    if not by or not _DATE.match(on):
        raise CardInvalid("boilerplate_approval",
                          "boilerplate_approval needs approved_by and approved_on (YYYY-MM-DD)")
    return {"approved_by": by, "approved_on": on}


def to_store(clean: dict[str, Any], previous: dict[str, Any] | None, approval: dict[str, str] | None,
             *, actor_id: str, now: str) -> dict[str, Any]:
    """The card document to save. The boilerplate's approval fields come from
    ``approval`` when the text changed, else they are carried over; ``changed_*``
    is stamped from the caller and the clock, never from the body."""
    prev_bp = ((previous or {}).get("boilerplate") or {})
    text = clean["boilerplate"]["text"]
    if text != (prev_bp.get("text") or ""):
        bp = {"text": text, "approved_by": (approval or {}).get("approved_by", ""),
              "approved_on": (approval or {}).get("approved_on"), "changed_by": actor_id, "changed_at": now}
    else:
        bp = {"text": text, "approved_by": prev_bp.get("approved_by", ""),
              "approved_on": prev_bp.get("approved_on"), "changed_by": prev_bp.get("changed_by"),
              "changed_at": prev_bp.get("changed_at")}
    roles = clean["reviewer_roles"]
    return {**{k: v for k, v in clean.items() if k != "boilerplate"}, "boilerplate": bp,
            "reviewers": [uid for uid in dict.fromkeys(roles.values()) if uid]}


def boilerplate_changed(clean: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    return clean["boilerplate"]["text"] != (((previous or {}).get("boilerplate") or {}).get("text") or "")


def sections(card: dict[str, Any] | None) -> dict[str, bool]:
    """Which of the 11 sections are complete (contract §5.1)."""
    c = card or {}
    ident = c.get("identity") or {}
    bp = c.get("boilerplate") or {}
    msg = c.get("messaging") or {}
    avoid = c.get("avoid") or {}
    contact = c.get("media_contact") or {}
    return {
        "reviewers": bool((c.get("reviewer_roles") or {}).get("comms_lead")),
        "identity": all(ident.get(k) for k in ("legal_name", "short_name", "website", "hq_city", "hq_state")),
        "boilerplate": bool(bp.get("text") and bp.get("approved_on")),
        "messaging": bool(msg.get("positioning") and msg.get("key_messages")),
        "proof_points": bool(c.get("proof_points")),
        "spokespeople": any(s.get("name") and s.get("title") for s in c.get("spokespeople") or []),
        "product_names": bool(c.get("product_names")),
        "avoid": bool(avoid.get("words") or avoid.get("competitor_policy")),
        "media_contact": bool(contact.get("name") and contact.get("email")),
        "press_kit": bool(c.get("press_kit")),
        "keywords": bool(c.get("keywords")),
    }


def _dateline(ident: dict[str, Any]) -> dict[str, str]:
    written = str(ident.get("dateline") or "").strip()
    if written:
        city, _, state = written.partition(",")
        return {"city": city.strip(), "state": state.strip()}
    return {"city": str(ident.get("hq_city") or ""), "state": str(ident.get("hq_state") or "")}


def to_agent(brand: dict[str, Any], card: dict[str, Any] | None) -> dict[str, Any]:
    """The flat card the AI core reads (``evidence.CARD_FIELDS``). A brand with
    no card yields names only, so every other field becomes a [VERIFY]
    placeholder downstream — never an invented value."""
    c = card or {}
    ident = c.get("identity") or {}
    bp = c.get("boilerplate") or {}
    msg = c.get("messaging") or {}
    avoid = c.get("avoid") or {}
    return {
        "brand_id": brand.get("id"),
        "legal_name": ident.get("legal_name") or "",
        "short_name": ident.get("short_name") or brand.get("name") or "",
        "website": ident.get("website") or (f"https://{brand['domain']}" if brand.get("domain") else ""),
        "newsroom_url": ident.get("newsroom_url") or "",
        "dateline": _dateline(ident),
        "boilerplate": ({"text": bp.get("text"), "approver": bp.get("approved_by") or "",
                         "approved_at": bp.get("approved_on") or ""} if bp.get("text") else {}),
        "mission": msg.get("mission") or "",
        "positioning": msg.get("positioning") or "",
        "key_messages": list(msg.get("key_messages") or []),
        "proof_points": [{"text": p.get("claim"), "source": p.get("source"), "date": p.get("date")}
                         for p in c.get("proof_points") or [] if p.get("claim")],
        "spokespeople": [dict(s) for s in c.get("spokespeople") or []],
        "products": list(c.get("product_names") or []),
        "avoid": list(avoid.get("words") or []),
        "competitor_policy": avoid.get("competitor_policy") or "",
        "media_contact": dict(c.get("media_contact") or {}),
        "keywords": list(c.get("keywords") or []),
    }
