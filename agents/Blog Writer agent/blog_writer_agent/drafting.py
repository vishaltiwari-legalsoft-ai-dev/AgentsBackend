"""Ledger-grounded drafting + the house guideline pass + per-block revision.

A draft is a list of blocks — the unit the writer comments on. Blocks may only
cite ledger ids that exist; anything else is dropped and noted, so the Sources
section never lists a claim the agent didn't actually read. Before the writer
ever sees a draft it goes through the guideline pass: a line-edit against the
company writing rules (guidelines.md) plus a deterministic scan for banned
vocabulary, dead openers, and em dashes; anything still off is noted honestly.
A comment on a block is classified into the cheapest honest action: rewrite
from the evidence already banked, or run targeted research first.
"""
from __future__ import annotations

import re
from pathlib import Path

from blog_writer_agent import llm as bw_llm
from blog_writer_agent import research
from blog_writer_agent import voice as bw_voice

MAX_FIX_ROUNDS = 3  # style-fix retries before leftovers get an honest note

_GUIDELINES_PATH = Path(__file__).resolve().parent / "guidelines.md"
_guidelines_cache: str | None = None


def guidelines_text() -> str:
    global _guidelines_cache
    if _guidelines_cache is None:
        _guidelines_cache = _GUIDELINES_PATH.read_text(encoding="utf-8")
    return _guidelines_cache


_DRAFT_SYSTEM = (
    "You are a senior editorial writer drafting a publish-ready blog post for "
    "a brand. Use ONLY the evidence ledger for factual claims; cite by ledger "
    'id. Return JSON {"meta": {"title", "description", "slug"}, "blocks": '
    '[{"kind": "intro"|"section"|"conclusion", "heading", "text", "cites": '
    '[ledger ids]}], "internal_links": [{"url", "title"}]}. Rules: never '
    "overlap an existing post's topic — angle away from them; weave in the "
    "anecdotes class for lived-experience color; internal_links only from the "
    "existing-post list; every number or factual assertion needs a cite. "
    "Voice: lead each section with its main point, short paragraphs of 2-3 "
    "sentences, one idea per sentence, contractions, second person, specific "
    "numbers over vague claims, plain verbs (is/has/uses), no em dashes, no "
    "marketing hype. Write like a human expert, not a content farm."
)

_POLISH_SYSTEM = (
    "You are the house line editor. Edit every draft block to follow the "
    "house writing rules given in the message exactly, without changing "
    "facts, claims, or which ledger ids are cited. Return JSON "
    '{"meta": {"title", "description", "slug"}, "blocks": [{"id", "heading", '
    '"text", "cites"}]} — keep every block id, return every block.'
)

_FIX_SYSTEM_TPL = (
    "You are the house line editor. The draft blocks below still break these "
    "specific house rules: {violations}. Fix ONLY those problems without "
    'changing facts or cites. Return JSON {{"blocks": [{{"id", "heading", '
    '"text", "cites"}}]}}.'
)

# Deterministic scan — the subset of the hard bans a machine can check without
# judgment: 7A vocabulary, em dashes, dead openers/transitions, engagement bait.
_BANNED_WORDS = (
    "delve, realm, harness, unlock, tapestry, paradigm, cutting-edge, revolutionize, "
    "intricate, intricacies, showcasing, crucial, pivotal, surpass, meticulously, vibrant, "
    "unparalleled, underscore, leverage, synergy, innovative, game-changer, testament, "
    "commendable, meticulous, groundbreaking, foster, showcase, enhance, holistic, garner, "
    "accentuate, pioneering, trailblazing, unleash, versatile, transformative, redefine, "
    "seamless, optimize, scalable, robust, breakthrough, empower, streamline, frictionless, "
    "elevate, effortless, data-driven, insightful, proactive, mission-critical, visionary, "
    "disruptive, reimagine, unprecedented, leading-edge, synergize, democratize, "
    "state-of-the-art, immersive, plug-and-play, turnkey, future-proof, paradigm-shifting, "
    "supercharge, captivate"
).split(", ")
_BANNED_PHRASES = [
    # 7C dead openings / 7D dead transitions
    "in today's", "it is important to note", "it is worth noting", "in order to",
    "let's dive", "let's explore", "let's unpack", "at the end of the day",
    "moving forward", "to put this in perspective", "it goes without saying",
    "nobody is talking about", "most people don't realize", "furthermore",
    "additionally", "moreover", "that said", "that being said", "with that in mind",
    "it is also worth mentioning", "on top of that",
    # 7E engagement bait / 7F hype
    "let that sink in", "read that again", "this changes everything",
    "are you paying attention", "game-changer", "10x your",
    # 7B bloated verbs
    "serves as", "stands as", "boasts a", "marks a significant",
    "plays a role in", "aims to", "seeks to",
    # 9C analogy setups
    "think of it as", "it's like a", "it is like a", "kind of like",
    "works like a", "acts like a", "functions as", "a bridge between",
    "a lens for", "a roadmap for", "the engine of", "the backbone of",
    "the dna of", "the glue that holds",
    # 11 meta commentary / disclaimers
    "in this article", "in this section", "this article will",
    "let me walk you through", "as of my last update", "based on available information",
]
# 8B/8C negative-parallelism shapes a machine can catch with confidence.
_REFRAME_PATTERNS = [
    (r"\bisn'?t (?:just |only )?about\b", "reframe: \"isn't about X...\""),
    (r"\bnot (?:just|only) about\b", "reframe: \"not just about X\""),
    (r"\bit'?s not (?:about|the)\b[^.?!]*[.?!]\s*it'?s\b", "reframe: \"it's not X. it's Y\""),
    (r"\bit was never about\b", "reframe: \"it was never about X\""),
    (r"\bthe (?:real|actual|deeper|hidden) (?:issue|problem|question|answer|point)\b", "reframe: \"the real problem...\""),
    (r"\bforget [^.?!]{2,40}[.?!]\s*(?:focus|think|start)\b", "reframe: \"forget X. focus on Y\""),
    (r"\bstop \w+ing[^.?!]{0,40}[.?!]\s*start \w+ing\b", "reframe: \"stop X-ing. start Y-ing\""),
    (r"\bis dead[.?!]", "reframe: \"X is dead\""),
    (r"\byou don'?t need [^.?!]{2,60}[.?!]\s*you need\b", "reframe: \"you don't need X. you need Y\""),
    (r"\bnot [^.?!,;]{2,40}[.?!]\s*(?:just|this is|it'?s)\b", "reframe: \"Not X. Y.\""),
]


def style_violations(text: str) -> list[str]:
    """Mechanical guideline check. Returns human-readable violation labels."""
    found: list[str] = []
    lowered = text.lower()
    if "—" in text:
        found.append("em dash")
    for word in _BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            found.append(f'banned word "{word}"')
    for phrase in _BANNED_PHRASES:
        if phrase in lowered:
            found.append(f'banned phrase "{phrase}"')
    for pattern, label in _REFRAME_PATTERNS:
        if re.search(pattern, lowered):
            found.append(label)
    return found
_CLASSIFY_SYSTEM = (
    "You classify a writer's comment on one draft block. Return JSON "
    '{"action": "rewrite"|"research", "queries": [..]}. Choose "research" only '
    "when the comment asks for facts/evidence the ledger lacks (then give 1-3 "
    'concrete search queries); otherwise "rewrite" with queries [].'
)
_REWRITE_SYSTEM = (
    "You rewrite one block of a blog draft to honour the writer's comment. "
    'Return JSON {"text", "cites": [ledger ids]}. Use ONLY the ledger for '
    "facts; keep the block's role in the post; cites may include the fresh "
    "targeted items provided."
)


def _ledger_digest(run: dict) -> str:
    return "\n".join(
        f"[{i['id']}] ({i.get('source_class', '?')}, {i.get('credibility', '')}) "
        f"{i['claim']} — \"{i.get('quote', '')[:200]}\" ({i.get('url', '')})"
        for i in run["ledger"]
    ) or "(empty)"


def _valid_cites(run: dict, cites: list, notes: list[str], where: str) -> list[str]:
    known = {i["id"] for i in run["ledger"]}
    good = [c for c in cites if c in known]
    dropped = [c for c in cites if c not in known]
    if dropped:
        notes.append(f"{where}: dropped unknown cite(s) {', '.join(dropped)}")
    return good


def _voice_section(voice: dict | None) -> str:
    digest = bw_voice.digest(voice)
    if not digest:
        return ""
    return f"BRAND VOICE PROFILE (from studying the brand's published posts — match it):\n{digest}\n\n"


def build_draft(run: dict, inventory: dict | None, *, voice: dict | None = None, llm=None) -> dict:
    llm = llm or bw_llm.llm_json

    # A draft that exists but never got its guideline pass (e.g. the polish
    # call failed last time) is finished here without redrafting from scratch.
    existing_draft = run.get("draft")
    if not (existing_draft and not existing_draft.get("guidelines_applied")):
        if not run["ledger"]:
            raise ValueError("no evidence yet — run research before drafting")
        _draft_raw(run, inventory, voice, llm)
    apply_guidelines(run, voice=voice, llm=llm)
    return run


def _draft_raw(run: dict, inventory: dict | None, voice: dict | None, llm) -> None:
    posts = (inventory or {}).get("posts", [])
    existing = "\n".join(f"- {p['title']} ({p['url']})" for p in posts) or "(none known)"
    payload = llm(
        _DRAFT_SYSTEM,
        f"{_voice_section(voice)}"
        f"Brand: {run['brand_name']} ({run['domain']})\nTopic: {run['topic']}\n"
        f"Writer notes: {run.get('notes') or '(none)'}\n\n"
        f"Existing posts on the site (do not duplicate; use for internal links):\n{existing}\n\n"
        f"Evidence ledger:\n{_ledger_digest(run)}",
        temperature=0.4,
    )

    notes: list[str] = []
    blocks = []
    for n, raw in enumerate(payload.get("blocks", []), start=1):
        block_id = f"b{n}"
        blocks.append(
            {
                "id": block_id,
                "kind": raw.get("kind", "section"),
                "heading": raw.get("heading", ""),
                "text": raw.get("text", ""),
                "cites": _valid_cites(run, raw.get("cites", []), notes, block_id),
                "history": [],
            }
        )
    if not blocks:
        raise ValueError("draft came back empty — nothing to review")

    known_urls = {p["url"] for p in posts}
    links = [l for l in payload.get("internal_links", []) if l.get("url") in known_urls]

    run["draft"] = {
        "meta": payload.get("meta", {"title": run["topic"], "description": "", "slug": ""}),
        "blocks": blocks,
        "internal_links": links,
        "notes": notes,
        "guidelines_applied": False,
    }
    research.save_run(run)


def _apply_block_edits(run: dict, payload: dict, notes: list[str]) -> int:
    """Merge id-matched block edits from a polish/fix call. Returns match count."""
    draft = run["draft"]
    by_id = {b["id"]: b for b in draft["blocks"]}
    matched = 0
    for edited in payload.get("blocks", []):
        block = by_id.get(edited.get("id")) if isinstance(edited, dict) else None
        if not block:
            continue
        matched += 1
        new_text = str(edited.get("text", "") or "").strip()
        if new_text and new_text != block["text"]:
            block["history"].append(block["text"])
            block["text"] = new_text
        if edited.get("heading") is not None:
            block["heading"] = str(edited["heading"])
        if edited.get("cites") is not None:
            block["cites"] = _valid_cites(run, edited["cites"], notes, block["id"])
    return matched


def _scan_blocks(blocks: list[dict]) -> list[str]:
    return sorted({v for b in blocks for v in style_violations(b["text"])})


def _enforce_style(run: dict, blocks: list[dict], notes: list[str], llm) -> None:
    """Fix-loop until the mechanical scan is clean or rounds run out; whatever
    survives gets an honest note — never silently shown as compliant."""
    violations = _scan_blocks(blocks)
    rounds = 0
    while violations and rounds < MAX_FIX_ROUNDS:
        rounds += 1
        fix = llm(
            _FIX_SYSTEM_TPL.format(violations="; ".join(violations)),
            "Draft blocks:\n"
            + str([{"id": b["id"], "heading": b["heading"], "text": b["text"], "cites": b["cites"]} for b in blocks]),
            temperature=0.2,
        )
        _apply_block_edits(run, fix, notes)
        violations = _scan_blocks(blocks)
    if violations:
        notes.append("style check still flags: " + "; ".join(violations))


def apply_guidelines(run: dict, *, voice: dict | None = None, llm=None) -> dict:
    """The house line-edit: every block rewritten to the company writing rules,
    then the mechanical scan + fix loop; leftovers are noted honestly."""
    draft = run.get("draft")
    if not draft:
        raise ValueError("no draft yet — nothing to line-edit")
    llm = llm or bw_llm.llm_json

    blocks_json = [
        {"id": b["id"], "kind": b["kind"], "heading": b["heading"], "text": b["text"], "cites": b["cites"]}
        for b in draft["blocks"]
    ]
    payload = llm(
        _POLISH_SYSTEM,
        f"HOUSE WRITING RULES:\n{guidelines_text()}\n\n"
        f"{_voice_section(voice)}"
        f"Post meta: {draft['meta']}\n\nDraft blocks:\n{blocks_json}",
        temperature=0.3,
    )

    notes = draft["notes"]
    matched = _apply_block_edits(run, payload, notes)
    if matched == 0:
        notes.append("guideline pass returned no usable rewrite — draft shown as written")
    meta = payload.get("meta")
    if isinstance(meta, dict):
        draft["meta"] = {**draft["meta"], **{k: v for k, v in meta.items() if v}}

    if matched:
        _enforce_style(run, draft["blocks"], notes, llm)
    elif _scan_blocks(draft["blocks"]):
        notes.append("style check still flags: " + "; ".join(_scan_blocks(draft["blocks"])))

    draft["guidelines_applied"] = True
    research.save_run(run)
    return run


def _find_block(run: dict, block_id: str) -> dict:
    for block in (run.get("draft") or {}).get("blocks", []):
        if block["id"] == block_id:
            return block
    raise KeyError(block_id)


def revise_block(run: dict, block_id: str, comment: str, *, voice: dict | None = None, llm=None, search=None, fetch=None) -> dict:
    llm = llm or bw_llm.llm_json
    block = _find_block(run, block_id)

    decision = llm(
        _CLASSIFY_SYSTEM,
        f"Topic: {run['topic']}\nBlock heading: {block['heading']}\n"
        f"Block text:\n{block['text']}\n\nWriter comment: {comment}",
    )
    fresh: list[dict] = []
    queries = [q for q in decision.get("queries", []) if isinstance(q, str) and q.strip()]
    if decision.get("action") == "research" and queries:
        fresh = research.mini_research(run, queries, search=search, fetch=fetch, llm=llm)

    fresh_digest = "\n".join(f"[{i['id']}] {i['claim']} ({i['url']})" for i in fresh) or "(none)"
    rewrite = llm(
        _REWRITE_SYSTEM,
        f"HOUSE WRITING RULES (the rewrite must follow these):\n{guidelines_text()}\n\n"
        f"{_voice_section(voice)}"
        f"Topic: {run['topic']}\nWriter comment: {comment}\n"
        f"Block ({block['kind']}, heading: {block['heading']}):\n{block['text']}\n\n"
        f"Fresh evidence from targeted research:\n{fresh_digest}\n\n"
        f"Full evidence ledger:\n{_ledger_digest(run)}",
        temperature=0.4,
    )

    notes = run["draft"]["notes"]
    block["history"].append(block["text"])
    block["text"] = rewrite.get("text", block["text"])
    block["cites"] = _valid_cites(run, rewrite.get("cites", block["cites"]), notes, block_id)
    block["last_comment"] = comment
    # Every AI rewrite goes through the same style gate as the full draft.
    _enforce_style(run, [block], notes, llm)
    research.save_run(run)
    return run
