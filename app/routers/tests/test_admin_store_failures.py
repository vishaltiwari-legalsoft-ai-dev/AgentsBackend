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

from app.routers import admin as admin_router
from app.routers.tests.conftest import client
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
    # The whole chart, nobody missing: 6 + 6 + 1 + 5 reportees.
    assert len(rows) == 18


def test_org_chart_identifies_a_manager_and_nobody_else():
    assert org_chart.manager_for_user(_u("k", "k@x.com", "Kier Dela Rosa")).person.name == \
        "Kier Anthony M. Dela Rosa"
    assert org_chart.manager_for_user(_u("h", "h@x.com", "Haylie Anne Logan")) is None
    assert org_chart.manager_for_user(_u("b", "brix.ayo@x.com", "Brix Ayo")) is None


# --------------------------------------------------------------------------- #
# GET /api/usage/team — the repo layer: None means "could not read"
# --------------------------------------------------------------------------- #

def test_list_runs_for_user_months_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_runs_for_user_months("u1", ["2026-10"]) is None
    assert firestore_repo.list_runs_for_user_months("", ["2026-10"]) == []


def test_count_runs_for_user_month_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.count_runs_for_user_month("u1", "2026-10") is None
    assert firestore_repo.count_runs_for_user_month("", "2026-10") == 0


# --------------------------------------------------------------------------- #
# GET /api/usage/team — the HTTP layer
# --------------------------------------------------------------------------- #

_KIER = {"id": "k1", "email": "kier@legalsoft.com", "is_admin": False, "timezone": "UTC"}
_HAYLIE = {"id": "h1", "email": "haylie@legalsoft.com", "is_admin": True, "timezone": "UTC"}
_MEMBER = {"id": "b1", "email": "brix.ayo@legalsoft.com", "is_admin": False, "timezone": "UTC"}

_DIRECTORY = [
    _u("k1", "kier@legalsoft.com", "Kier Dela Rosa"),
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
    asked: list[tuple[str, str]] = []
    counts = {("k1", this_month): 5, ("c1", this_month): 9, ("y1", this_month): 0}

    def count(uid, ym):
        asked.append((uid, ym))
        return counts.get((uid, ym), 0)

    monkeypatch.setattr(firestore_repo, "count_runs_for_user_month", count)

    r = client.get("/api/usage/team?months=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewer"]["admin"] is True and body["viewer"]["manager"] is False
    assert body["team"] is None
    assert all(uid != "cron" for uid, _ in asked), "the scheduler must never be counted"
    months = body["humans"]["months"]
    assert [m["year_month"] for m in months] == [this_month, admin_router._month_back(this_month, 1)]
    newest = months[0]
    assert newest["runs"] == 14 and newest["users"] == 2
    assert [u["user_id"] for u in newest["by_user"]] == ["c1", "k1"]
    assert newest["by_user"][0]["name"] == "Chelsea E"   # no profile name → derived
    assert months[1] == {"year_month": months[1]["year_month"], "runs": 0, "users": 0, "by_user": []}
    assert "cron" in body["humans"]["excluded"]


def test_team_usage_admin_view_answers_502_when_any_month_cannot_be_read(as_caller, directory, monkeypatch):
    as_caller(_HAYLIE)
    monkeypatch.setattr(
        firestore_repo, "count_runs_for_user_month",
        lambda uid, ym: None if uid == "y1" else 1,
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
