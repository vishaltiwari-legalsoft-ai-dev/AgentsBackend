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

MAX_SEEDS = 5          # Serper EXPANSION cap (cost: 5 seeds + 12 checks ≈ $0.01/run) — every user seed is still a candidate
MAX_SERP_CHECKS = 12
MAX_TOPICS = 15
MAX_LIVE_TOPICS = 10   # avoided (cannibalizing) topics ride along beyond this cap — never hidden
CANNIBAL_OVERLAP = 0.8
TREND_SCORE = {"rising": 1.0, "new": 0.7, "flat": 0.5, "falling": 0.2}
DIFFICULTY_SCORE = {"easy win": 1.0, "medium": 0.6, "hard": 0.25, None: 0.5}

# Topic `source` values — persisted on run.topics and read by the console.
SRC_SEED = "seed"                        # the user typed it: a hard requirement
SRC_SITE_GAP = "site gap"                # site review: buyers want it, no page covers it
SRC_SUGGESTED_SEED = "suggested seed"    # site review's buyer-query suggestion (no protection)
SRC_GOOGLE = "google suggestion"         # Serper related/PAA for one of the seeds
SRC_SEARCH_DATA = "search data"          # our own Search Console question, ranking past page 1
SRC_IDEA = "new idea"                    # model ideation, unverified
SRC_COMPETITOR = "competitor-content"    # a rival publishes on it

# Who earns one of the scarce SERP checks first. Deliberately not alphabetical:
# sorting on (volume, key) let generic "why/who/what…" questions win every tie.
SOURCE_PRIORITY = {
    SRC_SEED: 0, SRC_SITE_GAP: 1, SRC_SUGGESTED_SEED: 2,
    SRC_GOOGLE: 3, SRC_SEARCH_DATA: 3, SRC_IDEA: 4, SRC_COMPETITOR: 5,
}
# A competitor gap topic never outranks what the brand or its own site review asked for.
CLAMP_ANCHORS = (SRC_SEED, SRC_SITE_GAP, SRC_SUGGESTED_SEED)

NO_REVIEW_NOTE = "Topics aren't checked against what you sell yet — run Analyze website."
EMPTY_REVIEW_NOTE = (
    "Your site review lists no covered or missing topics, so the blog plan isn't checked "
    "against what you sell — re-run Analyze website."
)


def _tokens(text: str) -> set[str]:
    return {w for w in text.lower().split() if w not in STOPWORDS and len(w) > 2}


def _token_overlap(cand: set[str], other: set[str]) -> bool:
    """True when `cand` is (near-)covered by `other`: a subset, or sharing >=80% of its tokens."""
    if not cand or not other:
        return False
    if cand <= other:
        return True
    shared = cand & other
    return (len(shared) / len(cand)) >= CANNIBAL_OVERLAP


def _cannibalized_by(display: str, corpus_pages: list[dict] | None) -> str | None:
    """Return the URL of a corpus page this candidate would cannibalize, or None."""
    if not corpus_pages:
        return None
    cand = _tokens(display)
    if not cand:
        return None
    for page in corpus_pages:
        for field in ("target_query", "title"):
            if _token_overlap(cand, _tokens(page.get(field) or "")):
                return page.get("url")
    return None


def _query_counts_for(cand: set[str], q: set[str]) -> bool:
    """A query counts toward a topic only when it contains every topic word.

    The old reverse direction (query ⊆ topic) let one short, high-impression
    query inflate every longer topic that mentions it; and a one-word topic would
    soak up every query using that word — so it matches its exact query only.
    """
    if not cand or not q:
        return False
    if cand == q:
        return True
    return len(cand) >= 2 and cand <= q


def _match_impressions(candidate: str, rows: list[QueryStat]) -> int:
    """Impressions of GSC queries that contain the candidate topic."""
    cand = _tokens(candidate)
    if not cand:
        return 0
    return sum(r.impressions for r in rows if _query_counts_for(cand, _tokens(r.query)))


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


def _impact(angle: str) -> str:
    """How this topic grows footfall — the reader's 'why bother' in one line."""
    return {
        "pricing guide": "Catches buyers comparing costs — bottom-funnel traffic that converts.",
        "comparison": "Wins people choosing between options — high-intent visits.",
        "FAQ answer": "Answers real questions — steady search traffic plus AI-answer citations.",
        "how-to guide": "Builds topical authority — compounds into rankings for the whole cluster.",
    }[angle]


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


def _provenance(source: str, from_seed: str | None, seed_is_users: bool) -> str:
    """Where this topic truly came from — one source, one true sentence."""
    if source == SRC_GOOGLE:
        if not from_seed:
            return "Google shows this as a related search"
        whose = "your seed" if seed_is_users else "the site review's suggested seed"
        return f'Google suggests this for {whose} "{from_seed}"'
    return {
        SRC_SEED: "You listed this as a seed keyword",
        SRC_SUGGESTED_SEED: "Your site review suggested this as a query your buyers type",
        SRC_SITE_GAP: "Your site review found buyers looking for this and no page covers it",
        SRC_SEARCH_DATA: "Your site already appears in Google for this question, but past page 1",
        SRC_COMPETITOR: "A competitor is publishing on this topic",
        SRC_IDEA: "AI-suggested",
    }.get(source, "Topic candidate")


def _why(source: str, from_seed: str | None, seed_is_users: bool,
         volume: int, trend: str, difficulty: str | None, evidence: list[str]) -> str:
    parts = []
    if volume:
        parts.append(f"~{volume:,} impressions/mo already visible to us")
    trend_text = {"rising": "interest is rising", "new": "newly appearing searches",
                  "falling": "interest is cooling", "flat": ""}[trend]
    if trend_text:
        parts.append(trend_text)
    if difficulty:
        parts.append(difficulty if not evidence else f"{difficulty}: {'; '.join(evidence)}")
    lead = _provenance(source, from_seed, seed_is_users)
    if not parts and source not in (SRC_GOOGLE, SRC_SEARCH_DATA):
        # Nothing from Google stands behind it yet — say so instead of implying it.
        lead += " — not yet checked against Google"
    return " · ".join([lead] + parts)


def _volume_label(volume: int, source: str) -> str:
    if volume:
        return f"~{volume:,}/mo (our impressions)"
    if source == SRC_GOOGLE:
        return "interest signal only"   # Google itself surfaces the phrase; no volume figure
    return "no search data yet"


def _clean_strings(value) -> list[str]:
    """A review field as a list of non-empty strings — older/malformed reviews degrade to []."""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _seed_lists(brand: dict) -> tuple[list[str], list[str]]:
    """(typed seeds, suggested seeds). `seed_entries` comes from
    `site_brain.effective_seeds`; a plain brand's `seeds` are all typed."""
    entries = brand.get("seed_entries")
    if entries is None:
        entries = [{"text": s, "source": SRC_SEED} for s in brand.get("seeds") or []]
    typed: list[str] = []
    suggested: list[str] = []
    for entry in entries:
        text = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        (typed if entry.get("source") == SRC_SEED else suggested).append(text)
    return typed, suggested


def build_topics(
    brand: dict,
    rows: list[QueryStat],
    prev_rows: list[QueryStat],
    search=None,
    max_checks: int = MAX_SERP_CHECKS,
    corpus_pages: list[dict] | None = None,
    competitor_topics: list[str] | None = None,
    site_review: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Return (ranked topics, degradation notes) for one brand.

    Seeds: `brand["seed_entries"]` (from `site_brain.effective_seeds`) separates
    the user's typed seeds — hard requirements: every one is a candidate and keeps
    at least one live topic — from the site review's suggested seeds, which get
    no protection. A plain brand's `seeds` are all treated as typed.

    `site_review` (the latest `sitereview-{id}` doc) adds its `missing_topics` as
    "site gap" candidates and uses its `covered_topics` to drop candidates the site
    already covers; `positioning` rides along for the later fit-judge phase and is
    not read yet. No review (or one with neither list) -> a visible note.

    `corpus_pages` (our own crawled/summarised pages) grounds the anti-cannibalization
    check; `competitor_topics` (flattened hot-topics + recent-post topics from
    competitor profiles) seeds gap candidates tagged source="competitor-content".
    All optional, offline data.
    """
    from . import keywords  # lazy: keywords.py imports from this module at load time

    notes: list[str] = []
    if search is None:
        if sources.serper_available():
            search = sources.serper_search
        else:
            notes.append("Serper key missing — topics built from Search Console data only")

    review = site_review if isinstance(site_review, dict) else {}
    site_gaps = _clean_strings(review.get("missing_topics"))
    covered = [t for t in (_tokens(c) for c in _clean_strings(review.get("covered_topics"))) if t]
    if not review:
        notes.append(NO_REVIEW_NOTE)
    elif not site_gaps and not covered:
        notes.append(EMPTY_REVIEW_NOTE)

    # Candidate pool. `candidates` keeps its (display, source) shape because the
    # ideation prompt below quotes it. Insertion runs in SOURCE_PRIORITY order, so
    # the first-come dedupe always keeps the strongest source for a phrase — a
    # later seed can no longer be swallowed as an earlier seed's Google suggestion.
    candidates: dict[str, tuple[str, str]] = {}  # normalized -> (display, source)
    order: dict[str, int] = {}                   # normalized -> insertion index (stable tie-break)
    parent: dict[str, str] = {}                  # normalized -> normalized seed it was expanded from

    def add(cand: str, source: str, from_key: str | None = None) -> str | None:
        """Add one candidate; return its key when it is (now or already) in the pool."""
        text = " ".join(cand.split()).strip("?. ").strip()
        if not text:
            return None
        key = text.lower()
        if key in candidates:
            return key
        if source != SRC_SEED:  # the user's own words are never filtered out
            if not 8 <= len(text) <= 90:
                return None
            cand_tokens = _tokens(text)
            if any(_token_overlap(cand_tokens, c) for c in covered):
                return None     # the site review says the site already covers this
        candidates[key] = (text, source)
        order[key] = len(order)
        if from_key:
            parent[key] = from_key
        return key

    typed, suggested = _seed_lists(brand)
    user_seed_keys = list(dict.fromkeys(k for k in (add(s, SRC_SEED) for s in typed) if k))
    for gap in site_gaps:
        add(gap, SRC_SITE_GAP)
    suggested_keys = [k for k in (add(s, SRC_SUGGESTED_SEED) for s in suggested) if k]

    # Google expansion stays capped for cost; typed seeds expand first.
    seed_serps: dict[str, dict] = {}
    for key in list(dict.fromkeys(user_seed_keys + suggested_keys))[:MAX_SEEDS]:
        if not search:
            break
        try:
            serp = search(candidates[key][0])
        except CredentialMissing as exc:
            notes.append(f"Serper: {exc}")
            search = None
            break
        seed_serps[key] = serp
        for cand in serp["related"] + serp["paa"]:
            add(cand, SRC_GOOGLE, from_key=key)
    # Question-shaped queries we already get impressions for but don't rank on.
    for r in rows:
        if r.query.lower().startswith(QUESTION_STARTS) and r.impressions >= 50 and r.position > 10:
            add(r.query, SRC_SEARCH_DATA)

    # Fresh ideation: topics the brand does NOT already touch — new ground, not
    # a rearrangement of what the site/searches surfaced.
    try:
        ideas = sources.llm_json(
            "You are a content strategist. Answer with a JSON array of strings only.",
            f"Suggest 12 NEW blog topics (plain keyword phrases, not clickbait titles) that would bring "
            f"qualified visitors to {brand.get('name', brand['domain'])} ({brand['domain']}). Avoid anything "
            f"close to these existing candidates: {list(candidates.values())[:40]}. Mix buyer-intent and "
            f"question topics.",
        )
        if isinstance(ideas, list):
            for idea in ideas:
                if isinstance(idea, str):
                    add(idea, SRC_IDEA)
    except CredentialMissing as exc:
        notes.append(f"New-topic ideation skipped: {exc}")

    # Competitor gaps: topics rivals cover that we don't yet — from Task 5's
    # competitor-profiles doc (hot_topics + recent-post topics, flattened by the caller).
    for topic in competitor_topics or []:
        if isinstance(topic, str) and topic.strip():
            add(topic, SRC_COMPETITOR)

    # Pre-select who gets scored (and the scarce SERP checks): source priority,
    # then our own impressions, then insertion order — never the alphabet.
    # Every typed seed is always in.
    volumes = {key: _match_impressions(display, rows) for key, (display, _) in candidates.items()}
    ordered = sorted(candidates, key=lambda k: (
        SOURCE_PRIORITY.get(candidates[k][1], len(SOURCE_PRIORITY)), -volumes[k], order[k]))
    user_set = set(user_seed_keys)
    picked = [k for k in ordered if k in user_set]
    picked += [k for k in ordered if k not in user_set][: max(0, MAX_TOPICS * 2 - len(picked))]

    keyed: list[tuple[str, dict]] = []
    checks = 0
    max_volume = max(volumes.values(), default=0)
    for key in picked:
        display, source = candidates[key]
        volume = volumes[key]
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
        angle = _angle(display)
        avoided_url = _cannibalized_by(display, corpus_pages)
        seed_key = parent.get(key)
        from_seed = candidates[seed_key][0] if seed_key else None
        keyed.append((key, {
            "keyword": display,
            "source": source,
            "from_seed": from_seed,
            "impact": _impact(angle),
            "angle": angle,
            "intent": keywords.intent_of(display),
            "volume_est": volume or None,
            "volume_label": _volume_label(volume, source),
            "trend": trend,
            "difficulty": difficulty,
            "est_monthly_clicks": round(volume * 0.11) if volume else None,
            "why": _why(source, from_seed, seed_key in user_set, volume, trend, difficulty, evidence),
            "score": round(score, 3),
            "avoided": bool(avoided_url),
            "avoided_reason": f"overlaps {avoided_url}" if avoided_url else None,
        }))
    topics = [t for _, t in keyed]

    # Competitor-content candidates are a gap signal, not a mandate — never let
    # one outrank what the brand asked for (its seeds) or what its own site review
    # grounded (site gaps, suggested seeds), typed seeds or not.
    anchor_scores = [t["score"] for t in topics if t["source"] in CLAMP_ANCHORS]
    if anchor_scores:
        floor = min(anchor_scores)
        # Clamped topics still need relative order among themselves — step each
        # one down a notch further than the last, ranked by its pre-clamp score,
        # so a batch of gap topics doesn't all collapse to one identical value.
        to_clamp = sorted(
            (t for t in topics if t["source"] == SRC_COMPETITOR and t["score"] >= floor),
            key=lambda t: t["score"], reverse=True,
        )
        for i, t in enumerate(to_clamp):
            t["score"] = round(floor - 0.001 - i * 0.001, 3)

    # Priority is a display label derived from score — compute it last, after the
    # competitor clamp above has had its final say, so a clamped topic never shows
    # a priority earned by its pre-clamp score.
    for t in topics:
        s = t["score"]
        t["priority"] = "high" if s >= 0.62 else "medium" if s >= 0.45 else "low"

    # Avoided (cannibalizing) topics are never hidden (a9 rule) but always sort
    # after live ones, and only live topics count against the display cap.
    live = sorted((kt for kt in keyed if not kt[1]["avoided"]), key=lambda kt: kt[1]["score"], reverse=True)
    avoided = sorted((t for t in topics if t["avoided"]), key=lambda t: t["score"], reverse=True)

    # Every typed seed keeps at least one live topic — itself, or its best Google
    # child — even where the cap would cut it: the cap makes room by dropping the
    # lowest-scored unprotected topics. A seed with no live family (it overlaps an
    # existing page) still shows, in the avoided list.
    shown = {id(t) for _, t in live[:MAX_LIVE_TOPICS]}
    keep: list[dict] = []
    for seed_key in user_seed_keys:
        family = [t for k, t in live if k == seed_key or parent.get(k) == seed_key]
        if not family:
            continue
        pick = (next((t for t in family if id(t) in shown), None)
                or next((t for k, t in live if k == seed_key), None)
                or family[0])
        if all(pick is not t for t in keep):
            keep.append(pick)
    kept = {id(t) for t in keep}
    room = max(0, MAX_LIVE_TOPICS - len(keep))
    chosen = kept | {id(t) for t in [t for _, t in live if id(t) not in kept][:room]}
    return [t for _, t in live if id(t) in chosen] + avoided, notes
