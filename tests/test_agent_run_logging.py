"""The per-agent usage trail behind the admin Database panel.

Three guards:
1. ``record_activity`` writes all three stores (the agent's own
   ``agent_runs__<id>`` table, the master ``runs`` table, and the
   ``creative_events`` usage counters) with who/what attribution.
2. MANDATE: every router that declares an ``*_AGENT_ID`` constant must feed
   the run tables — via the shared ``run_tracking`` service or (Graphics
   Designer) the richer staged ``firestore_repo.start_run`` path. A new agent
   that ships without logging fails this test.
3. The Firebase-console deep links the Database panel shows are well-formed.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services import firestore_repo, run_tracking

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "routers"

_AGENT_ID_RE = re.compile(r'^[A-Z0-9_]*AGENT_ID\s*=\s*"(a\d+)"', re.M)


class _FakeDoc:
    def __init__(self, store: dict, key: tuple):
        self._store, self._key = store, key

    def set(self, data: dict, merge: bool = False) -> None:
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


def test_record_activity_writes_agent_table_master_runs_and_usage(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _FakeDb(store))

    user = {"id": "u1", "email": "someone@legalsoft.com",
            "session_id": "s1", "timezone": "Asia/Kolkata"}
    run_tracking.record_activity(
        user, agent_id="a2", agent_name="SEO Analyst", category="seo",
        action="ask", task="Asked: why did clicks drop?",
        brand="Legal Soft", brand_id="legal-soft",
    )

    agent_docs = [d for (col, _), d in store.items() if col == "agent_runs__a2"]
    assert len(agent_docs) == 1
    doc = agent_docs[0]
    assert doc["user"] == "someone@legalsoft.com"
    assert doc["user_id"] == "u1"
    assert doc["agent_name"] == "SEO Analyst"
    assert doc["action"] == "ask"
    assert doc["task"] == "Asked: why did clicks drop?"
    assert doc["brand"] == "Legal Soft"
    assert doc["status"] == "completed"

    master = [d for (col, _), d in store.items() if col == "runs"]
    assert len(master) == 1
    assert master[0]["run_status"] == "completed"
    assert "Asked: why did clicks drop?" in master[0]["run_summary"]
    assert master[0]["agent_id"] == "a2"

    events = [d for (col, _), d in store.items() if col == "creative_events"]
    assert len(events) == 1
    assert events[0]["agent_id"] == "a2"
    assert events[0]["action"] == "generate"


def test_record_activity_never_raises_without_a_database():
    # conftest replaces _db with a raising stub — logging must swallow it.
    run_tracking.record_activity(
        run_tracking.CRON_USER, agent_id="a6", agent_name="Marketing Research",
        category="data", action="cron_refresh", task="sweep",
    )


def test_every_agent_router_logs_runs():
    offenders = []
    for py in sorted(ROUTERS_DIR.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        if not _AGENT_ID_RE.search(src):
            continue
        if "run_tracking" not in src and "firestore_repo.start_run" not in src:
            offenders.append(py.name)
    assert not offenders, (
        "These agent routers declare an agent id but never log who used them "
        f"(mandatory — see app/services/run_tracking.py): {offenders}"
    )


def test_mandate_scan_sees_the_known_agents():
    # The offender scan above is only meaningful if it actually finds agents.
    found: set[str] = set()
    for py in ROUTERS_DIR.glob("*.py"):
        found.update(_AGENT_ID_RE.findall(py.read_text(encoding="utf-8")))
    assert {"a1", "a2", "a6", "a9", "a10", "a11"} <= found


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
