from seo_geo_agent.sources import CredentialMissing, PageFacts
from seo_blog_agent import outline, rules

SHEET = {"keyword": "legal virtual assistant",
         "serp": {"paa": ["What does a legal VA do?"], "related": ["legal va cost"], "aio_present": True,
                  "top3": [{"url": "https://a.com/post", "title": "T", "position": 1}]},
         "gap": [{"keyword": "virtual paralegal", "tag": "secondary", "volume": 100, "overlap": 1,
                  "source": "ahrefs_pasted"}],
         "usage": {"main_count_top1": 4, "target_min": 4, "target_max": 6, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}], "degraded": []}


def fake_fetch(url):
    return PageFacts(url=url, status=200, title="Best Legal VAs 2026", meta_description="desc",
                     h1=["Best Legal VAs"], h2=["What is a legal VA", "Costs", "Hiring steps"],
                     h3=["FAQ: What does a legal VA do?"], word_count=2000,
                     text="body " * 500, schema_types=["Article"])


def fake_fetch_raw(url):
    return {"status": 200, "final_url": url,
            "text": '<a href="https://clio.com/report">x</a> <a href="https://a.com/other">i</a>'
                    ' <a href="https://www.abajournal.com/s">y</a>'}


def fake_llm_ok(system, prompt):
    if '"eeat"' in system:
        return {"eeat": True, "key_takeaways": False, "tables": True, "tools": False,
                "lacks": ["no pricing table"]}
    if '"our_score"' in system:
        return {"our_score": 92, "competitor_scores": [80, 75, 70], "beats_all": True, "weaknesses": []}
    if '"title"' in system:
        return {"title": "Legal Virtual Assistant: The 2026 Hiring Guide",
                "description": "How to hire a legal VA.", "slug": "legal-virtual-assistant"}
    return {"outline": [{"heading": "What is a legal virtual assistant?", "level": 2,
                         "note": "answer in 2 sentences", "keywords": ["legal virtual assistant"]}]}


def no_llm(system, prompt):
    raise CredentialMissing("no key")


def test_profile_extracts_structure_and_external_links():
    p = outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                   fetch_raw=fake_fetch_raw, llm=fake_llm_ok)
    assert p["h2"] == ["What is a legal VA", "Costs", "Hiring steps"]
    assert p["external_links"] == 2  # clio.com + abajournal.com; own-domain link excluded
    assert p["features"]["lacks"] == ["no pricing table"]
    assert p["available"] is True


def test_build_outline_targets_and_meta():
    profiles = [outline.competitor_profile(f"https://{d}.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=fake_llm_ok)
                for d in ("a", "b", "c")]
    doc = outline.build_outline(SHEET, profiles, llm=fake_llm_ok)
    assert doc["targets"]["word_count"] == round(2000 * (1 + rules.TARGET_UPLIFT))
    assert doc["targets"]["links"] == max(rules.MIN_LINKS, round(2 * (1 + rules.TARGET_UPLIFT)))
    assert doc["meta"]["slug"] == "legal-virtual-assistant"
    assert doc["evaluator"]["beats_all"] is True
    assert doc["outline"][0]["heading"].startswith("What is")


def test_evaluator_never_lies_when_it_cannot_win():
    calls = {"n": 0}

    def llm(system, prompt):
        if '"our_score"' in system:
            calls["n"] += 1
            return {"our_score": 60, "competitor_scores": [80], "beats_all": False, "weaknesses": ["thin"]}
        return fake_llm_ok(system, prompt)

    profiles = [outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=fake_llm_ok)]
    doc = outline.build_outline(SHEET, profiles, llm=llm)
    assert calls["n"] == rules.EVALUATOR_MAX_ROUNDS
    assert doc["evaluator"]["beats_all"] is False
    assert "honest" in doc["evaluator"]["note"]


def test_llm_down_gives_structural_fallback():
    profiles = [outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=no_llm)]
    doc = outline.build_outline(SHEET, profiles, llm=no_llm)
    assert doc["outline"]  # structural fallback from shared competitor themes
    assert any("skipped" in n or "fallback" in n for n in doc["degraded"])
    assert doc["evaluator"]["beats_all"] is None
