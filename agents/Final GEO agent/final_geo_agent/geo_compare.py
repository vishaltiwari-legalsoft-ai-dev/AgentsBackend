"""GEO agent — head-to-head competitor comparison over stored poll answers.

The question this module answers is the one the team actually asked for: *when
an engine answers a buyer's question, who gets named and cited instead of us?*
``geo_metrics`` already scores the brand; this scores everyone in the same
answers, on the same denominators, so the two can be printed side by side.

Pure functions, no I/O. Honesty rules carried over from ``geo_metrics``:
every rate keeps its n, a rival with no domain on record gets ``None`` for
citations rather than a flattering zero, and "who else is cited" is measured
from the answers, never from a hard-coded list of rivals.
"""
from __future__ import annotations

import re
from collections import defaultdict
from statistics import mean

from final_geo_agent import geo_metrics

# A bare hostname, with or without scheme/www. Deliberately strict: an alias
# like "Clio" must not be mistaken for a domain and silently become the thing
# we count citations against.
_DOMAIN_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)", re.I
)


def normalize_domain(value: str) -> str:
    """``"https://www.Clio.com/x"`` -> ``"clio.com"``; anything that is not a
    hostname -> ``""``."""
    match = _DOMAIN_RE.match((value or "").strip().lower())
    return match.group(1) if match else ""


def entity_domains(cfg: dict, brand: dict) -> dict[str, str]:
    """entity key -> the domain its citations are counted against.

    ``self`` comes from the brand record. A competitor uses its explicit
    ``domain`` when the team set one, else the first alias that is actually a
    hostname. No domain = the key is absent here, and the panel says
    "no domain on record" instead of printing a rate it cannot compute.
    """
    domains: dict[str, str] = {}
    own = normalize_domain(brand.get("domain", ""))
    if own:
        domains["self"] = own
    for comp in cfg.get("competitors") or []:
        key = comp.get("key") or comp.get("name", "")
        if not key:
            continue
        domain = normalize_domain(comp.get("domain", ""))
        if not domain:
            for alias in comp.get("aliases") or []:
                domain = normalize_domain(alias)
                if domain:
                    break
        if domain:
            domains[key] = domain
    return domains


def entity_names(cfg: dict, brand: dict) -> dict[str, str]:
    names = {"self": brand.get("name", "") or brand.get("id", "")}
    for comp in cfg.get("competitors") or []:
        key = comp.get("key") or comp.get("name", "")
        if key:
            names[key] = comp.get("name", "") or key
    return names


# --------------------------------------------------------------- per entity


def avg_position(answers: list[dict], entity: str) -> float | None:
    """Mean 1-based mention order across the answers that name ``entity``.

    Lower is better: being the first name in the answer is what gets clicked.
    ``None`` when the entity is never named — an average over nothing.
    """
    positions = [
        pos
        for a in geo_metrics.measurable(answers)
        if (pos := (a.get("mentions") or {}).get(entity))
    ]
    return round(mean(positions), 2) if positions else None


def per_engine_rate(answers: list[dict], entity: str) -> dict[str, float | None]:
    by_engine: dict[str, list[dict]] = defaultdict(list)
    for a in answers:
        by_engine[a.get("engine", "unknown")].append(a)
    return {
        engine: geo_metrics.mention_stats(subset, entity)["rate"]
        for engine, subset in by_engine.items()
    }


def prompt_rates(answers: list[dict]) -> dict[str, dict[str, float]]:
    """prompt_id -> {entity: fraction of that prompt's measured runs naming it}.

    Rates, not "appeared at least once". Presence saturates: over a week of
    three runs on three engines nearly every prompt names nearly everyone at
    some point, and a scoreboard of all-zeros is worse than no scoreboard.
    A rate says who OWNS the question.
    """
    counts: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for a in geo_metrics.measurable(answers):
        pid = a.get("prompt_id") or ""
        if not pid:
            continue
        # a prompt measured but naming nobody still belongs in the denominator
        counts.setdefault(pid, {})
        totals[pid] = totals.get(pid, 0) + 1
        for entity in (a.get("mentions") or {}):
            counts[pid][entity] = counts[pid].get(entity, 0) + 1
    return {
        pid: {entity: hits / totals[pid] for entity, hits in per_entity.items()}
        for pid, per_entity in counts.items()
    }


def head_to_head(answers: list[dict], rival: str) -> dict:
    """Question-by-question scoreboard of one rival against us.

    A question is ours when the engines name us more often than them on it.
    Ties count separately, and questions where neither of us is ever named are
    open ground — often the most actionable bucket in the table.
    """
    rates = prompt_rates(answers)
    ahead = behind = tied = both_absent = 0
    behind_prompts: list[str] = []
    for pid, per_entity in rates.items():
        mine = per_entity.get("self", 0.0)
        theirs = per_entity.get(rival, 0.0)
        if mine > theirs:
            ahead += 1
        elif theirs > mine:
            behind += 1
            behind_prompts.append(pid)
        elif mine > 0:
            tied += 1
        else:
            both_absent += 1
    return {
        "n_prompts": len(rates),
        "ahead": ahead,
        "behind": behind,
        "tied": tied,
        "both_absent": both_absent,
        "behind_prompt_ids": sorted(behind_prompts)[:12],
    }


def comparison_rows(
    answers: list[dict],
    entities: list[str],
    names: dict[str, str],
    domains: dict[str, str],
    aliases: dict[str, list[str]] | None = None,
) -> list[dict]:
    """One row per tracked entity — us first, then rivals by mention rate."""
    sov = geo_metrics.share_of_voice(answers, entities)
    rows: list[dict] = []
    for key in entities:
        is_self = key == "self"
        domain = domains.get(key, "")
        mention = geo_metrics.mention_stats(answers, key)
        citation = geo_metrics.citation_share(answers, domain) if domain else None
        rows.append({
            "key": key,
            "name": names.get(key, key),
            "is_self": is_self,
            "domain": domain,
            "mention": mention,
            # None = no domain on record for this rival, so their citation rate
            # is unknown. It is NOT zero, and the panel must not draw it as one.
            "citation": citation,
            "sov_share": sov["share"].get(key),
            "sov_credit": sov["credit"].get(key),
            "avg_position": avg_position(answers, key),
            # the exact strings this rate was matched on. A 0% next to the
            # names we searched for is debuggable; a bare 0% is not.
            "match_names": list((aliases or {}).get(key) or []),
            "per_engine": per_engine_rate(answers, key),
            "vs_self": None if is_self else head_to_head(answers, key),
        })
    rows.sort(key=lambda r: (not r["is_self"], -(r["mention"]["rate"] or 0.0)))
    return rows


# ------------------------------------------------------------ per question


def question_matrix(
    answers: list[dict], entities: list[str], names: dict[str, str]
) -> list[dict]:
    """Per buyer question: our rate, every tracked rival's rate, and who leads.

    This is the literal ask from the review — "which companies are cited for
    the same answers as us" — one row per question, everyone on the same n.
    """
    per_prompt: dict[str, dict] = {}
    for a in geo_metrics.measurable(answers):
        pid = a.get("prompt_id") or ""
        if not pid:
            continue
        row = per_prompt.setdefault(pid, {
            "prompt_id": pid,
            "text": a.get("prompt_text", ""),
            "intent": a.get("intent", ""),
            "n": 0,
            "hits": defaultdict(int),
            "engines": set(),
        })
        row["n"] += 1
        row["engines"].add(a.get("engine", ""))
        for entity in (a.get("mentions") or {}):
            if entity in entities:
                row["hits"][entity] += 1

    out: list[dict] = []
    for row in per_prompt.values():
        rates = {
            key: round(row["hits"].get(key, 0) / row["n"], 3) if row["n"] else 0.0
            for key in entities
        }
        self_rate = rates.get("self", 0.0)
        ahead = [k for k, rate in rates.items() if k != "self" and rate > self_rate]
        leader = max(rates.items(), key=lambda kv: kv[1], default=("", 0.0))
        out.append({
            "prompt_id": row["prompt_id"],
            "text": row["text"],
            "intent": row["intent"],
            "n": row["n"],
            "rates": rates,
            "self_rate": self_rate,
            "rivals_ahead": [
                {"key": k, "name": names.get(k, k), "rate": rates[k]}
                for k in sorted(ahead, key=lambda k: -rates[k])
            ],
            "leader": leader[0] if leader[1] > 0 else "",
            "engines": sorted(e for e in row["engines"] if e),
        })
    # worst first: the questions rivals own and we don't are the work list
    out.sort(key=lambda r: (r["self_rate"], -len(r["rivals_ahead"]), r["text"]))
    return out


# ------------------------------------------------- who else is in the room


def untracked_domains(
    answers: list[dict],
    known_domains: list[str],
    top_n: int = 15,
) -> list[dict]:
    """Domains cited on OUR questions that belong to nobody we track yet.

    The discovery half of the feature: the team asked to see which companies
    turn up in the same answers, and the only honest source for that is the
    citations the engines actually returned. Ranked by how often they are
    cited in answers where we are not named — those are the ones taking the
    slot. Never filtered against a hard-coded rival list.
    """
    known = {d for d in (normalize_domain(x) for x in known_domains) if d}
    counts: dict[str, int] = defaultdict(int)
    absent: dict[str, int] = defaultdict(int)
    examples: dict[str, list[str]] = defaultdict(list)
    for a in geo_metrics.measurable(answers):
        we_are_named = "self" in (a.get("mentions") or {})
        seen: set[str] = set()
        for cite in a.get("citations") or []:
            domain = normalize_domain(cite.get("domain") or "")
            if not domain or domain in seen:
                continue
            if any(domain == k or domain.endswith("." + k) for k in known):
                continue
            seen.add(domain)
            counts[domain] += 1
            if not we_are_named:
                absent[domain] += 1
            pid = a.get("prompt_id") or ""
            if pid and pid not in examples[domain] and len(examples[domain]) < 3:
                examples[domain].append(pid)
    ranked = sorted(
        counts.items(), key=lambda kv: (-absent.get(kv[0], 0), -kv[1], kv[0])
    )[:top_n]
    return [
        {
            "domain": domain,
            "count": count,
            "answers_you_absent": absent.get(domain, 0),
            "example_prompt_ids": examples[domain],
        }
        for domain, count in ranked
    ]


def entity_keys(cfg: dict) -> list[str]:
    """``self`` plus every tracked competitor key, in configured order."""
    keys = ["self"]
    for comp in cfg.get("competitors") or []:
        key = comp.get("key") or comp.get("name", "")
        if key and key not in keys:
            keys.append(key)
    return keys


def build(
    answers: list[dict], cfg: dict, brand: dict,
    aliases: dict[str, list[str]] | None = None,
) -> dict:
    """The whole comparison payload for one brand over one window."""
    names = entity_names(cfg, brand)
    domains = entity_domains(cfg, brand)
    entities = entity_keys(cfg)
    measured = geo_metrics.measurable(answers)
    return {
        "brand_id": brand.get("id", ""),
        "entities": entities,
        "names": names,
        "domains": domains,
        "rows": comparison_rows(answers, entities, names, domains, aliases),
        "questions": question_matrix(answers, entities, names),
        "untracked_domains": untracked_domains(answers, list(domains.values())),
        "n_answers": len(answers),
        "n_measured": len(measured),
        # a comparison with nobody to compare against is a real state, and the
        # panel says so rather than rendering a one-row table as a "result"
        "tracked_competitors": len(entities) - 1,
    }
