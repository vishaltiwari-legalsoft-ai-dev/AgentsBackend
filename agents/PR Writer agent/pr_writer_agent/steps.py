"""The step functions a router calls — one HTTP request each, no persistence.

Each ``run_*`` builds its prompt from prompts.py, makes one generation call
(plus at most one repair call) through llm.call_json, then *finalizes* the
payload in code: provenance, ids, URLs, dates, owners and every hard
requirement are stamped or enforced here, never trusted from the model.
Each returns plain dicts with a ``usage`` entry the router can log.

The deterministic package parts (cover sheet, quote approval log) are here
too; they make no model call.
"""
from __future__ import annotations

import json
import re
from datetime import date

from pr_writer_agent import content_types as cts
from pr_writer_agent import evidence as ev
from pr_writer_agent import llm as pr_llm
from pr_writer_agent import qa, render
from pr_writer_agent.prompts import (AUDIENCE_MATRIX, OUTLET_EXAMPLES, STEPS, card_for_prompt,
                                     tagged_intake)

# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

REQUIRED_FIELDS = ("brand", "content_type", "news", "why_now", "audience", "key_facts",
                   "spokespeople", "release_timing")
SCALAR_FIELDS = ("brand", "content_type", "news", "why_now", "audience", "release_timing",
                 "partner_details", "cta")
LIST_FIELDS = {"key_facts": "fact", "spokespeople": "name", "target_publications": "name",
               "keywords": "keyword", "assets": "asset", "sensitivities": "note"}

QUESTIONS = {
    "brand": "Which brand is this for?",
    "content_type": "Which content type: press release, media advisory, product launch, partnership, "
                    "milestone, award, event or byline?",
    "news": "What is the news, in one sentence?",
    "why_now": "Why now — what date or event makes this timely?",
    "audience": "Who is the target audience (e.g. PI firm owners; legal tech press)?",
    "key_facts": "Which key facts and figures should appear, and where does each come from?",
    "spokespeople": "Who is quoted? Exact name, exact title and talking points.",
    "release_timing": "Release date, and is it embargoed (time, time zone, date)?",
    "partner_details": "Partner details: legal name, approved boilerplate, spokesperson and approval contact.",
}


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def _grounded(excerpt: str, sentence: str, pasted: str) -> str | None:
    """Where the excerpt really is: 'stated' (the user's own sentence),
    'extracted' (pasted material) or None (nowhere — the model made it up)."""
    e = _norm(excerpt)
    if not e:
        return None
    if e in _norm(sentence):
        return "stated"
    if e in _norm(pasted):
        return "extracted"
    return None


def _restates(excerpt: str, sentence: str) -> bool:
    """Whether an excerpt taken from the user sentence is most of it (≥ 80%),
    i.e. the news itself rather than a supporting fact inside it."""
    e, s = _norm(excerpt).rstrip("."), _norm(sentence).rstrip(".")
    return bool(e) and bool(s) and e in s and len(e) >= 0.8 * len(s)


def _empty(value) -> bool:
    return value in (None, "", [], {})


def merge_intake(sentence: str, stated: dict, pasted: str, model_fields: dict) -> tuple[dict, list[str]]:
    """Stated inputs win, always. Model extraction only fills gaps, and only
    with values whose excerpt is really in the sentence or pasted text."""
    merged: dict[str, dict] = {}
    notes: list[str] = []
    stated = {k: v for k, v in (stated or {}).items() if not _empty(v)}

    for name in SCALAR_FIELDS:
        got = model_fields.get(name) or {}
        value, where = got.get("value"), _grounded(got.get("excerpt", ""), sentence, pasted)
        if name == "news" and sentence.strip() and name not in stated:
            stated[name] = sentence.strip()
        if name in stated:
            merged[name] = {"value": stated[name], "provenance": "stated", "excerpt": ""}
            if not _empty(value) and where and _norm(json.dumps(value)) != _norm(json.dumps(stated[name])):
                notes.append(f"{name}: the pasted material says {value!r}; the stated value was kept")
            continue
        if _empty(value) or got.get("from") == "none":
            merged[name] = {"value": None, "provenance": "missing", "excerpt": ""}
            continue
        if where is None:
            notes.append(f"{name}: extraction dropped — its excerpt is not in the sentence or pasted text")
            merged[name] = {"value": None, "provenance": "missing", "excerpt": ""}
            continue
        if name == "release_timing" and isinstance(value, dict) and value.get("release_date") \
                and not re.search(r"\b\d{4}\b", got.get("excerpt", "")):
            notes.append("release_timing: the year was not written; confirm the release date")
            where = "extracted"
        merged[name] = {"value": value, "provenance": where, "excerpt": got.get("excerpt", "")}

    for name, key in LIST_FIELDS.items():
        items = []
        for raw in stated.get(name) or []:
            item = dict(raw) if isinstance(raw, dict) else {key: str(raw)}
            items.append({**item, "provenance": "stated", "excerpt": ""})
        seen = {_norm(i.get(key)) for i in items}
        for raw in (model_fields.get(name) or {}).get("items") or []:
            where = _grounded(raw.get("excerpt", ""), sentence, pasted)
            if where is None:
                notes.append(f"{name}: dropped {raw.get(key)!r} — not found in the sentence or pasted text")
                continue
            if name == "key_facts" and where == "stated" and _restates(raw.get("excerpt", ""), sentence):
                # The news sentence is already the brief's first evidence item;
                # counted again as a "key fact" it hid that none was given.
                notes.append("key_facts: dropped a fact that only restates the news sentence")
                continue
            if _norm(raw.get(key)) in seen:
                continue
            seen.add(_norm(raw.get(key)))
            item = {k: v for k, v in raw.items() if k not in ("from", "excerpt")}
            items.append({**item, "provenance": where, "excerpt": raw.get("excerpt", "")})
        prov = "missing" if not items else ("stated" if any(i["provenance"] == "stated" for i in items) else "extracted")
        merged[name] = {"value": items, "provenance": prov, "excerpt": ""}
    return merged, notes


def missing_fields(intake: dict) -> list[str]:
    missing = [f for f in REQUIRED_FIELDS if (intake.get(f) or {}).get("provenance") == "missing"]
    if (intake.get("content_type") or {}).get("value") == "partnership" \
            and (intake.get("partner_details") or {}).get("provenance") == "missing":
        missing.append("partner_details")
    return missing


def run_intake(sentence: str, *, stated: dict | None = None, pasted: str = "",
               known_brands: list[str], invoke=None) -> dict:
    step = STEPS["intake"]
    user = (
        f"KNOWN BRANDS: {json.dumps(known_brands)}\n"
        f"CONTENT TYPE IDS: {json.dumps(list(cts.CONTENT_TYPES))}\n"
        f"USER SENTENCE: {sentence}\n"
        f"STATED FIELDS: {json.dumps(stated or {}, ensure_ascii=False)}\n\n"
        "PASTED MATERIAL:\n" + (ev.fence("pasted", "pasted-1", pasted, limit=12000) if pasted else "(none)")
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    fields = result.payload["fields"]
    merged, notes = merge_intake(sentence, stated or {}, pasted, fields)
    brand = merged["brand"]
    if brand["provenance"] == "extracted" and brand["value"] not in known_brands:
        notes.append(f"brand {brand['value']!r} is not a known brand")
        merged["brand"] = {"value": None, "provenance": "missing", "excerpt": ""}
    missing = missing_fields(merged)
    return {
        "intake": merged,
        "missing": missing,
        # Asked once, as one list (blueprint §5); drafting then proceeds with placeholders.
        "questions": [QUESTIONS[f] for f in missing],
        "notes": notes,
        "injection_notes": result.payload.get("injection_notes", []) + [
            f"pasted text matches injection pattern {p}" for p in ev.injection_signals(pasted)],
        "usage": result.usage,
    }


# --------------------------------------------------------------------------- #
# Research extraction (search + fetch happen in the router; this is the one
# model call that turns fetched pages into verified evidence)
# --------------------------------------------------------------------------- #

MAX_PAGES = 5
PAGE_CHARS = 6000


def run_research_extract(pages: list[dict], intake: dict, card: dict, *, start: int = 1, invoke=None) -> dict:
    pages = [{"id": f"pg-{i + 1}", **p} for i, p in enumerate(pages[:MAX_PAGES])]
    if not pages:
        return {"evidence": [], "notes": ["no pages fetched — nothing to extract"], "usage": {"calls": 0}}
    step = STEPS["research_extract"]
    brief = {k: (intake.get(k) or {}).get("value") for k in ("brand", "content_type", "news", "audience")}
    user = f"BRIEF: {json.dumps(brief, ensure_ascii=False)}\n\n" + "\n\n".join(
        f"PAGE {p['id']} — {p.get('url', '')}\n" + ev.fence("web_page", p["id"], p.get("text", ""), limit=PAGE_CHARS)
        for p in pages)
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    c = ev.normalize_card(card)
    items, notes = ev.from_pages(pages, result.payload["evidence"],
                                 brand_domains=[c.get("website") or "", c.get("newsroom_url") or ""], start=start)
    notes += [f"page text: {n}" for n in result.payload.get("injection_notes", [])]
    for p in pages:
        for sig in ev.injection_signals(p.get("text", "")):
            notes.append(f"{p.get('url')}: instruction-like text ({sig})")
    return {"evidence": items, "notes": notes, "usage": result.usage}


def build_evidence(intake: dict, card: dict, web: list[dict] | None = None) -> list[dict]:
    return ev.from_brief(intake) + ev.from_brand_card(card) + list(web or [])


# --------------------------------------------------------------------------- #
# Angle
# --------------------------------------------------------------------------- #

def _stated_keywords(intake: dict) -> list[str]:
    return [i["keyword"] for i in (intake.get("keywords") or {}).get("value") or []
            if i.get("provenance") == "stated" and i.get("keyword")]


def finalize_angle(payload: dict, intake: dict, card: dict) -> tuple[dict, list[str]]:
    """Requester keywords are hard requirements: the primary keyword comes
    from them when they exist, and all of them stay in the keyword set."""
    notes = []
    stated = _stated_keywords(intake)
    angle = dict(payload)
    if stated:
        if angle["primary_keyword"].lower() not in [k.lower() for k in stated]:
            notes.append(f"primary keyword {angle['primary_keyword']!r} replaced by the requester's {stated[0]!r}")
            angle["primary_keyword"] = stated[0]
        rest = [k for k in stated if k.lower() != angle["primary_keyword"].lower()]
        secondary = rest + [k for k in angle["secondary_keywords"]
                            if k.lower() not in {r.lower() for r in rest} and k.lower() != angle["primary_keyword"].lower()]
        angle["secondary_keywords"] = secondary[:max(5, len(rest))]
    return angle, notes


def run_angle(intake: dict, card: dict, evidence: list[dict], *, invoke=None) -> dict:
    step = STEPS["angle"]
    c = ev.normalize_card(card)
    ct = cts.get((intake.get("content_type") or {}).get("value") or "press_release")
    user = (
        f"CONTENT TYPE: {ct.label} — {ct.purpose}\n\n"
        f"AUDIENCE MATRIX:\n{json.dumps(AUDIENCE_MATRIX, indent=1)}\n\n"
        f"BRIEF:\n{tagged_intake(intake)}\n\n"
        f"REQUESTER KEYWORDS (STATED): {json.dumps(_stated_keywords(intake))}\n"
        f"BRAND CARD:\n{card_for_prompt(c)}\n\n"
        f"EVIDENCE:\n{ev.digest(evidence)}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    angle, notes = finalize_angle(result.payload, intake, c)
    return {"angle": angle, "notes": notes, "usage": result.usage}


# --------------------------------------------------------------------------- #
# Draft
# --------------------------------------------------------------------------- #

def _intake_value(intake: dict, name: str):
    return (intake.get(name) or {}).get("value")


def _release_line(model_line: str, intake: dict, ct: cts.ContentType, notes: list[str]) -> str:
    if not ct.release_line:
        return ""
    timing = intake.get("release_timing") or {}
    embargo = (timing.get("value") or {}).get("embargo") if isinstance(timing.get("value"), dict) else None
    line = (model_line or "").strip()
    if embargo and timing.get("provenance") == "stated" and not line.upper().startswith("EMBARGOED"):
        notes.append("release line set to the stated embargo (the draft said otherwise)")
        return f"EMBARGOED UNTIL {embargo.removeprefix('Embargoed until ').removeprefix('embargoed until ')}"
    return line or ("MEDIA ADVISORY" if ct.id == "media_advisory" else "FOR IMMEDIATE RELEASE")


def _miss(text: str, bounds: tuple[int, int] | None) -> int:
    """How many characters ``text`` is outside ``bounds`` (0 = inside)."""
    if not bounds:
        return 0
    n = len((text or "").strip())
    return max(bounds[0] - n, n - bounds[1], 0)


def _best_option(options: list[dict], bounds: tuple[int, int] | None) -> str:
    """The model's first option that fits the anatomy, else its closest one.
    Models rank well but count characters badly: the top headline was 67
    characters for a 70-100 rule while the second fit (live eval 2026-10-28)."""
    return min(options, key=lambda o: _miss(o["text"], bounds))["text"]


def finalize_draft(payload: dict, ct: cts.ContentType, card: dict, intake: dict,
                   evidence: list[dict], speakers: list[dict]) -> dict:
    c = ev.normalize_card(card)
    known = set(ev.index(evidence))
    sp_ids = {s["id"] for s in speakers}
    notes: list[str] = []
    blocks = []
    allowed = set(ct.allowed_kinds())
    for n, raw in enumerate(payload["blocks"], start=1):
        text, dropped = ev.drop_unknown_cites(raw["text"], known)
        if dropped:
            notes.append(f"b{n}: removed citation(s) to unknown evidence {', '.join(dropped)}")
        speaker = raw.get("speaker_id") if raw["kind"] == "quote" else None
        if raw["kind"] == "quote" and speaker not in sp_ids:
            notes.append(f"b{n}: quote attributed to unknown speaker {speaker!r} — left unattributed")
            speaker = None
        if raw["kind"] not in allowed and raw["kind"] != "quote" and blocks and blocks[-1]["kind"] != "quote":
            # A prose block of a kind this type does not have (a "cta" in a media
            # advisory, live eval 2026-10-28) keeps its words in the block before
            # it instead of failing QA on structure, which no rewrite can fix.
            blocks[-1]["text"] = f"{blocks[-1]['text'].rstrip()} {text.strip()}"
            notes.append(f"a {raw['kind']!r} block is not part of a {ct.label.lower()}; "
                         f"its text was added to {blocks[-1]['id']}")
            continue
        blocks.append({"id": f"b{len(blocks) + 1}", "kind": raw["kind"], "text": text, "speaker_id": speaker,
                       "history": []})

    options = [{"text": o["text"].strip(), "rationale": o["rationale"], "chars": len(o["text"].strip())}
               for o in payload["headline_options"]]
    subs = [{"text": s["text"].strip(), "chars": len(s["text"].strip())} for s in payload["subhead_options"]]
    timing = _intake_value(intake, "release_timing") or {}
    partner = _intake_value(intake, "partner_details") if ct.boilerplates >= 2 else None
    draft = {
        "content_type": ct.id,
        "brand_short_name": c["short_name"],
        "release_line": _release_line(payload["release_line"], intake, ct, notes),
        "headline": _best_option(options, ct.headline_chars),
        "headline_options": options,
        "subhead": _best_option(subs, ct.subhead_chars) if (subs and ct.subhead_chars) else "",
        "subhead_options": subs if ct.subhead_chars else [],
        "dateline": render.dateline(c, timing.get("release_date") if isinstance(timing, dict) else None)
        if ct.dateline else "",
        "blocks": blocks,
        "speakers": speakers,
        "boilerplates": render.boilerplates(c, partner if isinstance(partner, dict) else None, ct.boilerplates),
        "media_contact": render.media_contact(c) if ct.media_contact else "",
        "quote_status": {b["id"]: "pending" for b in blocks if b["kind"] == "quote"},
        "notes": notes,
        "injection_notes": list(payload.get("injection_notes", [])),
    }
    return draft


def attach_qa(draft: dict, ct: cts.ContentType, card: dict, evidence: list[dict], *,
              primary_keyword: str | None, allowed: list[str], today: date) -> dict:
    report = qa.run_qa(draft, ct, card=card, evidence=evidence, primary_keyword=primary_keyword,
                       allowed_urls=allowed, today=today)
    draft["qa"] = report.to_dict()
    draft["markdown"] = render.render_markdown(draft, ct)
    return draft


def run_draft(intake: dict, card: dict, evidence: list[dict], angle: dict, speakers: list[dict], *,
              today: date, invoke=None) -> dict:
    step = STEPS["draft"]
    c = ev.normalize_card(card)
    ct = cts.get(_intake_value(intake, "content_type") or "")
    allowed = ev.allowed_urls(c, intake, evidence)
    sensitivities = [i["note"] for i in _intake_value(intake, "sensitivities") or []]
    user = (
        f"CONTENT TYPE RULES:\n{cts.spec_for_prompt(ct)}\n\n"
        f"ANGLE:\n{json.dumps(angle, ensure_ascii=False, indent=1)}\n\n"
        f"BRIEF:\n{tagged_intake(intake)}\n\n"
        f"SENSITIVITIES (hard requirements): {json.dumps(sensitivities, ensure_ascii=False)}\n"
        f"BRAND CARD:\n{card_for_prompt(c)}\n\n"
        f"SPEAKERS:\n{json.dumps([{k: s[k] for k in ('id', 'name', 'title', 'organization', 'talking_points')} for s in speakers], ensure_ascii=False, indent=1)}\n\n"
        f"ALLOWED URLS: {json.dumps(allowed)}\n\n"
        f"EVIDENCE:\n{ev.digest(evidence)}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    draft = finalize_draft(result.payload, ct, c, intake, evidence, speakers)
    attach_qa(draft, ct, c, evidence, primary_keyword=angle.get("primary_keyword"), allowed=allowed, today=today)
    draft["usage"] = result.usage
    return draft


# --------------------------------------------------------------------------- #
# QA fix — one line-edit round against the deterministic findings
# --------------------------------------------------------------------------- #

def _verify_count(text: str) -> int:
    return len(qa.VERIFY.findall(text or ""))


def run_qa_fix(draft: dict, card: dict, evidence: list[dict], *, primary_keyword: str | None,
               allowed: list[str], today: date, invoke=None) -> dict:
    ct = cts.get(draft["content_type"])
    fixable = [f for f in draft["qa"]["findings"] if f["check"] in qa.FIXABLE]
    if not fixable:
        draft.setdefault("notes", []).append("QA fix skipped: no findings a rewrite can fix")
        draft["usage"] = {"calls": 0}
        return draft
    step = STEPS["qa_fix"]
    blocks = [{"id": b["id"], "kind": b["kind"], "text": b["text"]} for b in draft["blocks"]]
    user = (
        f"CONTENT TYPE RULES:\n{cts.spec_for_prompt(ct)}\n\n"
        f"FINDINGS TO FIX:\n{json.dumps(fixable, ensure_ascii=False, indent=1)}\n\n"
        f"HEADLINE: {draft['headline']}\nSUBHEAD: {draft.get('subhead', '')}\n"
        f"BLOCKS:\n{json.dumps(blocks, ensure_ascii=False, indent=1)}\n\n"
        f"EVIDENCE IDS: {', '.join(ev.index(evidence))}"
    )
    try:
        result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    except (pr_llm.LLMTruncated, pr_llm.LLMOutputInvalid) as exc:
        # The draft is already complete and checked; a rewrite that ran past its
        # cap or came back malformed leaves it as written, its findings still
        # listed for the reviewer. Failing the step instead stranded the run: the
        # retry repeats the same rewrite (live eval 2026-10-28, b09). A provider
        # outage (LLMUnavailable) still fails loudly.
        draft.setdefault("notes", []).append(
            f"QA fix not applied ({type(exc).__name__}): the draft is shown as written, with its findings")
        draft["usage"] = {"calls": 0}
        return draft
    payload, known, notes = result.payload, set(ev.index(evidence)), draft.setdefault("notes", [])
    by_id = {b["id"]: b for b in draft["blocks"]}
    for edit in payload["blocks"]:
        block = by_id.get(edit["id"])
        if not block:
            continue
        text, dropped = ev.drop_unknown_cites(edit["text"], known)
        if dropped:
            notes.append(f"{block['id']}: fix removed citation(s) to unknown evidence {', '.join(dropped)}")
        if _verify_count(text) < _verify_count(block["text"]):
            notes.append(f"{block['id']}: fix rejected — it removed a [VERIFY] placeholder without a source")
            continue
        if text != block["text"]:
            block["history"].append(block["text"])
            block["text"] = text
    # A rewrite replaces the headline/subhead only if it fits the anatomy at
    # least as well; a fix that misses by more is a regression, not a fix.
    if payload.get("headline") and _miss(payload["headline"], ct.headline_chars) <= _miss(
            draft["headline"], ct.headline_chars):
        draft["headline"] = payload["headline"].strip()
    if payload.get("subhead") and ct.subhead_chars and _miss(payload["subhead"], ct.subhead_chars) <= _miss(
            draft.get("subhead", ""), ct.subhead_chars):
        draft["subhead"] = payload["subhead"].strip()
    for u in payload.get("unfixable", []):
        notes.append(f"not fixable without new facts — {u['check']}: {u['why']}")
    draft.setdefault("injection_notes", []).extend(payload.get("injection_notes", []))
    attach_qa(draft, ct, card, evidence, primary_keyword=primary_keyword, allowed=allowed, today=today)
    draft["usage"] = result.usage
    return draft


# --------------------------------------------------------------------------- #
# Fact-check table (blueprint §8)
# --------------------------------------------------------------------------- #

_FROM_WHOM = re.compile(r"\bfrom ([^\]]+)$", re.I)


def finalize_fact_check(payload: dict, draft: dict, evidence: list[dict], today: date) -> tuple[list[dict], list[str]]:
    idx = ev.index(evidence)
    texts = {b["id"]: _norm(qa.scan_text(b["text"])) for b in draft["blocks"]}
    texts["headline"] = _norm(draft.get("headline"))
    texts["subhead"] = _norm(draft.get("subhead"))
    rows, notes = [], []
    for raw in payload["claims"]:
        claim = ev.strip_cites(raw["claim"]).strip()
        if _norm(claim) not in texts.get(raw["block_id"], "") and not any(_norm(claim) in t for t in texts.values()):
            notes.append(f"fact-check row dropped — claim not found in the draft: {claim[:80]!r}")
            continue
        ids = [i for i in raw["evidence_ids"] if i in idx]
        items = [idx[i] for i in ids]
        stale = [ev.is_stale(i["date"], today) for i in items]
        reasons = []
        if not items:
            reasons.append("no source")
        if any(i["provenance"] == "extracted" and i["origin"] == "brief" for i in items):
            reasons.append("extracted from pasted text")
        if any(s is True for s in stale):
            reasons.append("source over 24 months old")
        if raw["category"] == "superlative" and not any(i["third_party"] for i in items):
            reasons.append("superlative without a third-party source")
        rows.append({
            "n": len(rows) + 1,
            "claim": claim,
            "block_id": raw["block_id"],
            "category": raw["category"],
            "evidence_ids": ids,
            "source": "; ".join(f"{i['source_name']}{' — ' + i['url'] if i['url'] else ''}" for i in items),
            "source_date": "; ".join(i["date"] for i in items if i["date"]),
            "date_checked": today.isoformat(),
            "status": "Needs confirmation" if reasons else "Verified",
            "status_reason": ", ".join(reasons),
            "owner": "; ".join(dict.fromkeys(i["owner"] for i in items if i["owner"])) or "comms lead",
            "over_24_months": True if any(s is True for s in stale) else (None if items and all(s is None for s in stale) else False),
        })
    # Every open [VERIFY] is a row, so nothing unconfirmed is missing from the table.
    for m in qa.VERIFY.finditer(render.render_markdown(draft, cts.get(draft["content_type"]))):
        who = _FROM_WHOM.search(m.group(0).rstrip("]"))
        rows.append({
            "n": len(rows) + 1, "claim": m.group(0), "block_id": "", "category": "other",
            "evidence_ids": [], "source": "", "source_date": "", "date_checked": today.isoformat(),
            "status": "Needs confirmation", "status_reason": "placeholder in the draft",
            "owner": who.group(1).strip() if who else "comms lead", "over_24_months": None,
        })
    return rows, notes


def run_fact_check(draft: dict, evidence: list[dict], *, today: date, invoke=None) -> dict:
    step = STEPS["fact_check"]
    blocks = [{"id": b["id"], "kind": b["kind"], "text": b["text"]} for b in draft["blocks"]]
    user = (
        f"HEADLINE: {draft['headline']}\nSUBHEAD: {draft.get('subhead', '')}\n"
        f"DRAFT BLOCKS:\n{json.dumps(blocks, ensure_ascii=False, indent=1)}\n\n"
        f"EVIDENCE:\n{ev.digest(evidence)}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    rows, notes = finalize_fact_check(result.payload, draft, evidence, today)
    return {"rows": rows, "notes": notes, "injection_notes": result.payload.get("injection_notes", []),
            "usage": result.usage}


# --------------------------------------------------------------------------- #
# SEO, distribution, snippets — independent; can run concurrently
# --------------------------------------------------------------------------- #

def _host(url: str) -> str:
    return re.sub(r"^https?://(?:www\.)?|^www\.", "", (url or "").lower()).split("/")[0]


def run_seo(draft: dict, angle: dict, card: dict, evidence: list[dict], *, site_urls: list[str] | None = None,
            invoke=None) -> dict:
    step = STEPS["seo"]
    c = ev.normalize_card(card)
    brand_hosts = {_host(c.get("website") or ""), _host(c.get("newsroom_url") or "")} - {""}
    site = [u for u in (site_urls or []) + [c.get("website") or "", c.get("newsroom_url") or ""] if u]
    external = [e["url"] for e in evidence if e["third_party"] and e["url"]]
    user = (
        f"PRIMARY KEYWORD: {angle.get('primary_keyword')}\n"
        f"SECONDARY KEYWORDS: {json.dumps(angle.get('secondary_keywords', []))}\n"
        f"BRAND SITE URLS: {json.dumps(list(dict.fromkeys(site)))}\n"
        f"THIRD-PARTY EVIDENCE URLS: {json.dumps(list(dict.fromkeys(external)))}\n\n"
        f"DRAFT:\n{draft['markdown']}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    seo, notes = dict(result.payload), []
    keep = [l for l in seo["internal_links"] if _host(l["url"]) in brand_hosts]
    if len(keep) != len(seo["internal_links"]):
        notes.append("internal link(s) outside the brand's site removed")
    seo["internal_links"] = keep
    if seo["external_link"] and seo["external_link"]["url"] not in external:
        notes.append(f"external link {seo['external_link']['url']} is not a cited third-party source — removed")
        seo["external_link"] = None
    seo["primary_keyword"] = angle.get("primary_keyword")
    seo["secondary_keywords"] = angle.get("secondary_keywords", [])
    findings = qa.check_seo_block(seo) + qa.check_snippets({"social_summary": seo["social_summary"]}, draft["markdown"])
    return {"seo": seo, "qa": [f.to_dict() for f in findings], "notes": notes, "usage": result.usage}


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _scrub(value, hits: list[str]):
    if isinstance(value, str):
        out = _PHONE.sub("[contact removed]", _EMAIL.sub("[contact removed]", value))
        if out != value:
            hits.append(value[:60])
        return out
    if isinstance(value, list):
        return [_scrub(v, hits) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, hits) for k, v in value.items()}
    return value


def run_distribution(intake: dict, angle: dict, draft: dict, *, invoke=None) -> dict:
    step = STEPS["distribution"]
    ct = cts.get(draft["content_type"])
    pubs = [i for i in (_intake_value(intake, "target_publications") or [])]
    stated_pubs = [i["name"] for i in pubs if i.get("provenance") == "stated"]
    timing = _intake_value(intake, "release_timing") or {}
    user = (
        f"CONTENT TYPE: {ct.label}; typical outlets: {', '.join(ct.outlets)}\n"
        f"ANGLE: {angle.get('angle_paragraph')}\nAUDIENCE: {angle.get('audience')}\n"
        f"RELEASE TIMING: {json.dumps(timing, ensure_ascii=False)}\n"
        f"TARGET PUBLICATIONS (STATED): {json.dumps(stated_pubs, ensure_ascii=False)}\n"
        f"OUTLET EXAMPLES: {json.dumps(OUTLET_EXAMPLES, indent=1)}\n\n"
        f"HEADLINE: {draft['headline']}\nLEDE: {qa.scan_text(next((b['text'] for b in draft['blocks'] if b['kind'] in ('lede', 'thesis', 'what')), ''))}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    hits: list[str] = []
    plan = _scrub(result.payload, hits)
    notes = ["contact details removed from the plan — the team's media list owns contacts"] if hits else []
    for ch in plan["channels"]:
        allowed = OUTLET_EXAMPLES[ch["channel"]]
        extra = [o for o in ch["outlet_examples"] if o not in allowed and o not in stated_pubs]
        if extra:
            notes.append(f"{ch['channel']}: outlet(s) not on the example list removed: {', '.join(extra)}")
        ch["outlet_examples"] = [o for o in ch["outlet_examples"] if o in allowed or o in stated_pubs]
    blob = _norm(json.dumps(plan["channels"]))
    for pub in stated_pubs:
        if _norm(pub) not in blob:
            notes.append(f"stated target publication {pub!r} is not covered by the plan")
    return {"distribution": plan, "notes": notes, "usage": result.usage}


def run_snippets(draft: dict, *, invoke=None) -> dict:
    step = STEPS["snippets"]
    result = pr_llm.call_json(step, step.system(), f"DRAFT:\n{draft['markdown']}", invoke=invoke)
    snippets = {k: v for k, v in result.payload.items() if k != "injection_notes"}
    findings = qa.check_snippets(snippets, draft["markdown"])
    return {"snippets": snippets, "qa": [f.to_dict() for f in findings], "usage": result.usage}


# --------------------------------------------------------------------------- #
# Reviewer notes — model judgement plus code-owned lists it cannot hide
# --------------------------------------------------------------------------- #

def run_reviewer_notes(intake_result: dict, escalation: dict, draft: dict, fact_rows: list[dict], *,
                       extra_injection_notes: list[str] | None = None, invoke=None) -> dict:
    step = STEPS["reviewer_notes"]
    failed = [f for f in draft["qa"]["findings"]]
    open_rows = [r for r in fact_rows if r["status"] != "Verified"]
    user = (
        f"BRIEF:\n{tagged_intake(intake_result['intake'])}\n\n"
        f"INTAKE NOTES: {json.dumps(intake_result.get('notes', []), ensure_ascii=False)}\n"
        f"ESCALATION SCREEN: {json.dumps({k: escalation.get(k) for k in ('escalate', 'categories', 'reason')})}\n"
        f"CHECK RESULTS: {json.dumps(failed, ensure_ascii=False)}\n"
        f"OPEN FACT-CHECK ROWS: {json.dumps(open_rows, ensure_ascii=False)}\n\n"
        f"DRAFT:\n{draft['markdown']}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    md = draft["markdown"]
    injection = list(dict.fromkeys(
        list(intake_result.get("injection_notes", [])) + list(draft.get("injection_notes", []))
        + list(extra_injection_notes or [])))
    return {
        "notes": {
            **result.payload,
            # Code-owned: the model summarizes, it cannot drop any of these.
            "failed_checks": [f"[{f['severity']}] {f['check']} ({f['where']}): {f['message']}" for f in failed],
            "open_verify": qa.VERIFY.findall(md),
            "pending_quotes": qa.QUOTE_PENDING.findall(md),
            "intake_notes": intake_result.get("notes", []) + draft.get("notes", []),
            "injection_notes": injection,
        },
        "usage": result.usage,
    }


# --------------------------------------------------------------------------- #
# Block revision from a reviewer comment
# --------------------------------------------------------------------------- #

def run_revise_block(draft: dict, block_id: str, comment: str, card: dict, evidence: list[dict], *,
                     primary_keyword: str | None, allowed: list[str], today: date, invoke=None,
                     commenter: str = "") -> dict:
    """Revise one block from a reviewer's comment.

    ``new_facts_from_comment`` is model output, so it is not trusted: a fact
    becomes ``rv-N`` evidence only when its text is really in the comment
    (whitespace and quotes normalized), and it is attributed to ``commenter``
    — the signed-in person who wrote the comment — never to a name the model
    supplied. A fact that fails the check is not added, and its citation is
    removed from the text, so the sentence goes to the fact-check unsourced.
    """
    block = next((b for b in draft["blocks"] if b["id"] == block_id), None)
    if block is None:
        raise KeyError(block_id)
    ct = cts.get(draft["content_type"])
    step = STEPS["revise_block"]
    speaker = next((s for s in draft["speakers"] if s["id"] == block.get("speaker_id")), None)
    user = (
        f"CONTENT TYPE RULES:\n{cts.spec_for_prompt(ct)}\n\n"
        f"BLOCK {block['id']} ({block['kind']}"
        + (f", quote by {speaker['name']}, {speaker['title']}" if speaker else "")
        + f"):\n{block['text']}\n\n"
        f"REVIEWER COMMENT:\n{ev.fence('reviewer_comment', block_id, comment, limit=2000)}\n\n"
        f"EVIDENCE:\n{ev.digest(evidence)}"
    )
    result = pr_llm.call_json(step, step.system(), user, invoke=invoke)
    payload, notes = result.payload, draft.setdefault("notes", [])
    text = payload["text"]
    said = ev._norm_ws(comment)
    for fact in payload["new_facts_from_comment"]:
        claim = str(fact.get("fact") or "").strip()
        ref = f"[{fact.get('ref')}]"
        if claim and ev._norm_ws(claim) in said:
            item = ev.add_reviewer_fact(evidence, claim, commenter or "reviewer")
            text = text.replace(ref, f"[{item['id']}]")
        else:
            notes.append(f"{block_id}: a fact the model attributed to the comment is not in it, "
                         f"so it was not added as a source: {claim[:80]!r}")
            text = re.sub(r"\s?" + re.escape(ref), "", text)
    text, dropped = ev.drop_unknown_cites(text, set(ev.index(evidence)))
    if dropped:
        notes.append(f"{block_id}: revision removed citation(s) to unknown evidence {', '.join(dropped)}")
    if text != block["text"]:
        block["history"].append(block["text"])
        block["text"] = text
        block["last_comment"] = comment
        if block["kind"] == "quote":
            # Any edit to a quote resets it to pending approval (blueprint §6, §11).
            draft.setdefault("quote_status", {})[block_id] = "pending"
    if payload.get("declined"):
        notes.append(f"{block_id}: part of the comment was not applied — {payload['declined']}")
    draft.setdefault("injection_notes", []).extend(payload.get("injection_notes", []))
    attach_qa(draft, ct, card, evidence, primary_keyword=primary_keyword, allowed=allowed, today=today)
    return {"draft": draft, "change_summary": payload["change_summary"], "usage": result.usage}


# --------------------------------------------------------------------------- #
# Deterministic package parts (no model)
# --------------------------------------------------------------------------- #

def version_name(brand: str, content_type: str, topic: str, version: str, today: date) -> str:
    slug = lambda s: re.sub(r"[^A-Za-z0-9]+", "", s.title())[:40]
    return f"{slug(brand)}_{slug(content_type)}_{slug(topic)}_v{version}_{today.isoformat()}"


def cover_sheet(draft: dict, intake: dict, *, version: str, today: date) -> dict:
    ct = cts.get(draft["content_type"])
    return {
        "brand": draft["brand_short_name"],
        "content_type": ct.label,
        "version": version,
        "version_name": version_name(draft["brand_short_name"], ct.id, str(_intake_value(intake, "news") or "")[:40],
                                     version, today),
        "date": today.isoformat(),
        "status": "Draft for review",  # the agent never marks anything approved
        "open_verify": draft["qa"]["counts"]["verify_open"],
        "quotes_pending": sum(1 for s in draft.get("quote_status", {}).values() if s == "pending"),
        "qa_errors": draft["qa"]["counts"]["errors"],
    }


def quote_approval_log(draft: dict) -> list[dict]:
    speakers = {s["id"]: s for s in draft["speakers"]}
    rows = []
    for b in draft["blocks"]:
        if b["kind"] != "quote":
            continue
        sp = speakers.get(b.get("speaker_id") or "", {})
        external = sp.get("organization") and sp.get("organization") != draft["brand_short_name"]
        rows.append({
            "block_id": b["id"],
            "quote": ev.strip_cites(b["text"]),
            "speaker": sp.get("name", "[VERIFY: speaker]"),
            "title": sp.get("title", "[VERIFY: title]"),
            "organization": sp.get("organization", ""),
            "approver": sp.get("approval_contact") or (
                f"{sp.get('organization')} (written approval required)" if external else sp.get("name", "")),
            "status": "Pending approval" if draft.get("quote_status", {}).get(b["id"], "pending") == "pending"
            else draft["quote_status"][b["id"]],
        })
    return rows
