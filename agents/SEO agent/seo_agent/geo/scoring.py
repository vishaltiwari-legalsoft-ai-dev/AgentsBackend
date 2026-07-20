"""GEO scoring: mention / citation / accuracy / share-of-voice → score /10.

Failed engines are reported as no-data and excluded from the average — an
outage must never read as a visibility collapse (honesty rule)."""

from __future__ import annotations

from urllib.parse import urlparse

from seo_agent.schemas import GeoAnswer


def _domain_matches(url: str, brand_domain: str) -> bool:
    netloc = (urlparse(url).netloc or url).lower().removeprefix("www.")
    return netloc == brand_domain or netloc.endswith("." + brand_domain)


def evaluate_answer(answer: GeoAnswer, brand_name: str, brand_domain: str) -> GeoAnswer:
    text = answer.answer_text.lower()
    answer.mentioned = bool(brand_name) and brand_name.lower() in text
    answer.cited = any(_domain_matches(c, brand_domain) for c in answer.citations)
    return answer


def _mention_count(text: str, name: str) -> int:
    return text.lower().count(name.lower()) if name else 0


def score_run(
    answers: list[GeoAnswer], competitors: list[str], cfg: dict
) -> tuple[float, dict, dict, list[str]]:
    by_engine: dict[str, list[GeoAnswer]] = {}
    for a in answers:
        by_engine.setdefault(a.engine, []).append(a)

    engine_scores: dict[str, float] = {}
    no_data: list[str] = []
    agg = {"mention": 0.0, "citation": 0.0, "accuracy": 0.0, "sov": 0.0}

    for engine, ans in by_engine.items():
        ok = [a for a in ans if not a.error]
        if not ok:
            no_data.append(engine)
            continue
        mention = sum(a.mentioned for a in ok) / len(ok)
        citation = sum(a.cited for a in ok) / len(ok)
        with_acc = [a.accuracy for a in ok if a.accuracy is not None]
        accuracy = sum(with_acc) / len(with_acc) if with_acc else mention  # proxy when unevaluated
        brand_hits = comp_hits = 0
        for a in ok:
            brand_hits += 1 if a.mentioned else 0
            comp_hits += sum(1 for c in competitors if _mention_count(a.answer_text, c) > 0)
        sov = brand_hits / (brand_hits + comp_hits) if (brand_hits + comp_hits) else 0.0
        blend = (
            float(cfg["g_mention"]) * mention + float(cfg["g_citation"]) * citation
            + float(cfg["g_accuracy"]) * accuracy + float(cfg["g_sov"]) * sov
        )
        engine_scores[engine] = round(10 * blend, 2)
        for key, val in (("mention", mention), ("citation", citation),
                         ("accuracy", accuracy), ("sov", sov)):
            agg[key] += val

    n = len(engine_scores)
    components = {k: round(v / n, 4) for k, v in agg.items()} if n else {}
    score = round(sum(engine_scores.values()) / n, 2) if n else 0.0
    return score, components, engine_scores, sorted(no_data)
