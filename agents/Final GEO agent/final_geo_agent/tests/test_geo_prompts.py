"""Prompt intake — bulk paste, personas, transactional writes, universe cap.

Offline, against the local-file state adapter; the only fake is ``llm_json``.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from final_geo_agent import geo_prompts, geo_store
from seo_geo_agent import state

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}
BID = BRAND["id"]

PERSONAS = [
    {"label": "Solo attorney", "description": "Runs a one-lawyer practice, no admin staff"},
    {"label": "Firm administrator", "description": "Buys tooling for a 20-lawyer firm"},
]

QUESTION = "Which legal intake service is best?"


def seed(n):
    prompts = [
        {"id": f"p{i}", "text": f"best legal va provider {i}", "intent": "category",
         "stage": "consideration", "enabled": True, "source": "ai"}
        for i in range(n)
    ]
    geo_prompts.save_universe(BID, prompts)
    return prompts


def fake_llm(monkeypatch, prompts):
    calls = []

    def llm(system, prompt, **_kw):
        calls.append((system, prompt))
        return {"prompts": prompts}

    monkeypatch.setattr(geo_prompts, "llm_json", llm)
    return calls


@pytest.fixture()
def count_mutations(monkeypatch):
    calls = []
    real = geo_store.mutate

    def counted(doc_id, change):
        calls.append(doc_id)
        return real(doc_id, change)

    monkeypatch.setattr(geo_store, "mutate", counted)
    return calls


# ----------------------------------------------------------------- parsing ----


@pytest.mark.parametrize("line", [
    f"- {QUESTION}",
    f"* {QUESTION}",
    f"• {QUESTION}",
    f"1. {QUESTION}",
    f"1) {QUESTION}",
    f"(1) {QUESTION}",
    f"12. {QUESTION}",
    f'"{QUESTION}"',
    f"'{QUESTION}'",
    f"“{QUESTION}”",
    f'- "{QUESTION}"',
    f"3) “{QUESTION}”",
    "   Which   legal intake\tservice is best?   ",
])
def test_parse_strips_every_list_format(line):
    assert geo_prompts.parse_prompt_lines(line) == [QUESTION]


def test_parse_keeps_order_drops_blanks_and_accepts_a_list():
    raw = "\n\n- first question here\n\n\n2. second question here\n   \n- \nthird question here\n"
    assert geo_prompts.parse_prompt_lines(raw) == [
        "first question here", "second question here", "third question here",
    ]
    assert geo_prompts.parse_prompt_lines(["a list item\nwith two lines", "", "and another"]) == [
        "a list item", "with two lines", "and another",
    ]


def test_parse_keeps_a_number_that_is_part_of_the_question():
    assert geo_prompts.parse_prompt_lines("1.5 million calls a year — which service copes?") == [
        "1.5 million calls a year — which service copes?",
    ]
    assert geo_prompts.parse_prompt_lines("2024 best legal answering service") == [
        "2024 best legal answering service",
    ]


# ------------------------------------------------------------------ intake ----


def test_add_prompts_returns_records_and_the_saved_universe():
    res = geo_prompts.add_prompts(
        BID,
        "- Which intake service handles Spanish-speaking clients?\n"
        "- Can a virtual receptionist do conflict checks?",
        intent="problem", stage="purchase",
    )
    assert [p["text"] for p in res["added"]] == [
        "Which intake service handles Spanish-speaking clients?",
        "Can a virtual receptionist do conflict checks?",
    ]
    assert res["skipped"] == []
    assert res["total"] == 2
    rec = res["added"][0]
    assert rec["source"] == "custom" and rec["enabled"] is True and rec["persona"] == ""
    assert rec["intent"] == "problem" and rec["stage"] == "purchase"
    assert len(rec["id"]) == 8
    assert geo_prompts.load_universe(BID) == res["universe"]
    assert geo_prompts.enabled_prompts(BID) == res["added"]


def test_add_prompts_falls_back_to_default_intent_and_stage():
    res = geo_prompts.add_prompts(BID, "a perfectly fine question", intent="weird", stage="nope")
    assert (res["added"][0]["intent"], res["added"][0]["stage"]) == ("category", "consideration")


def test_add_prompts_skips_by_length_with_reasons():
    res = geo_prompts.add_prompts(BID, ["hey", "x" * 401, "a perfectly fine question"])
    assert [s["reason"] for s in res["skipped"]] == ["too short", "too long"]
    assert [p["text"] for p in res["added"]] == ["a perfectly fine question"]


def test_add_prompts_dedupes_within_the_batch_case_insensitively():
    res = geo_prompts.add_prompts(
        BID,
        "Best legal intake service?\nbest LEGAL intake service?\n- \"Best legal intake service?\"",
    )
    assert len(res["added"]) == 1
    assert [s["reason"] for s in res["skipped"]] == ["duplicate in your list"] * 2


def test_add_prompts_dedupes_against_the_universe_case_insensitively():
    seed(2)
    res = geo_prompts.add_prompts(BID, "BEST legal VA provider 1\nbrand new question here")
    assert res["skipped"] == [
        {"text": "BEST legal VA provider 1", "reason": "already in the universe"},
    ]
    assert res["total"] == 3
    assert len(geo_prompts.load_universe(BID)["prompts"]) == 3


# --------------------------------------------------------------------- cap ----


def test_cap_accepts_partially_and_names_the_reason():
    seed(200)
    pasted = "\n".join(f"pasted question number {i}" for i in range(60))
    res = geo_prompts.add_prompts(BID, pasted)
    assert len(res["added"]) == 50
    assert len(res["skipped"]) == 10
    assert {s["reason"] for s in res["skipped"]} == {"universe is full (250)"}
    assert [s["text"] for s in res["skipped"]][0] == "pasted question number 50"
    assert res["total"] == 250 == len(geo_prompts.load_universe(BID)["prompts"])


def test_cap_counts_disabled_prompts_too():
    # the cap bounds the document and the sweep alike; toggling prompts off
    # does not make room
    doc = geo_prompts.save_universe(BID, [dict(p, enabled=False) for p in seed(250)])
    assert not any(p["enabled"] for p in doc["prompts"])
    res = geo_prompts.add_prompts(BID, "one more question please")
    assert res["added"] == []
    assert res["skipped"][0]["reason"] == "universe is full (250)"


def test_save_universe_refuses_more_than_the_cap():
    with pytest.raises(ValueError, match="universe is full"):
        geo_prompts.save_universe(BID, [{"id": f"p{i}", "text": f"q {i}"} for i in range(251)])
    assert geo_prompts.load_universe(BID) is None


# ---------------------------------------------------------------- personas ----


def test_set_personas_derives_slug_keys_and_load_returns_them():
    doc = geo_prompts.set_personas(BID, PERSONAS)
    assert [p["key"] for p in doc["personas"]] == ["solo-attorney", "firm-administrator"]
    assert doc["personas"][0]["description"] == "Runs a one-lawyer practice, no admin staff"
    assert doc["prompts"] == []
    assert geo_prompts.load_universe(BID)["personas"] == doc["personas"]


def test_set_personas_key_is_a_slug_capped_at_24_chars():
    doc = geo_prompts.set_personas(BID, [
        {"label": "Solo Practitioner Attorney (US)!"},
        {"key": "  In-House Counsel ", "label": "Counsel"},
    ])
    keys = [p["key"] for p in doc["personas"]]
    assert keys == ["solo-practitioner-attorn", "in-house-counsel"]
    assert all(len(k) <= geo_prompts.PERSONA_KEY_MAX for k in keys)


@pytest.mark.parametrize("bad", [
    [{"label": "A"}],                                            # label too short
    [{"label": "x" * 61}],                                       # label too long
    [{"label": "Solo", "description": "d" * 241}],               # description too long
    [{"label": f"Persona {i}"} for i in range(9)],               # more than 8
    [{"label": "Solo attorney"}, {"label": "solo attorney"}],    # same key twice
    [{"label": "!!!"}],                                          # no key derivable
    "not a list",
])
def test_set_personas_rejects_invalid_input_without_writing(bad):
    with pytest.raises(ValueError):
        geo_prompts.set_personas(BID, bad)
    assert geo_prompts.load_universe(BID) is None


def test_add_prompts_tags_a_known_persona_and_coerces_an_unknown_one():
    geo_prompts.set_personas(BID, PERSONAS)
    tagged = geo_prompts.add_prompts(
        BID, "do i need a receptionist for one lawyer", persona="solo-attorney",
    )
    assert tagged["added"][0]["persona"] == "solo-attorney"
    untagged = geo_prompts.add_prompts(
        BID, "which intake vendor scales to 20 lawyers", persona="ghost",
    )
    assert untagged["added"][0]["persona"] == ""
    assert [p["persona"] for p in geo_prompts.enabled_prompts(BID)] == ["solo-attorney", ""]


def test_dropping_a_persona_untags_its_prompts():
    geo_prompts.set_personas(BID, PERSONAS)
    geo_prompts.add_prompts(BID, "do i need a receptionist for one lawyer", persona="solo-attorney")
    doc = geo_prompts.set_personas(BID, PERSONAS[1:])
    assert [p["key"] for p in doc["personas"]] == ["firm-administrator"]
    assert doc["prompts"][0]["persona"] == ""
    assert doc["prompts"][0]["text"] == "do i need a receptionist for one lawyer"


def test_save_universe_preserves_personas_and_carries_persona_by_id_when_omitted():
    geo_prompts.set_personas(BID, PERSONAS)
    res = geo_prompts.add_prompts(
        BID, "do i need a receptionist for one lawyer", persona="solo-attorney",
    )
    rec = res["added"][0]
    # an editor that predates personas round-trips the record without the key
    legacy = {k: v for k, v in rec.items() if k != "persona"}
    legacy["enabled"] = False
    doc = geo_prompts.save_universe(BID, [
        legacy,
        {"id": "new1", "text": "fresh question here", "persona": "ghost"},
    ])
    assert len(doc["personas"]) == 2
    assert doc["personas"] == geo_prompts.load_universe(BID)["personas"]
    assert doc["prompts"][0]["persona"] == "solo-attorney"
    assert doc["prompts"][0]["enabled"] is False
    assert doc["prompts"][1]["persona"] == ""


def test_legacy_universe_without_personas_reads_as_an_empty_list():
    state.save(geo_prompts.prompts_doc_id(BID), {"brand_id": BID, "prompts": [
        {"id": "p0", "text": "old prompt here", "intent": "category",
         "stage": "consideration", "enabled": True, "source": "ai"},
    ]})
    doc = geo_prompts.load_universe(BID)
    assert doc["personas"] == []
    assert geo_prompts.enabled_prompts(BID)[0]["text"] == "old prompt here"


# ---------------------------------------------------------------- generate ----


def test_generate_with_personas_instructs_the_model_and_tags_prompts(monkeypatch):
    geo_prompts.set_personas(BID, PERSONAS)
    calls = fake_llm(monkeypatch, [
        {"text": "do i need a receptionist for one lawyer", "intent": "problem",
         "stage": "awareness", "persona": "solo-attorney"},
        {"text": "best intake vendor for a 20 lawyer firm", "intent": "category",
         "stage": "consideration", "persona": "firm-administrator"},
        {"text": "who answers after hours calls", "intent": "problem",
         "stage": "awareness", "persona": "made-up"},
        {"text": "untagged question here", "intent": "category", "stage": "purchase"},
    ])
    doc = geo_prompts.generate_universe(BRAND)
    system, prompt = calls[0]
    assert '"persona"' in system
    assert "key=solo-attorney" in prompt and "Runs a one-lawyer practice" in prompt
    assert [p["persona"] for p in doc["prompts"]] == [
        "solo-attorney", "firm-administrator", "", "",
    ]
    assert all(p["source"] == "ai" for p in doc["prompts"])
    assert doc["personas"] == geo_prompts.load_universe(BID)["personas"]


def test_generate_without_personas_is_unchanged(monkeypatch):
    calls = fake_llm(monkeypatch, [
        {"text": "best legal intake service", "intent": "category", "stage": "consideration"},
    ])
    doc = geo_prompts.generate_universe(BRAND)
    system, prompt = calls[0]
    assert "persona" not in system.lower() and "persona" not in prompt.lower()
    assert doc["prompts"][0]["persona"] == ""
    assert doc["personas"] == []


def test_regenerate_keeps_custom_prompts_and_their_persona(monkeypatch):
    geo_prompts.set_personas(BID, PERSONAS)
    geo_prompts.add_prompts(
        BID, "can a virtual receptionist do conflict checks?", persona="solo-attorney",
    )
    fake_llm(monkeypatch, [
        {"text": "best legal intake service", "intent": "category", "stage": "consideration"},
        {"text": "Can a virtual receptionist do conflict checks?",      # dupe of the custom one
         "intent": "category", "stage": "purchase"},
    ])
    doc = geo_prompts.generate_universe(BRAND)
    by_text = {p["text"]: p for p in doc["prompts"]}
    custom = by_text["can a virtual receptionist do conflict checks?"]
    assert custom["source"] == "custom" and custom["persona"] == "solo-attorney"
    assert "Can a virtual receptionist do conflict checks?" not in by_text
    assert by_text["best legal intake service"]["source"] == "ai"
    assert len(doc["prompts"]) == 2


def test_regenerate_clamps_the_draft_to_the_cap(monkeypatch):
    geo_prompts.add_prompts(BID, "\n".join(f"team question {i}" for i in range(240)))
    fake_llm(monkeypatch, [{"text": f"drafted question {i}"} for i in range(40)])
    doc = geo_prompts.generate_universe(BRAND)
    assert len(doc["prompts"]) == 250
    assert sum(p["source"] == "ai" for p in doc["prompts"]) == 10
    assert sum(p["source"] == "custom" for p in doc["prompts"]) == 240


def test_regenerate_refuses_before_the_model_call_when_custom_fills_the_cap(monkeypatch):
    geo_prompts.save_universe(BID, [
        {"id": f"c{i}", "text": f"team question {i}", "source": "custom"} for i in range(250)
    ])
    calls = fake_llm(monkeypatch, [{"text": "drafted question"}])
    with pytest.raises(ValueError, match="universe is full"):
        geo_prompts.generate_universe(BRAND)
    assert calls == []


# ------------------------------------------------------------------ compat ----


def test_add_custom_prompt_returns_the_universe_or_raises_with_the_reason():
    doc = geo_prompts.add_custom_prompt(
        BID, "Which intake service handles Spanish-speaking clients?",
    )
    assert doc["prompts"][-1]["source"] == "custom"
    with pytest.raises(ValueError, match="already in the universe"):
        geo_prompts.add_custom_prompt(
            BID, "which intake service handles spanish-speaking clients?",
        )
    with pytest.raises(ValueError, match="too short"):
        geo_prompts.add_custom_prompt(BID, "hey")
    with pytest.raises(ValueError):
        geo_prompts.add_custom_prompt(BID, "   ")
    assert len(geo_prompts.load_universe(BID)["prompts"]) == 1


# ------------------------------------------------------------- write path ----


def test_every_write_is_one_mutate_call(count_mutations):
    geo_prompts.add_prompts(
        BID, "- first question here\n- second question here\n- third question here",
    )
    assert count_mutations == [geo_prompts.prompts_doc_id(BID)]
    geo_prompts.set_personas(BID, PERSONAS)
    geo_prompts.save_universe(BID, geo_prompts.load_universe(BID)["prompts"])
    assert len(count_mutations) == 3


def test_an_empty_paste_writes_nothing(count_mutations):
    res = geo_prompts.add_prompts(BID, "\n\n- \n")
    assert res == {
        "added": [], "skipped": [], "total": 0,
        "universe": {"brand_id": BID, "prompts": [], "personas": []},
    }
    assert count_mutations == []
    assert geo_prompts.load_universe(BID) is None


def test_overlapping_pastes_lose_nothing():
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                geo_prompts.add_prompts, BID,
                "\n".join(f"worker {w} question {i}" for i in range(5)),
            )
            for w in range(8)
        ]
        for f in futures:
            assert len(f.result()["added"]) == 5
    assert len(geo_prompts.load_universe(BID)["prompts"]) == 40
