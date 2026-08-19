"""Deep-research loop: angle fan-out, evidence ledger, gap-driven rounds."""
from __future__ import annotations

import pytest
from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import research, state

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com"}


def _search(query, client=None):
    slug = "".join(c if c.isalnum() else "-" for c in query)[:40]
    return {
        "organic": [
            {"link": f"https://source.example/{slug}", "title": f"About {query}", "position": 1},
        ],
        "related": [],
        "paa": [],
    }


def _fetch(url, client=None):
    return {"url": url, "title": "A source", "text": f"Long body text from {url}", "status": 200}


def _scripted_llm(evidence_per_call, gaps):
    """Fake llm_json: extraction calls return evidence, the gap call returns gaps."""
    calls = {"n": 0}

    def fake(system, prompt, **kw):
        calls["n"] += 1
        if "gap" in system.lower():
            return {"gaps": gaps}
        return {"evidence": evidence_per_call}

    return fake


def _ev(claim, url="https://source.example/a"):
    return {
        "claim": claim,
        "quote": f"quote for {claim}",
        "url": url,
        "source_name": "Source",
        "source_class": "studies",
        "date": "2026-01-01",
        "credibility": "industry report",
    }


def test_new_run_shape_and_index():
    run = research.new_run(BRAND, "virtual receptionists for law firms")
    assert run["id"].startswith("bw-")
    assert run["status"] == "research"
    assert run["ledger"] == [] and run["rounds"] == []
    assert state.load(f"run-{run['id']}") is not None
    listed = research.list_runs()
    assert listed and listed[0]["id"] == run["id"]


def test_round_one_fans_out_over_all_angles():
    run = research.new_run(BRAND, "legal intake automation")
    seen: list[str] = []

    def spy_search(query, client=None):
        seen.append(query)
        return _search(query)

    run = research.research_step(
        run, search=spy_search, fetch=_fetch,
        llm=_scripted_llm([_ev("claim A")], ["missing pricing data"]),
    )
    assert len(run["rounds"]) == 1
    round_angles = {q["angle"] for q in run["rounds"][0]["queries"]}
    assert round_angles == {a["key"] for a in research.ANGLES}
    assert len(seen) == len(research.ANGLES)
    assert run["ledger"] and run["ledger"][0]["id"] == "ev-1"
    assert run["gaps"] == ["missing pricing data"]


def test_round_two_queries_come_from_gaps():
    run = research.new_run(BRAND, "legal intake automation")
    run = research.research_step(
        run, search=_search, fetch=_fetch,
        llm=_scripted_llm([_ev("claim A")], ["law firm intake cost statistics"]),
    )
    seen: list[str] = []

    def spy_search(query, client=None):
        seen.append(query)
        return _search(query)

    run = research.research_step(
        run, search=spy_search, fetch=_fetch,
        llm=_scripted_llm([_ev("claim B", "https://other.example/b")], []),
    )
    assert seen == ["law firm intake cost statistics"]
    assert len(run["rounds"]) == 2


def test_duplicate_evidence_not_readded_and_saturation_flips():
    run = research.new_run(BRAND, "legal intake automation")
    llm = _scripted_llm([_ev("claim A")], ["gap one"])
    run = research.research_step(run, search=_search, fetch=_fetch, llm=llm)
    assert len(run["ledger"]) == 1
    # Same claim+url extracted again → zero new items → saturated.
    run = research.research_step(run, search=_search, fetch=_fetch, llm=llm)
    assert len(run["ledger"]) == 1
    assert run["status"] == "saturated"


def test_round_cap_flips_status_capped():
    run = research.new_run(BRAND, "legal intake automation")
    for i in range(research.ROUND_CAP):
        run = research.research_step(
            run, search=_search, fetch=_fetch,
            llm=_scripted_llm([_ev(f"claim {i}", f"https://s.example/{i}")], [f"gap {i}"]),
        )
    assert run["status"] == "capped"
    # "Go deeper" is still allowed after the cap.
    run = research.research_step(
        run, search=_search, fetch=_fetch,
        llm=_scripted_llm([_ev("late claim", "https://s.example/late")], []),
    )
    assert len(run["rounds"]) == research.ROUND_CAP + 1


def test_no_search_and_no_key_raises_credential_missing():
    run = research.new_run(BRAND, "legal intake automation")
    with pytest.raises(CredentialMissing):
        research.research_step(run, fetch=_fetch, llm=_scripted_llm([], []))


def test_mini_research_appends_targeted_evidence():
    run = research.new_run(BRAND, "legal intake automation")
    added = research.mini_research(
        run, ["average intake call cost"],
        search=_search, fetch=_fetch,
        llm=_scripted_llm([_ev("intake calls cost $12", "https://s.example/cost")], []),
    )
    assert added and added[0]["id"] == "ev-1"
    assert research.load_run(run["id"])["ledger"] == added


def test_the_run_index_trims_to_the_newest_per_owner(monkeypatch):
    """``runs-index`` is ONE Firestore document that every save appends to, and
    ``state.save`` here is not best-effort: once the doc crosses 1 MiB the write
    raises and the agent stops persisting runs permanently. The trim is per
    owner so one busy user cannot evict a colleague's runs from the only list
    ``list_runs`` reads."""
    monkeypatch.setattr(research, "INDEX_CAP_PER_USER", 3)

    colleague = research.new_run(BRAND, "colleague topic", user_id="u2")
    mine = [research.new_run(BRAND, f"topic {i}", user_id="u1") for i in range(5)]

    rows = state.load("runs-index")["runs"]
    assert [r["id"] for r in rows] == [m["id"] for m in reversed(mine[-3:])] + [colleague["id"]]
    assert sum(1 for r in rows if r["user_id"] == "u1") == 3  # oldest two dropped
    assert colleague["id"] in {r["id"] for r in rows}, "a busy user evicted another owner's run"

