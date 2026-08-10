"""End-to-end pipeline over a frozen fixture SERP — fully offline.

Covers: full analyze (SERP → extract → terms → subtopics → bands → score),
snapshot-pinned deterministic rescore, page-type exclusions (video/product/
own-domain, forum = vocabulary-only), keyless degraded mode, article-share
warning, and the volatility label.
"""
import re

import pytest

from final_geo_agent import opt_pipeline, opt_semantic, opt_serp
from final_geo_agent.opt_serp import SerpData, SerpResult

CONCEPTS = {
    "ratio": ["ratio", "1:8", "part", "parts", "proportion", "measure"],
    "steep": ["steep", "steeping", "hours", "overnight", "twelve", "time"],
    "storage": ["store", "storage", "fridge", "refrigerator", "last", "keep", "airtight", "week"],
    "equipment": ["jar", "filter", "grinder", "cheesecloth", "scale"],
    "generic": ["coffee", "cold", "brew", "water", "make"],
}


class FakeEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            words = set(re.findall(r"[a-z0-9:]+", t.lower()))
            vec = [float(sum(1 for w in triggers if w in words)) for triggers in CONCEPTS.values()]
            out.append(vec if any(vec) else [0.0] * (len(CONCEPTS) - 1) + [1.0])
        return out


def _filler(doc_i: int, sentences: int) -> str:
    return " ".join(
        f"Note {j} for batch {doc_i}: the counter stayed clean on day {j} and the morning felt calm before anyone woke up in house number {doc_i}."
        for j in range(sentences)
    )


def article_html(doc_i: int, pad_sentences: int) -> str:
    return f"""<html lang="en"><body><article>
<h1>Cold Brew Guide Number {doc_i}</h1>
<p>Making cold brew at home is simple once the basics are in place. {_filler(doc_i, pad_sentences)}</p>
<h2>What ratio should I use?</h2>
<p>The golden measure is a 1:8 ratio of coffee to water for guide {doc_i}. Check the proportion with a scale every single batch you run.</p>
<h2>How long should it steep?</h2>
<p>Steep the coffee overnight, twelve hours is the sweet spot in guide {doc_i}. Steeping much past a full day turns the batch woody.</p>
<h2>How long does cold brew last in the fridge?</h2>
<p>Store the finished brew in an airtight bottle in the fridge for guide {doc_i}. It will keep for about a week before it goes flat.</p>
</article></body></html>"""


FORUM_HTML = """<html lang="en"><body><div>
<p>Honestly the immersion method is forgiving, I use a mason vessel and a coarse setting on my burr machine and it comes out fine every run of the season.</p>
<p>My tip for beginners is to taste at hour twelve and pull it when the body feels right for you, the exact timing matters less than people claim online in these threads.</p>
<p>Someone asked about dilution earlier in the thread, I cut mine with equal amounts of cold water and it still tastes strong enough for the whole morning routine.</p>
<p>Another regular here swears by freezing portions in trays, which sounds odd but works well for busy weeks when nobody has patience for a fresh batch at dawn.</p>
<p>The gear debate comes up every month and the answer never changes, any large vessel with a lid does the job and the fancy dedicated brewers mostly buy you convenience at cleanup rather than any real difference in the cup itself.</p>
<p>For anyone lurking and wondering whether the method is worth the counter space, the answer from most of this community is yes, because the result is smoother than anything the hot methods produce on a rushed weekday morning schedule.</p>
</div></body></html>"""


def build_provider(n_articles: int = 9) -> opt_serp.FixtureProvider:
    results, pages = [], {}
    pads = [4, 6, 8, 10, 12, 14, 16, 20, 28]
    for i in range(n_articles):
        url = f"https://site{i}.example.com/cold-brew"
        results.append(SerpResult(rank=i + 1, url=url, title=f"Cold Brew Guide {i}"))
        pages[url] = article_html(i, pads[i % len(pads)])
    results.append(SerpResult(rank=n_articles + 1, url="https://www.youtube.com/watch?v=abc", title="Cold brew video"))
    forum_url = "https://www.reddit.com/r/coffee/comments/xyz/cold_brew"
    results.append(SerpResult(rank=n_articles + 2, url=forum_url, title="r/coffee thread"))
    pages[forum_url] = FORUM_HTML
    results.append(SerpResult(rank=n_articles + 3, url="https://mybrand.example.com/blog/cold-brew", title="Our own guide"))
    results.append(SerpResult(rank=n_articles + 4, url="https://www.amazon.com/dp/B0COLD", title="Cold brew maker"))
    serp = SerpData(keyword="how to make cold brew coffee", locale="en-US", provider="fixture",
                    results=results, paa=["How long does cold brew last?"], aio_present=True)
    return opt_serp.FixtureProvider(serp, pages)


DRAFT = """# My easy cold brew
Cold brew is simple to make at home.

## The right mix
Use one part coffee to eight parts water. A kitchen scale keeps things honest.

## Waiting it out
Let the jar sit overnight. Twelve hours works for most beans.
"""


def run_analyze(draft: str = DRAFT):
    return opt_pipeline.analyze(
        "how to make cold brew coffee", "en-US", draft,
        provider=build_provider(), embedder=FakeEmbedder(),
        own_domain="mybrand.example.com",
    )


def test_end_to_end_analyze():
    doc = run_analyze()
    meta = doc["meta"]
    assert meta["n_docs"] == 9 and meta["volatility"] == "first-analysis"
    assert meta["article_share"] > 0.7 and meta["warnings"] == []

    by_url = {r["url"]: r for r in doc["results"]}
    assert by_url["https://www.youtube.com/watch?v=abc"]["excluded"] == "video result"
    assert by_url["https://mybrand.example.com/blog/cold-brew"]["excluded"] == "own domain"

    # structure bands come from the 9 articles only (forum stays out)
    assert doc["structure_bands"]["word_count"]["n"] == 9
    # forum vocabulary still reached the term corpus: 9 articles + 1 forum = 10 columns
    assert len(doc["term_profile"][0]["counts"]) == 10

    labels = " ".join(s["label"] for s in doc["subtopics"])
    assert len(doc["subtopics"]) >= 3 and "1:8" in labels or "ratio" in labels

    report = doc["last_report"]
    assert 0 <= report["total"] <= 100
    assert report["winners_median"] is not None and report["winners_median"] > report["total"]
    assert any("fridge" in g["message"] for g in report["gaps"])   # storage gap surfaced
    assert doc["disclaimer"].startswith("This score measures")


def test_rescore_is_deterministic_and_pinned():
    doc = run_analyze()
    aid = doc["meta"]["analysis_id"]
    first = doc["last_report"]["total"]
    again = opt_pipeline.rescore(aid, DRAFT, embedder=FakeEmbedder())
    third = opt_pipeline.rescore(aid, DRAFT, embedder=FakeEmbedder())
    assert again["total"] == third["total"] == first

    improved = DRAFT + "\n## Keeping it fresh\nStore the bottle in the fridge, it will keep about a week.\n"
    better = opt_pipeline.rescore(aid, improved, embedder=FakeEmbedder())
    assert better["total"] > first                     # closing the storage gap pays
    assert opt_pipeline.get_analysis(aid)["last_report"]["total"] == better["total"]


def test_keyless_semantic_degrades_honestly(monkeypatch):
    monkeypatch.setattr(opt_semantic.runtime_config, "get", lambda *a, **k: "")
    doc = opt_pipeline.analyze(
        "how to make cold brew coffee", "en-US", DRAFT,
        provider=build_provider(), own_domain="mybrand.example.com",
    )
    assert "semantic_unavailable" in doc["meta"]["degraded"]
    assert any("lexical-only" in w for w in doc["meta"]["warnings"])
    report = doc["last_report"]
    assert report["semantic_coverage"] is None         # never a fake 0
    assert 0 <= report["total"] <= 100                 # lexical blend still works


def test_article_share_warning_on_commercial_serp():
    results = [
        SerpResult(rank=1, url="https://siteA.example.com/cold-brew", title="Guide A"),
        SerpResult(rank=2, url="https://siteB.example.com/cold-brew", title="Guide B"),
    ] + [
        SerpResult(rank=i + 3, url=f"https://shop{i}.example.com/product/brewer-{i}", title=f"Brewer {i}")
        for i in range(6)
    ]
    pages = {
        "https://siteA.example.com/cold-brew": article_html(0, 8),
        "https://siteB.example.com/cold-brew": article_html(1, 10),
    }
    provider = opt_serp.FixtureProvider(
        SerpData(keyword="cold brew maker", locale="en-US", provider="fixture", results=results),
        pages,
    )
    doc = opt_pipeline.analyze("cold brew maker", "en-US", "", provider=provider, embedder=FakeEmbedder())
    assert any("may not want an article" in w for w in doc["meta"]["warnings"])
    assert any("usable winners" in w for w in doc["meta"]["warnings"])   # small-n honesty


def test_second_snapshot_gets_volatility_label():
    run_analyze()
    doc2 = run_analyze()
    assert doc2["meta"]["volatility"] == "stable"      # identical fixture SERP
    index = opt_pipeline.list_analyses()
    assert index[0]["id"] == doc2["meta"]["analysis_id"]
    assert index[0]["score"] == doc2["last_report"]["total"]


def test_page_type_classifier():
    cfg = opt_pipeline.load_config().serp
    assert opt_serp.classify_page_type("https://www.youtube.com/watch?v=1", cfg) == "video"
    assert opt_serp.classify_page_type("https://reddit.com/r/coffee/x", cfg) == "forum"
    assert opt_serp.classify_page_type("https://www.amazon.com/dp/B01", cfg) == "product"
    assert opt_serp.classify_page_type("https://blog.example.com/guide", cfg) == "article"
