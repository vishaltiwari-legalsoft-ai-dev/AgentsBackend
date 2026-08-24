"""The activity trail, enforced by what it records rather than by what a file says.

THE RULE lives in ``app/services/run_tracking.py`` — one place, in prose, next
to the code that implements it. This file is what holds the service to it.

What it used to be
------------------
``if "run_tracking" not in src`` over each router file. A router that imported
the module, defined a ``_track`` helper and called it on one endpoint out of
thirty-two passed. Measured at the time: seven agent routers owned 151 of the
backend's 182 endpoints and recorded activity at 63 call sites, and the check
was green. Graphics Designer, whose trail has a different (legitimate) shape,
was accommodated by adding a second substring to the same ``in`` test — so the
one router with a genuinely different design was waved through by name.

What it is now
--------------
Three layers, none of which reads source code looking for words:

1. **The writer's contract** — ``record_activity`` writes the agent's own
   table, the master ``runs`` table and the usage counters, and never raises.
2. **The structural law** — walked over the *live* ``app.main.app`` route
   table: every mutating endpoint on an agent router either carries the trail
   dependency or carries :func:`silent` with the reason it does not. Route
   introspection rather than an AST scan of the text on purpose: an AST scan
   can see that ``trail.records(...)`` appears in a file, but not whether it is
   wired into the route that was actually mounted. The dependant graph is the
   wiring, so it cannot be satisfied by a call that no request will reach.
   ``test_a_router_that_only_imports_the_trail_is_caught`` pins that the old
   check's blind spot is closed.
3. **The behaviour** — endpoints are driven through the real app against a fake
   store, and the rows that land are read back and asserted: what a produced
   deliverable records, what a configuration change records (and does not),
   what a failed request records, what a step inside an open run records, and
   that a store which cannot be written to never turns into a failed request.
"""
from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path

import app  # noqa: F401 - side effect: registers the agent roots on sys.path
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from app.services import agent_config, firestore_repo, run_tracking
from app.services.run_tracking import CHANGE, JOB, OUTPUT, ActivityTrail, silent

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "routers"

#: The caller every driven endpoint below runs as. Creator, because several of
#: the endpoints under test are Creator-gated and the role is not what is being
#: tested here.
USER = {"id": "u1", "email": "someone@legalsoft.com", "session_id": "s1",
        "timezone": "Asia/Kolkata", "is_admin": True, "is_creator": True}


# --------------------------------------------------------------------------- #
# A Firestore stand-in that records what was written
# --------------------------------------------------------------------------- #

class _FakeDoc:
    def __init__(self, store: dict, key: tuple):
        self._store, self._key = store, key

    def set(self, data: dict, merge: bool = False) -> None:
        if merge:
            self._store.setdefault(self._key, {}).update(data)
        else:
            self._store[self._key] = data

    def update(self, data: dict) -> None:
        self._store.setdefault(self._key, {}).update(data)


class _FakeCollection:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name

    def document(self, doc_id: str) -> _FakeDoc:
        return _FakeDoc(self._store, (self._name, doc_id))


class _FakeDb:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name)


def rows(store: dict, collection: str) -> list[dict]:
    return [doc for (col, _), doc in store.items() if col == collection]


# --------------------------------------------------------------------------- #
# 1. The writer's contract
# --------------------------------------------------------------------------- #

def test_record_activity_writes_agent_table_master_runs_and_usage(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _FakeDb(store))

    run_tracking.record_activity(
        USER, agent_id="a2", agent_name="SEO Analyst", category="seo",
        action="ask", task="Asked: why did clicks drop?",
        brand="Legal Soft", brand_id="legal-soft",
    )

    agent_docs = rows(store, "agent_runs__a2")
    assert len(agent_docs) == 1
    doc = agent_docs[0]
    assert doc["user"] == "someone@legalsoft.com"
    assert doc["user_id"] == "u1"
    assert doc["agent_name"] == "SEO Analyst"
    assert doc["action"] == "ask"
    assert doc["task"] == "Asked: why did clicks drop?"
    assert doc["brand"] == "Legal Soft"
    assert doc["status"] == "completed"

    master = rows(store, "runs")
    assert len(master) == 1
    assert master[0]["run_status"] == "completed"
    assert "Asked: why did clicks drop?" in master[0]["run_summary"]
    assert master[0]["agent_id"] == "a2"

    events = rows(store, "creative_events")
    assert len(events) == 1
    assert events[0]["agent_id"] == "a2"
    assert events[0]["action"] == "generate"


def test_an_audit_only_unit_writes_the_trail_but_not_the_usage_counters(monkeypatch):
    """A deletion or a config edit belongs in the who-did-what tables and
    nowhere near "creatives produced" — counting one as the other is how a
    dashboard starts overstating output."""
    store: dict = {}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _FakeDb(store))

    run_tracking.record_activity(
        USER, agent_id="a2", agent_name="SEO Analyst", category="seo",
        action="brand_deleted", task="Brand deleted — acme", usage_action=None,
    )

    assert len(rows(store, "agent_runs__a2")) == 1
    assert len(rows(store, "runs")) == 1
    assert rows(store, "creative_events") == []


def test_record_activity_never_raises_without_a_database():
    # conftest replaces _db with a raising stub — logging must swallow it.
    run_tracking.record_activity(
        run_tracking.CRON_USER, agent_id="a6", agent_name="Marketing Research",
        category="data", action="cron_refresh", task="sweep",
    )


def test_a_trail_that_cannot_write_says_so_in_the_logs(caplog):
    """Best-effort is not the same as silent. The caller's request must survive
    a dead store; the operator must still be able to find out it is dead."""
    with caplog.at_level(logging.WARNING, logger="agentos.trail"):
        run_tracking.record_activity(
            USER, agent_id="a2", agent_name="SEO Analyst", category="seo",
            action="ask", task="Asked something",
        )
    assert any("could not write" in r.message for r in caplog.records), caplog.text


def test_silent_refuses_a_placeholder_reason():
    """The marker exists to make "this one does not log" a statement someone
    wrote down. ``@silent("n/a")`` would be the omission with a sticker on it."""
    with pytest.raises(ValueError):
        silent("n/a")


# --------------------------------------------------------------------------- #
# 2. The structural law, over the live application
# --------------------------------------------------------------------------- #

_AGENT_ID_ATTR = re.compile(r"^[A-Z0-9_]*AGENT_ID$")
_AGENT_ID_VALUE = re.compile(r"^a\d+$")


def agent_routers() -> dict[str, str]:
    """``{module: agent id}`` for every router module that claims an agent.

    Read off the imported module's attributes, not its text: a constant that is
    defined is a fact about the module, a string that appears in it is not.
    """
    found: dict[str, str] = {}
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"app.routers.{path.stem}")
        ids = sorted(
            value for name in dir(module)
            if _AGENT_ID_ATTR.match(name)
            and isinstance(value := getattr(module, name), str)
            and _AGENT_ID_VALUE.match(value)
        )
        if ids:
            found[module.__name__] = ids[0]
    return found


AGENT_ROUTERS = agent_routers()


def _flat_dependency_calls(dependant) -> set:
    calls = {dependant.call} if dependant.call else set()
    for sub in dependant.dependencies:
        calls |= _flat_dependency_calls(sub)
    return calls


def trail_marker(route: APIRoute):
    """The trail this route declares, or ``None``."""
    for call in _flat_dependency_calls(route.dependant):
        marker = getattr(call, "__activity_trail__", None)
        if marker is not None:
            return marker
    return None


def silence_reason(route: APIRoute) -> str:
    return str(getattr(route.endpoint, "__activity_silence__", "") or "")


def _label(route: APIRoute) -> str:
    method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
    return f"{method} {route.path} ({route.endpoint.__name__})"


def undeclared_mutations(routes) -> list[str]:
    """Every mutating route that neither records nor says why it does not.

    The checker, factored out so it can be pointed at something other than the
    real app — see the blind-spot test below, which is the whole reason this is
    a function and not four lines inlined into one assertion.
    """
    offenders = []
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods - {"HEAD", "OPTIONS"}
        if methods <= {"GET"}:
            continue  # reads are silent by the rule itself, not by declaration
        if trail_marker(route) is None and not silence_reason(route):
            offenders.append(_label(route))
    return sorted(offenders)


def agent_routes() -> list[APIRoute]:
    return [
        route for route in fastapi_app.routes
        if isinstance(route, APIRoute) and route.endpoint.__module__ in AGENT_ROUTERS
    ]


def test_the_sweep_actually_covers_the_service():
    """Non-vacuity. Every claim below is of the form "nothing is wrong"; if
    route discovery quietly returned nothing they would all pass on nothing."""
    assert set(AGENT_ROUTERS.values()) >= {"a1", "a2", "a6", "a9", "a10"}, AGENT_ROUTERS
    assert len(agent_routes()) >= 130, len(agent_routes())
    declared = {_label(r) for r in agent_routes() if trail_marker(r)}
    assert len(declared) >= 60, sorted(declared)
    # One from each agent, so a whole router losing its trail is visible here
    # and not only in the aggregate count.
    for expected in (
        "POST /api/blog/runs (create_run)",
        "POST /api/creative/runs (create)",
        "POST /api/geo/brands/{brand_id}/poll/step (poll_step)",
        "POST /api/gd/runs (create_run_endpoint)",
        "POST /api/mr/ask (ask)",
        "POST /api/seo-geo/run/{brand_id} (run_brand)",
    ):
        assert expected in declared, sorted(declared)


def test_every_mutating_agent_endpoint_declares_the_trail_or_its_silence():
    """THE STRUCTURAL LAW. A new POST/PUT/DELETE on an agent router cannot be
    added without saying, at the declaration, whether it records — which is the
    thing the substring check could never ask."""
    offenders = undeclared_mutations(agent_routes())
    assert offenders == [], (
        "these endpoints neither declare an activity trail nor say why they are "
        "silent — add `act: Activity = trail.records(...)` to the signature, or "
        "`@silent(\"<reason>\")` under the route decorator. THE RULE is stated "
        f"in app/services/run_tracking.py: {offenders}"
    )


def test_every_declared_trail_is_bound_to_a_catalogued_agent():
    """A trail bound to an id the catalog does not know writes into an
    ``agent_runs__<id>`` table the admin panel labels "unknown"."""
    wrong = sorted(
        f"{_label(route)} -> {marker.trail.agent_id}"
        for route in agent_routes()
        if (marker := trail_marker(route)) and marker.trail.agent_id not in agent_config.AGENT_LABELS
    )
    assert wrong == [], wrong


def test_every_declared_trail_names_a_known_unit_of_work():
    unknown = sorted(
        f"{_label(route)} -> {marker.unit}"
        for route in agent_routes()
        if (marker := trail_marker(route)) and marker.unit not in (JOB, OUTPUT, CHANGE)
    )
    assert unknown == [], unknown


def test_the_silent_endpoints_state_a_reason_a_reader_can_weigh():
    """The counterpart to the law: silence is allowed, unexplained silence is
    not. Kept as an assertion as well as a guard inside ``silent()`` so that
    setting the attribute by hand cannot get round the factory."""
    silent_routes = {_label(r): silence_reason(r) for r in agent_routes() if silence_reason(r)}
    assert len(silent_routes) >= 10, silent_routes
    thin = sorted(label for label, reason in silent_routes.items() if len(reason) < 15)
    assert thin == [], thin


def test_the_gets_that_hand_the_user_a_file_are_recorded():
    """The one class of read the rule does cover. These are click-driven
    downloads, not page renders — if one of them loses its trail, "who exported
    the client's numbers" stops having an answer."""
    declared = {route.path for route in agent_routes() if trail_marker(route)}
    for path in (
        "/api/blog/runs/{run_id}/export",
        "/api/mr/runs/{run_id}/pdf",
        "/api/mr/lead-analysis/pdf",
        "/api/mr/snapshots/vendor/{slug}/pdf",
    ):
        assert path in declared, sorted(declared)


def test_the_reads_that_render_a_panel_stay_out_of_the_trail():
    """The other half of the rule, and the one that protects the datastore: a
    row per page load would grow ``runs`` faster than the work it describes."""
    recorded_reads = sorted(
        route.path for route in agent_routes()
        if route.methods - {"HEAD", "OPTIONS"} <= {"GET"} and trail_marker(route)
    )
    assert recorded_reads == [
        "/api/blog/runs/{run_id}/export",
        "/api/mr/lead-analysis/pdf",
        "/api/mr/runs/{run_id}/pdf",
        "/api/mr/snapshots/vendor/{slug}/pdf",
    ], recorded_reads


def test_a_router_that_only_imports_the_trail_is_caught():
    """The old check's blind spot, closed and pinned.

    This ghost router is exactly what used to pass: it imports the trail
    module, binds an ``ActivityTrail``, declares an agent id, and never wires
    any of it into an endpoint. The substring test asked "does the word appear
    in this file" and it does; the structural check asks "does the request path
    reach it" and it does not.
    """
    looks_compliant = (
        "from app.services.run_tracking import ActivityTrail\n"
        'GHOST_AGENT_ID = "a99"\n'
        'trail = ActivityTrail(agent_id=GHOST_AGENT_ID, agent_name="Ghost", category="test")\n'
    )
    assert "run_tracking" in looks_compliant  # the whole of the old test

    ghost_trail = ActivityTrail(agent_id="a99", agent_name="Ghost", category="test")
    router = APIRouter()

    @router.post("/ghost/thing")
    def ghost_thing(user: dict = Depends(get_current_user)) -> dict:
        return {"bound_to": ghost_trail.agent_id}  # referenced, never wired

    probe = FastAPI()
    probe.include_router(router)
    assert undeclared_mutations(probe.routes) == ["POST /ghost/thing (ghost_thing)"]


def test_the_checker_accepts_a_router_that_actually_wires_the_trail():
    """The other direction — otherwise "reject everything" would pass the test
    above, and the law would be unusable rather than enforced."""
    ghost_trail = ActivityTrail(agent_id="a99", agent_name="Ghost", category="test")
    router = APIRouter()

    @router.post("/ghost/wired")
    def ghost_wired(act=ghost_trail.records("thing", "Did a thing")) -> dict:
        return {}

    @router.post("/ghost/declared-silent")
    @silent("a step inside an open run, recorded by the run's own row")
    def ghost_silent() -> dict:
        return {}

    probe = FastAPI()
    probe.include_router(router)
    assert undeclared_mutations(probe.routes) == []


# --------------------------------------------------------------------------- #
# 3. The behaviour, driven through the real application
# --------------------------------------------------------------------------- #

client = TestClient(fastapi_app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _offline_workspace(monkeypatch, tmp_path):
    """Every agent this file drives, pointed at a throwaway workspace."""
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "seo"))
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path / "gd"))


@pytest.fixture()
def as_caller():
    """Install the signed-in caller, and put the overrides map back afterwards.

    Save/restore inside a fixture rather than at import time — the pattern
    ``tests/test_dependency_override_hygiene.py`` sanctions and the one the
    router suites use.
    """
    saved = dict(fastapi_app.dependency_overrides)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    fastapi_app.dependency_overrides.clear()
    fastapi_app.dependency_overrides.update(saved)


@pytest.fixture()
def store(monkeypatch):
    """A Firestore stand-in that keeps what was written, replacing the repo-root
    guard's raising stub for the length of this test."""
    data: dict = {}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _FakeDb(data))
    return data


def test_a_produced_deliverable_lands_in_all_three_stores(store, as_caller, monkeypatch):
    from seo_geo_agent import audit as seo_audit

    monkeypatch.setattr(seo_audit, "site_audit", lambda brand: {"issues": []})

    r = client.post("/api/seo-geo/audit/legalsoft/run")
    assert r.status_code == 200, r.text

    trail_rows = rows(store, "agent_runs__a2")
    assert len(trail_rows) == 1, trail_rows
    assert trail_rows[0]["action"] == "audit"
    assert "legalsoft.com" in trail_rows[0]["task"]
    assert trail_rows[0]["user"] == "someone@legalsoft.com"
    assert trail_rows[0]["brand_id"] == "legalsoft"
    assert len(rows(store, "runs")) == 1
    assert [e["action"] for e in rows(store, "creative_events")] == ["generate"]


def test_a_configuration_change_is_audited_without_counting_as_output(
    store, as_caller, monkeypatch
):
    from seo_geo_agent import insights

    monkeypatch.setattr(insights, "set_todo_status", lambda *a, **kw: None)

    r = client.post("/api/seo-geo/todos/legalsoft/t-7", json={"status": "done"})
    assert r.status_code == 200, r.text

    trail_rows = rows(store, "agent_runs__a2")
    assert len(trail_rows) == 1
    assert trail_rows[0]["action"] == "todo_status"
    assert "t-7" in trail_rows[0]["task"] and "done" in trail_rows[0]["task"]
    assert len(rows(store, "runs")) == 1
    assert rows(store, "creative_events") == [], "a to-do tick is not a creative"


def test_a_request_that_failed_records_nothing(store, as_caller):
    r = client.post("/api/seo-geo/todos/legalsoft/t-7", json={"status": "nonsense"})
    assert r.status_code == 422, r.text
    assert store == {}, "a rejected request is not a unit of work"


def test_an_unknown_brand_records_nothing(store, as_caller):
    r = client.post("/api/seo-geo/audit/no-such-brand/run")
    assert r.status_code == 404, r.text
    assert store == {}


def test_a_staged_run_keeps_one_row_per_run_not_one_per_request(store, as_caller):
    """The Graphics Designer's shape, asserted as a property rather than
    excused as a special case: the editor talks to the server constantly, and
    the run's document is updated in place instead of multiplied."""
    created = client.post("/api/gd/runs", json={})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]

    assert list(rows(store, "agent_runs__a1")) and len(rows(store, "agent_runs__a1")) == 1
    assert ("agent_runs__a1", run_id) in store, sorted(store)
    assert store[("agent_runs__a1", run_id)]["stages"], "the stage slots were not opened"
    assert ("runs", run_id) in store
    assert [e["action"] for e in rows(store, "creative_events")] == ["session"]

    for _ in range(5):
        patched = client.post(f"/api/gd/runs/{run_id}/config",
                              json={"aspect_ratio": "1:1"})
        assert patched.status_code == 200, patched.text

    assert len(rows(store, "agent_runs__a1")) == 1, "editor traffic multiplied the run rows"
    assert len(rows(store, "runs")) == 1


def test_a_trail_that_cannot_be_written_never_breaks_the_endpoint(as_caller, monkeypatch):
    """No ``store`` fixture here on purpose: the repo-root guard leaves
    ``firestore_repo._db`` raising, which is exactly a dead datastore."""
    from seo_geo_agent import audit as seo_audit

    monkeypatch.setattr(seo_audit, "site_audit", lambda brand: {"issues": []})

    r = client.post("/api/seo-geo/audit/legalsoft/run")
    assert r.status_code == 200, r.text
    assert r.json() == {"issues": []}, "the trail leaked into the response"


def test_the_cron_trail_is_attributed_to_the_scheduler_not_to_nobody(
    store, monkeypatch, tmp_path
):
    """Cron authenticates with a shared key and has no signed-in user; the row
    it writes must still say who did it."""
    from seo_geo_agent import insights

    monkeypatch.setenv("SEO_CRON_KEY", "test-only-cron-key")
    monkeypatch.setattr(insights, "list_brands", lambda: [])

    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "test-only-cron-key"})
    assert r.status_code == 200, r.text

    trail_rows = rows(store, "agent_runs__a2")
    assert len(trail_rows) == 1
    assert trail_rows[0]["action"] == "cron"
    assert trail_rows[0]["user"] == run_tracking.CRON_USER["email"]
    assert [e["action"] for e in rows(store, "creative_events")] == ["session"]


def test_a_rejected_cron_key_records_nothing(store):
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "wrong"})
    assert r.status_code in (403, 503), r.text
    assert store == {}


# --------------------------------------------------------------------------- #
# The admin panel's deep links — unchanged, and still the thing that makes the
# per-agent tables reachable from the UI.
# --------------------------------------------------------------------------- #

def test_firestore_console_urls(monkeypatch):
    from app.routers import admin

    monkeypatch.setattr(admin.settings, "gcp_project_id", "proj-x", raising=False)
    monkeypatch.setattr(admin.settings, "firestore_database", "lsbrandkit", raising=False)
    assert admin._firestore_console_url("agent_runs__a2") == (
        "https://console.firebase.google.com/project/proj-x"
        "/firestore/databases/lsbrandkit/data/~2Fagent_runs__a2"
    )
    monkeypatch.setattr(admin.settings, "firestore_database", "(default)", raising=False)
    assert admin._firestore_console_url().endswith("/databases/-default-/data")
