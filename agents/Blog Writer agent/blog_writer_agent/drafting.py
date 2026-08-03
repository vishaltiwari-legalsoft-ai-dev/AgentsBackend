"""Ledger-grounded drafting + the per-block revision desk.

A draft is a list of blocks — the unit the writer comments on. Blocks may only
cite ledger ids that exist; anything else is dropped and noted, so the Sources
section never lists a claim the agent didn't actually read. A comment on a
block is classified into the cheapest honest action: rewrite from the evidence
already banked, or run targeted research first and rewrite with it.
"""
from __future__ import annotations

from blog_writer_agent import llm as bw_llm
from blog_writer_agent import research

_DRAFT_SYSTEM = (
    "You are a senior editorial writer drafting a blog post for a brand. Use "
    "ONLY the evidence ledger for factual claims; cite by ledger id. Return "
    'JSON {"meta": {"title", "description", "slug"}, "blocks": [{"kind": '
    '"intro"|"section"|"conclusion", "heading", "text", "cites": [ledger ids]}], '
    '"internal_links": [{"url", "title"}]}. Rules: never overlap an existing '
    "post's topic — angle away from them; weave in the anecdotes class for "
    "lived-experience color; internal_links only from the existing-post list; "
    "every number or factual assertion needs a cite; write like a human "
    "expert, not a content farm."
)
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


def build_draft(run: dict, inventory: dict | None, *, llm=None) -> dict:
    if not run["ledger"]:
        raise ValueError("no evidence yet — run research before drafting")
    llm = llm or bw_llm.llm_json

    posts = (inventory or {}).get("posts", [])
    existing = "\n".join(f"- {p['title']} ({p['url']})" for p in posts) or "(none known)"
    payload = llm(
        _DRAFT_SYSTEM,
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
    }
    research.save_run(run)
    return run


def _find_block(run: dict, block_id: str) -> dict:
    for block in (run.get("draft") or {}).get("blocks", []):
        if block["id"] == block_id:
            return block
    raise KeyError(block_id)


def revise_block(run: dict, block_id: str, comment: str, *, llm=None, search=None, fetch=None) -> dict:
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
    research.save_run(run)
    return run
