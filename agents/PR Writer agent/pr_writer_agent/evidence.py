"""The evidence list a draft may cite — every id and every provenance field
stamped by code, never by the model.

Four origins, four id prefixes:

- ``br-N`` the intake brief. ``provenance`` is ``stated`` (the requester typed
  it) or ``extracted`` (pulled from pasted text — needs the requester's
  confirmation).
- ``bc-N`` the brand card (approved proof points, HQ).
- ``ev-N`` web research. Kept only when the model's verbatim quote is really
  on the page we fetched; the URL is the page we read.
- ``rv-N`` a fact a reviewer supplied in a revision comment.

Untrusted text (pages, pasted documents, partner material) reaches a prompt
only through :func:`fence`, which wraps it in a tag the system prompt names as
data and strips anything that could close that tag early.
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

STALE_MONTHS = 24

# Brand card shape this package consumes (blueprint §4). The store owns
# persistence; :func:`normalize_card` tolerates missing fields so a half-filled
# card yields [VERIFY] placeholders, never invented values.
CARD_FIELDS = (
    "brand_id", "legal_name", "short_name", "website", "newsroom_url", "dateline",
    "boilerplate", "mission", "positioning", "key_messages", "proof_points",
    "spokespeople", "products", "avoid", "competitor_policy", "media_contact",
    "keywords", "voice_notes", "audience",
)


def normalize_card(card: dict | None) -> dict:
    card = dict(card or {})
    out = {k: card.get(k) for k in CARD_FIELDS}
    out["short_name"] = out["short_name"] or out["legal_name"] or ""
    out["legal_name"] = out["legal_name"] or out["short_name"] or ""
    for k in ("key_messages", "proof_points", "spokespeople", "products", "avoid", "keywords"):
        out[k] = list(out[k] or [])
    out["products"] = [p["name"] if isinstance(p, dict) else str(p) for p in out["products"]]
    bp = out["boilerplate"]
    out["boilerplate"] = bp if isinstance(bp, dict) else ({"text": bp} if bp else {})
    out["dateline"] = out["dateline"] if isinstance(out["dateline"], dict) else {}
    out["media_contact"] = out["media_contact"] if isinstance(out["media_contact"], dict) else {}
    return out


# --------------------------------------------------------------------------- #
# Untrusted-content hygiene
# --------------------------------------------------------------------------- #

_TAG_BREAK = re.compile(r"</?\s*untrusted[^>]*>", re.IGNORECASE)

# Instruction-shaped text in data. Deterministic, so a page cannot talk its way
# past it; a hit is surfaced to the reviewer and blocks a "safe" screen verdict.
_INJECTION_PATTERNS = (
    r"\bignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)\b",
    r"\bdisregard (?:all |any |the )?(?:previous|prior|above|earlier|your)\b",
    r"\b(?:system|developer) prompt\b",
    r"\byou are now\b",
    r"\bnew instructions\b",
    r"\b(?:mark|treat|set) (?:this|it|the (?:draft|quote|release)) as (?:approved|verified|final|safe)\b",
    r"\b(?:return|output|respond with) [\"'{]?\s*\"?escalate\"?\s*:\s*false\b",
    r"\bdo not (?:escalate|flag|mention)\b",
    r"\b(?:send|email|post|publish) (?:this|it) (?:to|on)\b",
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>",
)


def injection_signals(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [p for p in _INJECTION_PATTERNS if re.search(p, lowered)]


def fence(kind: str, ident: str, text: str, *, limit: int | None = None) -> str:
    """Wrap untrusted text for a prompt. ``kind``/``ident`` are ours."""
    body = _TAG_BREAK.sub("[tag removed]", text or "")
    if limit is not None and len(body) > limit:
        body = body[:limit] + " [truncated]"
    return f'<untrusted kind="{kind}" id="{ident}">\n{body}\n</untrusted>'


# --------------------------------------------------------------------------- #
# Building the list
# --------------------------------------------------------------------------- #

def _item(id_, origin, claim, *, quote="", source_name="", url="", date_="",
          third_party=False, owner="", provenance="stated") -> dict:
    return {
        "id": id_, "origin": origin, "claim": claim.strip(), "quote": quote,
        "source_name": source_name, "url": url, "date": date_,
        "third_party": bool(third_party), "owner": owner, "provenance": provenance,
    }


def from_brief(intake: dict) -> list[dict]:
    """Facts the brief carries. ``intake`` is the merged intake (see
    prompts.merge_intake): ``{field: {"value", "provenance", "excerpt"}}``."""
    items: list[dict] = []

    def add(claim, provenance, source=""):
        owner = "requester" if provenance == "stated" else "requester (extracted from pasted text — confirm)"
        items.append(_item(f"br-{len(items) + 1}", "brief", claim, source_name=source or "intake brief",
                           owner=owner, provenance=provenance))

    news = intake.get("news") or {}
    if news.get("value"):
        add(str(news["value"]), news.get("provenance", "stated"))
    why_now = intake.get("why_now") or {}
    if why_now.get("value"):
        add(str(why_now["value"]), why_now.get("provenance", "stated"))
    timing = intake.get("release_timing") or {}
    tv = timing.get("value")
    if isinstance(tv, dict) and (tv.get("embargo") or tv.get("release_date")):
        add(f"Release timing: {tv.get('embargo') or tv.get('release_date')}", timing.get("provenance", "stated"))
    facts = intake.get("key_facts") or {}
    for fact in facts.get("value") or []:
        if isinstance(fact, dict) and fact.get("fact"):
            add(str(fact["fact"]), fact.get("provenance") or facts.get("provenance", "stated"),
                str(fact.get("source") or ""))
        elif isinstance(fact, str) and fact.strip():
            add(fact, facts.get("provenance", "stated"))
    partner = intake.get("partner_details") or {}
    pv = partner.get("value")
    if isinstance(pv, dict):
        for key in ("name", "description", "spokesperson"):
            if pv.get(key):
                add(f"Partner {key}: {pv[key]}", partner.get("provenance", "stated"))
    return items


def from_brand_card(card: dict, *, start: int = 1) -> list[dict]:
    card = normalize_card(card)
    approver = (card["boilerplate"] or {}).get("approver") or "brand card owner"
    items: list[dict] = []
    for pp in card["proof_points"]:
        if isinstance(pp, str):
            pp = {"text": pp}
        if not pp.get("text"):
            continue
        items.append(_item(
            f"bc-{start + len(items)}", "brand_card", str(pp["text"]),
            source_name=str(pp.get("source") or "brand card"), url=str(pp.get("url") or ""),
            date_=str(pp.get("date") or ""), owner=approver,
        ))
    dl = card["dateline"]
    if dl.get("hq"):
        items.append(_item(f"bc-{start + len(items)}", "brand_card",
                           f"{card['short_name']} is headquartered at {dl['hq']}",
                           source_name="brand card", owner=approver))
    return items


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def from_pages(pages: list[dict], extracted: list[dict], *, brand_domains: list[str],
               start: int = 1) -> tuple[list[dict], list[str]]:
    """Keep a model-extracted item only when its quote is verbatim on the page
    it names. Returns (items, notes). ``pages``: ``[{"id": "pg-1", "url",
    "title", "text"}]`` — what we fetched."""
    by_id = {p["id"]: p for p in pages}
    own = {_domain(d) if "://" in d else d.lower().removeprefix("www.") for d in brand_domains if d}
    items, notes, seen = [], [], set()
    for raw in extracted:
        page = by_id.get(str(raw.get("page_id") or ""))
        claim, quote = str(raw.get("claim") or "").strip(), str(raw.get("quote") or "").strip()
        if not page or not claim or not quote:
            notes.append(f"dropped evidence with no page/claim/quote: {claim[:80]!r}")
            continue
        if _norm_ws(quote) not in _norm_ws(page.get("text", "")):
            notes.append(f"dropped evidence whose quote is not on {page.get('url')}: {quote[:80]!r}")
            continue
        key = claim.lower()
        if key in seen:
            continue
        seen.add(key)
        dom = _domain(page.get("url", ""))
        items.append(_item(
            f"ev-{start + len(items)}", "web", claim, quote=quote,
            source_name=page.get("title") or dom, url=page.get("url", ""),
            date_=str(raw.get("date") or ""),
            third_party=bool(dom) and not any(dom == d or dom.endswith("." + d) for d in own),
            owner="comms lead", provenance="extracted",
        ))
    return items, notes


def add_reviewer_fact(evidence: list[dict], fact: str, attributed_to: str) -> dict:
    n = sum(1 for e in evidence if e["origin"] == "reviewer") + 1
    item = _item(f"rv-{n}", "reviewer", fact, source_name=f"reviewer comment ({attributed_to or 'unattributed'})",
                 owner=attributed_to or "reviewer", provenance="stated")
    evidence.append(item)
    return item


def index(evidence: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in evidence}


def digest(evidence: list[dict]) -> str:
    """The evidence block for a prompt. Brief/card/reviewer facts are ours;
    web quotes are fenced as untrusted."""
    lines = []
    for e in evidence:
        tags = [e["origin"], e["provenance"]]
        if e["third_party"]:
            tags.append("third-party")
        if e["date"]:
            tags.append(f"dated {e['date']}")
        head = f"[{e['id']}] ({', '.join(tags)}) {e['claim']}"
        if e["origin"] == "web":
            head += "\n" + fence("web_quote", e["id"], e["quote"], limit=400)
        lines.append(head)
    return "\n".join(lines) or "(no evidence)"


# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #

_DATE = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


def parse_date(value: str) -> date | None:
    m = _DATE.search(value or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def is_stale(value: str, today: date, months: int = STALE_MONTHS) -> bool | None:
    """True when older than ``months``; None when the date is unknown."""
    d = parse_date(value)
    if d is None:
        return None
    age = (today.year - d.year) * 12 + (today.month - d.month) - (1 if today.day < d.day else 0)
    return age > months


# --------------------------------------------------------------------------- #
# Speakers and URLs — what a draft may attribute and link to
# --------------------------------------------------------------------------- #

def speakers(intake: dict, card: dict) -> tuple[list[dict], list[str]]:
    """Speakers a quote may reference, ids ``sp-N``. Names and titles come from
    the brief or the brand card, never from the model. Returns (speakers, notes)."""
    card = normalize_card(card)
    on_card = {str(s.get("name", "")).strip().lower(): s for s in card["spokespeople"] if isinstance(s, dict)}
    out, notes = [], []
    field = intake.get("spokespeople") or {}
    for raw in field.get("value") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        name = str(raw["name"]).strip()
        card_row = on_card.get(name.lower())
        title = str(raw.get("title") or "").strip()
        if card_row and card_row.get("title"):
            if title and title.lower() != str(card_row["title"]).lower():
                notes.append(f"{name}: brief title {title!r} differs from brand card {card_row['title']!r} — card used")
            title = str(card_row["title"])
        if not title:
            title = f"[VERIFY: exact title for {name}]"
        if not card_row and not raw.get("organization"):
            notes.append(f"{name} is not an approved spokesperson on the brand card")
        out.append({
            "id": f"sp-{len(out) + 1}", "name": name, "title": title,
            "organization": str(raw.get("organization") or card["short_name"]),
            "talking_points": raw.get("talking_points") or "",
            "provenance": raw.get("provenance") or field.get("provenance", "stated"),
            "approval_contact": (card_row or {}).get("approval_contact", ""),
        })
    if not out:
        # No speaker in the brief: the brand card's approved spokesperson (the
        # first listed is the default voice) speaks before any placeholder does.
        card_row = next((s for s in card["spokespeople"] if isinstance(s, dict) and str(s.get("name") or "").strip()),
                        None)
        if card_row:
            name = str(card_row["name"]).strip()
            out.append({
                "id": "sp-1", "name": name,
                "title": str(card_row.get("title") or "").strip() or f"[VERIFY: exact title for {name}]",
                "organization": card["short_name"],
                "talking_points": str(card_row.get("topics") or ""), "provenance": "brand_card",
                "approval_contact": str(card_row.get("approval_contact") or ""),
            })
            notes.append(f"no spokesperson in the brief — the brand card's spokesperson {name} is quoted")
    if not out:
        out.append({
            "id": "sp-1", "name": "[VERIFY: spokesperson name]", "title": "[VERIFY: exact title]",
            "organization": card["short_name"], "talking_points": "", "provenance": "missing",
            "approval_contact": "",
        })
        notes.append("no spokesperson in the brief — quote attributed to a [VERIFY] placeholder")
    return out, notes


_URL = re.compile(r"https?://[^\s)\]>\"']+|www\.[^\s)\]>\"']+", re.IGNORECASE)


def urls_in(text: str) -> list[str]:
    return [u.rstrip(".,;:") for u in _URL.findall(text or "")]


def allowed_urls(card: dict, intake: dict, evidence: list[dict]) -> list[str]:
    card = normalize_card(card)
    urls = [card.get("website") or "", card.get("newsroom_url") or ""]
    # Only the requester's own inputs: a URL that arrived in pasted material is
    # exactly what an injected page would plant, so it waits for confirmation.
    for f in intake.values():
        if not isinstance(f, dict):
            continue
        value = f.get("value")
        if isinstance(value, list):
            urls += [u for i in value if isinstance(i, dict) and i.get("provenance") == "stated"
                     for u in urls_in(json.dumps(i))]
        elif f.get("provenance") == "stated":
            urls += urls_in(json.dumps(value) if isinstance(value, dict) else str(value))
    urls += [e["url"] for e in evidence if e.get("url")]
    return list(dict.fromkeys(u for u in urls if u))


# --------------------------------------------------------------------------- #
# Inline citation markers — "...live Nov. 12. [br-2]" or "[ev-1, bc-3]"
# --------------------------------------------------------------------------- #

_ID = r"(?:br|bc|ev|rv)-\d+"
CITE = re.compile(rf"\[\s*{_ID}(?:\s*,\s*{_ID})*\s*\]")
_CITE_WITH_SPACE = re.compile(rf"\s?\[\s*{_ID}(?:\s*,\s*{_ID})*\s*\]")


def cites_in(text: str) -> list[str]:
    return [i for m in CITE.findall(text or "") for i in re.findall(_ID, m)]


def strip_cites(text: str) -> str:
    return _CITE_WITH_SPACE.sub("", text or "")


def drop_unknown_cites(text: str, known: set[str]) -> tuple[str, list[str]]:
    """Remove citation ids that are not in the evidence list. The sentence then
    reads as uncited, which QA flags — never as falsely sourced."""
    dropped: list[str] = []

    def repl(m: re.Match) -> str:
        ids = re.findall(_ID, m.group(0))
        keep = [i for i in ids if i in known]
        dropped.extend(i for i in ids if i not in known)
        return f"[{', '.join(keep)}]" if keep else ""

    return re.sub(r"\s{2,}", " ", CITE.sub(repl, text or "")).strip(), dropped
