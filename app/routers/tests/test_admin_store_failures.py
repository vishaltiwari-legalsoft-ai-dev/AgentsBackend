"""An unreadable Firestore must never render as "you have no data".

Three admin reads used to answer ``[]`` on any exception, so a Firestore outage
(or a query whose composite index does not exist) reached the user as an empty
usage dashboard, an empty image library, and a Database panel missing every
per-agent table. ``count_collection`` already modelled this correctly by
returning ``None``; these pin the same contract on the other three.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

from app.main import app as fastapi_app
from app.routers import admin as admin_router
from app.routers.tests.conftest import client
from app.security import require_admin
from app.services import firestore_repo, org_chart


@pytest.fixture(autouse=True)
def _harness(as_admin):
    """These endpoints sit behind ``require_admin``; the gate itself is not what
    is under test here, so the shared harness installs an admin caller past it."""
    as_admin()


def _dead(*_a, **_kw):
    raise RuntimeError("503 the datastore is unavailable")


# --- the repo layer: None means "could not read", [] means "nothing there" ---

def test_list_usage_events_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_usage_events("u1", "2026-08-01") is None


def test_list_gallery_images_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_gallery_images() is None


def test_list_agent_run_collections_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_agent_run_collections() is None


# --- the HTTP layer: 502, not a zeroed page ---------------------------------

def test_usage_dashboard_answers_502_when_events_cannot_be_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_usage_events", lambda *a, **kw: None)
    r = client.get("/api/usage")
    assert r.status_code == 502, r.text
    assert "Could not read" in r.json()["detail"]


def test_usage_dashboard_still_renders_a_genuinely_empty_week(monkeypatch):
    """The other half of the contract — an empty result is still a 200."""
    monkeypatch.setattr(firestore_repo, "list_usage_events", lambda *a, **kw: [])
    r = client.get("/api/usage")
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["sessions"] == 0


def test_image_library_answers_502_when_it_cannot_be_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_gallery_images", lambda **kw: None)
    r = client.get("/api/admin/image-library")
    assert r.status_code == 502, r.text


def test_image_library_still_renders_a_genuinely_empty_gallery(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_gallery_images", lambda **kw: [])
    r = client.get("/api/admin/image-library")
    assert r.status_code == 200 and r.json()["total"] == 0, r.text


def test_db_panel_survives_a_failed_collection_discovery(monkeypatch):
    """Discovery failing must not take the panel down — the catalogued agents
    are still listed, and ``connected`` already tells the truth via the counts."""
    monkeypatch.setattr(firestore_repo, "list_agent_run_collections", lambda: None)
    monkeypatch.setattr(firestore_repo, "count_collection", lambda _n: None)
    r = client.get("/api/admin/db/collections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is False
    assert any(c["name"].startswith("agent_runs__") for c in body["collections"])


# --------------------------------------------------------------------------- #
# GET /api/usage/team — the org chart's matching rules, pure
# --------------------------------------------------------------------------- #

def _u(uid: str, email: str, name: str = "") -> dict:
    return {"id": uid, "email": email, "name": name}


def test_org_chart_matches_on_email_only_when_the_chart_has_one():
    person = org_chart.Person("Chelsea Estrella", "SEO Specialist", email="CE@x.com")
    # The display name says Chelsea Estrella, but the e-mail differs → no match.
    assert org_chart.match_person(person, [_u("1", "other@x.com", "Chelsea Estrella")]) == ("none", None)
    how, user = org_chart.match_person(person, [_u("2", "ce@x.com", "Someone Else")])
    assert (how, user["id"]) == ("email", "2")


def test_org_chart_matches_first_and_last_in_the_display_name():
    person = org_chart.Person("Dexter Jumig", "Marketing Analyst")
    how, user = org_chart.match_person(person, [_u("d1", "dj@x.com", "Dexter Jumig")])
    assert (how, user["id"]) == ("name", "d1")


def test_org_chart_matches_the_email_local_part_with_a_last_initial():
    """The real case: ``chelsea.e@practice360.ai`` with no display name yet."""
    person = org_chart.Person("Chelsea Estrella", "SEO Specialist")
    how, user = org_chart.match_person(person, [_u("c1", "chelsea.e@practice360.ai")])
    assert (how, user["id"]) == ("name", "c1")
    # ...and the same initial with the wrong first name does not.
    assert org_chart.match_person(person, [_u("x", "chelsey.e@practice360.ai")]) == ("none", None)


def test_org_chart_ignores_the_parenthetical_nickname_and_middle_initials():
    mabs = org_chart.Person("Mari Ann Belle S. Del Socorro (Mabs)", "UI/UX Designer")
    assert org_chart.name_tokens(mabs.name) == ["mari", "ann", "belle", "del", "socorro"]
    how, user = org_chart.match_person(mabs, [_u("m1", "mabs@x.com", "Mari Ann Del Socorro")])
    assert (how, user["id"]) == ("name", "m1")
    kier = org_chart.Person("Kier Anthony M. Dela Rosa", "SEO Manager")
    how, user = org_chart.match_person(kier, [_u("k1", "k@x.com", "Kier Dela Rosa")])
    assert (how, user["id"]) == ("name", "k1")


def test_org_chart_attaches_nobody_when_two_users_match():
    person = org_chart.Person("Kamran Shah", "UI/UX Designer")
    users = [_u("a", "kamran.shah@x.com", "Kamran Shah"), _u("b", "kamran.s@y.com")]
    assert org_chart.match_person(person, users) == ("ambiguous", None)
    rows = {r["name"]: r for r in org_chart.resolve_reportees(users)}
    assert rows["Kamran Shah"]["match"] == "ambiguous"
    assert rows["Kamran Shah"]["user_id"] is None and rows["Kamran Shah"]["email"] is None


def test_org_chart_reports_none_for_someone_who_has_not_signed_in():
    rows = {r["name"]: r for r in org_chart.resolve_reportees([])}
    assert rows["Brix Ayo"]["match"] == "none"
    assert rows["Brix Ayo"]["user_id"] is None
    # The whole chart by name, nobody missing and nobody twice: the 18
    # team members plus the three team managers and Raj, who report to Anushka.
    assert len(rows) == 22


def test_org_chart_identifies_a_manager_and_nobody_else():
    # A manager with an e-mail on the chart is matched on that e-mail only: the
    # right address is a manager whatever the display name says, and a display
    # name alone (which anyone can set on their Google profile) is not.
    assert org_chart.manager_for_user(
        _u("k", "kier.delarosa@legalsoft.com", "K.")).person.name == "Kier Anthony M. Dela Rosa"
    assert org_chart.manager_for_user(_u("k2", "k@x.com", "Kier Dela Rosa")) is None
    # Raj was set aside as a manager on 2026-10-08: a reportee now, not a manager.
    assert org_chart.manager_for_user(_u("r", "r@x.com", "Raj Dobariya")) is None
    # Everyone on the chart reports to Anushka — the three managers included.
    anushka = org_chart.manager_for_user(_u("a", "anushka.p@legalsoft.com", "Anushka"))
    names = [r.name for r in anushka.reportees]
    assert len(names) == len(set(names)) == 22
    for who in ("Kier Anthony M. Dela Rosa", "Angelica Mhay Canlas-David",
                "Daniel Sernin Noche Amorsolo", "Raj Dobariya", "Brix Ayo", "Chelsea Estrella"):
        assert who in names
    assert "Anushka Prasad" not in names and "Haylie Anne Logan" not in names
    assert org_chart.manager_for_user(_u("h", "h@x.com", "Haylie Anne Logan")) is None
    assert org_chart.manager_for_user(_u("b", "brix.ayo@x.com", "Brix Ayo")) is None


# --------------------------------------------------------------------------- #
# GET /api/usage/team — the repo layer: None means "could not read"
# --------------------------------------------------------------------------- #

def test_list_runs_for_user_months_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_runs_for_user_months("u1", ["2026-10"]) is None
    assert firestore_repo.list_runs_for_user_months("", ["2026-10"]) == []


def test_count_runs_for_user_by_month_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.count_runs_for_user_by_month("u1") is None
    assert firestore_repo.count_runs_for_user_by_month("") == {}


def test_user_run_rows_retries_one_cold_deadline_then_reads(monkeypatch):
    from google.api_core import exceptions as gexc
    calls = {"n": 0}

    class _Query:
        def where(self, **kw): return self
        def select(self, *a): return self
        def limit(self, *a): return self
        def stream(self, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise gexc.DeadlineExceeded("504 Stream removed")
            return iter([_Doc({"date": "2026-10-07"})])

    class _Doc:
        def __init__(self, d): self._d = d
        def to_dict(self): return self._d

    class _Db:
        def collection(self, name): return _Query()

    monkeypatch.setattr(firestore_repo, "_db", lambda: _Db())
    assert firestore_repo.count_runs_for_user_by_month("u1") == {"2026-10": 1}
    assert calls["n"] == 2
    # Two deadlines in a row are a failed read, never an empty one.
    class _AlwaysLate(_Query):
        def stream(self, timeout=None):
            raise gexc.DeadlineExceeded("504 Stream removed")

    class _DbLate:
        def collection(self, name): return _AlwaysLate()

    monkeypatch.setattr(firestore_repo, "_db", lambda: _DbLate())
    assert firestore_repo.count_runs_for_user_by_month("u1") is None


def test_run_row_month_reads_whichever_date_field_a_row_carries():
    # record_activity rows carry year_month; create_run rows (GD / Blog /
    # Creative — most human work) historically carried only date + created_at.
    assert firestore_repo.run_row_month({"year_month": "2026-09", "date": "2026-10-01"}) == "2026-09"
    assert firestore_repo.run_row_month({"date": "2026-10-07", "created_at": "2026-10-07T10:00:00+00:00"}) == "2026-10"
    assert firestore_repo.run_row_month({"created_at": "2026-08-30T23:59:00+00:00"}) == "2026-08"
    assert firestore_repo.run_row_month({}) == ""


def test_list_runs_for_user_months_buckets_create_run_rows_too(monkeypatch):
    rows = [
        {"agent_id": "a1", "date": "2026-10-03", "created_at": "2026-10-03T09:00:00+00:00"},   # create_run shape
        {"agent_id": "a2", "year_month": "2026-10", "day": "2026-10-04", "created_at": "2026-10-04T09:00:00+00:00"},
        {"agent_id": "a1", "date": "2026-09-30", "created_at": "2026-09-30T09:00:00+00:00"},
    ]
    monkeypatch.setattr(firestore_repo, "_user_run_rows", lambda uid, fields: list(rows))
    got = firestore_repo.list_runs_for_user_months("u1", ["2026-10"])
    assert [r["agent_id"] for r in got] == ["a1", "a2"]
    assert firestore_repo.count_runs_for_user_by_month("u1") == {"2026-10": 2, "2026-09": 1}
    assert firestore_repo.runs_for_user_by_month_and_agent("u1") == {
        "2026-10": {"a1": 1, "a2": 1}, "2026-09": {"a1": 1},
    }


# --------------------------------------------------------------------------- #
# GET /api/usage/team — the HTTP layer
# --------------------------------------------------------------------------- #

_KIER = {"id": "k1", "email": "kier.delarosa@legalsoft.com", "is_admin": False, "timezone": "UTC"}
_HAYLIE = {"id": "h1", "email": "haylie@legalsoft.com", "is_admin": True, "timezone": "UTC"}
_MEMBER = {"id": "b1", "email": "brix.ayo@legalsoft.com", "is_admin": False, "timezone": "UTC"}

_DIRECTORY = [
    _u("k1", "kier.delarosa@legalsoft.com", "Kier Dela Rosa"),
    _u("c1", "chelsea.e@practice360.ai") | {"last_login": "2026-10-07T01:00:00+00:00"},
    _u("y1", "yans.suarez@legalsoft.com", "Yans Suarez"),
    _u("h1", "haylie@legalsoft.com", "Haylie Anne Logan"),
    _u("b1", "brix.ayo@legalsoft.com", "Brix Ayo"),
    {"id": "cron", "email": "cron@scheduler", "name": ""},
]


def _row(when: datetime, agent_id: str = "a2") -> dict:
    return {"agent_id": agent_id, "day": when.strftime("%Y-%m-%d"), "created_at": when.isoformat()}


@pytest.fixture()
def directory(monkeypatch):
    by_email = {u["email"]: u for u in _DIRECTORY}
    monkeypatch.setattr(firestore_repo, "get_user_by_email", lambda e: by_email.get(e.lower()))
    monkeypatch.setattr(firestore_repo, "list_users", lambda: [dict(u) for u in _DIRECTORY])


def test_team_usage_manager_view(as_caller, directory, monkeypatch):
    as_caller(_KIER)
    now = datetime.now(timezone.utc)
    chelsea_rows = [
        _row(now, "a2"), _row(now, "a10"),
        _row(now - timedelta(days=3), "a2"),
        _row(now - timedelta(days=40), "a2"),   # outside the window: not counted anywhere
    ]

    def runs(uid, year_months):
        if uid == "c1":
            return chelsea_rows
        if uid == "y1":
            return None                        # the read failed
        raise AssertionError(f"unexpected read for {uid}")

    monkeypatch.setattr(firestore_repo, "list_runs_for_user_months", runs)

    r = client.get("/api/usage/team")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewer"] == {"manager": True, "admin": False,
                              "matched_as": "Kier Anthony M. Dela Rosa"}
    assert body["humans"] is None
    team = body["team"]
    assert team["manager"]["name"] == "Kier Anthony M. Dela Rosa"
    assert team["today"] == now.strftime("%Y-%m-%d")
    rows = {x["name"]: x for x in team["reportees"]}
    assert len(rows) == 6

    chelsea = rows["Chelsea Estrella"]
    assert chelsea["match"] == "name" and chelsea["user_id"] == "c1"
    assert chelsea["read_ok"] is True
    assert chelsea["last_login"] == "2026-10-07T01:00:00+00:00"
    assert (chelsea["today"], chelsea["week"]) == (2, 3)
    # ``month`` depends on where in the month "3 days ago" fell; it is at
    # least today's two and never includes the 40-day-old row.
    assert 2 <= chelsea["month"] <= 3
    assert chelsea["by_agent"]["a2"] >= 1 and chelsea["by_agent"]["a10"] == 1
    assert chelsea["last_run_at"] == now.isoformat()

    yans = rows["Yans Suarez"]
    assert yans["read_ok"] is False
    assert yans["today"] is None and yans["week"] is None and yans["month"] is None

    marian = rows["Marian Portillo"]
    assert marian["match"] == "none" and marian["user_id"] is None
    assert marian["read_ok"] is True and marian["today"] == 0

    assert team["totals"]["today"] == 2 and team["totals"]["week"] == 3


def test_team_usage_admin_view_counts_humans_only_newest_month_first(as_caller, directory, monkeypatch):
    as_caller(_HAYLIE)
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    asked: list[str] = []
    counts = {
        "k1": {this_month: {"a2": 5}},
        "c1": {this_month: {"a2": 6, "a10": 3}, "2024-01": {"a1": 3}},
        "y1": {},
    }

    def count(uid):
        asked.append(uid)
        return counts.get(uid, {})

    monkeypatch.setattr(firestore_repo, "runs_for_user_by_month_and_agent", count)

    r = client.get("/api/usage/team?months=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewer"]["admin"] is True and body["viewer"]["manager"] is False
    assert body["team"] is None
    assert "cron" not in asked, "the scheduler must never be counted"
    months = body["humans"]["months"]
    assert [m["year_month"] for m in months] == [this_month, admin_router._month_back(this_month, 1)]
    newest = months[0]
    assert newest["runs"] == 14 and newest["users"] == 2
    assert [u["user_id"] for u in newest["by_user"]] == ["c1", "k1"]
    assert newest["by_user"][0]["name"] == "Chelsea E"   # no profile name → derived
    assert newest["by_user"][0]["by_agent"] == {"a2": 6, "a10": 3}   # busiest first
    assert newest["by_user"][1]["by_agent"] == {"a2": 5}
    assert months[1] == {"year_month": months[1]["year_month"], "runs": 0, "users": 0, "by_user": []}
    assert "cron" in body["humans"]["excluded"]


def test_team_usage_admin_view_answers_502_when_any_month_cannot_be_read(as_caller, directory, monkeypatch):
    as_caller(_HAYLIE)
    monkeypatch.setattr(
        firestore_repo, "runs_for_user_by_month_and_agent",
        lambda uid: None if uid == "y1" else {"2026-01": {"a1": 1}},
    )
    r = client.get("/api/usage/team")
    assert r.status_code == 502, r.text
    assert "Could not read" in r.json()["detail"]
    assert "nothing is shown" in r.json()["detail"]


def test_team_usage_plain_member_gets_nulls_and_no_directory_read(as_caller, directory, monkeypatch):
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "list_users", _dead)
    r = client.get("/api/usage/team")
    assert r.status_code == 200, r.text
    assert r.json()["viewer"] == {"manager": False, "admin": False, "matched_as": None}
    assert r.json()["team"] is None and r.json()["humans"] is None


def test_team_usage_answers_502_when_the_caller_profile_cannot_be_read(as_caller, monkeypatch):
    as_caller(_KIER)
    monkeypatch.setattr(firestore_repo, "get_user_by_email", _dead)
    r = client.get("/api/usage/team")
    assert r.status_code == 502, r.text
    assert "Could not read" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Asks — the feedback / problem / agent-request forms
# --------------------------------------------------------------------------- #

@pytest.fixture()
def as_member(as_caller):
    """A plain member, with the module's admin pass-through removed so
    ``require_admin`` really runs: the autouse harness overrides the gate
    itself, which is right for the tests above and wrong for a 403 test."""

    def _install(user: dict | None = None) -> dict:
        caller = as_caller(_MEMBER if user is None else user)
        fastapi_app.dependency_overrides.pop(require_admin, None)
        return caller

    return _install


class _Snap:
    def __init__(self, doc_id: str, data: dict | None):
        self.id, self._data = doc_id, data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return None if self._data is None else dict(self._data)


class _AskRef:
    def __init__(self, docs: dict, doc_id: str):
        self._docs, self._id = docs, doc_id

    def get(self, timeout=None):
        return _Snap(self._id, self._docs.get(self._id))

    def set(self, data: dict, timeout=None):
        self._docs[self._id] = dict(data)

    def update(self, data: dict, timeout=None):
        self._docs[self._id].update(data)


class _AskQuery:
    """Enough of a Firestore collection for the asks repo: one equality
    filter, one order_by, limit, stream, count, document."""

    def __init__(self, docs: dict, flt=None, desc_on=None, lim=None):
        self._docs, self._flt, self._desc_on, self._lim = docs, flt, desc_on, lim

    def where(self, filter):
        return _AskQuery(self._docs, (filter.field_path, filter.value), self._desc_on, self._lim)

    def order_by(self, field, direction=None):
        return _AskQuery(self._docs, self._flt, field, self._lim)

    def limit(self, n):
        return _AskQuery(self._docs, self._flt, self._desc_on, n)

    def _rows(self) -> list[tuple[str, dict]]:
        rows = [(i, d) for i, d in self._docs.items()
                if self._flt is None or d.get(self._flt[0]) == self._flt[1]]
        if self._desc_on:
            rows.sort(key=lambda r: r[1].get(self._desc_on, ""), reverse=True)
        return rows[: self._lim] if self._lim else rows

    def stream(self, timeout=None):
        return iter(_Snap(i, d) for i, d in self._rows())

    def count(self):
        n = len(self._rows())
        return SimpleNamespace(get=lambda timeout=None: [[SimpleNamespace(value=n)]])

    def document(self, doc_id: str) -> _AskRef:
        return _AskRef(self._docs, doc_id)


class _AskDb:
    def __init__(self, docs: dict):
        self.docs = docs

    def collection(self, name: str) -> _AskQuery:
        assert name == firestore_repo.ASKS_COLLECTION, name
        return _AskQuery(self.docs)


def _ask_doc(ask_id: str, created_at: str, status: str = "new", kind: str = "feedback") -> dict:
    return {"id": ask_id, "kind": kind, "fields": {"note": f"n-{ask_id}"},
            "from": {"user_id": "u9", "email": "m@legalsoft.com", "name": "M"},
            "page": "/home", "status": status, "created_at": created_at, "updated_at": created_at}


@pytest.fixture()
def asks_db(monkeypatch) -> dict:
    """An in-memory ``asks`` collection standing in for Firestore."""
    docs: dict = {}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _AskDb(docs))
    return docs


# --- POST /api/asks: validation ---------------------------------------------

@pytest.mark.parametrize("body, word", [
    ({"kind": "feedback"}, "note"),
    ({"kind": "feedback", "note": "   "}, "note"),
    ({"kind": "issue", "where": "Home"}, "note"),
    ({"kind": "agent", "job": "Sort mail"}, "name"),
    ({"kind": "agent", "name": "Mailbot"}, "agent should do"),
    ({"kind": "wish", "note": "x"}, "kind"),
])
def test_ask_without_its_required_text_is_422_with_a_plain_detail(as_caller, monkeypatch, body, word):
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "create_ask", _dead)
    r = client.post("/api/asks", json=body)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and word in r.json()["detail"]


# --- POST /api/asks: the row ---------------------------------------------------

def test_ask_files_one_row_in_the_callers_name(as_caller, asks_db, monkeypatch):
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "get_user_by_email",
                        lambda e: {"id": "b1", "email": e, "name": "Brix Ayo"})
    r = client.post("/api/asks", json={
        "kind": "agent", "name": "  Mailbot ", "job": "Sort the inbox", "gets": "a sheet",
        "cadence": "", "note": "ignored for this kind", "page": "/hub/agents",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "created_at"}
    doc = asks_db[body["id"]]
    assert doc["kind"] == "agent" and doc["status"] == "new"
    assert doc["fields"] == {"name": "Mailbot", "job": "Sort the inbox", "gets": "a sheet"}
    assert doc["from"] == {"user_id": "b1", "email": "brix.ayo@legalsoft.com", "name": "Brix Ayo"}
    assert doc["page"] == "/hub/agents"
    assert doc["created_at"] == body["created_at"] == doc["updated_at"]


def test_ask_trims_and_caps_every_field(as_caller, asks_db, monkeypatch):
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "get_user_by_email", lambda e: None)
    r = client.post("/api/asks", json={
        "kind": "issue", "note": " " + "x" * 5000, "where": "y" * 4100, "page": "/p" * 300,
    })
    assert r.status_code == 201, r.text
    doc = asks_db[r.json()["id"]]
    assert len(doc["fields"]["note"]) == 4000 and len(doc["fields"]["where"]) == 4000
    assert len(doc["page"]) == 200
    assert doc["from"]["name"] == ""      # no users doc -> blank, not invented


def test_ask_still_files_when_the_profile_read_fails(as_caller, asks_db, monkeypatch):
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "get_user_by_email", _dead)
    r = client.post("/api/asks", json={"kind": "feedback", "note": "Love it"})
    assert r.status_code == 201, r.text
    assert asks_db[r.json()["id"]]["from"] == {
        "user_id": "b1", "email": "brix.ayo@legalsoft.com", "name": ""}


def test_ask_answers_502_when_the_write_fails(as_caller, monkeypatch):
    """The repo-root guard leaves ``_db`` raising: a dead store is a 502 with
    the house sentence, never a 201 for a row that does not exist."""
    as_caller(_MEMBER)
    monkeypatch.setattr(firestore_repo, "get_user_by_email", lambda e: None)
    r = client.post("/api/asks", json={"kind": "feedback", "note": "Love it"})
    assert r.status_code == 502, r.text
    assert "Could not save" in r.json()["detail"] and "Nothing was filed" in r.json()["detail"]


# --- the repo layer: None means "could not read" ------------------------------

def test_asks_repo_reports_none_on_a_failed_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_asks() is None
    assert firestore_repo.list_asks("new") is None
    assert firestore_repo.count_asks("new") is None
    with pytest.raises(RuntimeError):
        firestore_repo.set_ask_status("a1", "seen")
    with pytest.raises(ValueError):
        firestore_repo.set_ask_status("a1", "archived")


# --- GET /api/admin/asks -----------------------------------------------------------

def test_admin_inbox_is_newest_first_with_the_new_count(asks_db):
    asks_db.update({
        "old": _ask_doc("old", "2026-10-01T09:00:00+00:00", "done"),
        "mid": _ask_doc("mid", "2026-10-05T09:00:00+00:00", "seen"),
        "new1": _ask_doc("new1", "2026-10-08T09:00:00+00:00"),
        "new2": _ask_doc("new2", "2026-10-09T09:00:00+00:00", kind="agent"),
    })
    r = client.get("/api/admin/asks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["id"] for a in body["asks"]] == ["new2", "new1", "mid", "old"]
    assert body["total"] == 4 and body["new"] == 2
    assert set(body["asks"][0]) == {
        "id", "kind", "fields", "from", "page", "status", "created_at", "updated_at"}

    r = client.get("/api/admin/asks?status=new&limit=1")
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["asks"]] == ["new2"]
    assert r.json()["total"] == 1
    assert r.json()["new"] == 2, "the badge count is the whole inbox's, not the page's"

    assert client.get("/api/admin/asks?status=done").status_code == 422


def test_admin_inbox_answers_502_when_it_cannot_be_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_asks", lambda *a, **kw: None)
    r = client.get("/api/admin/asks")
    assert r.status_code == 502, r.text
    assert "Could not read" in r.json()["detail"]
    monkeypatch.setattr(firestore_repo, "list_asks", lambda *a, **kw: [])
    monkeypatch.setattr(firestore_repo, "count_asks", lambda s: None)
    assert client.get("/api/admin/asks").status_code == 502


def test_admin_inbox_renders_a_genuinely_empty_inbox(asks_db):
    r = client.get("/api/admin/asks")
    assert r.status_code == 200 and r.json() == {"asks": [], "total": 0, "new": 0}


# --- POST /api/admin/asks/{id}/status ---------------------------------------------

def test_admin_marks_an_ask_seen_then_done(asks_db):
    asks_db["a1"] = _ask_doc("a1", "2026-10-08T09:00:00+00:00")
    r = client.post("/api/admin/asks/a1/status", json={"status": "seen"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "a1" and r.json()["status"] == "seen"
    assert r.json()["updated_at"] > r.json()["created_at"]
    assert asks_db["a1"]["status"] == "seen"
    assert asks_db["a1"]["updated_at"] == r.json()["updated_at"]
    assert client.post("/api/admin/asks/a1/status", json={"status": "done"}).json()["status"] == "done"
    assert client.get("/api/admin/asks").json()["new"] == 0


def test_admin_status_change_404s_an_unknown_ask_and_422s_a_bad_status(asks_db):
    assert client.post("/api/admin/asks/nope/status", json={"status": "seen"}).status_code == 404
    asks_db["a1"] = _ask_doc("a1", "2026-10-08T09:00:00+00:00")
    r = client.post("/api/admin/asks/a1/status", json={"status": "archived"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert asks_db["a1"]["status"] == "new"


def test_admin_status_change_answers_502_when_the_store_is_down():
    # The repo-root guard leaves ``_db`` raising.
    r = client.post("/api/admin/asks/a1/status", json={"status": "seen"})
    assert r.status_code == 502, r.text
    assert "Could not save" in r.json()["detail"]


# --- the door ---------------------------------------------------------------------

def test_a_member_is_refused_on_both_admin_ask_routes(as_member, monkeypatch):
    as_member()
    monkeypatch.setattr(firestore_repo, "list_asks", _dead)
    monkeypatch.setattr(firestore_repo, "set_ask_status", _dead)
    assert client.get("/api/admin/asks").status_code == 403
    assert client.post("/api/admin/asks/a1/status", json={"status": "seen"}).status_code == 403
