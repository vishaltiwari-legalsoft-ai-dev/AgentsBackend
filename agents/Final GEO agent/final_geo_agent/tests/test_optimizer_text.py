"""Layer 3 text bridge — tokenizer, n-grams, term profile, brand marking."""
from final_geo_agent import opt_text
from final_geo_agent.opt_config import load_config

CFG = load_config().terms


def test_special_tokens_survive_whole():
    counts, surfaces = opt_text.count_terms(
        "Use a 1:8 ratio and steep for 24 hours with 200g of coffee.", CFG
    )
    assert counts["1:8"] == 1
    assert counts["24hours"] == 1 and opt_text.display_form("24hours", surfaces) == "24 hours"
    assert counts["200g"] == 1


def test_lemmatized_counting_displays_surface_form():
    counts, surfaces = opt_text.count_terms(
        "Brewing is easy. I brewed yesterday and she brews daily. Brewing wins.", CFG
    )
    assert counts["brew"] == 4                    # brewing x2 + brewed + brews merge
    assert opt_text.display_form("brew", surfaces) == "brewing"


def test_ngrams_never_edge_on_stopwords():
    counts, _ = opt_text.count_terms("The ratio of water matters for the coffee to water ratio.", CFG)
    assert "ratio of" not in counts               # ends on a stopword
    assert "coffee to water" in counts            # stopword mid-gram is fine


def test_term_profile_rewards_consensus():
    corpus = [
        "Steep time matters. Steep time is the main lever for taste.",
        "Watch your steep time closely, steep time changes everything here.",
        "A long steep time gives body to the final cup of coffee.",
        "Grind size is my only obsession, nothing else matters at all.",
        "Steep time and patience beat any gadget you can buy online.",
    ]
    profile = opt_text.term_profile(corpus, CFG)
    terms = {e["term"]: e for e in profile}
    assert "steep time" in terms
    consensus = terms["steep time"]
    assert consensus["prevalence"] == 0.8
    loner = terms.get("grind size")
    assert loner is None or loner["importance"] < consensus["importance"]


def test_brand_terms_flagged_capitalized_or_gazetteer():
    corpus = [
        "I always buy Starbucks beans because the Starbucks roast is consistent.",
        "My friends swear by Starbucks too, though cold brew from home wins.",
        "Even so, Starbucks makes cold brew approachable for beginners everywhere.",
    ]
    profile = opt_text.term_profile(corpus, CFG, top_n=100)
    flagged = opt_text.brand_terms(profile, corpus)
    flagged_displays = {e["display"] for e in profile if e["term"] in flagged}
    assert "starbucks" in flagged_displays
    assert "cold brew" not in flagged_displays
