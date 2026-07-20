"""All tunables in one place. Runtime overrides (Firestore via store.load_config)
merge over DEFAULTS — no magic numbers anywhere else in the package."""

from __future__ import annotations

DEFAULTS: dict = {
    # --- P1 content score weights (must sum to 1.0) ---
    "w_term_coverage": 0.40,
    "w_topics": 0.25,
    "w_structure": 0.20,
    "w_depth": 0.15,
    # --- P1 pipeline ---
    "serp_top_n": 10,          # organic results considered
    "min_pages": 6,            # benchmark fails below this after filter+crawl
    "top3_weight": 2.0,        # rank 1-3 weight multiplier in term stats
    "max_terms": 40,           # term targets kept per benchmark
    "stuffing_zero_multiple": 2.0,  # term credit hits 0 at max_count * this
    # --- P2 GEO score weights (must sum to 1.0) ---
    "g_mention": 0.30,
    "g_citation": 0.30,
    "g_accuracy": 0.25,
    "g_sov": 0.15,
    # engine key -> OpenRouter model id ("" disables; ai_overview uses SerpAPI)
    "geo_engines": {
        "gpt": "openai/gpt-4o:online",
        "gemini": "google/gemini-2.5-flash:online",
        "perplexity": "perplexity/sonar",
        "ai_overview": "serpapi",
    },
    # --- brands: {slug: {name, domain, competitors: [str], questions: [str]}} ---
    "brands": {},
}


def effective_config(overrides: dict | None = None) -> dict:
    cfg = {**DEFAULTS}
    for key, value in (overrides or {}).items():
        if key in DEFAULTS and value is not None:
            cfg[key] = value
    return cfg
