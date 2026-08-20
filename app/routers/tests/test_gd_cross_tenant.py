"""Cross-tenant coverage for the Graphics Designer router (``/api/gd``).

Every run-scoped endpoint in ``graphics_designer.py`` funnels through one
four-line helper::

    def _owned_run(run_id: str, user: dict) -> dict:
        run = get_run(run_id)
        if not run or run.get("user_id") != str(user["id"]):
            raise HTTPException(404, "Run not found")
        return run

``get_run(run_id)`` itself is unscoped — it will happily return any tenant's
run — so that one comparison is the *only* thing standing between user B and
user A's creative. It is also a line each endpoint author has to remember to
type: seventeen call sites, no compiler, and until this file, no test. Deleting
it from all seventeen shipped a green suite.

So this suite drives every one of the seventeen through the HTTP surface, twice:

* :func:`test_owner_reaches_every_run_scoped_endpoint` proves the request shape
  is valid — the owner gets *something other than* 404. Without it a stranger's
  404 proves nothing, because a malformed request 404s for everyone.
* :func:`test_another_users_run_is_404_everywhere` proves the stranger is
  refused, and refused with **404 "Run not found"** rather than 403 — the
  convention across this codebase is that "not yours" and "does not exist" are
  indistinguishable, so the response never confirms the id is real.

The owner's expected status is deliberately *not* pinned to 200. Several of
these endpoints legitimately answer 409 (stage not ready) or 503 (no image
model offline); what matters for tenancy is only that the owner gets past the
ownership gate that the stranger does not.
"""
from __future__ import annotations

import io

import pytest

from app.routers.tests.conftest import client

#: The tenant who owns the run. Both ids are plain strings, which is what
#: ``get_current_user`` puts in ``user["id"]`` in production (a Firestore
#: document id). The non-string case has its own suite —
#: ``test_tenant_id_type_contract.py``.
OWNER = {"id": "gd-tenant-a", "email": "a@legalsoft.com", "is_admin": False,
         "is_creator": False, "session_id": "", "timezone": "UTC"}

#: A different signed-in tenant. Note ``is_admin: False`` *and* the admin case
#: below: unlike the Browser Agent, ``_owned_run`` has no admin bypass, and
#: ``test_not_even_an_admin_may_open_another_tenants_run`` pins that difference.
STRANGER = {"id": "gd-tenant-b", "email": "b@legalsoft.com", "is_admin": False,
            "is_creator": False, "session_id": "", "timezone": "UTC"}

#: What ``_owned_run`` says. Asserted verbatim so a refactor that switches to
#: 403 — or to a message naming the real owner — fails here rather than in
#: production.
NOT_FOUND = "Run not found"


def _png(colour: tuple[int, int, int, int] = (12, 34, 56, 255)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    """A throwaway run store, no Firestore, and the owner signed in.

    ``RUNS_ROOT`` is bound at import time from ``GD_RUNS_DIR``, so the env var
    alone does not move the store once the module is loaded — the module
    attribute is patched too, and the env var kept so anything that re-reads it
    agrees.

    ``find_brand_logo`` is stubbed to "this brand has no ingested logo" because
    the repo-root guard makes any real Firestore call raise: without the stub,
    ``/brand-logo`` and ``/stage4`` blow up for the *owner* and the owner-side
    half of this suite could not run at all. This is the seam the root conftest
    names ("tests that legitimately exercise Firestore paths monkeypatch the
    specific repo function"); the guard itself is untouched.
    """
    from app.services import firestore_repo
    from graphics_designer_agent import runs as gd_runs

    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path / "gd"))
    monkeypatch.setattr(gd_runs, "RUNS_ROOT", tmp_path / "gd")
    monkeypatch.setattr(firestore_repo, "find_brand_logo", lambda brand_id: None)
    as_caller(OWNER)


@pytest.fixture()
def owned_run(_harness) -> dict:
    """A run belonging to :data:`OWNER`, plus one artifact ref inside it.

    The artifact is uploaded rather than generated: generation needs an image
    model, and the artifact endpoint's tenancy check does not care how the
    bytes got there.
    """
    created = client.post("/api/gd/runs", json={})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]

    uploaded = client.post(
        f"/api/gd/runs/{run_id}/subject/upload",
        files={"file": ("subject.png", io.BytesIO(_png()), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return {"id": run_id, "artifact": uploaded.json()["ref"]}


def _calls(run: dict) -> dict[str, callable]:
    """Every endpoint that reaches ``_owned_run``, keyed by a readable name.

    One entry per call site (seventeen). Each request body is the *valid* shape
    for that endpoint — a 422 from a bad body would short-circuit the ownership
    check and make the stranger's failure meaningless.
    """
    rid = run["id"]
    ref = run["artifact"]
    return {
        "GET /gd/runs/{id}": lambda: client.get(f"/api/gd/runs/{rid}"),
        "POST /config": lambda: client.post(f"/api/gd/runs/{rid}/config", json={}),
        "POST /text-preview": lambda: client.post(
            f"/api/gd/runs/{rid}/text-preview", json={}),
        "POST /suggest-placement": lambda: client.post(
            f"/api/gd/runs/{rid}/suggest-placement"),
        "POST /generate": lambda: client.post(
            f"/api/gd/runs/{rid}/generate", json={"stage": 1}),
        "GET /brand-logo": lambda: client.get(f"/api/gd/runs/{rid}/brand-logo"),
        "GET /brand-logos": lambda: client.get(f"/api/gd/runs/{rid}/brand-logos"),
        "POST /stage4": lambda: client.post(
            f"/api/gd/runs/{rid}/stage4", data={"use_ai": "false"}),
        "POST /elements/upload": lambda: client.post(
            f"/api/gd/runs/{rid}/elements/upload",
            files={"file": ("e.png", io.BytesIO(_png()), "image/png")}),
        "POST /subject/upload": lambda: client.post(
            f"/api/gd/runs/{rid}/subject/upload",
            files={"file": ("s.png", io.BytesIO(_png((9, 9, 9, 255))), "image/png")}),
        "POST /tweak": lambda: client.post(
            f"/api/gd/runs/{rid}/tweak", json={"instruction": "make it bluer"}),
        "POST /approve": lambda: client.post(
            f"/api/gd/runs/{rid}/approve", json={"stage": 1}),
        "POST /back": lambda: client.post(f"/api/gd/runs/{rid}/back", json={"stage": 1}),
        "GET /prompt": lambda: client.get(
            f"/api/gd/runs/{rid}/prompt", params={"stage": 1}),
        "POST /suggest": lambda: client.post(
            f"/api/gd/runs/{rid}/suggest", json={"kind": "concept"}),
        "POST /plan": lambda: client.post(
            f"/api/gd/runs/{rid}/plan", json={"brief": "a launch post"}),
        "GET /artifact/{ref}": lambda: client.get(f"/api/gd/runs/{rid}/artifact/{ref}"),
    }


#: Names only — parametrize ids stay readable and the fixture builds the calls.
ENDPOINTS = list(_calls({"id": "x", "artifact": "y"}))


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_owner_reaches_every_run_scoped_endpoint(owned_run, endpoint):
    """The owner gets past ``_owned_run`` on all seventeen.

    Not asserted as 200: offline, ``/generate`` is an honest 503 (no image
    model) and ``/tweak`` a 409 (nothing approved yet). The claim under test is
    narrower and is the one the stranger test depends on — the owner's request
    is well-formed and reaches the handler, so a 404 for anyone else is about
    ownership and nothing else.
    """
    response = _calls(owned_run)[endpoint]()
    assert response.status_code != 404, (
        f"{endpoint}: the owner was refused — this test's request shape is wrong, "
        f"which would make the cross-tenant assertion vacuous. Got {response.text!r}"
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_another_users_run_is_404_everywhere(owned_run, as_caller, endpoint):
    """A second tenant is refused at all seventeen — with 404, not 403.

    The status *and* the body are pinned. 403 would confirm the run id exists
    and belongs to someone, which is exactly the leak the 404 convention avoids;
    a detail naming the owner would leak more.
    """
    as_caller(STRANGER)
    response = _calls(owned_run)[endpoint]()
    assert response.status_code == 404, f"{endpoint}: {response.status_code} {response.text!r}"
    assert response.json()["detail"] == NOT_FOUND, endpoint


def test_not_even_an_admin_may_open_another_tenants_run(owned_run, as_caller):
    """``_owned_run`` has no admin bypass — unlike the Browser Agent's
    ``_run_or_404``, which does.

    Pinned because the two helpers are three lines apart in shape and one
    ``or user.get("is_admin")`` apart in meaning. A refactor that unifies them
    has to pick one behaviour, and this test makes that a decision rather than
    an accident. See ``test_browser_agent_cross_tenant.py`` for the other side.
    """
    as_caller({**STRANGER, "is_admin": True, "is_creator": True})
    response = client.get(f"/api/gd/runs/{owned_run['id']}")
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND


def test_another_tenant_cannot_edit_the_runs_config(owned_run, as_caller):
    """The refusal is real, not cosmetic: the run is unchanged afterwards.

    Status-only assertions cannot tell "rejected" from "applied, then reported
    as rejected". This one reads the state back as the owner.
    """
    before = client.get(f"/api/gd/runs/{owned_run['id']}").json()["config"]["aspect_ratio"]
    other = next(ar for ar in ("1:1", "4:5", "9:16") if ar != before)

    as_caller(STRANGER)
    assert client.post(
        f"/api/gd/runs/{owned_run['id']}/config", json={"aspect_ratio": other}
    ).status_code == 404

    as_caller(OWNER)
    after = client.get(f"/api/gd/runs/{owned_run['id']}").json()["config"]["aspect_ratio"]
    assert after == before, "a stranger's rejected config patch still landed on the run"


def test_another_tenants_artifact_bytes_are_never_served(owned_run, as_caller):
    """The download path is the one that hands over actual content.

    Both halves matter. The owner really does get the PNG back (so the ref is
    live, and a stranger's 404 is not just a dead path), and the stranger's 404
    says "Run not found" — the run-level refusal — rather than "Artifact not
    found", which is what this endpoint answers for a *bad ref on your own run*.
    Distinguishing the two is the difference between "you may not look here" and
    "there is nothing here", and only the first is a tenancy guarantee.
    """
    url = f"/api/gd/runs/{owned_run['id']}/artifact/{owned_run['artifact']}"
    mine = client.get(url)
    assert mine.status_code == 200 and mine.content[:4] == b"\x89PNG"

    as_caller(STRANGER)
    theirs = client.get(url)
    assert theirs.status_code == 404
    assert theirs.json()["detail"] == NOT_FOUND


def test_a_run_id_that_exists_and_one_that_does_not_are_indistinguishable(
    owned_run, as_caller
):
    """Someone else's run and a run that never existed answer identically.

    This is the property the 404 convention buys, stated directly: an attacker
    holding a guessed id learns nothing from the response about whether the
    guess was right.
    """
    as_caller(STRANGER)
    real = client.get(f"/api/gd/runs/{owned_run['id']}")
    invented = client.get("/api/gd/runs/000000000000")
    assert (real.status_code, real.json()) == (invented.status_code, invented.json())
