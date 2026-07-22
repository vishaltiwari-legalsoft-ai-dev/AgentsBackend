"""Blog topic lab — ranked topic ideas with volume, trend, and difficulty evidence.

Volume is a proxy (our own Search Console impressions where available, interest
tiers otherwise) because Google does not sell absolute volumes cheaply; the
ranking only needs relative numbers, and the dashboard labels them estimates.
"""
from __future__ import annotations

from . import sources
from .sources import CredentialMissing, QueryStat

STOPWORDS = {"a", "an", "the", "for", "to", "of", "in", "on", "and", "or", "is", "are", "with", "your", "my"}
QUESTION_STARTS = ("how", "what", "why", "when", "which", "can", "do", "does", "is", "are", "should")
WEAK_DOMAINS = ("reddit.com", "quora.com", "forum", "medium.com", "pinterest.", "facebook.com", "youtube.com")

MAX_SEEDS = 5          # Serper calls cost money; 5 seeds + 12 checks ≈ $0.01/run
MAX_SERP_CHECKS = 12
MAX_TOPICS = 15
TREND_SCORE = {"rising": 1.0, "new": 0.7, "flat": 0.5, "falling": 0.2}
DIFFICULTY_SCORE = {"easy win": 1.0, "medium": 0.6, "hard": 0.25, None: 0.5}


def _tokens(text: str) -> set[str]:
    return {w for w in text.lower().split() if w not in STOPWORDS and len(w) > 2}


def _match_impressions(candidate: str, rows: list[QueryStat]) -> int:
    """Impressions of GSC queries that overlap the candidate topic."""
    cand = _tokens(candidate)
    if not cand:
        return 0
    total = 0
    for r in rows:
        q = _tokens(r.query)
        if cand <= q or (q and q <= cand):
            total += r.impressions
    return total


def _trend(candidate: str, rows: list[QueryStat], prev_rows: list[QueryStat]) -> str:
    now = _match_impressions(candidate, rows)
    prev = _match_impressions(candidate, prev_rows)
    if now and not prev:
        return "new"
    if not now:
        return "flat"
    if now >= prev * 1.2:
        return "rising"
    if now <= prev * 0.8:
        return "falling"
    return "flat"


def _angle(candidate: str) -> str:
    lower = candidate.lower()
    if lower.startswith(QUESTION_STARTS) or lower.endswith("?"):
        return "FAQ answer"
    if " vs " in f" {lower} " or "best " in lower:
        return "comparison"
    if any(w in lower for w in ("cost", "price", "pricing", "salary", "rates")):
        return "pricing guide"
    return "how-to guide"


def _difficulty(serp: dict, own_domain: str) -> tuple[str, list[str]]:
    evidence = []
    weak = [r["link"] for r in serp["organic"] if any(w in r["link"].lower() for w in WEAK_DOMAINS)]
    if any(own_domain in r["link"] for r in serp["organic"][:3]):
        return "hard", ["we already rank top-3 — write only if refreshing"]
    if weak:
        evidence.append(f"{len(weak)} weak result(s) in the top 10 (forums/UGC)")
    if serp.get("aio_present"):
        evidence.append("Google shows an AI Overview — strong citation target")
    if len(weak) >= 3:
        return "easy win", evidence
    if len(weak) >= 1:
        return "medium", evidence
    return "hard", evidence or ["top 10 is all strong sites"]


def _why(volume: int, trend: str, difficulty: str | None, evidence: list[str]) -> str:
    parts = []
    if volume:
        parts.append(f"~{volume:,} impressions/mo already visible to us")
    trend_text = {"rising": "interest is rising", "new": "newly appearing searches",
                  "falling": "interest is cooling", "flat": ""}[trend]
    if trend_text:
        parts.append(trend_text)
    if difficulty:
        parts.append(difficulty if not evidence else f"{difficulty}: {'; '.join(evidence)}")
    return " · ".join(parts) or "expansion keyword from live Google suggestions"


def build_topics(
    brand: dict,
    rows: list[QueryStat],
    prev_rows: list[QueryStat],
    search=None,
    max_checks: int = MAX_SERP_CHECKS,
) -> tuple[list[dict], list[str]]:
    """Return (ranked topics, degradation notes) for one brand."""
    notes: list[str] = []
    if search is None:
        if sources.serper_available():
            search = sources.serper_search
        else:
            notes.append("Serper key missing — topics built from Search Console data only")

    # Candidate pool: brand seeds expanded through live Google suggestions, plus
    # question-shaped queries we already get impressions for but don't rank on.
    candidates: dict[str, tuple[str, str]] = {}  # normalized -> (display, source)

    def add(cand: str, source: str) -> None:
        text = " ".join(cand.split()).strip("?. ").strip()
        if 8 <= len(text) <= 90:
            candidates.setdefault(text.lower(), (text, source))

    seed_serps: dict[str, dict] = {}
    for seed in [s for s in brand.get("seeds", []) if s.strip()][:MAX_SEEDS]:
        add(seed, "seed")
        if search:
            try:
                serp = search(seed)
                seed_serps[seed.lower()] = serp
                for cand in serp["related"] + serp["paa"]:
                    add(cand, "google suggestion")
            except CredentialMissing as exc:
                notes.append(f"Serper: {exc}")
                search = None
    for r in rows:
        if r.query.lower().startswith(QUESTION_STARTS) and r.impressions >= 50 and r.position > 10:
            add(r.query, "search data")

    # Score every candidate on volume proxy + trend; SERP-check only the top few.
    scored = []
    for key, (display, source) in candidates.items():
        volume = _match_impressions(display, rows)
        scored.append((volume, key, display, source))
    scored.sort(reverse=True)

    topics: list[dict] = []
    checks = 0
    max_volume = max((v for v, _, _, _ in scored), default=0)
    for volume, key, display, source in scored[: MAX_TOPICS * 2]:
        trend = _trend(display, rows, prev_rows)
        difficulty: str | None = None
        evidence: list[str] = []
        serp = seed_serps.get(key)
        if search and serp is None and checks < max_checks:
            try:
                serp = search(display)
                checks += 1
            except CredentialMissing as exc:
                notes.append(f"Serper: {exc}")
                search = None
        if serp:
            difficulty, evidence = _difficulty(serp, brand["domain"])
        score = (
            0.4 * DIFFICULTY_SCORE[difficulty]
            + 0.35 * TREND_SCORE[trend]
            + 0.25 * (volume / max_volume if max_volume else 0.3)
        )
        if source == "seed":
            score -= 0.08  # the lab's job is discovery — the user already knows their seeds
        topics.append({
            "keyword": display,
            "source": source,
            "angle": _angle(display),
            "volume_est": volume or None,
            "volume_label": f"~{volume:,}/mo (our impressions)" if volume else "interest signal only",
            "trend": trend,
            "difficulty": difficulty,
            "est_monthly_clicks": round(volume * 0.11) if volume else None,
            "why": _why(volume, trend, difficulty, evidence),
            "score": round(score, 3),
        })

    topics.sort(key=lambda t: t["score"], reverse=True)
    return topics[:MAX_TOPICS], notes
