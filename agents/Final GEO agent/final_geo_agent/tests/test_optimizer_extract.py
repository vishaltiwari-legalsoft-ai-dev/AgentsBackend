"""Layer 2 extraction — offline tests over frozen HTML fixtures.

Covers the spec's required edge cases: div-soup, comment-heavy page,
JSON-LD-only recipe, interstitial wall, syndicated duplicates, plus exact
DOM feature measurement and language-mismatch dropping.
"""
import pathlib

from final_geo_agent import opt_extract
from final_geo_agent.opt_config import load_config

HTML_DIR = pathlib.Path(__file__).parent / "fixtures" / "html"
CFG = load_config().extract


def load(name: str) -> str:
    return (HTML_DIR / name).read_text(encoding="utf-8")


def test_features_measured_from_dom():
    doc = opt_extract.extract_document(
        load("features.html"), CFG, url="https://coffeelab.example.com/cold-brew-guide"
    )
    f = doc.features
    assert doc.usable and not doc.flags
    assert f["h1_count"] == 1 and f["h2_count"] == 4 and f["h3_count"] == 1
    assert f["question_headings"] == 2
    assert f["image_count"] == 2          # decorative 40px + data: URI don't count
    assert f["list_count"] == 2 and f["table_count"] == 1
    assert f["external_links"] == 2 and f["internal_links"] >= 2
    assert f["numeric_density"] > 1       # 1:8, 200g, 12/24 hours...
    assert 150 < f["word_count"] < 500
    assert f["paragraph_count"] >= 4 and f["avg_sentence_len"] > 5


def test_sections_follow_headings():
    doc = opt_extract.extract_document(load("features.html"), CFG)
    headings = [s.heading for s in doc.sections]
    assert any(h.endswith("?") for h in headings)
    ratio_section = next(s for s in doc.sections if s.heading == "What ratio should I use?")
    assert "1:8" in ratio_section.text


def test_divsoup_survives_via_density_fallback():
    doc = opt_extract.extract_document(load("divsoup.html"), CFG)
    assert doc.usable
    assert "immersion" in doc.text            # real prose kept
    assert "Shop All Products" not in doc.text  # link-dense chrome dropped
    assert "Big summer promo" not in doc.text


def test_comment_section_cut():
    doc = opt_extract.extract_document(load("comments.html"), CFG)
    assert doc.usable
    assert "pint jar" in doc.text
    assert "Totally trying this tomorrow" not in doc.text
    assert doc.features["word_count"] < 350   # article only, not the 6 comments


def test_jsonld_recipe_merged_and_typed():
    doc = opt_extract.extract_document(load("jsonld_recipe.html"), CFG)
    assert "Recipe" in doc.schema_types
    assert "coarsely ground coffee" in doc.text   # substance lived only in JSON-LD
    assert doc.usable                              # merge lifts it over the thin bar


def test_interstitial_flagged_unusable():
    doc = opt_extract.extract_document(load("interstitial.html"), CFG)
    assert "interstitial" in doc.flags
    assert not doc.usable


def test_language_mismatch_dropped():
    doc = opt_extract.extract_document(load("german.html"), CFG, expected_lang="en")
    assert doc.lang == "de"
    assert "language_mismatch" in doc.flags
    assert not doc.usable


def test_syndicated_copies_deduped():
    docs = [
        opt_extract.extract_document(load("syndicated_a.html"), CFG),
        opt_extract.extract_document(load("syndicated_b.html"), CFG),
    ]
    docs = opt_extract.drop_duplicates(docs, CFG)
    assert docs[0].usable
    assert not docs[1].usable and "duplicate" in docs[1].flags


def test_unrelated_pages_not_deduped():
    docs = [
        opt_extract.extract_document(load("features.html"), CFG),
        opt_extract.extract_document(load("divsoup.html"), CFG),
    ]
    assert opt_extract.near_duplicates(docs, CFG) == []


def test_language_detector_heuristic():
    assert opt_extract.detect_language("the ratio of coffee to water is key and it is easy") == "en"
    assert opt_extract.detect_language("el café frío es una de las bebidas que preparamos en casa para el verano") == "es"
