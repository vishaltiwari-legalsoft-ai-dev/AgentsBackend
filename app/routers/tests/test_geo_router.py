"""Integration tests for the GEO router (/api/geo). Fully offline.

The real geo modules run end-to-end; only the outer seams are faked: the
engine adapters and the a2 brand registry."""
from __future__ import annotations

import os

os.environ["SEO_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.routers.tests.conftest import client
from final_geo_agent import geo_engines, geo_prompts, geo_window
from seo_geo_agent import insights, state
from final_geo_agent.geo_engines import EngineAnswer

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}

#: The default caller here is a creator — the registry-shaping half of this
#: router is gated, and a Creator passes that gate implicitly
#: (``security.is_geo_editor``). ``is_geo_editor`` is spelled out rather than
#: inferred because these dicts stand in for the payload
#: ``get_current_user`` builds, and that payload always carries the flag.
OWNER = {"id": "u1", "email": "owner@legalsoft.com", "is_admin": False,
         "is_creator": True, "is_geo_editor": True, "session_id": "",
         "timezone": "UTC"}
#: A signed-in colleague with neither role, for the gate tests.
VIEWER = {"id": "u2", "email": "viewer@legalsoft.com", "is_admin": False,
          "is_creator": False, "is_geo_editor": False, "session_id": "",
          "timezone": "UTC"}
#: The role this file exists to pin: a GEO editor who is NOT a Creator. Every
#: gated route must open for them, and nothing outside GEO may.
GEO_EDITOR = {"id": "u3", "email": "nino.b@legalsoft.com", "is_admin": False,
              "is_creator": False, "is_geo_editor": True, "session_id": "",
              "timezone": "UTC"}


#: The real registry reader, captured before ``_harness`` freezes it. The
#: self-serve brand tests at the bottom of this file need the real store.
REAL_LIST_BRANDS = insights.list_brands


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path, as_caller):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "geo_state"))
    # a real OPENROUTER_API_KEY in local .env must never leak into offline
    # tests — with the fallback live it would fire REAL paid engine polls
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    as_caller(OWNER)


@pytest.fixture()
def fake_engines(monkeypatch):
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, model="fake",
            text=f"Legal Soft and Clio both handle: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        ),
    )


def put_prompts(n=2):
    prompts = [{"id": f"p{i}", "text": f"best legal va {i}", "intent": "category",
                "stage": "consideration", "enabled": True} for i in range(1, n + 1)]
    resp = client.put(f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": prompts})
    assert resp.status_code == 200
    return prompts


@pytest.fixture()
def day_doc_reads(monkeypatch):
    """Every day-doc this request pulls out of the datastore.

    A read-COST fixture, deliberately: the values the listing shows are already
    covered above, and the regression worth catching here produces exactly the
    right numbers at 28 document fetches per brand.
    """
    seen: list[str] = []
    real = state.load

    def counting(doc_id):
        if doc_id.startswith("geo-poll-"):
            seen.append(doc_id)
        return real(doc_id)

    monkeypatch.setattr(state, "load", counting)
    return seen


def test_brand_listing_counts_answers_without_hydrating_them(fake_engines, day_doc_reads):
    """``GET /geo/brands`` used to fetch a week of answers per brand to take a
    ``len``: 7 days x 4 engines = 28 document fetches each, up to 900 KB apiece,
    to produce one integer per brand.

    The first listing after this shipped still reconstructs the counter once
    (a brand polled before it existed must not read as "not polled yet"); every
    listing after that reads no day-doc at all.
    """
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    day_doc_reads.clear()
    first = client.get("/api/geo/brands").json()["brands"][0]
    assert first["recent_answers"] == 2                  # honest, not zero
    reconstruction = len(day_doc_reads)
    assert reconstruction == geo_window.MAX_DAYS * len(geo_window.ENGINES)

    day_doc_reads.clear()
    second = client.get("/api/geo/brands").json()["brands"][0]
    assert second["recent_answers"] == 2
    assert day_doc_reads == [], "the listing is hydrating the corpus again"


def test_brand_listing_says_not_polled_only_when_that_is_true(day_doc_reads):
    """0 renders as "Not polled yet" in the console, so it must mean that."""
    body = client.get("/api/geo/brands").json()["brands"][0]
    assert body["recent_answers"] == 0


def test_geo_config_reports_engine_availability():
    resp = client.get("/api/geo/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["engines"]) == set(geo_engines.ALL_ENGINES)
    # offline + keyless: nothing should claim to be available
    assert not any(body["engines"].values())


def test_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/prompts").status_code == 404
    assert client.post("/api/geo/brands/nope/poll/step", json={}).status_code == 404


def test_an_unknown_brand_never_answers_before_the_caller_is_checked(unauthenticated, as_caller):
    """404 vs 401/403 on an unknown id is a brand-existence oracle.

    Brand resolution is a dependency now, and a dependency's position in a
    handler signature decides nothing here: auth is a SUB-dependency of it, so
    it always resolves first.
    """
    unauthenticated()
    assert client.get("/api/geo/brands/nope/report").status_code in (401, 403)

    as_caller(VIEWER)   # signed in, neither creator nor GEO editor
    assert client.post("/api/geo/brands/nope/rescan", json={}).status_code == 403
    assert client.get("/api/geo/brands/nope/report").status_code == 404


def test_prompts_roundtrip_and_brand_listing():
    put_prompts(2)
    got = client.get(f"/api/geo/brands/{BRAND['id']}/prompts").json()
    assert len(got["prompts"]) == 2
    brands = client.get("/api/geo/brands").json()["brands"]
    assert brands[0]["prompts"] == 2


def test_put_prompts_empty_clears_the_universe_and_persists():
    """An empty list is a full clear, and it is accepted.

    It used to 422 "At least one prompt is required", so emptying a universe
    meant deleting prompts one at a time. The team asked for the clear; the
    read-back is the half that matters — a 200 that did not persist would be
    the same lie in a friendlier status code.
    """
    put_prompts(2)
    resp = client.put(f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompts"] == []

    got = client.get(f"/api/geo/brands/{BRAND['id']}/prompts").json()
    assert got["prompts"] == []
    assert client.get("/api/geo/brands").json()["brands"][0]["prompts"] == 0


def test_a_cleared_brand_still_fails_loudly_when_a_check_is_attempted(fake_engines):
    """Allowing the clear must not turn "nothing to measure" into a quiet 200.

    This is the pairing that makes the relaxation safe. A brand with no
    questions has nothing to poll, and the honest answer is a refusal that says
    so — not an empty sweep the console renders as a score of zero, which reads
    identically to "measured, and nobody mentions you".
    """
    put_prompts(2)
    assert client.put(
        f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": []}
    ).status_code == 200

    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step", json={})
    assert resp.status_code == 409, resp.text
    assert "No enabled prompts" in resp.json()["detail"]


def test_two_people_checking_one_brand_the_second_is_refused_not_duplicated(
    fake_engines, as_caller,
):
    """The team's ask, on the wire: two signed-in people press Check now on the
    same brand. The second must be told a check is already running — not quietly
    handed the same batch of paid engine calls to repeat."""
    put_prompts(3)

    mine = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                       json={"runs": 2, "batch_size": 2, "poll_token": "tab-1"}).json()
    assert mine["lease_held_by"] is None and mine["done"] < mine["total"]

    as_caller(VIEWER)
    theirs = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                         json={"runs": 2, "batch_size": 2, "poll_token": "tab-1"})

    assert theirs.status_code == 200          # a valid request with an honest no
    body = theirs.json()
    # the same literal token from a different account must NOT share the lease
    assert body["lease_held_by"] == OWNER["email"]
    assert body["stop_code"] == "lease_held"
    assert body["unlocks_at"]
    assert body["terminal"] is True
    assert "already running" in body["terminal_reason"]


def test_a_poll_token_the_client_mangled_still_polls(fake_engines):
    """An unusable token falls back to per-step leasing. It must never 422 a
    poll loop over the shape of a value whose only job is to be unique."""
    put_prompts(2)

    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                       json={"runs": 1, "batch_size": 10, "poll_token": "!!!"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["done"] == 2


def test_a_poll_without_a_token_behaves_exactly_as_before(fake_engines):
    put_prompts(2)

    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                       json={"runs": 1, "batch_size": 10})

    assert resp.status_code == 200, resp.text
    assert resp.json()["done"] == 2 and resp.json()["lease_held_by"] is None


def test_a_brand_can_only_be_checked_once_a_day_however_many_people_click(
    fake_engines, as_caller,
):
    """The largest uncontrolled spend path in the agent: a fresh UTC day used to
    buy one ~440-call sweep per click, per person."""
    put_prompts(2)

    first = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                        json={"runs": 1, "batch_size": 10, "poll_token": "t1"})
    assert first.json()["stop_code"] is None

    as_caller(VIEWER)
    second = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                         json={"runs": 3, "batch_size": 10, "poll_token": "t2"})

    assert second.status_code == 200
    body = second.json()
    assert body["stop_code"] == "already_checked_today"
    assert body["unlocks_at"]
    assert body["terminal"] is True


def test_the_status_endpoint_says_the_check_is_spent(fake_engines):
    """So the button explains itself instead of only failing when pressed."""
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10, "poll_token": "t1"})

    status = client.get(f"/api/geo/brands/{BRAND['id']}/poll/status").json()

    assert status["manual_check_used"] is True
    assert status["manual_check_by"] == OWNER["email"]
    assert status["manual_check_unlocks_at"]


def test_a_client_that_sends_no_token_is_still_held_to_one_check_a_day(
    fake_engines, as_caller,
):
    """The gate needs a loop id, and the router always supplies one — client
    token, else the session, else the account. A console that has not been
    updated yet must not slip the limit by sending nothing."""
    put_prompts(2)

    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    as_caller(VIEWER)
    second = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                         json={"runs": 3, "batch_size": 10})

    assert second.json()["stop_code"] == "already_checked_today"


def test_poll_step_without_keys_503():
    put_prompts(1)
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step", json={})
    assert resp.status_code == 503
    assert "Secrets" in resp.json()["detail"]


def test_poll_step_before_prompts_409(fake_engines):
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step", json={})
    assert resp.status_code == 409


def test_poll_report_answers_flow(fake_engines):
    put_prompts(2)
    resp = client.post(
        f"/api/geo/brands/{BRAND['id']}/poll/step",
        json={"runs": 1, "batch_size": 10},
    )
    assert resp.status_code == 200
    progress = resp.json()
    assert progress["done"] == progress["total"] == 2

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["blended"]["mention"]["rate"] == 1.0
    assert report["blended"]["mention"]["n_answers"] == 2
    assert report["engines"]["perplexity"]["citation"]["rate"] == 0.0
    assert report["source_gap"][0]["domain"] == "g2.com"

    answers = client.get(
        f"/api/geo/brands/{BRAND['id']}/answers", params={"engine": "perplexity"}
    ).json()
    assert answers["total"] == 2
    assert answers["answers"][0]["brand_mentioned"] is True


def test_brand_config_roundtrip():
    resp = client.put(
        f"/api/geo/brands/{BRAND['id']}/config",
        json={"competitors": [{"key": "clio", "name": "Clio", "aliases": ["Clio"]}],
              "daily_cap": 100},
    )
    assert resp.status_code == 200
    cfg = client.get(f"/api/geo/brands/{BRAND['id']}/config").json()
    assert cfg["daily_cap"] == 100
    assert cfg["competitors"][0]["key"] == "clio"


# ----------------------- competitor comparison + score history ----------------


def track_clio():
    resp = client.put(
        f"/api/geo/brands/{BRAND['id']}/config",
        json={"competitors": [
            {"key": "clio", "name": "Clio", "aliases": ["Clio"], "domain": "clio.com"},
        ]},
    )
    assert resp.status_code == 200


def test_comparison_scores_rivals_on_the_same_answers(fake_engines):
    track_clio()
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    doc = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    rows = {r["key"]: r for r in doc["rows"]}
    assert doc["tracked_competitors"] == 1
    # the stub answer names both of us, us first
    assert rows["self"]["mention"]["rate"] == 1.0
    assert rows["clio"]["mention"]["rate"] == 1.0
    assert rows["self"]["avg_position"] == 1.0
    assert rows["clio"]["avg_position"] == 2.0
    # both have a domain on record and neither is cited — g2.com is
    assert rows["self"]["citation"]["rate"] == 0.0
    assert rows["clio"]["citation"]["rate"] == 0.0
    assert rows["clio"]["vs_self"]["tied"] == 2   # named in every answer we are

    # every question shows both of us, and g2 is the untracked co-citation
    assert len(doc["questions"]) == 2
    assert doc["questions"][0]["rates"]["clio"] == 1.0
    assert [d["domain"] for d in doc["untracked_domains"]] == ["g2.com"]


def test_comparison_with_nobody_tracked_says_so(fake_engines):
    put_prompts(1)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    doc = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert doc["tracked_competitors"] == 0
    assert [r["key"] for r in doc["rows"]] == ["self"]


def test_comparison_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/comparison").status_code == 404


def test_history_banks_a_point_when_a_sweep_completes(fake_engines):
    track_clio()
    put_prompts(10)
    progress = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                           json={"runs": 1, "batch_size": 10}).json()
    assert progress["done"] == progress["total"] == 10

    body = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["source"] == "sweep"
    assert point["n_measured"] == 10
    assert point["mention_rate"] == 1.0
    assert point["competitors"]["clio"] == 1.0
    assert 0 < point["score"] <= 100
    # first measurement: no move to report, and we say that rather than "0%"
    assert body["trend"]["since_last"]["direction"] == "unknown"
    assert body["names"]["clio"] == "Clio"


def test_history_is_empty_and_honest_before_any_sweep():
    body = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()
    assert body["points"] == []
    assert body["trend"]["current"] is None
    assert body["min_point_answers"] > 0


def test_history_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/history").status_code == 404


def test_tracking_a_competitor_first_does_not_erase_the_brand_aliases(fake_engines):
    """Regression: PUT /config used to create the document, so a competitor
    added before anything read the config produced a config with no `self`
    aliases — and the brand then went unnamed in its own answers."""
    track_clio()
    cfg = client.get(f"/api/geo/brands/{BRAND['id']}/config").json()
    assert cfg["aliases"]["self"]
    put_prompts(1)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["blended"]["mention"]["rate"] == 1.0


def test_rescan_finds_a_competitor_tracked_after_the_poll(fake_engines):
    """The whole point of the rescan: a rival added today is measurable today,
    from answers already on disk, without re-billing a single engine call."""
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    before = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert before["tracked_competitors"] == 0

    track_clio()   # Clio is named in the stored answers, but nobody was looking
    stale = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert next(r for r in stale["rows"] if r["key"] == "clio")["mention"]["rate"] == 0.0

    result = client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={"days": 7}).json()
    assert result["answers_scanned"] == 2
    assert result["answers_updated"] == 2
    assert "clio" in result["entities"]

    after = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert next(r for r in after["rows"] if r["key"] == "clio")["mention"]["rate"] == 1.0
    # and we are still measured exactly as before — a rescan is not a re-poll
    assert next(r for r in after["rows"] if r["is_self"])["mention"]["rate"] == 1.0


def test_rescan_needs_the_geo_editor_role(as_caller):
    as_caller(VIEWER)
    assert client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={}).status_code == 403


# --------------------------- the GEO editor role ------------------------------
#
# Six people administer this agent and three of them are on outside domains.
# Before this role the only elevated one was Creator, which also unlocks
# Settings → Secrets, the admin database viewer, model config and every other
# agent — so the choice was "hand out far too much" or "nobody but the owners
# can edit a prompt universe". These pin the third option: exactly the eight
# GEO registry routes, and nothing else.

#: The eight routes the role opens, with a body each that gets past validation
#: — a 422 here would prove nothing about the gate.
GATED_CALLS: list[tuple[str, str, dict]] = [
    ("post", "prompts/generate", {}),
    ("post", "prompts/custom", {"text": "best legal va provider", "intent": "category"}),
    ("post", "prompts/bulk", {"text": "best legal va provider", "intent": "category"}),
    ("put", "prompts", {"prompts": []}),
    ("put", "personas", {"personas": []}),
    ("put", "config", {"daily_cap": 100}),
    ("post", "rescan", {"days": 7}),
    ("post", "strategy/generate", {}),
]


@pytest.mark.parametrize(("verb", "suffix", "body"), GATED_CALLS)
def test_a_signed_in_colleague_without_the_role_is_refused(verb, suffix, body, as_caller):
    """Read and poll stay open to everyone; shaping the registry does not."""
    as_caller(VIEWER)
    resp = getattr(client, verb)(f"/api/geo/brands/{BRAND['id']}/{suffix}", json=body)
    assert resp.status_code == 403, f"{verb.upper()} {suffix} -> {resp.status_code}"


@pytest.mark.parametrize(("verb", "suffix", "body"), GATED_CALLS)
def test_a_geo_editor_who_is_not_a_creator_gets_through_the_gate(
    verb, suffix, body, as_caller,
):
    """The point of the whole change: these six people can do this work without
    being made Creators.

    Asserts "not 403", not "200" — ``prompts/generate`` and
    ``strategy/generate`` need a model key and honestly answer 503 offline.
    What is being pinned here is the gate, and a 503 is already past it.
    """
    as_caller(GEO_EDITOR)
    resp = getattr(client, verb)(f"/api/geo/brands/{BRAND['id']}/{suffix}", json=body)
    assert resp.status_code != 403, f"{verb.upper()} {suffix} -> {resp.text}"


def test_the_role_does_not_reach_past_the_geo_registry(as_caller):
    """A GEO editor is not a small Creator.

    The role was introduced so that six people could stop needing Creator; if
    it quietly carried any of Creator's other reach it would have failed at the
    only thing it was for. Sampled at the two panels that matter most —
    Settings → Secrets and the admin database viewer.
    """
    as_caller(GEO_EDITOR)
    for path in ("/api/admin/settings", "/api/admin/db/collections", "/api/admin/users"):
        assert client.get(path).status_code == 403, f"{path} reachable by a GEO editor"


def test_the_gate_reads_the_derived_flag_not_the_creator_claim(as_caller):
    """``require_geo_editor`` checks one flag, and ``get_current_user`` is what
    puts the Creator implication into it.

    A caller whose payload says creator but not geo_editor cannot occur through
    the real dependency — this asserts the guard has not grown a second, drifting
    copy of the "creators are editors" rule that would make the flag advisory.
    """
    as_caller({**OWNER, "is_geo_editor": False})
    assert client.put(
        f"/api/geo/brands/{BRAND['id']}/config", json={"daily_cap": 100}
    ).status_code == 403


# ------------------------- prompt intent is a choice --------------------------


@pytest.mark.parametrize("suffix", ["prompts/custom", "prompts/bulk"])
def test_creating_a_prompt_without_an_intent_is_refused(suffix):
    """``intent`` used to default to "category", which is in
    ``geo_engines.SERP_INTENTS`` — so every question the team typed was billed
    an AI Overview + AI Mode call per sweep, including the brand-name questions
    a paid search call tells you nothing about. The author has to choose now.
    """
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/{suffix}",
                       json={"text": "best legal va provider"})
    assert resp.status_code == 422
    assert any(err["loc"][-1] == "intent" for err in resp.json()["detail"])


@pytest.mark.parametrize("suffix", ["prompts/custom", "prompts/bulk"])
def test_the_intent_choice_is_exactly_two_values(suffix):
    """Two, and they are the store's own vocabulary — not a third spelling the
    poll planner would silently coerce back to "category"."""
    for intent in ("category", "brand"):
        resp = client.post(f"/api/geo/brands/{BRAND['id']}/{suffix}",
                           json={"text": f"a question about {intent} intent",
                                 "intent": intent})
        assert resp.status_code == 200, resp.text
    for rejected in ("problem", "Category", "", "awareness"):
        resp = client.post(f"/api/geo/brands/{BRAND['id']}/{suffix}",
                           json={"text": "another distinct question here",
                                 "intent": rejected})
        assert resp.status_code == 422, f"{rejected!r} was accepted"


def test_a_brand_intent_prompt_is_stored_where_the_serp_filter_can_see_it():
    """The choice has to survive to the poll planner or it is decoration.

    ``brand`` must land as ``brand`` — the value ``SERP_INTENTS`` excludes — so
    the question is measured on the chat engines and costs no billed search
    call. This asserts the stored record, which is what the planner filters on.
    """
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/prompts/custom",
                       json={"text": "is Legal Soft any good for PI firms",
                             "intent": "brand"})
    assert resp.status_code == 200, resp.text
    stored = [p for p in resp.json()["prompts"] if p["text"].startswith("is Legal Soft")]
    assert stored and stored[0]["intent"] == "brand"
    assert "brand" not in geo_engines.SERP_INTENTS
    assert "category" in geo_engines.SERP_INTENTS


def test_a_problem_intent_prompt_still_round_trips_though_nobody_can_type_one(
    fake_engines,
):
    """``problem`` was NOT deleted — it was only removed from the human picker.

    The two-value choice is about the one distinction that changes cost: does
    the question name the brand. ``problem`` behaves as a discovery question
    (it is in ``SERP_INTENTS`` alongside ``category``) and the AI generator
    still produces it, so every path that reads a stored prompt must keep
    handling it. Narrowing the create endpoints must not have quietly made
    existing rows unreadable — that would be a data migration nobody asked for,
    discovered by a panel throwing on somebody's existing universe.
    """
    stored = [
        {"id": "p1", "text": "how do small firms cut intake costs",
         "intent": "problem", "stage": "awareness", "enabled": True},
        {"id": "p2", "text": "best legal va provider",
         "intent": "category", "stage": "consideration", "enabled": True},
    ]
    saved = client.put(f"/api/geo/brands/{BRAND['id']}/prompts",
                       json={"prompts": stored})
    assert saved.status_code == 200, saved.text
    assert [p["intent"] for p in saved.json()["prompts"]] == ["problem", "category"]

    # It reads back intact...
    got = client.get(f"/api/geo/brands/{BRAND['id']}/prompts").json()
    assert [p["intent"] for p in got["prompts"]] == ["problem", "category"]

    # ...it is polled like the discovery question it is, not skipped...
    step = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                       json={"runs": 1, "batch_size": 10})
    assert step.status_code == 200, step.text
    assert step.json()["total"] == 2
    assert "problem" in geo_engines.SERP_INTENTS

    # ...and every downstream read renders it without an error.
    for path in ("report", "comparison", "answers"):
        resp = client.get(f"/api/geo/brands/{BRAND['id']}/{path}")
        assert resp.status_code == 200, f"{path}: {resp.text}"
    intents = {a.get("intent") for a in
               client.get(f"/api/geo/brands/{BRAND['id']}/answers").json()["answers"]}
    assert "problem" in intents


def test_rescan_keeps_a_live_point_labelled_live(fake_engines):
    track_clio()
    put_prompts(10)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={"days": 7})

    points = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()["points"]
    assert len(points) == 1
    assert points[0]["source"] == "sweep"   # rebuilt, not relabelled


# ------------------------ self-serve brand management -------------------------
#
# Adding a brand was the last thing in this panel that still needed a Creator,
# and removing one could only be done with a destructive delete that leaves
# eighteen document families — a live Search Console refresh token among them —
# with no route that can reach them. These pin the two replacements.


@pytest.fixture()
def real_registry(monkeypatch):
    """Undo ``_harness``'s frozen brand list for the tests that create brands.

    The rest of this file wants a fixed registry; these tests are ABOUT the
    registry, and ``SEO_LOCAL_DIR`` already points at this test's tmp_path, so
    the real store starts from the packaged default (``legalsoft``) and nothing
    written here escapes the test.
    """
    monkeypatch.setattr(insights, "list_brands", REAL_LIST_BRANDS)
    return insights


def add_brand(name="Acme Legal", url="https://www.AcmeLegal.com/pricing?utm=x"):
    return client.post("/api/geo/brands", json={"name": name, "url": url})


#: Captured so the "nothing invents questions" test can put it back.
REAL_GENERATE_UNIVERSE = geo_prompts.generate_universe


def monkeypatch_generate(fn):
    geo_prompts.generate_universe = fn


def prompts_for(brand_id, n=2):
    prompts = [{"id": f"p{i}", "text": f"best legal va {i}", "intent": "category",
                "stage": "consideration", "enabled": True} for i in range(1, n + 1)]
    resp = client.put(f"/api/geo/brands/{brand_id}/prompts", json={"prompts": prompts})
    assert resp.status_code == 200, resp.text
    return prompts


def test_a_geo_editor_can_add_a_brand_without_a_creator(real_registry, as_caller):
    as_caller(GEO_EDITOR)
    resp = add_brand()
    assert resp.status_code == 201, resp.text
    brand = resp.json()["brand"]
    assert brand["id"] == "acme-legal"
    # a pasted URL, reduced to the host — this string is the brand's identity in
    # Search Console and in every alias the GEO engine matches on
    assert brand["domain"] == "acmelegal.com"
    assert brand["gsc_property"] == "sc-domain:acmelegal.com"

    listed = {b["id"] for b in client.get("/api/geo/brands").json()["brands"]}
    assert "acme-legal" in listed


def test_a_new_brand_has_no_questions_and_nothing_invents_any(real_registry):
    """The team writes their own. An auto-generated universe would be a paid
    model call nobody asked for, followed by a measurement of questions nobody
    chose — and this panel's whole trust story is that the numbers are about
    questions the team stands behind."""
    def never(*a, **k):
        raise AssertionError("something generated a prompt universe for a new brand")

    monkeypatch_generate(never)
    try:
        assert add_brand().json()["prompts"] == 0
    finally:
        monkeypatch_generate(REAL_GENERATE_UNIVERSE)

    universe = client.get("/api/geo/brands/acme-legal/prompts").json()
    assert universe["prompts"] == []
    row = next(b for b in client.get("/api/geo/brands").json()["brands"]
               if b["id"] == "acme-legal")
    assert row["prompts"] == 0
    assert row["recent_answers"] == 0


def test_a_new_brand_starts_with_the_scheduled_check_off(real_registry):
    body = add_brand().json()
    assert body["auto_poll"] is False
    assert body["poll_interval_days"] == 7

    row = next(b for b in client.get("/api/geo/brands").json()["brands"]
               if b["id"] == "acme-legal")
    # the switch is on the LIST row, so the panel renders a per-brand toggle
    # without one extra request per brand
    assert row["auto_poll"] is False
    assert row["poll_interval_days"] == 7
    assert row["next_due_at"] is None          # never polled, not "due in 7 days"

    cfg = client.get("/api/geo/brands/acme-legal/config").json()
    assert cfg["auto_poll"] is False and cfg["poll_interval_days"] == 7


def test_the_scheduled_check_is_settable_through_the_api(real_registry):
    add_brand()
    put = client.put("/api/geo/brands/acme-legal/config",
                     json={"auto_poll": True, "poll_interval_days": 3})
    assert put.status_code == 200, put.text
    assert put.json()["auto_poll"] is True

    row = next(b for b in client.get("/api/geo/brands").json()["brands"]
               if b["id"] == "acme-legal")
    assert row["auto_poll"] is True and row["poll_interval_days"] == 3


def test_adding_a_brand_does_not_make_the_next_listing_read_a_month_of_day_docs(
    real_registry, day_doc_reads,
):
    """The ~150-fetch-per-brand trap.

    ``recent_answer_count`` treats a missing ``answer_counts_at`` as "this brand
    polled before the counter existed" and rebuilds it from a 30-day x 5-engine
    window. For a brand created seconds ago that read is guaranteed to find
    nothing — twelve new brands would be ~1,800 document reads landing on
    whoever opened the panel first, to compute twelve zeroes.
    """
    add_brand()
    day_doc_reads.clear()

    listing = client.get("/api/geo/brands").json()["brands"]

    assert next(b for b in listing if b["id"] == "acme-legal")["recent_answers"] == 0
    assert [d for d in day_doc_reads if "acme-legal" in d] == [], (
        f"the new brand's counter was rebuilt on first listing: {day_doc_reads}"
    )


def test_adding_the_same_brand_twice_is_refused_not_overwritten(real_registry):
    """A slug collision must not adopt somebody else's brand: the same id owns
    the same ``geo-config-*`` and ``gsc-auth-*`` documents underneath it."""
    assert add_brand(url="acmelegal.com").status_code == 201
    clash = add_brand(name="Acme Legal", url="somewhere-else.com")
    assert clash.status_code == 409, clash.text
    assert "already exists" in clash.json()["detail"]

    brands = client.get("/api/geo/brands").json()["brands"]
    assert [b["id"] for b in brands].count("acme-legal") == 1
    assert next(b for b in brands if b["id"] == "acme-legal")["domain"] == "acmelegal.com"


@pytest.mark.parametrize(
    ("name", "url"),
    [("Acme", "not-a-domain"), ("Acme", "https://"), ("!!!", "acme.com")],
)
def test_something_that_is_not_a_brand_is_refused_with_a_reason(real_registry, name, url):
    resp = client.post("/api/geo/brands", json={"name": name, "url": url})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]


def test_adding_a_brand_needs_the_geo_editor_role(real_registry, as_caller):
    as_caller(VIEWER)
    assert add_brand().status_code == 403
    # ...and nothing was written on the way to the refusal
    as_caller(OWNER)
    assert [b["id"] for b in client.get("/api/geo/brands").json()["brands"]] == ["legalsoft"]


# ------------------------------- soft removal ---------------------------------


def test_switching_a_brand_off_removes_it_everywhere_and_destroys_nothing(
    real_registry, fake_engines,
):
    """The self-serve "remove": one flag, four surfaces, zero destruction.

    ``enabled`` is already the filter GEO, Blog Writer and Issues each apply, so
    flipping it takes the brand out of all of them in one write — while the
    measurements, the prompt universe and the Search Console grant stay exactly
    where they are. The destructive path stays Creator-only for the reason
    ``insights.delete_brand`` documents.
    """
    add_brand()
    prompts_for("acme-legal", 2)
    client.post("/api/geo/brands/acme-legal/poll/step", json={"runs": 1, "batch_size": 10})
    state.save("gsc-auth-acme-legal", {"refresh_token": "live-token",
                                       "property": "sc-domain:acmelegal.com"})
    assert client.get("/api/geo/brands/acme-legal/answers").json()["total"] > 0

    off = client.put("/api/geo/brands/acme-legal/config", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    # gone from every surface that reads the shared registry
    assert "acme-legal" not in {b["id"] for b in client.get("/api/geo/brands").json()["brands"]}
    assert client.get("/api/geo/brands/acme-legal/answers").status_code == 404
    assert "acme-legal" not in {b["id"] for b in client.get("/api/blog/brands").json()["brands"]}
    assert "acme-legal" not in str(client.get("/api/issues").json())

    # ...and nothing was destroyed
    assert state.load("gsc-auth-acme-legal")["refresh_token"] == "live-token"
    assert state.load("geo-prompts-acme-legal")["prompts"]


def test_a_switched_off_brand_can_be_switched_back_on_by_the_same_role(
    real_registry, fake_engines,
):
    """Reversible, or "soft delete" is just a slower way of losing the data.

    ``PUT config`` resolves the brand from the whole registry rather than the
    enabled-only view precisely so the route that switched a brand off can
    switch it on again — every other route keeps the enabled-only resolver.
    """
    add_brand()
    prompts_for("acme-legal", 2)
    client.post("/api/geo/brands/acme-legal/poll/step", json={"runs": 1, "batch_size": 10})
    measured = client.get("/api/geo/brands/acme-legal/answers").json()["total"]

    client.put("/api/geo/brands/acme-legal/config", json={"enabled": False})
    back = client.put("/api/geo/brands/acme-legal/config", json={"enabled": True})
    assert back.status_code == 200, back.text
    assert back.json()["enabled"] is True

    assert "acme-legal" in {b["id"] for b in client.get("/api/geo/brands").json()["brands"]}
    assert client.get("/api/geo/brands/acme-legal/answers").json()["total"] == measured


def test_switching_a_brand_off_needs_the_geo_editor_role(real_registry, as_caller):
    add_brand()
    as_caller(VIEWER)
    assert client.put("/api/geo/brands/acme-legal/config",
                      json={"enabled": False}).status_code == 403
    as_caller(OWNER)
    assert "acme-legal" in {b["id"] for b in client.get("/api/geo/brands").json()["brands"]}


def test_switching_a_brand_off_does_not_touch_its_geo_config(real_registry):
    """``enabled`` lives on the registry record, not on ``geo-config-{id}``. A
    request carrying only ``enabled`` must not rewrite the config document —
    that document holds the spend counters."""
    add_brand()
    client.put("/api/geo/brands/acme-legal/config", json={"daily_cap": 123})
    before = state.load("geo-config-acme-legal")

    client.put("/api/geo/brands/acme-legal/config", json={"enabled": False})

    assert state.load("geo-config-acme-legal") == before


# ------------------------------- the panel explaining its own numbers, on the
# wire. Both of these were reported as measurement bugs and neither is one: the
# engines are asked different amounts on purpose, and a spent DataForSEO budget
# silently freezes the two Google engines. The backend measured both correctly
# and said neither.


@pytest.fixture()
def fake_engines_with_serp(monkeypatch):
    """A chat engine and a billed Google engine, which are priced differently:
    the chat engine samples every question three times, the SERP engine fetches
    the discovery questions once."""
    monkeypatch.setattr(
        geo_engines, "available_engines",
        lambda: {"perplexity": True, "gemini": False, "chatgpt": False, "aio": True},
    )
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, model="fake",
            via="dataforseo" if engine == "aio" else "native",
            text=f"Legal Soft and Clio both handle: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        ),
    )


def test_the_report_says_how_many_answers_each_engine_owes(fake_engines_with_serp):
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 3, "batch_size": 50})

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()

    perplexity, aio = report["engines"]["perplexity"], report["engines"]["aio"]
    assert (perplexity["n_answers"], perplexity["n_expected"]) == (6, 6)
    assert (aio["n_answers"], aio["n_expected"]) == (2, 2)
    # the counts differ because the expectations differ — which is the sentence
    # the panel could not say before
    assert perplexity["n_answers"] != aio["n_answers"]


def test_the_report_publishes_the_checks_its_expectation_scales_by(
    fake_engines_with_serp,
):
    """`n_expected` is per check; `n_sweeps` is how many checks ran in the
    window. Per BRAND, not per engine — an engine paused on a spent search
    budget ran zero checks of its own, so a per-engine count would zero its
    expectation and erase the very shortfall the panel needs to show."""
    from final_geo_agent import geo_runlog

    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 3, "batch_size": 50})

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["n_sweeps"] == 1
    aio = report["engines"]["aio"]
    assert aio["n_answers"] == aio["n_expected"] * report["n_sweeps"]

    # a second check ran earlier in the window; AI Overview answered on neither
    # more nor fewer questions, so the window's shortfall becomes visible
    earlier = geo_window.day_ids(3)[2]
    geo_runlog.record_run(BRAND["id"], {"day": earlier, "trigger": "cron"})

    scaled = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert scaled["n_sweeps"] == 2
    owed = scaled["engines"]["aio"]["n_expected"] * scaled["n_sweeps"]
    assert scaled["engines"]["aio"]["n_answers"] < owed
    # and the date that explains the gap is on the same payload
    assert "serp_capped_since" in scaled


def test_a_report_over_a_window_with_no_checks_says_zero(fake_engines_with_serp):
    """0 checks is "no checks in this period", which the panel says rather than
    divides by."""
    put_prompts(2)

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()

    assert report["n_sweeps"] == 0
    assert report["blended"]["n_answers"] == 0


def test_the_report_carries_the_pause_so_a_cold_open_can_see_it(fake_engines_with_serp):
    """The panel loads `/report`, not `/poll/step`.

    While the pause was only on the step response, the "search credit spent"
    notice existed for exactly as long as one browser tab driving one check —
    so a brand whose credit ran out three weeks ago opened as a normal-looking
    stale number for everyone else. Nobody polls here: this is what the page
    knows before anyone touches it.
    """
    put_prompts(2)
    assert client.put(f"/api/geo/brands/{BRAND['id']}/config",
                      json={"aio_monthly_cap": 1}).status_code == 200

    fresh = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert fresh["search_credit_spent"] is False
    assert fresh["serp_capped_since"] is None

    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 50})

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["search_credit_spent"] is True
    assert (report["search_credit_used"], report["search_credit_limit"]) == (2, 1)
    # a flag with no date is as useless as a date with no flag
    assert report["serp_capped_since"]
    # and it lands beside the per-engine last measurement it is read with
    assert report["engine_last_seen"]["aio"] == report["serp_capped_since"]


def test_the_two_read_surfaces_never_describe_one_brand_differently(
    fake_engines_with_serp,
):
    """`/report` publishes the honest names, `/poll/status` publishes those AND
    the `aio_*` spelling the console's poll loop already reads. Two spellings
    are tolerable; two ANSWERS are not."""
    put_prompts(2)
    assert client.put(f"/api/geo/brands/{BRAND['id']}/config",
                      json={"aio_monthly_cap": 1}).status_code == 200
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 50})

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    status = client.get(f"/api/geo/brands/{BRAND['id']}/poll/status").json()

    for honest, legacy in (
        ("search_credit_spent", "aio_capped"),
        ("search_credit_used", "aio_credits_month"),
        ("search_credit_limit", "aio_monthly_cap"),
    ):
        assert report[honest] == status[honest] == status[legacy]
    assert report["serp_capped_since"] == status["serp_capped_since"]


def test_geo_config_publishes_how_each_engine_is_asked(fake_engines_with_serp):
    """The console was mirroring `SERP_ENGINES` and `CHAT_RUNS_PER_PROMPT` as
    its own constants to explain the differing answer counts. A copied fact
    starts lying the day the real one changes — this is the fact itself."""
    specs = client.get("/api/geo/config").json()["engine_specs"]

    assert specs["chatgpt"]["kind"] == "chat"
    assert specs["chatgpt"]["runs_per_prompt"] == geo_engines.CHAT_RUNS_PER_PROMPT
    assert specs["chatgpt"]["intents"] is None            # asked everything

    assert specs["aio"]["kind"] == specs["ai_mode"]["kind"] == "serp"
    assert specs["aio"]["runs_per_prompt"] == 1           # billed: fetched once
    assert specs["aio"]["intents"] == list(geo_engines.SERP_INTENTS)

    # the frontend's mirrored constant, derivable now instead of retyped
    serp = [e for e, s in specs.items() if s["kind"] == "serp"]
    assert serp == list(geo_engines.SERP_ENGINES)
    assert set(specs) == set(geo_engines.ENGINE_SPECS)


def test_the_status_endpoint_explains_a_paused_google_engine(fake_engines_with_serp):
    """A budget event must not look like a broken engine. Before this, a spent
    SERP month was only visible by starting a check."""
    put_prompts(2)
    assert client.put(f"/api/geo/brands/{BRAND['id']}/config",
                      json={"aio_monthly_cap": 1}).status_code == 200
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 50})

    status = client.get(f"/api/geo/brands/{BRAND['id']}/poll/status").json()

    assert status["aio_capped"] is True
    # the limit travels with the count, so the copy reads "2 of 1", not "2"
    assert (status["aio_credits_month"], status["aio_monthly_cap"]) == (2, 1)
    assert status["serp_capped_since"]

    # and it joins to the report's per-engine last measurement: "AI Overview
    # last measured X, paused since Y", one sentence, same clock
    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["engine_last_seen"]["aio"] <= status["serp_capped_since"]
