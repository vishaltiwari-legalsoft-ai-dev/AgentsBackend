"""The agent's own visual plan: how many visuals the post needs, what kind, what theme.

Output feeds the standalone visual-prompt document (see ``export``). Each entry
is a generation-ready prompt a designer — or the Graphics Designer agent — can
run with. An unusable LLM answer raises; there is no invented fallback plan.
"""
from __future__ import annotations

from blog_writer_agent import llm as bw_llm
from blog_writer_agent import research

VISUAL_TYPES = {"hero", "chart", "diagram", "illustration", "photo"}

_PLAN_SYSTEM = (
    "You are an art director planning the visuals for a blog post. Decide how "
    "many visuals the post needs (driven by its length and structure), what "
    "kind each is, and a theme consistent with the brand. Return JSON "
    '{"visuals": [{"section", "type": "hero"|"chart"|"diagram"|"illustration"|'
    '"photo", "theme", "prompt", "rationale"}]}. "section" names the block '
    'heading the visual sits under ("(top)" for a hero). "prompt" must be a '
    "complete, generation-ready image prompt. Charts/diagrams may only show "
    "data the draft actually cites."
)


def plan_visuals(run: dict, brand: dict, *, llm=None) -> dict:
    draft = run.get("draft")
    if not draft:
        raise ValueError("no draft yet — visuals are planned from the finished draft")
    llm = llm or bw_llm.llm_json

    outline = "\n".join(
        f"- [{b['kind']}] {b['heading'] or '(intro)'}: {b['text'][:160]}" for b in draft["blocks"]
    )
    payload = llm(
        _PLAN_SYSTEM,
        f"Brand: {brand.get('name', run['brand_name'])} ({run['domain']})\n"
        f"Post title: {draft['meta'].get('title', run['topic'])}\n\nPost outline:\n{outline}",
        temperature=0.4,
    )

    raw = payload.get("visuals", []) if isinstance(payload, dict) else []
    items = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("prompt", "")).strip():
            raise ValueError("visual plan came back malformed — retry the planning step")
        items.append(
            {
                "n": len(items) + 1,
                "section": entry.get("section", ""),
                "type": entry.get("type") if entry.get("type") in VISUAL_TYPES else "illustration",
                "theme": entry.get("theme", ""),
                "prompt": str(entry["prompt"]).strip(),
                "rationale": entry.get("rationale", ""),
            }
        )
    if not items:
        raise ValueError("visual plan came back empty — retry the planning step")

    run["visuals"] = {"items": items, "notes": []}
    research.save_run(run)
    return run
