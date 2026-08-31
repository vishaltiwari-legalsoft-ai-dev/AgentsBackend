"""The record: what `GET /api/runs` may show, and what it must refuse to guess.

Three properties are worth a test here, and they are the three that a panel
rendering somebody's own work history gets wrong in ways nobody notices:

1. **The scope is the caller.** The route hands the caller's id to the repo and
   shows exactly what comes back. A row belonging to someone else has no path
   to the response, and the test proves it by giving the repo a mixed
   collection and asserting on the filter argument rather than on the shape.

2. **A read that failed is not an empty record.** `list_runs_for_user` answers
   `None` when Firestore could not be read. Rendering that as `[]` tells the
   reader they have never run anything — the exact failure the repo's `None`
   contract exists to prevent — so the route answers 503 instead.

3. **A status the trail never wrote is not invented.** The four states the
   console draws are a mapping over what `run_tracking` actually stores. An
   append-only row is written after its work happened, so an unrecognised
   status on one is `done`; a staged run that says `in_progress` is `running`
   whatever else is on the row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.routers import runs as runs_router
from app.routers.tests.conftest import client
from app.services import firestore_repo

OWNER = {"id": "owner-1", "email": "owner@legalsoft.com", "session_id": "", "timezone": "UTC"}
STRANGER = {"id": "stranger-2", "email": "stranger@legalsoft.com", "session_id": "", "timezone": "UTC"}


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "r1",
        "run_id": "r1",
        "user_id": "owner-1",
        "user": "owner@legalsoft.com",
        "agent_id": "a1",
        "agent_name": "Graphic Designer",
        "action": "generate",
        "task": "Cyber incident response — square social",
        "brand": "Legal Soft Academy",
        "brand_id": "legalsoft",
        "status": "completed",
        "created_at": _iso(1),
        "updated_at": _iso(1),
        "day": _iso(1)[:10],
    }
    base.update(over)
    return base


@pytest.fixture()
def store(monkeypatch):
    """A stand-in for the two repo reads, recording what it was asked for."""
    state: dict[str, Any] = {"rows": [], "asked": [], "total": 0}

    def _list(user_id: str, limit: int = 200):
        state["asked"].append(user_id)
        rows = state["rows"]
        return None if rows is None else [r for r in rows if r.get("user_id") == user_id][:limit]

    monkeypatch.setattr(firestore_repo, "list_runs_for_user", _list)
    monkeypatch.setattr(firestore_repo, "count_runs_for_user", lambda uid: state["total"])
    return state


# --------------------------------------------------------------------------- #
# 1. the scope is the caller
# --------------------------------------------------------------------------- #

def test_the_record_is_read_for_the_caller_and_nobody_else(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(), _row(id="r2", run_id="r2", user_id="stranger-2", task="Not yours")]

    body = client.get("/api/runs").json()

    assert store["asked"] == ["owner-1"]
    assert [r["id"] for r in body["runs"]] == ["r1"]
    assert "Not yours" not in str(body)


def test_a_second_caller_sees_their_own_record(as_caller, store) -> None:
    store["rows"] = [_row(), _row(id="r2", run_id="r2", user_id="stranger-2", task="Theirs")]

    as_caller(STRANGER)
    body = client.get("/api/runs").json()

    assert [r["title"] for r in body["runs"]] == ["Theirs"]


def test_signing_out_closes_the_record(unauthenticated) -> None:
    unauthenticated()
    assert client.get("/api/runs").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# 2. a read that failed is not an empty record
# --------------------------------------------------------------------------- #

def test_an_unreadable_store_says_so_rather_than_showing_an_empty_record(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = None

    res = client.get("/api/runs")

    assert res.status_code == 503
    assert "could not be read" in res.json()["detail"]


def test_a_caller_with_no_runs_gets_an_empty_record_not_an_error(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = []

    res = client.get("/api/runs")

    assert res.status_code == 200
    assert res.json()["runs"] == []
    assert res.json()["window_complete"] is True


def test_a_total_that_could_not_be_counted_is_null_not_zero(as_caller, store, monkeypatch) -> None:
    """`0` beside "Runs" is a claim. `null` is the truth when the count failed.

    Only a full window asks Firestore to count — a window that did not fill
    already holds every row — so the count path is reached with one.
    """
    as_caller(OWNER)
    store["rows"] = [_row(id=f"r{i}", run_id=f"r{i}") for i in range(runs_router.SCAN_LIMIT)]
    monkeypatch.setattr(firestore_repo, "count_runs_for_user", lambda uid: None)

    assert client.get("/api/runs").json()["total"] is None


# --------------------------------------------------------------------------- #
# 3. states, durations and titles come off the row or not at all
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("completed", "done"),
        ("failed", "failed"),
        ("in_progress", "running"),
        ("queued", "queued"),
        ("", "done"),
        ("something-nobody-writes", "done"),
    ],
)
def test_every_stored_status_maps_onto_a_state_the_console_can_draw(
    as_caller, store, stored: str, expected: str
) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(status=stored)]

    run = client.get("/api/runs").json()["runs"][0]

    assert run["state"] == expected
    assert run["status_raw"] == stored


def test_a_staged_runs_own_status_wins_over_the_event_status(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(status="completed", run_status="in_progress")]

    assert client.get("/api/runs").json()["runs"][0]["state"] == "running"


def test_a_run_stamped_once_reports_no_duration_rather_than_zero(as_caller, store) -> None:
    """An append-only row has one timestamp written twice. `0s` would read as
    an instant run; `None` is what actually happened — nobody timed it."""
    stamp = _iso(1)
    as_caller(OWNER)
    store["rows"] = [_row(created_at=stamp, updated_at=stamp)]

    assert client.get("/api/runs").json()["runs"][0]["took_seconds"] is None


def test_a_staged_run_reports_the_time_between_its_two_stamps(as_caller, store) -> None:
    as_caller(OWNER)
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    store["rows"] = [_row(created_at=start.isoformat(), updated_at=(start + timedelta(seconds=184)).isoformat())]

    assert client.get("/api/runs").json()["runs"][0]["took_seconds"] == 184


def test_the_row_leads_with_the_picture_the_run_made(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(assets=[
        {"stage": 1, "url": "/api/gd/runs/r1/artifact?path=s1.png"},
        {"stage": 4, "url": "/api/gd/runs/r1/artifact?path=final.png"},
    ])]

    assert client.get("/api/runs").json()["runs"][0]["image"].endswith("final.png")


def test_a_run_that_made_no_picture_has_none_rather_than_a_placeholder(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(agent_id="a6", assets=[])]

    assert client.get("/api/runs").json()["runs"][0]["image"] is None


def test_a_row_with_no_written_task_falls_back_to_its_action_not_to_blank(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [_row(task="", run_summary="", action="report:monthly_summary")]

    assert client.get("/api/runs").json()["runs"][0]["title"] == "report:monthly_summary"


# --------------------------------------------------------------------------- #
# filtering and facets
# --------------------------------------------------------------------------- #

def test_the_facets_count_the_window_and_the_filter_narrows_it(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [
        _row(id="r1", run_id="r1", agent_id="a1", agent_name="Graphic Designer", brand="A"),
        _row(id="r2", run_id="r2", agent_id="a6", agent_name="Marketing Research", brand="B"),
        _row(id="r3", run_id="r3", agent_id="a6", agent_name="Marketing Research", brand="B",
             status="failed"),
    ]

    body = client.get("/api/runs").json()
    assert {a["id"]: a["count"] for a in body["facets"]["agents"]} == {"a6": 2, "a1": 1}
    assert body["facets"]["states"] == {"done": 2, "failed": 1}

    narrowed = client.get("/api/runs?agent=a6&state=failed").json()
    assert [r["id"] for r in narrowed["runs"]] == ["r3"]


def test_the_free_text_filter_reads_the_title_the_brand_and_the_agent(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [
        _row(id="r1", run_id="r1", task="Cyber incident response"),
        _row(id="r2", run_id="r2", task="Family law landing page"),
    ]

    assert [r["id"] for r in client.get("/api/runs?q=cyber").json()["runs"]] == ["r1"]
    assert [r["id"] for r in client.get("/api/runs?q=Legal Soft").json()["runs"]] == ["r1", "r2"]


def test_the_week_block_counts_only_the_last_seven_days(as_caller, store) -> None:
    as_caller(OWNER)
    store["rows"] = [
        _row(id="r1", run_id="r1", created_at=_iso(2)),
        _row(id="r2", run_id="r2", created_at=_iso(30)),
    ]

    assert client.get("/api/runs").json()["week"]["done"] == 1


def test_a_full_window_says_its_figures_are_partial(as_caller, store) -> None:
    """The facets and the week block describe what was read. When the read hit
    its cap they describe part of the record, and the panel has to be able to
    say so instead of presenting a partial count as a total."""
    as_caller(OWNER)
    store["rows"] = [_row(id=f"r{i}", run_id=f"r{i}") for i in range(runs_router.SCAN_LIMIT)]

    body = client.get("/api/runs").json()

    assert body["scanned"] == runs_router.SCAN_LIMIT
    assert body["window_complete"] is False


def test_the_week_block_names_who_the_caller_leaned_on(as_caller, store) -> None:
    """The "this week" figures and the per-specialist bars under them are counted
    from one window, so a headline cannot disagree with the list beneath it."""
    as_caller(OWNER)
    store["rows"] = [
        _row(id="r1", run_id="r1", agent_id="a6", agent_name="Marketing Research", created_at=_iso(1)),
        _row(id="r2", run_id="r2", agent_id="a6", agent_name="Marketing Research", created_at=_iso(3)),
        _row(id="r3", run_id="r3", agent_id="a1", agent_name="Graphic Designer", created_at=_iso(2)),
        _row(id="r4", run_id="r4", agent_id="a1", agent_name="Graphic Designer", created_at=_iso(20)),
    ]

    week = client.get("/api/runs").json()["week"]

    assert week["total"] == 3
    assert [(x["id"], x["count"]) for x in week["by_agent"]] == [("a6", 2), ("a1", 1)]


def test_a_complete_window_does_not_pay_for_a_second_count(as_caller, store, monkeypatch) -> None:
    """The window holds every run the caller has, so its length is the total.
    Counting it again is a Firestore round-trip for a number already in hand,
    on an endpoint three panels wait for."""
    counted: list[str] = []
    monkeypatch.setattr(
        firestore_repo, "count_runs_for_user",
        lambda uid: counted.append(uid) or 999,  # type: ignore[func-returns-value]
    )
    as_caller(OWNER)
    store["rows"] = [_row(id=f"r{i}", run_id=f"r{i}") for i in range(3)]

    body = client.get("/api/runs").json()

    assert body["total"] == 3
    assert counted == [], "nothing to count — the window already held everything"


def test_a_full_window_still_asks_for_the_real_total(as_caller, store, monkeypatch) -> None:
    monkeypatch.setattr(firestore_repo, "count_runs_for_user", lambda uid: 4242)
    as_caller(OWNER)
    store["rows"] = [_row(id=f"r{i}", run_id=f"r{i}") for i in range(runs_router.SCAN_LIMIT)]

    body = client.get("/api/runs").json()

    assert body["window_complete"] is False
    assert body["total"] == 4242
