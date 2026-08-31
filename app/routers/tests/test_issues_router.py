"""Integration tests for the Issues router (/api/issues). Fully offline.

The router is mounted on a FastAPI app of its own here — it is not yet
included in ``app.main`` — with the auth dependency overridden the same way
the shared conftest does it. Every document is seeded through the real save
functions against the local-file state adapter; only the outer seams (the
brand registry, the engine status, the run-log module) are faked.
"""
from __future__ import annotations

import os
import sys
import types

os.environ["SEO_OFFLINE"] = "1"

import app  # noqa: F401 - registers agent roots on sys.path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import issues as issues_router
from app.security import get_current_user
from final_geo_agent import geo_engines, geo_poll, geo_strategy
from seo_geo_agent import insights, state as seo_state

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}
OFF_BRAND = {"id": "dormant", "name": "Dormant", "domain": "dormant.com", "enabled": False}
USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}

GSC_403 = (
    "Search Console rejected sc-domain:legalsoft.com: <HttpError 403 when requesting "
    "https://searchconsole.googleapis.com/x?alt=json returned \"User does not have "
    "sufficient permission for site 'sc-domain:legalsoft.com'. See also: "
    "https://support.google.com/webmasters/answer/9999\">"
)

ALL_ON = {
    engine: {"connected": True, "mode": "native", "model": "m", "means": ""}
    for engine in ("perplexity", "gemini", "chatgpt", "aio", "ai_mode")
}

app_under_test = FastAPI()
app_under_test.include_router(issues_router.router, prefix="/api")
client = TestClient(app_under_test)


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND), dict(OFF_BRAND)])
    monkeypatch.setattr(geo_engines, "engine_status", lambda: dict(ALL_ON))
    app_under_test.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    app_under_test.dependency_overrides.clear()


@pytest.fixture()
def runlog(monkeypatch):
    """Install a fake ``final_geo_agent.geo_runlog`` whose entries the test sets."""
    entries: dict[str, list[dict]] = {}
    module = types.ModuleType(issues_router.GEO_RUNLOG_MODULE)
    module.recent_runs = lambda brand_id, n: list(entries.get(brand_id, []))[:n]
    monkeypatch.setitem(sys.modules, issues_router.GEO_RUNLOG_MODULE, module)
    return entries


def _codes(body: dict, brand_id: str = "legalsoft") -> set[str]:
    return {i["code"] for i in body["issues"] if i["brand_id"] == brand_id}


def test_requires_a_signed_in_caller():
    app_under_test.dependency_overrides.clear()
    assert client.get("/api/issues").status_code == 401


def test_composes_seo_and_geo_signals_for_enabled_brands_only(runlog):
    seo_state.save("run-legalsoft", {"brand_id": "legalsoft", "at": "2026-08-29",
                                     "degraded": [f"Search Console: {GSC_403}"],
                                     "summary": {}, "todos": [], "topics": []})
    geo_poll.ensure_config(BRAND)   # created, never swept

    r = client.get("/api/issues")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"issues", "counts", "generated_at"}
    assert _codes(body) == {"gsc_no_access", "never_swept"}
    assert not [i for i in body["issues"] if i["brand_id"] == "dormant"]

    gsc = next(i for i in body["issues"] if i["code"] == "gsc_no_access")
    assert gsc["severity"] == "high" and gsc["since"] == "2026-08-29"
    assert gsc["title"] == "Search Console has not granted access to sc-domain:legalsoft.com"
    assert "<HttpError" not in gsc["detail"] and "https://" not in gsc["detail"]
    assert gsc["fix"] == {"label": "Open the fix list", "workspace": "seo",
                          "subject": "legalsoft", "section": "fixes"}

    assert body["issues"][0]["severity"] == "high"
    assert body["counts"] == {"high": 1, "medium": 1, "low": 0}
    assert not [i for i in body["issues"] if i["code"].startswith("unreadable_")]


def test_never_measured_and_engine_off_and_failed_engine(runlog, monkeypatch):
    status = dict(ALL_ON) | {"aio": {"connected": False, "mode": "off", "model": "", "means": ""}}
    monkeypatch.setattr(geo_engines, "engine_status", lambda: status)
    geo_poll.ensure_config(BRAND)
    geo_poll.mark_poll_completed("legalsoft")
    runlog["legalsoft"] = [{"finished_at": "2026-08-30T02:10:00+00:00", "completed": False,
                            "engines": ["chatgpt"], "calls": {"chatgpt": 12},
                            "errors": {"chatgpt": 12}, "no_aio": 0, "score": None}]

    body = client.get("/api/issues").json()
    assert _codes(body) == {"seo_never_measured", "engine_off_aio", "engine_failed_chatgpt"}
    by_code = {i["code"]: i for i in body["issues"]}
    assert by_code["engine_failed_chatgpt"]["severity"] == "high"
    assert by_code["engine_off_aio"]["title"] == "Google AI Overview is not connected"
    assert body["counts"] == {"high": 1, "medium": 2, "low": 0}


def test_stale_plan_read_through_the_real_strategy_store(runlog, monkeypatch):
    geo_poll.ensure_config(BRAND)
    geo_poll.mark_poll_completed("legalsoft")
    seo_state.save(geo_strategy.strategy_doc_id("legalsoft"), {
        "brand_id": "legalsoft", "history": [],
        "current": {"generated_at": "2026-01-01T00:00:00+00:00", "summary": "s",
                    "waves": [{"weeks": "1-2", "actions": [{"id": "a1", "status": "todo"}]}]},
    })
    body = client.get("/api/issues").json()
    assert "plan_untouched" in _codes(body)
    assert next(i for i in body["issues"] if i["code"] == "plan_untouched")["fix"]["section"] == "plan"


def test_one_source_raising_is_a_low_issue_not_a_500(runlog, monkeypatch):
    seo_state.save("run-legalsoft", {"brand_id": "legalsoft", "at": "2026-08-29", "degraded": [],
                                     "summary": {}, "todos": [], "topics": []})

    def _boom(brand):
        raise RuntimeError("Firestore unavailable — index missing")

    monkeypatch.setattr(geo_poll, "ensure_config", _boom)
    r = client.get("/api/issues")
    assert r.status_code == 200, r.text
    body = r.json()
    unreadable = next(i for i in body["issues"] if i["code"] == "unreadable_geo_configuration")
    assert unreadable["severity"] == "low" and unreadable["brand_id"] == "legalsoft"
    assert unreadable["title"] == "GEO configuration could not be read"
    assert "Firestore" not in unreadable["detail"] and "index" not in unreadable["detail"]
    # the config-dependent rules stay silent, and say so, rather than guessing
    assert "never_swept" not in _codes(body) and "sweep_stale" not in _codes(body)


def test_missing_run_log_module_reads_as_unreadable_not_healthy(monkeypatch):
    monkeypatch.setitem(sys.modules, issues_router.GEO_RUNLOG_MODULE, None)
    geo_poll.ensure_config(BRAND)
    body = client.get("/api/issues").json()
    assert "unreadable_geo_run_log" in _codes(body)
    assert all(i["severity"] == "low" for i in body["issues"] if i["code"] == "unreadable_geo_run_log")


def test_brand_registry_failure_is_one_workspace_issue(runlog, monkeypatch):
    def _boom():
        raise RuntimeError("no brands doc")

    monkeypatch.setattr(insights, "list_brands", _boom)
    r = client.get("/api/issues")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["code"] for i in body["issues"]] == ["unreadable_brand_registry"]
    assert body["issues"][0]["brand"] == "All brands" and body["issues"][0]["brand_id"] == ""
    assert body["counts"] == {"high": 0, "medium": 0, "low": 1}


def test_engine_status_failure_drops_engine_rules_and_says_so(runlog, monkeypatch):
    def _boom():
        raise RuntimeError("runtime config unreachable")

    monkeypatch.setattr(geo_engines, "engine_status", _boom)
    geo_poll.ensure_config(BRAND)
    body = client.get("/api/issues").json()
    assert any(i["code"] == "unreadable_engine_status" and i["brand_id"] == "" for i in body["issues"])
    assert not any(i["code"].startswith("engine_off_") for i in body["issues"])
