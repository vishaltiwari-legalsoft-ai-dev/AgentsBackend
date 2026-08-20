"""Cross-tenant coverage for the Browser Agent router (``/api/browser``).

The Browser Agent is the odd one out of the four. Its helper is spelled
differently from the other three and means something different::

    def _run_or_404(run_id: str, user: dict) -> dict:
        run = browser_runs.get_run(run_id)
        if not run or (run.get("user_id") != str(user.get("id"))
                       and not user.get("is_admin")):
            raise HTTPException(status_code=404, detail="Unknown run")
        return run

Three differences from ``_owned_run`` / ``_owned``, all of them load-bearing:

1. ``user.get("id")`` rather than ``user["id"]`` — a caller with no id gets
   ``"None"`` instead of a ``KeyError``.
2. ``"Unknown run"``, not ``"Run not found"``. Four helpers, three messages.
3. **an ``is_admin`` bypass the other three do not have.** An admin reads,
   steps, stops and learns from every tenant's runs.

Point 3 is not covered by the helper alone: ``list_runs``, ``list_skills`` and
``list_digests`` each open the same door inline (``None if user.get("is_admin")
else str(user.get("id"))``), and ``get_skill`` / ``delete_skill`` /
``get_digest`` each carry their own hand-written copy of the whole predicate.
That is seven more places the same rule is re-typed. This suite pins every one
of them, and pins the bypass *explicitly* rather than leaving it as an
undocumented side effect of a boolean — so that unifying the four predicates has
to decide whether admins keep this reach, in the open.

``test_browser_agent_router.py`` already carries two cross-tenant tests
(``test_other_users_run_is_404``, ``test_another_users_skill_is_invisible_and_
undeletable``). They stay where they are — they are part of that suite's story.
This file is the complete surface.
"""
from __future__ import annotations

import pytest

from app.routers.tests.conftest import client

OWNER = {"id": "browser-tenant-a", "email": "a@legalsoft.com", "is_admin": False,
         "is_creator": False, "session_id": "", "timezone": "UTC"}
STRANGER = {"id": "browser-tenant-b", "email": "b@legalsoft.com", "is_admin": False,
            "is_creator": False, "session_id": "", "timezone": "UTC"}
#: A *different* tenant who also happens to be an admin — the point being that
#: the bypass is not "the owner is an admin", it is "any admin, any run".
ADMIN = {"id": "browser-tenant-c", "email": "boss@legalsoft.com", "is_admin": True,
         "is_creator": False, "session_id": "", "timezone": "UTC"}

UNKNOWN_RUN = "Unknown run"
UNKNOWN_SKILL = "Unknown skill"
UNKNOWN_DIGEST = "Unknown digest"

_ROUTE = [
    {"kind": "navigate", "url": "https://example.com/"},
    {"kind": "click", "expect": "Learn more", "role": "a"},
]


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    """Offline browser state in a throwaway directory, owner signed in.

    ``digest._summarize`` is stubbed because building a digest is the one
    Browser Agent path that calls the LLM, and the repo-root guard blanks the
    OpenRouter key — without the stub the *owner's* digest never gets created
    and there is nothing for a second tenant to be refused. Stubbing the
    module-level seam is exactly what the root conftest sanctions; the guard is
    untouched.

    The stub echoes the visited page titles into the summary, exactly as the
    real summarizer does. That is what makes a digest sensitive, and it gives
    the cross-tenant assertions something to look for other than a status code.
    """
    from browser_agent import digest as browser_digest

    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "browser_state"))
    monkeypatch.delenv("BROWSER_AGENT_DISABLED", raising=False)
    monkeypatch.setattr(
        browser_digest, "_summarize",
        lambda events, tabs: {
            "headline": "Reviewed: " + ", ".join(e.get("title", "") for e in events),
            "themes": [], "open_loops": [],
        },
    )
    as_caller(OWNER)


@pytest.fixture()
def owned(_harness, monkeypatch) -> dict:
    """One run (with real steps on it), one skill and one digest, all owned by
    :data:`OWNER`.

    The run is stepped twice so it carries a replayable route: without steps,
    ``POST /skills {run_id}`` fails validation at 422 *before* proving anything
    about who may read the run, and the admin-bypass case below would be
    testing the wrong thing.
    """
    from browser_agent import actions, brain

    monkeypatch.setattr(
        brain, "decide",
        lambda run, obs: actions.Action(kind="navigate", url="https://example.com/",
                                        why="go"),
    )
    run_id = client.post(
        "/api/browser/runs", json={"goal": "read the release notes"}
    ).json()["run_id"]
    for seq, extra in ((1, {}), (2, {"last_result": {"ok": True}})):
        stepped = client.post(
            f"/api/browser/runs/{run_id}/step",
            json={"protocol": actions.PROTOCOL, "seq": seq, "dom": {"elements": []},
                  **extra},
        )
        assert stepped.status_code == 200, stepped.text

    skill = client.post("/api/browser/skills", json={
        "name": "Open the release notes", "steps": _ROUTE,
        "goal": "open the release notes", "host": "example.com"})
    assert skill.status_code == 200, skill.text

    digest = client.post("/api/browser/digest", json={
        "events": [{"url": "https://example.com/private-doc", "title": "Q3 plan"}],
        "tabs": []})
    assert digest.status_code == 200, digest.text

    return {"run_id": run_id, "skill_id": skill.json()["id"],
            "digest_id": digest.json()["id"]}


def _run_calls(run_id: str) -> dict[str, callable]:
    """The four ``_run_or_404`` call sites."""
    from browser_agent import actions

    return {
        "GET /browser/runs/{id}": lambda: client.get(f"/api/browser/runs/{run_id}"),
        "POST /step": lambda: client.post(
            f"/api/browser/runs/{run_id}/step",
            json={"protocol": actions.PROTOCOL, "seq": 3, "dom": {"elements": []}}),
        "POST /stop": lambda: client.post(f"/api/browser/runs/{run_id}/stop"),
        "POST /skills {run_id}": lambda: client.post(
            "/api/browser/skills", json={"name": "Learned from you", "run_id": run_id}),
    }


RUN_ENDPOINTS = list(_run_calls("x"))


# --------------------------------------------------------------------------- #
# Runs — the ``_run_or_404`` sites
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("endpoint", RUN_ENDPOINTS)
def test_owner_reaches_every_run_scoped_endpoint(owned, endpoint):
    """The owner is not 404'd anywhere, so a stranger's 404 means tenancy."""
    response = _run_calls(owned["run_id"])[endpoint]()
    assert response.status_code != 404, f"{endpoint}: {response.text!r}"


@pytest.mark.parametrize("endpoint", RUN_ENDPOINTS)
def test_another_users_run_is_404_everywhere(owned, as_caller, endpoint):
    """A second tenant is refused at all four, with 404 and the same message.

    The detail is pinned as ``"Unknown run"`` — the Browser Agent's own wording.
    Three of the four routers under this refactor answer ``"Run not found"``
    instead; that inconsistency is real today and this assertion is where a
    unification has to notice it.
    """
    as_caller(STRANGER)
    response = _run_calls(owned["run_id"])[endpoint]()
    assert response.status_code == 404, f"{endpoint}: {response.status_code} {response.text!r}"
    assert response.json()["detail"] == UNKNOWN_RUN, endpoint


def test_another_tenants_run_is_absent_from_the_list(owned, as_caller):
    """``GET /browser/runs`` scopes with its own inline copy of the rule."""
    as_caller(STRANGER)
    assert client.get("/api/browser/runs").json()["runs"] == []


def test_a_stranger_cannot_stop_someone_elses_run(owned, as_caller):
    """The refusal is real: the run is still running afterwards.

    ``stop`` is the mutation with the least visible blast radius and the most
    obvious motive — a 404 that stopped the run anyway would look identical from
    the attacker's side, so the state is read back as the owner.
    """
    as_caller(STRANGER)
    assert client.post(f"/api/browser/runs/{owned['run_id']}/stop").status_code == 404

    as_caller(OWNER)
    assert client.get(f"/api/browser/runs/{owned['run_id']}").json()["status"] == "running"


# --------------------------------------------------------------------------- #
# Skills and digests — the hand-written copies of the same predicate
# --------------------------------------------------------------------------- #

def test_another_tenants_skill_is_invisible_unreadable_and_undeletable(owned, as_caller):
    """``get_skill`` and ``delete_skill`` each re-type the predicate inline;
    ``list_skills`` re-types the admin half of it. All three pinned here."""
    skill_id = owned["skill_id"]
    as_caller(STRANGER)

    listed = client.get("/api/browser/skills")
    assert listed.json()["skills"] == []

    read = client.get(f"/api/browser/skills/{skill_id}")
    assert read.status_code == 404 and read.json()["detail"] == UNKNOWN_SKILL

    removed = client.delete(f"/api/browser/skills/{skill_id}")
    assert removed.status_code == 404 and removed.json()["detail"] == UNKNOWN_SKILL

    as_caller(OWNER)
    assert client.get(f"/api/browser/skills/{skill_id}").status_code == 200, (
        "the stranger's rejected DELETE removed the skill anyway"
    )


def test_another_tenants_digest_is_invisible_and_unreadable(owned, as_caller):
    """A digest is a record of which pages someone had open. ``get_digest``
    carries the fourth hand-written copy of the predicate."""
    digest_id = owned["digest_id"]
    as_caller(STRANGER)

    assert client.get("/api/browser/digests").json()["digests"] == []
    read = client.get(f"/api/browser/digests/{digest_id}")
    assert read.status_code == 404 and read.json()["detail"] == UNKNOWN_DIGEST
    assert "Q3 plan" not in read.text


# --------------------------------------------------------------------------- #
# The admin bypass — pinned deliberately, not endorsed
# --------------------------------------------------------------------------- #
# The router's own docstring states it ("runs are private to their owner; admins
# may read all"), so it is intentional *for runs*. Whether it was intended to
# extend to reading another tenant's browsing digests, and to learning a skill
# from another tenant's run, is a question these tests are meant to force. If the
# answer is yes, they document it. If it is no, they are where that argument
# happens — before a refactor quietly settles it either way.

def test_an_admin_reads_every_tenants_run(owned, as_caller):
    as_caller(ADMIN)
    read = client.get(f"/api/browser/runs/{owned['run_id']}")
    assert read.status_code == 200
    assert read.json()["goal"] == "read the release notes"


def test_an_admins_run_list_is_every_tenants_runs(owned, as_caller):
    """``list_runs`` passes ``user_id=None`` for an admin, which is not a wider
    filter — it is *no* filter."""
    as_caller(ADMIN)
    listed = client.get("/api/browser/runs").json()["runs"]
    assert [r["id"] for r in listed] == [owned["run_id"]]


def test_an_admin_can_stop_another_tenants_run(owned, as_caller):
    """The bypass is not read-only. An admin mutates other tenants' runs."""
    as_caller(ADMIN)
    stopped = client.post(f"/api/browser/runs/{owned['run_id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"

    as_caller(OWNER)
    assert client.get(f"/api/browser/runs/{owned['run_id']}").json()["status"] == "stopped"


def test_an_admin_can_learn_a_skill_from_another_tenants_run(owned, as_caller):
    """The furthest reach of the bypass, and the least obviously intended.

    ``POST /skills {run_id}`` routes through ``_run_or_404``, so an admin may
    point it at any tenant's run — and the skill that comes back is stored under
    the *admin's* ``user_id`` while carrying the other tenant's route, including
    the URLs they visited. Copying, not just reading.
    """
    as_caller(ADMIN)
    learned = client.post("/api/browser/skills",
                          json={"name": "Lifted", "run_id": owned["run_id"]})
    assert learned.status_code == 200, learned.text
    body = learned.json()
    assert body["user_id"] == ADMIN["id"]
    assert body["steps"][0]["url"] == "https://example.com/"

    # And it is now the admin's own, listed and deletable by them.
    assert "Lifted" in {s["name"] for s in client.get("/api/browser/skills").json()["skills"]}


def test_an_admin_reads_every_tenants_skills_and_digests(owned, as_caller):
    """The bypass reaches the two surfaces the router docstring never mentions."""
    as_caller(ADMIN)
    assert client.get(f"/api/browser/skills/{owned['skill_id']}").status_code == 200
    digest = client.get(f"/api/browser/digests/{owned['digest_id']}")
    assert digest.status_code == 200
    assert digest.json()["user_id"] == OWNER["id"]
    assert "Q3 plan" in digest.json()["headline"], (
        "an admin reads the pages another tenant had open"
    )


def test_a_creator_who_is_not_an_admin_gets_no_bypass(owned, as_caller):
    """``is_creator`` is checked for the extension download but *not* here.

    Worth pinning because the two roles are treated as a superset in
    ``app.security`` (every creator is an admin) yet as unrelated flags in this
    router. A caller carrying only ``is_creator`` is refused — which is correct
    today only because ``get_current_user`` never mints that combination.
    """
    as_caller({**STRANGER, "is_creator": True, "is_admin": False})
    assert client.get(f"/api/browser/runs/{owned['run_id']}").status_code == 404


# --------------------------------------------------------------------------- #
# Watch rules — a per-user-looking endpoint backed by one global document
# --------------------------------------------------------------------------- #

def test_KNOWN_LEAK_watch_rules_are_one_global_document_shared_by_all_tenants(
    owned, as_caller
):
    """DOCUMENTS A LIVE CROSS-TENANT DEFECT. Do not read this as a spec.

    ``GET``/``PUT /browser/config`` take the caller only to authenticate them.
    ``browser_digest.load_config()`` and ``save_config(patch)`` take no user id
    at all and read/write the single document ``CONFIG_DOC``:

        @router.put("/browser/config")
        def put_config(body, user=Depends(get_current_user), ...):
            saved = browser_digest.save_config(patch)

    So every tenant sees every other tenant's watch rules, and any tenant's save
    silently destroys everyone else's. This is not a scoping line someone forgot
    to type — there is no per-user concept in this path to forget.

    Pinned as-is, and pinned loudly, for one reason: the refactor that gives
    these endpoints a tenant key will turn this test red, and someone will have
    to come here and delete it on purpose. That is the outcome we want. The fix
    belongs to ``senior-python-backend``; this suite only refuses to let it
    happen by accident.
    """
    assert client.put(
        "/api/browser/config",
        json={"watch_rules": [{"text": "watch for the acme contract"}]},
    ).status_code == 200

    as_caller(STRANGER)
    leaked = client.get("/api/browser/config").json()["watch_rules"]
    assert [r["text"] for r in leaked] == ["watch for the acme contract"], (
        "if this now fails, watch rules became per-tenant — good; delete this test"
    )

    assert client.put(
        "/api/browser/config", json={"watch_rules": [{"text": "b's own rule"}]}
    ).status_code == 200

    as_caller(OWNER)
    after = client.get("/api/browser/config").json()["watch_rules"]
    assert [r["text"] for r in after] == ["b's own rule"], (
        "the owner's watch rules survived another tenant's save — good; delete this test"
    )
