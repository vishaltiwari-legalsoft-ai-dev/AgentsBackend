"""GEO agent — visibility math over stored poll answers. Pure functions, no I/O.

Input is the list of answer records produced by ``geo_poll`` (one record per
prompt × run × engine). Honesty rules: every rate carries its n; variance is
computed across runs and reported, never hidden; when an engine has no
successful answers we report that instead of a fake 0%.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev

# Position-weighted credit: first entity mentioned in an answer gets 1.0, the
# second 1/2, third 1/3... The research rule stands: weights are for *positions*,
# never per-source-domain weights.


def _ok(answers: list[dict]) -> list[dict]:
    return [a for a in answers if not a.get("error")]


def mention_stats(answers: list[dict], entity: str = "self") -> dict:
    """Mention rate for ``entity`` with cross-run variance.

    Rate is computed per prompt (fraction of runs mentioning), then averaged
    across prompts — so a flaky prompt (1 of 3 runs) counts as 0.33, not 1.
    """
    ok = _ok(answers)
    by_prompt: dict[str, list[bool]] = defaultdict(list)
    for a in ok:
        by_prompt[a["prompt_id"]].append(entity in (a.get("mentions") or {}))
    if not by_prompt:
        return {"rate": None, "stdev": None, "n_prompts": 0, "n_answers": len(answers)}
    per_prompt = [mean(1.0 if hit else 0.0 for hit in hits) for hits in by_prompt.values()]
    return {
        "rate": round(mean(per_prompt), 4),
        "stdev": round(pstdev(per_prompt), 4) if len(per_prompt) > 1 else 0.0,
        "n_prompts": len(per_prompt),
        "n_answers": len(ok),
    }


def share_of_voice(answers: list[dict], entities: list[str]) -> dict:
    """Position-weighted share of voice across the given entities.

    Each answer distributes credit 1/position to every mentioned entity; SoV is
    an entity's credit share of the total. Answers mentioning no tracked entity
    contribute nothing (reported as ``unclaimed_answers``).
    """
    ok = _ok(answers)
    credit: dict[str, float] = {e: 0.0 for e in entities}
    unclaimed = 0
    for a in ok:
        mentions = a.get("mentions") or {}
        ranked = sorted(
            ((e, pos) for e, pos in mentions.items() if e in credit),
            key=lambda item: item[1],
        )
        if not ranked:
            unclaimed += 1
            continue
        for rank, (entity, _pos) in enumerate(ranked, start=1):
            credit[entity] += 1.0 / rank
    total = sum(credit.values())
    return {
        "share": {
            e: (round(c / total, 4) if total else None) for e, c in credit.items()
        },
        "credit": {e: round(c, 2) for e, c in credit.items()},
        "unclaimed_answers": unclaimed,
        "n_answers": len(ok),
    }


def citation_share(answers: list[dict], own_domain: str) -> dict:
    """How often our own domain is linked in answers that carry citations."""
    ok = [a for a in _ok(answers) if a.get("citations")]
    if not ok:
        return {"rate": None, "n_answers_with_citations": 0, "cited_answers": 0}
    own = own_domain.lower().removeprefix("www.")
    cited = sum(
        1
        for a in ok
        if any((c.get("domain") or "").endswith(own) for c in a["citations"])
    )
    return {
        "rate": round(cited / len(ok), 4),
        "n_answers_with_citations": len(ok),
        "cited_answers": cited,
    }


def source_mix(answers: list[dict], top_n: int = 15) -> list[dict]:
    """Most-cited domains across answers (measure, never hard-code)."""
    counts: dict[str, int] = defaultdict(int)
    for a in _ok(answers):
        for c in a.get("citations") or []:
            if c.get("domain"):
                counts[c["domain"]] += 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    total = sum(counts.values())
    return [
        {"domain": d, "count": n, "share": round(n / total, 4) if total else 0}
        for d, n in ranked
    ]


def source_gap(answers: list[dict], own_domain: str, top_n: int = 20) -> list[dict]:
    """Domains the engines cite for our prompts in answers where WE are not
    cited — the ranked list of places to earn presence on. The key actionable
    output of the whole agent."""
    own = own_domain.lower().removeprefix("www.")
    counts: dict[str, int] = defaultdict(int)
    examples: dict[str, set[str]] = defaultdict(set)
    for a in _ok(answers):
        citations = a.get("citations") or []
        if not citations:
            continue
        if any((c.get("domain") or "").endswith(own) for c in citations):
            continue  # we're cited here — not a gap
        for c in citations:
            domain = c.get("domain") or ""
            if not domain or domain.endswith(own):
                continue
            counts[domain] += 1
            if len(examples[domain]) < 3:
                examples[domain].add(a.get("prompt_id", ""))
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        {"domain": d, "count": n, "example_prompt_ids": sorted(examples[d])}
        for d, n in ranked
    ]


def prompt_rollup(answers: list[dict]) -> list[dict]:
    """Per-question view — the most intuitive artifact in the product: for
    every buyer question, how often the brand appeared, which rivals showed
    up instead, and on which engines. Errors excluded; every rate carries n."""
    rows: dict[str, dict] = {}
    for a in _ok(answers):
        pid = a.get("prompt_id", "")
        if not pid:
            continue
        row = rows.setdefault(pid, {
            "prompt_id": pid,
            "text": a.get("prompt_text", ""),
            "intent": a.get("intent", ""),
            "n": 0, "self_hits": 0, "cited_hits": 0,
            "rivals": defaultdict(int), "engines_hit": set(),
        })
        row["n"] += 1
        mentions = a.get("mentions") or {}
        if "self" in mentions:
            row["self_hits"] += 1
            row["engines_hit"].add(a.get("engine", ""))
        if a.get("brand_cited"):
            row["cited_hits"] += 1
        for entity in mentions:
            if entity != "self":
                row["rivals"][entity] += 1

    out = []
    for row in rows.values():
        rivals = sorted(row["rivals"].items(), key=lambda kv: -kv[1])
        out.append({
            "prompt_id": row["prompt_id"],
            "text": row["text"],
            "intent": row["intent"],
            "n": row["n"],
            "self_rate": round(row["self_hits"] / row["n"], 3) if row["n"] else 0.0,
            "cited_rate": round(row["cited_hits"] / row["n"], 3) if row["n"] else 0.0,
            "rivals": [{"key": k, "count": c} for k, c in rivals[:4]],
            "engines_hit": sorted(e for e in row["engines_hit"] if e),
        })
    out.sort(key=lambda r: (-r["self_rate"], r["text"]))
    return out


def engine_report(answers: list[dict], entities: list[str], own_domain: str) -> dict:
    """Full per-engine + blended report over one set of answer records."""
    by_engine: dict[str, list[dict]] = defaultdict(list)
    for a in answers:
        by_engine[a.get("engine", "unknown")].append(a)

    def block(subset: list[dict]) -> dict:
        errors = [a for a in subset if a.get("error")]
        return {
            "mention": mention_stats(subset),
            "sov": share_of_voice(subset, entities),
            "citation": citation_share(subset, own_domain),
            "source_mix": source_mix(subset),
            "n_answers": len(subset),
            "n_errors": len(errors),
        }

    return {
        "blended": block(answers),
        "engines": {engine: block(subset) for engine, subset in by_engine.items()},
        "source_gap": source_gap(answers, own_domain),
        "competitors": {
            e: mention_stats(answers, e) for e in entities if e != "self"
        },
        "prompt_rollup": prompt_rollup(answers),
    }
