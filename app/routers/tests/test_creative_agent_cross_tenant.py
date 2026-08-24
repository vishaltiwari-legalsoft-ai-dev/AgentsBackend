"""Cross-tenant coverage for the Creative Agent router (``/api/creative``).

The Creative rail carries the heaviest payloads in the product — finished
brochures, decks and carousel sets, downloadable as real files. Its entire
tenancy story is one helper, a near-copy of the Graphics Designer's::

    def _owned(run_id: str, user: dict) -> dict:
        run = cruns.get_run(run_id)
        if not run or run.get("user_id") != str(user["id"]):
            raise HTTPException(404, "Run not found")
        return run

Same shape as ``_owned_run``, different name, written separately, and — until
this file — tested by nobody. ``cruns.get_run`` is unscoped, so the comparison
is the whole guard, at eleven call sites.

Structure mirrors ``test_gd_cross_tenant.py`` deliberately: an owner pass that
proves each request is well-formed, and a stranger pass that proves the refusal.
The artifact download gets its own test because it is the only endpoint here
that hands over bytes, and proving it is closed requires a run that really
produced some.
"""
from __future__ import annotations

import pytest

from app.routers.tests.conftest import client

OWNER = {"id": "creative-tenant-a", "email": "a@legalsoft.com", "is_admin": False,
         "is_creator": False, "session_id": "", "timezone": "UTC"}
STRANGER = {"id": "creative-tenant-b", "email": "b@legalsoft.com", "is_admin": False,
            "is_creator": False, "session_id": "", "timezone": "UTC"}

#: What ``_owned`` says, asserted verbatim — same 404 convention as the rest of
#: the codebase, so "not yours" cannot be told from "does not exist".
NOT_FOUND = "Run not found"

#: A type the Creative Agent owns (social posts are bounced to the Graphics
#: Studio with a 400 before ownership is ever consulted). Resolved from the live
#: catalogue rather than hard-coded so a catalogue edit fails loudly here rather
#: than silently turning every case below into a 400.
CREATIVE_TYPE = "brochure"


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    """A throwaway creative-run store, with the owner signed in.

    ``CREATIVE_RUNS_ROOT`` is bound at import time, so the module attribute is
    what has to move — setting ``GD_CREATIVE_RUNS_DIR`` after import would leave
    every run in this suite writing into the repo's own ``creative_runs/``.
    """
    from graphics_designer_agent.creative import runs as cruns

    monkeypatch.setenv("GD_CREATIVE_RUNS_DIR", str(tmp_path / "creative"))
    monkeypatch.setattr(cruns, "CREATIVE_RUNS_ROOT", tmp_path / "creative")
    as_caller(OWNER)


def test_the_type_this_suite_drives_is_still_routed_here():
    """Guard for the fixtures below: if ``brochure`` stopped routing to this
    rail, every run-creation would 400 and the cross-tenant cases would pass
    against nothing."""
    types = client.get("/api/creative/types").json()["types"]
    assert CREATIVE_TYPE in {t["key"] for t in types}


@pytest.fixture()
def owned_run_id(_harness) -> str:
    created = client.post(
        "/api/creative/runs",
        json={"creative_type": CREATIVE_TYPE, "brief": "a one-page overview"},
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _calls(run_id: str) -> dict[str, callable]:
    """The ten non-download call sites of ``_owned``.

    Bodies are valid; several endpoints answer 400/428 for a run that is not far
    enough along, which is fine — the owner assertion is "reached the handler",
    not "succeeded". The eleventh site (``/artifact/{name}``) needs a finished
    run and is covered separately.
    """
    return {
        "GET /creative/runs/{id}": lambda: client.get(f"/api/creative/runs/{run_id}"),
        "POST /intent": lambda: client.post(
            f"/api/creative/runs/{run_id}/intent", json={"brief": "b"}),
        "POST /acknowledge": lambda: client.post(
            f"/api/creative/runs/{run_id}/acknowledge"),
        "POST /plan": lambda: client.post(
            f"/api/creative/runs/{run_id}/plan", json={"use_llm": False}),
        "POST /plan/text": lambda: client.post(
            f"/api/creative/runs/{run_id}/plan/text", json={"frames": []}),
        "POST /plan/approve": lambda: client.post(
            f"/api/creative/runs/{run_id}/plan/approve"),
        "POST /generate": lambda: client.post(f"/api/creative/runs/{run_id}/generate"),
        "POST /autonomous": lambda: client.post(
            f"/api/creative/runs/{run_id}/autonomous", json={"use_llm": False}),
        "POST /override": lambda: client.post(f"/api/creative/runs/{run_id}/override"),
        "GET /decisions": lambda: client.get(f"/api/creative/runs/{run_id}/decisions"),
    }


ENDPOINTS = list(_calls("x"))


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_owner_reaches_every_run_scoped_endpoint(owned_run_id, endpoint):
    """The owner is not 404'd anywhere — so the stranger's 404 is about tenancy."""
    response = _calls(owned_run_id)[endpoint]()
    assert response.status_code != 404, (
        f"{endpoint}: the owner was refused, so this case cannot prove anything "
        f"about a stranger. Got {response.text!r}"
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_another_users_run_is_404_everywhere(owned_run_id, as_caller, endpoint):
    """A second tenant is refused at every site, with 404 and no ownership hint."""
    as_caller(STRANGER)
    response = _calls(owned_run_id)[endpoint]()
    assert response.status_code == 404, f"{endpoint}: {response.status_code} {response.text!r}"
    assert response.json()["detail"] == NOT_FOUND, endpoint


def test_not_even_an_admin_may_open_another_tenants_creative(owned_run_id, as_caller):
    """``_owned`` has no admin bypass. Pinned so unifying the ownership
    predicates has to *choose* whether admins keep reading everyone's work —
    every one of them says no today."""
    as_caller({**STRANGER, "is_admin": True, "is_creator": True})
    response = client.get(f"/api/creative/runs/{owned_run_id}")
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND


def test_another_tenant_cannot_advance_someone_elses_run(owned_run_id, as_caller):
    """The rejection leaves no trace on the run.

    A 404 that still ran ``gather_intent`` would be worse than no check at all,
    so the decision log is read back afterwards: it must not have grown, and it
    must not contain the stranger's brief.
    """
    before = client.get(f"/api/creative/runs/{owned_run_id}/decisions").json()["decisions"]

    as_caller(STRANGER)
    assert client.post(
        f"/api/creative/runs/{owned_run_id}/intent",
        json={"brief": "a brief written by the wrong tenant"},
    ).status_code == 404

    as_caller(OWNER)
    after = client.get(f"/api/creative/runs/{owned_run_id}/decisions").json()["decisions"]
    assert after == before
    run = client.get(f"/api/creative/runs/{owned_run_id}").json()
    assert "wrong tenant" not in str(run)


def test_a_finished_creatives_files_are_not_downloadable_by_another_tenant(
    owned_run_id, as_caller
):
    """The bytes themselves, end to end.

    Runs the real four-step pipeline (offline: curated plan, deterministic
    render) so there is an actual artifact behind the URL, then proves the owner
    can fetch it and a second tenant cannot. The stranger's detail must be the
    *run-level* refusal, not "Unknown artifact" — that message is what this
    endpoint says about a bad name on your own run, and confusing the two would
    turn a tenancy hole into a passing test.
    """
    client.post(f"/api/creative/runs/{owned_run_id}/intent", json={"brief": "overview"})
    client.post(f"/api/creative/runs/{owned_run_id}/plan", json={"use_llm": False})
    client.post(f"/api/creative/runs/{owned_run_id}/plan/approve")
    produced = client.post(f"/api/creative/runs/{owned_run_id}/generate")
    assert produced.status_code == 200, produced.text
    artifacts = produced.json()["artifacts"]
    assert artifacts, "the pipeline produced nothing — nothing to prove about downloads"
    name = artifacts[0]["name"]
    url = f"/api/creative/runs/{owned_run_id}/artifact/{name}"

    mine = client.get(url)
    assert mine.status_code == 200 and mine.content

    as_caller(STRANGER)
    theirs = client.get(url)
    assert theirs.status_code == 404
    assert theirs.json()["detail"] == NOT_FOUND, (
        "the stranger got the artifact-level 404, which means the run-level "
        "ownership check did not fire"
    )


def test_a_real_run_id_and_an_invented_one_look_the_same_to_a_stranger(
    owned_run_id, as_caller
):
    """No oracle: a guessed id that happens to be real answers exactly as one
    that is not."""
    as_caller(STRANGER)
    real = client.get(f"/api/creative/runs/{owned_run_id}")
    invented = client.get("/api/creative/runs/000000000000")
    assert (real.status_code, real.json()) == (invented.status_code, invented.json())
