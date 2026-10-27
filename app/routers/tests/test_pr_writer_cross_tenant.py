"""PR Writer (a13) tenancy — participants only, people only.

Modelled on ``test_blog_writer_cross_tenant.py``, with the stricter rule PR
Writer was built on: a run is visible to its participants and to nobody else.
Admins are NOT implicit readers (ADMIN_EMAILS may hold whole ``@domain``
entries, and drafts can be embargoed), so an admin outside the run gets the
same 404 as a stranger. Every route is driven, not sampled: a route added
later and forgotten here fails the coverage check at the bottom.

Second half: no approval without a person. No route accepts an actor in its
body, every route needs a signed-in principal, the store refuses the agent's
own identity, and the role rows refuse the wrong person.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.main import app as fastapi_app
from app.routers import pr_writer as pr_router
from app.routers.tests.conftest import client
from app.route_inventory import iter_api_routes
from app.routers.tests.pr_writer_fakes import (
    ADMIN, BRANDOWNER, COMMS, LEGAL, NEWS, OWNER, REVIEWERS, STRANGER, install, person,
)
from app.scopes import SCOPE_REFUSED
from app.security import get_current_user
from app.services import firestore_repo as repo

GEO_ONLY = dict(person("geoonly000000000000000000000000x8", "contractor@aivirtual.com"), is_geo_only=True)


@pytest.fixture()
def world(monkeypatch):
    return install(monkeypatch, card_reviewers=REVIEWERS)


def _as(as_caller, who):
    as_caller(dict(who))


def _drafted_run(as_caller) -> dict:
    _as(as_caller, OWNER)
    run = client.post("/api/pr/runs", json={"brand_id": "legalsoft", "news": NEWS}).json()
    while run["next_step"]:
        run = client.post(f"/api/pr/runs/{run['id']}/steps/{run['next_step']}").json()
    assert run["current_version"] == "0.1", run
    return run


def _run_routes(run: dict) -> list[tuple[str, str, dict | None]]:
    rid = run["id"]
    claim = run["draft"]["claims"][0]["id"]
    quote = run["draft"]["quotes"][0]
    v = {"version": run["current_version"]}
    return [
        ("GET", f"/api/pr/runs/{rid}", None),
        ("GET", f"/api/pr/runs/{rid}/versions/0.1", None),
        ("GET", f"/api/pr/runs/{rid}/export?format=md", None),
        ("POST", f"/api/pr/runs/{rid}/steps/research", {}),
        ("POST", f"/api/pr/runs/{rid}/clarify", {"mode": "placeholders"}),
        ("POST", f"/api/pr/runs/{rid}/escalation/clear", {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/escalation/keep-paused", {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/escalation/close", {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/escalation/withdraw", {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/escalation/notes", {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/blocks/b1/revise", v | {"comment": "x"}),
        ("POST", f"/api/pr/runs/{rid}/claims/{claim}/verify", v | {"note": "x", "value": "y"}),
        ("POST", f"/api/pr/runs/{rid}/claims/{claim}/remove", v),
        ("POST", f"/api/pr/runs/{rid}/review", v),
        ("POST", f"/api/pr/runs/{rid}/signoffs/legal/sign-off", v),
        ("POST", f"/api/pr/runs/{rid}/signoffs/legal/ask-changes", v | {"note": "x"}),
        ("POST", f"/api/pr/runs/{rid}/approve", v),
        ("POST", f"/api/pr/runs/{rid}/quotes/{quote['id']}/approval", {"evidence": "x", "text_hash": quote["text_hash"]}),
        ("POST", f"/api/pr/runs/{rid}/participants", {"user_ids": [STRANGER["id"]]}),
    ]


def _send(method: str, path: str, body):
    return client.get(path) if method == "GET" else client.post(path, json=body)


# --------------------------------------------------------------------------- #
# Participants only
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("outsider", [STRANGER, ADMIN], ids=["stranger", "admin"])
def test_every_run_route_is_404_for_a_non_participant(world, as_caller, outsider):
    db, model = world
    run = _drafted_run(as_caller)
    before = {k: v for k, v in db.docs.items()}
    calls = len(model.calls)
    _as(as_caller, outsider)
    for method, path, body in _run_routes(run):
        r = _send(method, path, body)
        assert r.status_code == 404, (path, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "run_not_found", path
        assert NEWS not in r.text, path
    assert db.docs == before and len(model.calls) == calls  # nothing written, nothing billed


def test_an_unknown_run_answers_exactly_like_someone_elses(world, as_caller):
    run = _drafted_run(as_caller)
    _as(as_caller, STRANGER)
    theirs = client.get(f"/api/pr/runs/{run['id']}")
    missing = client.get("/api/pr/runs/pr-" + "0" * 32)
    malformed = client.get("/api/pr/runs/not-a-run")
    assert theirs.status_code == missing.status_code == malformed.status_code == 404
    assert theirs.json() == missing.json() == malformed.json()


def test_the_run_list_never_names_another_persons_run(world, as_caller):
    run = _drafted_run(as_caller)
    for outsider in (STRANGER, ADMIN):
        _as(as_caller, outsider)
        body = client.get("/api/pr/runs").text
        assert run["id"] not in body and NEWS not in body
    _as(as_caller, LEGAL)  # a named reviewer is a participant
    assert [r["id"] for r in client.get("/api/pr/runs").json()["runs"]] == [run["id"]]


def test_the_control_every_participant_reads_the_run(world, as_caller):
    run = _drafted_run(as_caller)
    for who in (OWNER, COMMS, BRANDOWNER, LEGAL):
        _as(as_caller, who)
        assert client.get(f"/api/pr/runs/{run['id']}").status_code == 200, who["email"]


# --------------------------------------------------------------------------- #
# GEO-only accounts never reach PR Writer
# --------------------------------------------------------------------------- #

def test_a_geo_only_account_is_refused_on_every_pr_route(world, monkeypatch):
    from app.security import create_token
    from app.config import settings

    monkeypatch.setattr(settings, "jwt_secret", "test-secret-for-pr-writer-geo-only-wall-0123", raising=False)
    monkeypatch.setattr(settings, "allowed_emails", GEO_ONLY["email"], raising=False)
    monkeypatch.setattr(settings, "geo_only_emails", GEO_ONLY["email"], raising=False)
    token = create_token(GEO_ONLY["id"], GEO_ONLY["email"])
    headers = {"Authorization": f"Bearer {token}"}
    paths = [("GET", "/api/pr/brands"), ("GET", "/api/pr/members"), ("GET", "/api/pr/runs"),
             ("POST", "/api/pr/runs"), ("GET", "/api/pr/brands/legalsoft/card"),
             ("POST", "/api/pr/brands/legalsoft/card/suggest")]
    for method, path in paths:
        r = client.request(method, path, headers=headers, json={"brand_id": "legalsoft", "news": NEWS})
        assert r.status_code == 403 and r.json()["detail"] == SCOPE_REFUSED, (path, r.text)


# --------------------------------------------------------------------------- #
# Role refusals
# --------------------------------------------------------------------------- #

def _locked(as_caller, run):
    _as(as_caller, OWNER)
    r = client.post(f"/api/pr/runs/{run['id']}/review", json={"version": run["current_version"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_each_sign_off_row_belongs_to_its_named_person(world, as_caller):
    run = _locked(as_caller, _drafted_run(as_caller))
    v = {"version": run["current_version"]}
    for who, row in ((COMMS, "legal"), (LEGAL, "comms_lead"), (BRANDOWNER, "legal"), (OWNER, "brand_owner")):
        _as(as_caller, who)
        r = client.post(f"/api/pr/runs/{run['id']}/signoffs/{row}/sign-off", json=v)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_your_row", (who["email"], row)
    _as(as_caller, LEGAL)
    assert client.post(f"/api/pr/runs/{run['id']}/signoffs/legal/sign-off", json=v).status_code == 200


def test_only_the_comms_lead_records_quotes_and_approves(world, as_caller):
    run = _locked(as_caller, _drafted_run(as_caller))
    q = run["draft"]["quotes"][0]
    for who in (OWNER, LEGAL, BRANDOWNER):
        _as(as_caller, who)
        r = client.post(f"/api/pr/runs/{run['id']}/quotes/{q['id']}/approval",
                        json={"evidence": "email", "text_hash": q["text_hash"]})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "comms_lead_only", who["email"]
        r = client.post(f"/api/pr/runs/{run['id']}/approve", json={"version": run["current_version"]})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "comms_lead_only", who["email"]


def test_only_the_designated_clearer_clears_an_escalation(world, as_caller):
    db, model = world
    model.escalate = True
    _as(as_caller, OWNER)
    run = client.post("/api/pr/runs", json={"brand_id": "legalsoft", "news": NEWS}).json()
    for who in (OWNER, COMMS, BRANDOWNER):
        _as(as_caller, who)
        r = client.post(f"/api/pr/runs/{run['id']}/escalation/clear", json={"note": "fine"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_the_clearer", who["email"]
    _as(as_caller, BRANDOWNER)
    r = client.post(f"/api/pr/runs/{run['id']}/escalation/close", json={"note": "no"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_comms_or_legal"


def test_only_the_card_comms_lead_or_an_admin_edits_the_card(world, as_caller):
    body = {"base_revision": 1, "card": {"reviewers": REVIEWERS}}
    for who in (OWNER, LEGAL, BRANDOWNER, STRANGER):
        _as(as_caller, who)
        r = client.put("/api/pr/brands/legalsoft/card", json=body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "cannot_edit_card", who["email"]
    _as(as_caller, ADMIN)
    assert client.put("/api/pr/brands/legalsoft/card", json=body).status_code == 200


# --------------------------------------------------------------------------- #
# No approval without a person
# --------------------------------------------------------------------------- #

_ACTOR_WORDS = ("actor", "user_id", "by", "signed_by", "approved_by_id", "owner_id", "agent")


def _pr_routes() -> list[tuple[str, APIRoute]]:
    """``(published path, route)`` for every PR Writer route the app serves.

    Walks the app with ``iter_api_routes`` (the conformance gate's walker):
    under fastapi 0.141 ``include_router`` leaves wrapped ``_IncludedRouter``
    entries in ``app.routes``, so a top-level ``isinstance(APIRoute)`` scan
    finds 0 of the PR routes and every sweep below passes on nothing. The
    count assertion in ``test_every_pr_route_needs_a_signed_in_person`` is
    what makes a vacuous pass impossible."""
    return [(path, r) for path, r in iter_api_routes(fastapi_app) if r.endpoint.__module__ == pr_router.__name__]


def _calls(dependant) -> set:
    out = {dependant.call} if dependant.call else set()
    for sub in dependant.dependencies:
        out |= _calls(sub)
    return out


def test_every_pr_route_needs_a_signed_in_person():
    routes = _pr_routes()
    assert len(routes) >= 25, [path for path, _ in routes]  # never vacuous: the PR router has 25+ routes
    assert len(routes) == 26, [path for path, _ in routes]  # 25 + card suggestions (2026-10-28)
    missing = [path for path, r in routes if get_current_user not in _calls(r.dependant)]
    assert missing == []


def test_no_pr_route_takes_an_actor_from_the_body():
    offenders = []
    for path, route in _pr_routes():
        body = route.dependant.body_params
        for param in body:
            model = param.type_ if hasattr(param, "type_") else None
            fields = getattr(getattr(param, "field_info", None), "annotation", None) or model
            names = set(getattr(fields, "model_fields", {}) or {})
            offenders += [f"{path}: {n}" for n in names if n in _ACTOR_WORDS]
    assert offenders == []
    # the body models themselves, read directly
    for name, obj in inspect.getmembers(pr_router, inspect.isclass):
        for field in getattr(obj, "model_fields", {}) or {}:
            assert field not in _ACTOR_WORDS, f"{name}.{field}"


def test_an_anonymous_request_cannot_sign_or_approve(world):
    rid = "pr-" + "a" * 32
    for path in (f"/api/pr/runs/{rid}/approve", f"/api/pr/runs/{rid}/signoffs/legal/sign-off"):
        assert client.post(path, json={"version": "1.0"}).status_code == 401


def test_the_store_refuses_the_agents_own_identity_on_every_human_write(world, as_caller):
    run = _locked(as_caller, _drafted_run(as_caller))
    rid = run["id"][3:]
    q = run["draft"]["quotes"][0]
    for actor in (repo.PR_AGENT_ACTOR, "system", "", "cron:pr"):
        with pytest.raises(repo.PrActorRefused):
            repo.record_pr_signoff(rid, role="final", actor_id=actor, version=2)
        with pytest.raises(repo.PrActorRefused):
            repo.attest_pr_quote(rid, q["id"], actor_id=actor, text_hash=q["text_hash"])
        with pytest.raises(repo.PrActorRefused):
            repo.update_pr_flow(rid, actor_id=actor, mutate=lambda r, f: f)


def test_the_route_set_here_is_the_route_set_mounted():
    """Coverage: a /api/pr route added later must be added to the sweep above."""
    swept = {"/api/pr/runs/{run_id}", "/api/pr/runs/{run_id}/versions/{version}", "/api/pr/runs/{run_id}/export",
             "/api/pr/runs/{run_id}/steps/{step}", "/api/pr/runs/{run_id}/clarify",
             "/api/pr/runs/{run_id}/escalation/clear", "/api/pr/runs/{run_id}/escalation/keep-paused",
             "/api/pr/runs/{run_id}/escalation/close", "/api/pr/runs/{run_id}/escalation/withdraw",
             "/api/pr/runs/{run_id}/escalation/notes", "/api/pr/runs/{run_id}/blocks/{block_id}/revise",
             "/api/pr/runs/{run_id}/claims/{claim_id}/verify", "/api/pr/runs/{run_id}/claims/{claim_id}/remove",
             "/api/pr/runs/{run_id}/review", "/api/pr/runs/{run_id}/signoffs/{row_id}/sign-off",
             "/api/pr/runs/{run_id}/signoffs/{row_id}/ask-changes", "/api/pr/runs/{run_id}/approve",
             "/api/pr/runs/{run_id}/quotes/{quote_id}/approval", "/api/pr/runs/{run_id}/participants"}
    mounted = {path for path, _ in _pr_routes() if "{run_id}" in path}
    assert mounted == swept
