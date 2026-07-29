"""Every tunable the pipeline enforces, in one place (spec §3: hardcode containment)."""
DR_THRESHOLD = 70          # citation domains below this are rejected (team step 9)
KEYWORD_COUNT_BONUS = 2    # main-keyword target = top-1 page's count .. count + 2 (team step 4)
TARGET_UPLIFT = 0.15       # word/link targets = competitor average * (1 + uplift) (team step 7)
LSI_COUNT = 10             # LSI/semantic keywords to select (team step 4)
EVALUATOR_MAX_ROUNDS = 3   # outline-vs-competitors revision loop cap (team step 8)
CITATION_MAX_ROUNDS = 3    # citation re-request rounds before honest "short by N" (team step 9)
MIN_LINKS = 3
TOP_N = 3                  # competitor pages analyzed
FREQUENT_TERMS = 15        # density benchmark terms from the #1 page
