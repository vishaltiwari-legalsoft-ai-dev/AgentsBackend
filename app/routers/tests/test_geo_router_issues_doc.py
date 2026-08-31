"""Router integration for the GEO surface that landed with the run log, prompt
personas, page check, plan assignment and the Issues record. Fully offline.

Two kinds of test, on purpose. Most fake the module seam the handler calls
(``geo_prompts.add_prompts``, ``page_check.check``, …) and prove the HTTP
contract around it: status codes, exception mapping, the guard, the body
shape. A few drive the real stores through the app instead, because the
wiring bugs the seam-fakes cannot see — a dumped field the store does not
expect, a fallback body missing a key — are the ones the console hits first.
"""
from __future__ import annotations

import os

os.environ["SEO_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.routers.tests.conftest import client
from final_geo_agent import (
    geo_engines, geo_history, geo_prompts, geo_runlog, geo_strategy, opt_pipeline,
    page_check,
)
from final_geo_agent.geo_engines import EngineAnswer
from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}
B = f"/api/geo/brands/{BRAND['id']}"

OWNER = {"id": "u1", "email": "owner@legalsoft.com", "is_admin": False,
         "is_creator": True, "session_id": "", "timezone": "UTC"}
VIEWER = {"id": "u2", "email": "viewer@legalsoft.com", "is_admin": False,
          "is_creator": False, "session_id": "", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path, as_caller):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "geo_state"))
    # a real key in local .env must never turn an offline test into a paid poll
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    monkeypatch.setattr(geo_engines, "dataforseo_creds", lambda: ("", ""))
    monkeypatch.setattr(geo_engines, "serpapi_key", lambda: "")
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    as_caller(OWNER)


@pytest.fixture()
def fake_engines(monkeypatch):
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {e: e == "perplexity" for e in geo_engines.ALL_ENGINES})
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, model="fake",
            text=f"Legal Soft and Clio both handle: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        ),
    )


# ------------------------------------------------------------------ prompts: bulk


def test_bulk_paste_answers_200_with_partial_acceptance(monkeypatch):
    seen = {}

    def fake_add(brand_id, raw, *, persona, intent, stage):
        seen.update(brand_id=brand_id, raw=raw, persona=persona, intent=intent, stage=stage)
        return {
            "added": [{"id": "a1b2c3d4", "text": "best legal va for solo firms", "persona": persona}],
            "skipped": [{"text": "hi", "reason": geo_prompts.REASON_TOO_SHORT}],
            "total": 1,
            "universe": {"brand_id": brand_id, "prompts": [], "personas": []},
        }

    monkeypatch.setattr(geo_prompts, "add_prompts", fake_add)
    resp = client.post(f"{B}/prompts/bulk", json={
        "text": "best legal va for solo firms\nhi", "persona": "solo-attorney",
        "intent": "problem", "stage": "awareness",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["reason"] for s in body["skipped"]] == [geo_prompts.REASON_TOO_SHORT]
    assert len(body["added"]) == 1 and body["total"] == 1
    assert seen == {"brand_id": "legalsoft", "raw": "best legal va for solo firms\nhi",
                    "persona": "solo-attorney", "intent": "problem", "stage": "awareness"}


def test_bulk_paste_maps_a_store_refusal_to_422(monkeypatch):
    def refuse(*a, **k):
        raise ValueError("universe is full (250)")

    monkeypatch.setattr(geo_prompts, "add_prompts", refuse)
    resp = client.post(f"{B}/prompts/bulk", json={"text": "one more prompt please"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "universe is full (250)"


def test_bulk_paste_is_creator_only(monkeypatch, as_caller):
    monkeypatch.setattr(geo_prompts, "add_prompts",
                        lambda *a, **k: pytest.fail("the store must not be reached"))
    as_caller(VIEWER)
    assert client.post(f"{B}/prompts/bulk", json={"text": "best legal va"}).status_code == 403


def test_bulk_paste_body_bounds():
    assert client.post(f"{B}/prompts/bulk", json={"text": ""}).status_code == 422
    assert client.post(f"{B}/prompts/bulk", json={"text": "x" * 20_001}).status_code == 422


def test_bulk_paste_through_the_real_store_lands_and_reports_per_line():
    """No seam faked: the router's dump reaches the store as the store expects."""
    resp = client.post(f"{B}/prompts/bulk", json={
        "text": "- best legal virtual assistant\n- best legal virtual assistant\n- hi",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["text"] for p in body["added"]] == ["best legal virtual assistant"]
    assert body["added"][0]["source"] == "custom" and body["added"][0]["persona"] == ""
    assert sorted(s["reason"] for s in body["skipped"]) == sorted(
        [geo_prompts.REASON_DUPLICATE_BATCH, geo_prompts.REASON_TOO_SHORT])
    got = client.get(f"{B}/prompts").json()
    assert [p["text"] for p in got["prompts"]] == ["best legal virtual assistant"]


# ------------------------------------------------------------------ personas


def test_personas_invalid_input_is_422_with_the_store_reason(monkeypatch):
    def refuse(brand_id, personas):
        raise ValueError("Persona label must be 2-60 characters")

    monkeypatch.setattr(geo_prompts, "set_personas", refuse)
    resp = client.put(f"{B}/personas", json={"personas": [{"label": "x"}]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Persona label must be 2-60 characters"


def test_personas_is_creator_only(as_caller):
    as_caller(VIEWER)
    assert client.put(f"{B}/personas", json={"personas": []}).status_code == 403


def test_personas_more_than_the_cap_is_422():
    too_many = [{"label": f"Persona {i}"} for i in range(geo_prompts.MAX_PERSONAS + 1)]
    assert client.put(f"{B}/personas", json={"personas": too_many}).status_code == 422


def test_personas_roundtrip_through_the_real_store_and_tag_a_paste():
    resp = client.put(f"{B}/personas", json={"personas": [
        {"label": "Solo attorney", "description": "Runs a one-lawyer practice"},
        {"key": "Firm Admin", "label": "Firm administrator"},
    ]})
    assert resp.status_code == 200, resp.text
    personas = resp.json()["personas"]
    assert [p["key"] for p in personas] == ["solo-attorney", "firm-admin"]

    pasted = client.post(f"{B}/prompts/bulk", json={
        "text": "how do solo attorneys handle intake calls", "persona": "solo-attorney",
    }).json()
    assert pasted["added"][0]["persona"] == "solo-attorney"

    # dropping the persona untags the prompt in the same write
    cleared = client.put(f"{B}/personas", json={"personas": []}).json()
    assert cleared["personas"] == []
    assert cleared["prompts"][0]["persona"] == ""


def test_prompts_fallback_carries_personas_before_any_write():
    body = client.get(f"{B}/prompts").json()
    assert body == {"brand_id": "legalsoft", "prompts": [], "personas": []}


def test_put_prompts_without_persona_keeps_the_stored_tag():
    """A client built before personas round-trips the universe without the
    field; that must not untag it."""
    client.put(f"{B}/personas", json={"personas": [{"label": "Solo attorney"}]})
    added = client.post(f"{B}/prompts/bulk", json={
        "text": "best intake service for solo attorneys", "persona": "solo-attorney",
    }).json()["added"][0]
    legacy = {k: v for k, v in added.items() if k != "persona"}
    saved = client.put(f"{B}/prompts", json={"prompts": [legacy]}).json()
    assert saved["prompts"][0]["persona"] == "solo-attorney"
    # and an explicit "" is an untag
    saved = client.put(f"{B}/prompts", json={"prompts": [{**legacy, "persona": ""}]}).json()
    assert saved["prompts"][0]["persona"] == ""


def test_put_prompts_over_the_cap_is_422():
    prompts = [{"id": f"p{i}", "text": f"prompt number {i}"}
               for i in range(geo_prompts.MAX_UNIVERSE + 1)]
    assert client.put(f"{B}/prompts", json={"prompts": prompts}).status_code == 422


# ------------------------------------------------------------------ config


def test_config_competitors_are_typed_and_replaced_whole():
    resp = client.put(f"{B}/config", json={"competitors": [
        {"key": "clio", "name": "Clio", "domain": "clio.com", "aliases": ["Clio"]},
        {"name": "Smith.ai"},
    ], "aio_monthly_cap": 500})
    assert resp.status_code == 200, resp.text
    cfg = resp.json()
    assert cfg["aio_monthly_cap"] == 500
    assert cfg["competitors"][1] == {"name": "Smith.ai"}      # no null keys stored
    assert "domain" not in cfg["competitors"][1]

    # removing a rival is a PUT of the list without it
    cfg = client.put(f"{B}/config", json={"competitors": [{"name": "Smith.ai"}]}).json()
    assert [c["name"] for c in cfg["competitors"]] == ["Smith.ai"]
    assert cfg["aio_monthly_cap"] == 500                       # untouched fields stay

    assert client.put(f"{B}/config", json={"competitors": [{"domain": "x.com"}]}).status_code == 422
    assert client.put(f"{B}/config", json={"aio_monthly_cap": 0}).status_code == 422
    assert client.put(f"{B}/config", json={"competitors": [{"name": "A"}] * 26}).status_code == 422


def test_geo_config_lists_every_engine_with_a_label():
    body = client.get("/api/geo/config").json()
    assert set(body["engines"]) == set(geo_engines.ALL_ENGINES)
    assert "ai_mode" in body["engine_status"]
    assert body["engine_labels"]["ai_mode"] == "Google AI Mode"
    assert body["default_aio_monthly_cap"] == geo_engines.SERP_RUNS_PER_PROMPT * 0 + 2000


# ------------------------------------------------------------------ history


def test_history_payload_carries_weekly_and_runs(monkeypatch):
    weekly = [{"week": "2026-W35", "start": "2026-08-24", "score": 41.0,
               "mention_rate": 0.5, "citation_rate": 0.1, "n_sweeps": 2,
               "all_partial": False, "delta_score": None}]
    runs = [{"id": "r1", "day": "20260829", "trigger": "cron", "completed": True}]
    asked = {}

    def fake_weekly(points, *, weeks):
        asked["weeks"] = weeks
        return weekly

    monkeypatch.setattr(geo_history, "weekly_rollup", fake_weekly)
    monkeypatch.setattr(geo_runlog, "recent_runs",
                        lambda brand_id, n=30: runs if (brand_id, n) == ("legalsoft", 30) else [])
    body = client.get(f"{B}/history").json()
    assert body["weekly"] == weekly
    assert body["runs"] == runs
    assert asked["weeks"] == geo_history.DEFAULT_ROLLUP_WEEKS
    assert body["points"] == [] and body["trend"]["current"] is None

    client.get(f"{B}/history", params={"weeks": 4})
    assert asked["weeks"] == 4


def test_history_before_any_sweep_has_empty_weekly_and_runs():
    body = client.get(f"{B}/history").json()
    assert body["weekly"] == [] and body["runs"] == []


def test_history_runs_come_from_the_real_run_log_after_a_sweep(fake_engines):
    prompts = [{"id": f"p{i}", "text": f"best legal va {i}"} for i in range(1, 3)]
    assert client.put(f"{B}/prompts", json={"prompts": prompts}).status_code == 200
    progress = client.post(f"{B}/poll/step", json={"runs": 1, "batch_size": 10}).json()
    assert progress["done"] == progress["total"] == 2
    body = client.get(f"{B}/history").json()
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["trigger"] == "manual" and run["completed"] is True
    assert run["engines"] == ["perplexity"]


# ------------------------------------------------------------------ report


def test_report_carries_the_persona_rollup(fake_engines):
    client.put(f"{B}/personas", json={"personas": [{"label": "Solo attorney"}]})
    client.post(f"{B}/prompts/bulk", json={
        "text": "best legal va for a solo practice", "persona": "solo-attorney",
    })
    client.post(f"{B}/prompts/bulk", json={"text": "best legal va for a large firm"})
    client.post(f"{B}/poll/step", json={"runs": 1, "batch_size": 10})

    report = client.get(f"{B}/report").json()
    rollup = {r["persona"]: r for r in report["persona_rollup"]}
    assert set(rollup) == {"solo-attorney", ""}
    assert rollup["solo-attorney"]["n_prompts"] == 1
    assert rollup["solo-attorney"]["mention_rate"] == 1.0
    assert report["persona_rollup"][-1]["persona"] == ""      # unassigned last


# ------------------------------------------------------------------ strategy actions


def test_action_update_accepts_an_assignee_and_a_status(monkeypatch):
    calls = []

    def fake_update(brand_id, action_id, *, status=None, assignee=None):
        calls.append((brand_id, action_id, status, assignee))
        return {"brand_id": brand_id, "current": {"waves": []}}

    monkeypatch.setattr(geo_strategy, "update_action", fake_update)
    resp = client.put(f"{B}/strategy/actions/act1", json={"assignee": "Priya"})
    assert resp.status_code == 200, resp.text
    resp = client.put(f"{B}/strategy/actions/act1", json={"status": "in_progress", "assignee": ""})
    assert resp.status_code == 200
    assert calls == [("legalsoft", "act1", None, "Priya"),
                     ("legalsoft", "act1", "in_progress", "")]


def test_action_update_rejects_a_bad_status_and_an_empty_body(monkeypatch):
    monkeypatch.setattr(geo_strategy, "update_action",
                        lambda *a, **k: pytest.fail("validation must happen before the store"))
    assert client.put(f"{B}/strategy/actions/act1", json={"status": "later"}).status_code == 422
    assert client.put(f"{B}/strategy/actions/act1",
                      json={"assignee": "x" * (geo_strategy.ASSIGNEE_MAX + 1)}).status_code == 422


def test_action_update_with_nothing_to_update_is_422_from_the_store():
    resp = client.put(f"{B}/strategy/actions/act1", json={})
    assert resp.status_code == 422
    assert "nothing to update" in resp.json()["detail"]


def test_action_update_without_a_plan_is_404():
    resp = client.put(f"{B}/strategy/actions/act1", json={"status": "done"})
    assert resp.status_code == 404


# ------------------------------------------------------------------ page check


def _check_doc(**block):
    return {
        "meta": {"analysis_id": "cold-brew-1a2b3c4d", "brand_id": "legalsoft"},
        "page_check": {"source_url": "https://legalsoft.com/x", "target_query": "cold brew",
                       "verdict": {"label": "likely helps"}, **block},
    }


def test_page_check_calls_the_check_with_the_brand_and_returns_its_doc(monkeypatch):
    seen = {}

    def fake_check(brand, *, url, draft, keyword, locale):
        seen.update(brand=brand["id"], url=url, draft=draft, keyword=keyword, locale=locale)
        return _check_doc()

    monkeypatch.setattr(page_check, "check", fake_check)
    resp = client.post(f"{B}/page-check", json={"url": "https://legalsoft.com/x", "keyword": "cold brew"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["page_check"]["verdict"]["label"] == "likely helps"
    assert seen == {"brand": "legalsoft", "url": "https://legalsoft.com/x", "draft": "",
                    "keyword": "cold brew", "locale": "en-US"}


@pytest.mark.parametrize(("exc", "status"), [
    (LookupError("unknown analysis"), 404),
    (ValueError("send exactly one of url or draft"), 422),
    (KeyError("unknown vertical: dentistry"), 422),
    (CredentialMissing("Serper key missing — add SEO_SERPER_API_KEY in Secrets"), 503),
])
def test_page_check_maps_each_exception_to_its_status(monkeypatch, exc, status):
    def raise_it(brand, **kw):
        raise exc

    monkeypatch.setattr(page_check, "check", raise_it)
    resp = client.post(f"{B}/page-check", json={"draft": "# cold brew\n\ntext"})
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]


def test_page_check_body_bounds():
    assert client.post(f"{B}/page-check", json={"url": "h" * 2001}).status_code == 422
    assert client.post(f"{B}/page-check", json={"keyword": "k" * 201}).status_code == 422


def test_page_check_is_readable_by_a_non_creator(monkeypatch, as_caller):
    monkeypatch.setattr(page_check, "check", lambda brand, **kw: _check_doc())
    as_caller(VIEWER)
    assert client.post(f"{B}/page-check", json={"url": "https://legalsoft.com/x"}).status_code == 200


def test_page_check_offline_without_a_serp_key_is_503():
    """No seam faked: the real check has no provider key offline."""
    resp = client.post(f"{B}/page-check", json={"draft": "# cold brew\n\nsome text"})
    assert resp.status_code == 503
    assert resp.json()["detail"]


def test_page_checks_list_and_get_are_brand_scoped(monkeypatch):
    monkeypatch.setattr(opt_pipeline, "list_analyses",
                        lambda brand_id: [{"id": "cold-brew-1a2b3c4d", "keyword": "cold brew"}]
                        if brand_id == "legalsoft" else [])
    assert client.get(f"{B}/page-checks").json()["analyses"][0]["id"] == "cold-brew-1a2b3c4d"

    def fake_get(brand_id, analysis_id):
        if (brand_id, analysis_id) != ("legalsoft", "cold-brew-1a2b3c4d"):
            raise LookupError(f"unknown analysis {analysis_id!r} for brand {brand_id!r}")
        return _check_doc()

    monkeypatch.setattr(opt_pipeline, "get_analysis", fake_get)
    assert client.get(f"{B}/page-checks/cold-brew-1a2b3c4d").status_code == 200
    assert client.get(f"{B}/page-checks/other-1a2b3c4d").status_code == 404
    # a path segment that is not an analysis id never reaches the store
    assert client.get(f"{B}/page-checks/..%2Fgeo-config-legalsoft").status_code in (404, 422)


def test_page_check_rescore_maps_lookup_to_404_and_returns_the_report(monkeypatch):
    def fake_rescore(brand_id, analysis_id, draft, embedder=None):
        if analysis_id != "cold-brew-1a2b3c4d":
            raise LookupError("unknown analysis")
        return {"total": 77.5, "gaps": [], "strengths": []}

    monkeypatch.setattr(opt_pipeline, "rescore", fake_rescore)
    ok = client.post(f"{B}/page-checks/cold-brew-1a2b3c4d/rescore", json={"draft": "# better"})
    assert ok.status_code == 200 and ok.json()["total"] == 77.5
    assert client.post(f"{B}/page-checks/gone-1a2b3c4d/rescore",
                       json={"draft": "# better"}).status_code == 404
    assert client.post(f"{B}/page-checks/cold-brew-1a2b3c4d/rescore", json={"draft": ""}).status_code == 422


def test_page_check_routes_answer_404_for_an_unknown_brand():
    assert client.post("/api/geo/brands/nope/page-check", json={"url": "https://x.com"}).status_code == 404
    assert client.get("/api/geo/brands/nope/page-checks").status_code == 404


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/geo/optimizer/analyze"),
    ("POST", "/api/geo/optimizer/rescore"),
    ("GET", "/api/geo/optimizer/analyses"),
    ("GET", "/api/geo/optimizer/analyses/abc"),
])
def test_the_brand_blind_optimizer_routes_are_gone(method, path):
    assert client.request(method, path, json={}).status_code in (404, 405)


# ------------------------------------------------------------------ issues


def test_issues_is_mounted_and_reads_the_shared_registry():
    body = client.get("/api/issues").json()
    assert set(body["counts"]) == {"high", "medium", "low"}
    assert body["generated_at"]
    assert all(i["brand_id"] in ("", "legalsoft") for i in body["issues"])
    # offline, keyless, never swept: the brand has open issues, not a clean bill
    assert body["counts"]["medium"] + body["counts"]["high"] > 0


def test_issues_refuses_an_anonymous_caller(unauthenticated):
    unauthenticated()
    assert client.get("/api/issues").status_code in (401, 403)
