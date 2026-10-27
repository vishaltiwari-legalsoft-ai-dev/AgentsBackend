"""Structured draft -> the blueprint §10 draft template, in markdown.

Everything a model could get wrong in a way that matters legally is owned
here, by code: the dateline (from the brand card's AP dateline form and the
brief's release date), every quote's attribution and its
``[QUOTE – PENDING APPROVAL: …]`` marker (from the speaker list, never from
model text), the boilerplate (the card's approved text, verbatim), the media
contact block and the closing ``###``.
"""
from __future__ import annotations

from datetime import date

from pr_writer_agent import evidence as ev
from pr_writer_agent.content_types import ContentType

AP_MONTHS = ("Jan.", "Feb.", "March", "April", "May", "June", "July",
             "Aug.", "Sept.", "Oct.", "Nov.", "Dec.")
QUOTE_MARKER = "[QUOTE – PENDING APPROVAL: {who}]"
ADVISORY_LABELS = {"who": "WHO", "what": "WHAT", "when": "WHEN", "where": "WHERE", "why": "WHY",
                   "context": "BACKGROUND"}


def ap_date(d: date) -> str:
    return f"{AP_MONTHS[d.month - 1]} {d.day}, {d.year}"


def dateline(card: dict, release_date: str | None) -> str:
    card = ev.normalize_card(card)
    dl = card["dateline"]
    city = str(dl.get("city") or "").strip().upper() or "[VERIFY: DATELINE CITY]"
    state = str(dl.get("state") or "").strip()
    place = f"{city}, {state}" if state else city
    d = ev.parse_date(release_date or "") if release_date and len(release_date) >= 10 else None
    when = ap_date(d) if d else "[VERIFY: release date]"
    return f"{place} — {when} —"


def quote_marker(speaker: dict, brand_short_name: str) -> str:
    who = f"{speaker['name']}, {speaker['title']}"
    if speaker.get("organization") and speaker["organization"] != brand_short_name:
        who += f", {speaker['organization']}"
    return QUOTE_MARKER.format(who=who)


def _quote_paragraph(text: str, speaker: dict) -> str:
    body = text.strip().strip('"“”').strip()
    if body.endswith("."):
        body = body[:-1] + ","
    elif not body.endswith(("?", "!", ",")):
        body += ","
    org = f" of {speaker['organization']}" if speaker.get("organization") else ""
    return f"“{body}” said {speaker['name']}, {speaker['title']}{org}."


def media_contact(card: dict) -> str:
    mc = ev.normalize_card(card)["media_contact"]
    parts = [str(mc.get(k) or f"[VERIFY: media contact {k}]") for k in ("name", "title", "email", "phone")]
    return " | ".join(parts)


def boilerplates(card: dict, partner: dict | None, count: int) -> list[dict]:
    """The approved boilerplate(s), verbatim. A missing one is a visible
    placeholder, never a model-written stand-in."""
    card = ev.normalize_card(card)
    out = []
    if count >= 1:
        text = (card["boilerplate"] or {}).get("text") or ""
        out.append({"brand": card["short_name"] or "[VERIFY: brand]",
                    "text": text or f"[VERIFY: approved boilerplate for {card['short_name']} from the comms lead]"})
    if count >= 2:
        partner = partner or {}
        name = partner.get("name") or "[VERIFY: partner name]"
        out.append({"brand": name,
                    "text": partner.get("boilerplate") or f"[VERIFY: {name}'s approved boilerplate from the partner's approval contact]"})
    return out


def render_markdown(draft: dict, ct: ContentType, *, show_cites: bool = False) -> str:
    speakers = {s["id"]: s for s in draft.get("speakers", [])}
    brand = draft.get("brand_short_name", "")
    clean = (lambda t: t) if show_cites else ev.strip_cites
    out: list[str] = []
    if ct.release_line and draft.get("release_line"):
        out.append(draft["release_line"])
    out.append(f"# {draft.get('headline', '')}")
    if ct.subhead_chars and draft.get("subhead"):
        out.append(f"## {draft['subhead']}")
    if ct.first_person and speakers:
        author = next(iter(speakers.values()))
        out.append(f"By {author['name']}, {author['title']}")
    if ct.dateline and not ct.lede_words:  # advisory: dateline on its own line
        out.append(draft.get("dateline", ""))
    for block in draft.get("blocks", []):
        kind, text = block["kind"], clean(block.get("text", "")).strip()
        if kind == "quote":
            sp = speakers.get(block.get("speaker_id") or "") or {
                "name": "[VERIFY: speaker]", "title": "[VERIFY: title]", "organization": ""}
            out.append(_quote_paragraph(text, sp) + "\n" + quote_marker(sp, brand))
        elif kind == "lede" and ct.dateline:
            out.append(f"{draft.get('dateline', '')} {text}")
        elif ct.id == "media_advisory" and kind in ADVISORY_LABELS:
            out.append(f"**{ADVISORY_LABELS[kind]}:** {text}")
        elif kind == "bio":
            out.append(f"About the author\n{text}")
        else:
            out.append(text)
    for bp in draft.get("boilerplates", []):
        out.append(f"About {bp['brand']}\n{bp['text']}")
    if ct.media_contact:
        out.append(f"Media Contact\n{draft.get('media_contact', '')}")
    if ct.release_line:
        out.append("###")
    return "\n\n".join(out) + "\n"
