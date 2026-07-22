"""Keyword lab — expand seeds into long-tail keywords, cluster by topic+intent,
and score clusters by ROI opportunity (volume proxy × how uncovered we are).

LLM does expansion and clustering when available; a deterministic heuristic
covers offline/degraded runs so the lab never returns nothing.
"""
from __future__ import annotations

from datetime import date

from . import sources, state
from .sources import CredentialMissing, QueryStat
from .topics import _match_impressions, _tokens

INTENTS = ("informational", "commercial", "transactional", "navigational", "local")
MAX_KEYWORDS = 200
MAX_CLUSTERS = 25
MAX_SEED_SERPS = 5

_TRANSACTIONAL = ("hire", "buy", "pricing", "price", "cost", "service", "services", "outsourc", "quote", "demo")
_COMMERCIAL = ("best", " vs ", "review", "top ", "compare", "alternative")
_LOCAL = ("near me", "in the us", "california", "los angeles", "usa")
_QUESTION = ("how", "what", "why", "when", "which", "can", "do", "does", "is", "are", "should", "guide")


def intent_of(keyword: str) -> str:
    k = f" {keyword.lower()} "
    if any(t in k for t in _LOCAL):
        return "local"
    if any(t in k for t in _TRANSACTIONAL):
        return "transactional"
    if any(t in k for t in _COMMERCIAL):
        return "commercial"
    if keyword.lower().startswith(_QUESTION) or keyword.strip().endswith("?"):
        return "informational"
    return "informational"


def expand_keywords(brand: dict, rows: list[QueryStat], search=None) -> tuple[list[str], list[str]]:
    """Seeds -> long-tail pool. Sources: Serper related+PAA, the LLM, and our
    own GSC queries. Returns (keywords, degradation notes)."""
    notes: list[str] = []
    pool: dict[str, str] = {}

    def add(kw: str) -> None:
        text = " ".join(kw.split()).strip("?. ").strip()
        if 6 <= len(text) <= 90:
            pool.setdefault(text.lower(), text)

    seeds = [s.strip() for s in brand.get("seeds", []) if s.strip()]
    for s in seeds:
        add(s)

    if search is None and sources.serper_available():
        search = sources.serper_search
    if search:
        for seed in seeds[:MAX_SEED_SERPS]:
            try:
                serp = search(seed)
                for kw in serp["related"] + serp["paa"]:
                    add(kw)
            except CredentialMissing as exc:
                notes.append(f"Serper: {exc}")
                search = None
                break
    else:
        notes.append("Serper key missing — expansion limited to the LLM and our own search data")

    try:
        generated = sources.llm_json(
            "You are an SEO keyword researcher. Answer with a JSON array of strings only.",
            f"Generate up to 120 long-tail keyword variations a US buyer or researcher would "
            f"actually search, covering informational, commercial, transactional and local intent, "
            f"for this business: {brand['name']} ({brand['domain']}). Seed topics: {', '.join(seeds)}.",
        )
        if isinstance(generated, list):
            for kw in generated:
                if isinstance(kw, str):
                    add(kw)
    except CredentialMissing as exc:
        notes.append(f"LLM expansion skipped: {exc}")

    for r in rows:
        if r.impressions >= 20:
            add(r.query)

    return list(pool.values())[:MAX_KEYWORDS], notes


def _heuristic_clusters(keywords: list[str]) -> list[dict]:
    """Greedy token-overlap grouping: same parent topic ≈ shares 2+ meaningful tokens."""
    clusters: list[dict] = []
    for kw in sorted(keywords, key=len):
        toks = _tokens(kw)
        home = None
        for c in clusters:
            if len(toks & c["_tokens"]) >= 2:
                home = c
                break
        if home:
            home["keywords"].append(kw)
            home["_tokens"] |= toks
        else:
            clusters.append({"name": kw, "keywords": [kw], "_tokens": set(toks)})
    for c in clusters:
        del c["_tokens"]
        c["intent"] = intent_of(c["name"])
    return clusters


def cluster_keywords(brand: dict, keywords: list[str]) -> tuple[list[dict], list[str]]:
    """Group keywords into parent-topic clusters with a dominant intent."""
    notes: list[str] = []
    try:
        raw = sources.llm_json(
            "You are an SEO strategist. Answer with JSON only: a list of objects "
            '{"name": str, "intent": one of ' + str(list(INTENTS)) + ', "keywords": [str]}.',
            f"Cluster these keywords for {brand['name']} ({brand['domain']}) into at most "
            f"{MAX_CLUSTERS} parent topics by search intent. Every keyword appears in exactly "
            f"one cluster. Keywords: {keywords}",
        )
        clusters = [
            {
                "name": str(c.get("name", ""))[:80],
                "intent": c.get("intent") if c.get("intent") in INTENTS else intent_of(str(c.get("name", ""))),
                "keywords": [str(k) for k in c.get("keywords", []) if isinstance(k, str)],
            }
            for c in raw
            if isinstance(c, dict) and c.get("keywords")
        ]
        if clusters:
            return clusters[:MAX_CLUSTERS], notes
        notes.append("LLM returned no usable clusters — heuristic grouping used")
    except CredentialMissing as exc:
        notes.append(f"LLM clustering skipped ({exc}) — heuristic grouping used")
    return _heuristic_clusters(keywords)[:MAX_CLUSTERS], notes


def score_clusters(clusters: list[dict], rows: list[QueryStat], ranks: dict[str, float] | None = None) -> None:
    """Volume proxy + our coverage -> opportunity. Mutates clusters in place.

    Coverage is honest about its basis: our own Search Console positions when we
    have them, live rank-snapshot positions otherwise — never a made-up
    authority score. gap = nothing of ours ranks top-20 for the cluster;
    weak = we rank but below the fold.
    """
    ranks = {k.lower(): v for k, v in (ranks or {}).items() if v}
    for c in clusters:
        volume = sum(_match_impressions(kw, rows) for kw in c["keywords"][:15])
        matched_positions = [
            r.position
            for kw in c["keywords"][:15]
            for r in rows
            if (_tokens(kw) <= _tokens(r.query) or (_tokens(r.query) and _tokens(r.query) <= _tokens(kw)))
            and r.position > 0
        ]
        if not matched_positions and ranks:
            matched_positions = [
                ranks[kw.lower()] for kw in [c["name"], *c["keywords"][:15]] if kw.lower() in ranks
            ]
        best = round(min(matched_positions), 1) if matched_positions else None
        if best is None or best > 20:
            coverage, factor = "gap", 1.0
        elif best > 8:
            coverage, factor = "weak", 0.6
        else:
            coverage, factor = "ranking", 0.25
        c["volume_est"] = volume
        c["best_position"] = best
        c["coverage"] = coverage
        c["opportunity"] = round((volume or 10) * factor, 1)
        c["tier"] = _tier(c)
        c["recommendation"] = _recommendation(c)
    clusters.sort(key=lambda c: ({"high": 0, "medium": 1, "watch": 2}[c["tier"]], -c["opportunity"]))


_BUYER_INTENTS = ("transactional", "commercial", "local")


def _tier(c: dict) -> str:
    """high = act now, medium = plan it, watch = already ranking, defend it."""
    if c["coverage"] == "ranking":
        return "watch"
    if c["intent"] in _BUYER_INTENTS:
        return "high"
    return "medium" if c["coverage"] == "gap" else "medium"


def _recommendation(c: dict) -> str:
    buyer = c["intent"] in _BUYER_INTENTS
    if c["coverage"] == "gap":
        return (
            "Buyers search this and we have no page in the top 20 — create a dedicated page this month."
            if buyer else
            "We're invisible here — one strong article claims this topic."
        )
    if c["coverage"] == "weak":
        return (
            f"We rank #{c['best_position']} but below the fold — refreshing the existing page is the "
            "fastest win in this tier." if buyer else
            f"Ranks #{c['best_position']} — improve the existing content to reach page-1 visibility."
        )
    return "Already ranking — keep the page fresh and defend it; no new work needed."


def add_cluster_owners(clusters: list[dict], own_domain: str, search, max_checks: int = 10) -> None:
    """For the top clusters, record who actually owns the SERP (competitor leverage)."""
    from .sources import domain_of

    checks = 0
    for c in clusters:
        if checks >= max_checks or c["tier"] == "watch":
            continue
        try:
            serp = search(c["name"])
        except CredentialMissing:
            return
        checks += 1
        owners: list[str] = []
        for r in serp["organic"]:
            d = domain_of(r["link"])
            if d and d != own_domain and d not in owners:
                owners.append(d)
        c["owned_by"] = owners[:3]
        c["aio_present"] = serp["aio_present"]


def run_keyword_lab(
    brand: dict, rows: list[QueryStat], search=None, trigger: str = "manual",
    extra_notes: list[str] | None = None,
) -> dict:
    """Full pass: expand -> cluster -> score -> persist. Returns the stored doc."""
    keywords, notes = expand_keywords(brand, rows, search=search)
    notes = list(extra_notes or []) + notes
    clusters, cluster_notes = cluster_keywords(brand, keywords)
    notes.extend(cluster_notes)
    ranks: dict[str, float] = {}
    if not rows:  # no Search Console — fall back to the latest rank snapshot
        snaps = (state.load(f"ranks-{brand['id']}") or {}).get("snapshots", [])
        if snaps:
            ranks = {k: e.get("position") for k, e in snaps[-1]["ranks"].items()}
    score_clusters(clusters, rows, ranks)
    owner_search = search or (sources.serper_search if sources.serper_available() else None)
    if owner_search:
        add_cluster_owners(clusters, brand["domain"], owner_search)
    doc = {
        "brand_id": brand["id"],
        "at": date.today().isoformat(),
        "trigger": trigger,
        "keyword_count": len(keywords),
        "degraded": notes,
        "clusters": clusters,
        "gaps": [c["name"] for c in clusters if c["coverage"] == "gap"],
    }
    state.save(f"keywords-{brand['id']}", doc)
    return doc


def latest(brand_id: str) -> dict | None:
    return state.load(f"keywords-{brand_id}")
