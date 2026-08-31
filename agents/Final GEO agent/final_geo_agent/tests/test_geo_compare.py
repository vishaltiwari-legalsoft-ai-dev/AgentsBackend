"""GEO competitor comparison — pure-math unit tests (no I/O, no network)."""
from final_geo_agent import geo_compare


def ans(prompt_id, run, engine="perplexity", mentions=None, citations=None,
        error=None, no_aio=False, text="q?"):
    return {
        "prompt_id": prompt_id,
        "prompt_text": text,
        "intent": "category",
        "run": run,
        "engine": engine,
        "mentions": mentions or {},
        "citations": citations or [],
        "error": error,
        "no_aio": no_aio,
    }


CFG = {
    "competitors": [
        {"key": "clio", "name": "Clio", "aliases": ["Clio", "clio.com"]},
        {"key": "smokeball", "name": "Smokeball", "aliases": ["Smokeball"]},
    ]
}
BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "www.legalsoft.com"}


# ------------------------------------------------------------------ domains


def test_normalize_domain_accepts_urls_rejects_plain_names():
    assert geo_compare.normalize_domain("https://www.Clio.com/pricing") == "clio.com"
    assert geo_compare.normalize_domain("legalsoft.com") == "legalsoft.com"
    assert geo_compare.normalize_domain("Clio") == ""
    assert geo_compare.normalize_domain("") == ""


def test_entity_key_prefers_key_then_name_and_is_blank_for_neither():
    assert geo_compare.entity_key({"key": "clio", "name": "Clio"}) == "clio"
    assert geo_compare.entity_key({"name": " Smokeball "}) == "Smokeball"
    assert geo_compare.entity_key({"key": "", "name": "Clio"}) == "Clio"
    assert geo_compare.entity_key({}) == ""


def test_one_key_rule_keeps_a_name_only_rival_consistent_across_every_map():
    """A rival keyed one way in the rows and another in the domain map is a
    rival whose numbers never meet."""
    cfg = {"competitors": [{"name": "MyCase", "domain": "mycase.com"}]}
    assert geo_compare.entity_keys(cfg) == ["self", "MyCase"]
    assert geo_compare.entity_names(cfg, BRAND)["MyCase"] == "MyCase"
    assert geo_compare.entity_domains(cfg, BRAND)["MyCase"] == "mycase.com"
    rows = geo_compare.build([ans("p1", 1, mentions={"MyCase": 1})], cfg, BRAND)["rows"]
    assert [r["key"] for r in rows] == ["self", "MyCase"]


def test_entity_domains_prefers_explicit_then_alias():
    cfg = {"competitors": [
        {"key": "a", "name": "A", "domain": "a.io", "aliases": ["other.com"]},
        {"key": "b", "name": "B", "aliases": ["B Corp", "b-corp.net"]},
        {"key": "c", "name": "C", "aliases": ["C"]},
    ]}
    domains = geo_compare.entity_domains(cfg, BRAND)
    assert domains["self"] == "legalsoft.com"   # www. stripped
    assert domains["a"] == "a.io"
    assert domains["b"] == "b-corp.net"
    assert "c" not in domains                   # no domain is not a zero


# ------------------------------------------------------------------- rows


def test_rival_without_domain_gets_null_citation_not_zero():
    answers = [ans("p1", 1, mentions={"self": 1, "smokeball": 2},
                   citations=[{"domain": "legalsoft.com"}])]
    doc = geo_compare.build(answers, CFG, BRAND)
    rows = {r["key"]: r for r in doc["rows"]}
    assert rows["self"]["citation"]["rate"] == 1.0
    assert rows["smokeball"]["citation"] is None
    assert rows["clio"]["citation"]["rate"] == 0.0   # has a domain, never cited


def test_rows_put_self_first_then_rivals_by_mention_rate():
    answers = [
        ans("p1", 1, mentions={"clio": 1}),
        ans("p2", 1, mentions={"clio": 1, "smokeball": 2}),
        ans("p3", 1, mentions={"self": 1}),
    ]
    rows = geo_compare.build(answers, CFG, BRAND)["rows"]
    assert rows[0]["key"] == "self"
    assert [r["key"] for r in rows[1:]] == ["clio", "smokeball"]


def test_avg_position_is_none_when_never_named():
    answers = [ans("p1", 1, mentions={"self": 2}), ans("p1", 2, mentions={"self": 4})]
    assert geo_compare.avg_position(answers, "self") == 3.0
    assert geo_compare.avg_position(answers, "clio") is None


def test_errors_and_no_aio_stay_out_of_every_comparison_number():
    answers = [
        ans("p1", 1, mentions={"clio": 1}, error="HTTP 500"),
        ans("p2", 1, engine="aio", no_aio=True),
        ans("p3", 1, mentions={"self": 1}),
    ]
    doc = geo_compare.build(answers, CFG, BRAND)
    rows = {r["key"]: r for r in doc["rows"]}
    assert doc["n_measured"] == 1
    assert rows["clio"]["mention"]["rate"] == 0.0
    assert rows["clio"]["vs_self"]["n_prompts"] == 1   # only p3 was measurable
    assert rows["clio"]["vs_self"]["behind"] == 0


# -------------------------------------------------------------- head to head


def test_head_to_head_compares_rates_so_it_cannot_saturate():
    """A whole-week window names nearly everyone on nearly every question at
    least once. "Appeared at all" would score that all-ties; the rate says who
    actually owns each question."""
    answers = [
        # p1: rival on 2 of 2 runs, we are on neither -> behind
        ans("p1", 1, mentions={"clio": 1}),
        ans("p1", 2, mentions={"clio": 1}),
        # p2: both named once each, but we are on 2 of 2 -> ahead
        ans("p2", 1, mentions={"self": 1, "clio": 2}),
        ans("p2", 2, mentions={"self": 1}),
        # p3: identical rates, both present -> tied
        ans("p3", 1, mentions={"self": 1, "clio": 2}),
        # p4: measured, nobody named -> open ground
        ans("p4", 1),
    ]
    h2h = geo_compare.head_to_head(answers, "clio")
    assert h2h == {
        "n_prompts": 4, "ahead": 1, "behind": 1, "tied": 1, "both_absent": 1,
        "behind_prompt_ids": ["p1"],
    }


def test_head_to_head_buckets_always_add_up():
    answers = [ans(f"p{i}", 1, mentions={"self": 1} if i % 2 else {"clio": 1})
               for i in range(6)]
    h2h = geo_compare.head_to_head(answers, "clio")
    assert h2h["ahead"] + h2h["behind"] + h2h["tied"] + h2h["both_absent"] == h2h["n_prompts"]


# --------------------------------------------------------------- questions


def test_question_matrix_ranks_our_worst_questions_first():
    answers = [
        ans("p1", 1, text="best intake service", mentions={"clio": 1}),
        ans("p1", 2, text="best intake service", mentions={"clio": 1}),
        ans("p2", 1, text="legal va pricing", mentions={"self": 1}),
    ]
    rows = geo_compare.build(answers, CFG, BRAND)["questions"]
    assert [r["prompt_id"] for r in rows] == ["p1", "p2"]
    assert rows[0]["self_rate"] == 0.0
    assert rows[0]["rates"]["clio"] == 1.0
    assert [r["name"] for r in rows[0]["rivals_ahead"]] == ["Clio"]
    assert rows[0]["leader"] == "clio"
    assert rows[1]["rivals_ahead"] == []


# ------------------------------------------------------- untracked domains


def test_untracked_domains_excludes_ours_and_tracked_rivals():
    answers = [
        ans("p1", 1, citations=[{"domain": "g2.com"}, {"domain": "www.clio.com"},
                                {"domain": "legalsoft.com"}], mentions={"self": 1}),
        ans("p2", 1, citations=[{"domain": "g2.com"}, {"domain": "capterra.com"}]),
    ]
    found = geo_compare.build(answers, CFG, BRAND)["untracked_domains"]
    by_domain = {d["domain"]: d for d in found}
    assert set(by_domain) == {"g2.com", "capterra.com"}
    assert by_domain["g2.com"]["count"] == 2
    # p2 is the answer we are absent from — that is what makes it a threat
    assert by_domain["g2.com"]["answers_you_absent"] == 1
    assert by_domain["capterra.com"]["answers_you_absent"] == 1


def test_untracked_domains_ranked_by_where_we_are_absent():
    answers = [
        ans("p1", 1, mentions={"self": 1}, citations=[{"domain": "loud.com"}]),
        ans("p2", 1, mentions={"self": 1}, citations=[{"domain": "loud.com"}]),
        ans("p3", 1, citations=[{"domain": "quiet.com"}]),
    ]
    found = geo_compare.untracked_domains(answers, ["legalsoft.com"])
    assert [d["domain"] for d in found] == ["quiet.com", "loud.com"]


def test_untracked_domains_count_the_distinct_questions_each_is_cited_on():
    """Volume and breadth are different threats: 30 citations on one question
    is a page that owns it; 30 across a dozen questions is a rival."""
    answers = [
        ans("p1", 1, citations=[{"domain": "g2.com"}, {"domain": "capterra.com"}]),
        ans("p1", 2, citations=[{"domain": "g2.com"}]),          # same question again
        ans("p2", 1, citations=[{"domain": "g2.com"}]),
        ans("p3", 1, citations=[{"domain": "g2.com"}], error="HTTP 500"),   # not measured
    ]
    found = {d["domain"]: d for d in geo_compare.untracked_domains(answers, [])}
    assert (found["g2.com"]["count"], found["g2.com"]["n_questions"]) == (3, 2)
    assert (found["capterra.com"]["count"], found["capterra.com"]["n_questions"]) == (1, 1)


def test_untracked_domain_counted_once_per_answer():
    answers = [ans("p1", 1, citations=[{"domain": "g2.com"}, {"domain": "www.g2.com"},
                                       {"domain": "sub.g2.com"}])]
    found = geo_compare.untracked_domains(answers, [])
    assert [(d["domain"], d["count"]) for d in found] == [("g2.com", 1), ("sub.g2.com", 1)]


def test_build_reports_when_there_is_nobody_to_compare_against():
    doc = geo_compare.build([ans("p1", 1, mentions={"self": 1})], {"competitors": []}, BRAND)
    assert doc["tracked_competitors"] == 0
    assert [r["key"] for r in doc["rows"]] == ["self"]
