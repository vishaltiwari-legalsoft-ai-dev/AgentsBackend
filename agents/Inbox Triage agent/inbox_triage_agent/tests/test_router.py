"""The routes, driven through the real application: who may reach what, the
one status shape every write answers with, the 503 sentences when the
deployment is not configured, the cron key, the idle-fire silence, and the
ledger entries that make the routes exist."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app  # noqa: F401 — registers the agent roots on sys.path
from app.config import settings
from app.main import app as fastapi_app
from app.security import get_current_user
from app.services import firestore_repo, run_tracking
from inbox_triage_agent import gmail_oauth, pipeline, sheet_writer
from inbox_triage_agent.pipeline import FireReport
from inbox_triage_agent.sheet_writer import SheetCheck

from .test_pipeline import SID, STORE_SEAMS, FakeStore, _connected_doc

client = TestClient(fastapi_app, raise_server_exceptions=False)

OWNER = {"id": "user-aaaa", "email": "her@legalsoft.com", "is_inbox_user": True,
         "session_id": "s", "timezone": "Asia/Kolkata"}
OUTSIDER = {"id": "user-bbbb", "email": "him@legalsoft.com", "is_inbox_user": False,
            "session_id": "s", "timezone": "UTC"}
STATUS_KEYS = {
    "enabled", "service_account_email", "gmail", "sheet", "backfill", "last_poll",
    "next_poll_at", "rows_24h", "needs_review", "generated_at",
}


@pytest.fixture(autouse=True)
def _isolated_overrides():
    saved = dict(fastapi_app.dependency_overrides)
    yield
    fastapi_app.dependency_overrides.clear()
    fastapi_app.dependency_overrides.update(saved)


@pytest.fixture()
def as_user():
    def _install(user: dict) -> None:
        caller = dict(user)
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(caller)
    return _install


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    for name in STORE_SEAMS:
        monkeypatch.setattr(firestore_repo, name, getattr(fake, name))
    monkeypatch.setattr(sheet_writer, "service_account_email", lambda: "hub@project.iam.gserviceaccount.com")
    from marketing_research_agent import sources_registry

    monkeypatch.setattr(sources_registry, "find_source", lambda sid: None)
    return fake


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", Fernet.generate_key().decode(), raising=False)


@pytest.fixture()
def trail(monkeypatch) -> list[dict]:
    """What the activity trail would have written."""
    rows: list[dict] = []
    monkeypatch.setattr(run_tracking, "record_activity", lambda user, **kw: rows.append(kw))
    return rows


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_for_an_outsider_is_disabled_and_reads_nothing(as_user, monkeypatch):
    as_user(OUTSIDER)
    monkeypatch.setattr(firestore_repo, "get_inbox_connection",
                        lambda uid: pytest.fail("must not read the store"))
    resp = client.get("/api/inbox/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == STATUS_KEYS
    assert body["enabled"] is False and body["service_account_email"] == ""
    assert body["gmail"]["connected"] is False and body["next_poll_at"] is None


def test_status_for_the_owner_reads_her_document_only(as_user, store):
    as_user(OWNER)
    store.connections[OWNER["id"]] = _connected_doc()
    store.connections["user-zzzz"] = {**_connected_doc(), "gmail": {"connected": True, "address": "x@y"}}
    body = client.get("/api/inbox/status").json()
    assert body["enabled"] is True and body["gmail"]["address"] == "her@firm.com"
    assert body["service_account_email"] == "hub@project.iam.gserviceaccount.com"
    assert body["sheet"]["id"] == SID and body["backfill"]["state"] == "running"
    assert "sealed" not in resp_text(body)


def resp_text(body) -> str:
    import json

    return json.dumps(body)


def test_status_needs_a_signed_in_caller():
    assert client.get("/api/inbox/status").status_code == 401


# --------------------------------------------------------------------------- #
# The role
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method, path", [
    ("POST", "/api/inbox/oauth/start"),
    ("POST", "/api/inbox/oauth/complete"),
    ("PUT", "/api/inbox/sheet"),
    ("POST", "/api/inbox/sheet/check"),
    ("POST", "/api/inbox/disconnect"),
])
def test_every_write_is_refused_to_an_account_off_the_list(as_user, method, path):
    as_user(OUTSIDER)
    resp = client.request(method, path, json={"code": "c", "state": "s", "ref": "x"})
    assert resp.status_code == 403 and resp.json()["detail"] == "Inbox Triage user only"


def test_creators_are_not_implied_onto_the_list(monkeypatch):
    from app import security

    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com", raising=False)
    assert security.is_inbox_user("Her@LegalSoft.com")
    assert not security.is_inbox_user("vishal.tiwari@legalsoft.com")  # a Creator
    assert not security.is_inbox_user("him@legalsoft.com")


# --------------------------------------------------------------------------- #
# oauth
# --------------------------------------------------------------------------- #

def test_oauth_start_answers_503_with_a_sentence_until_the_deployment_is_configured(as_user, monkeypatch, trail):
    as_user(OWNER)
    monkeypatch.setattr(settings, "inbox_google_client_id", "", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", Fernet.generate_key().decode(), raising=False)
    resp = client.post("/api/inbox/oauth/start")
    assert resp.status_code == 503 and "INBOX_GOOGLE_CLIENT_ID" in resp.json()["detail"]
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", "", raising=False)
    resp = client.post("/api/inbox/oauth/start")
    assert resp.status_code == 503 and "INBOX_TOKEN_KEY" in resp.json()["detail"]
    assert "\n" not in resp.json()["detail"]
    assert trail == [], "start records nothing — it is declared silent"


def test_oauth_start_returns_the_consent_url_bound_to_the_caller(as_user, configured):
    as_user(OWNER)
    resp = client.post("/api/inbox/oauth/start")
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith(gmail_oauth.AUTH_ENDPOINT) and "gmail.readonly" in url
    state = url.rsplit("state=", 1)[1]
    gmail_oauth.read_state(state, user_id=OWNER["id"])


def test_oauth_complete_refuses_a_bad_state_and_records_nothing(as_user, configured, trail, store):
    as_user(OWNER)
    resp = client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "junk"})
    assert resp.status_code == 400 and "start again" in resp.json()["detail"]
    assert trail == [] and store.connections == {}


def test_oauth_complete_answers_the_status_shape_and_records_a_change(as_user, configured, trail, store, monkeypatch):
    as_user(OWNER)

    def fake_connect(user_id, *, code, state, email):
        store.connections[user_id] = _connected_doc()
        return store.connections[user_id]
    monkeypatch.setattr(pipeline, "connect", fake_connect)
    resp = client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "s"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == STATUS_KEYS and body["gmail"]["connected"] is True
    assert "sealed" not in resp_text(body)
    assert [row["action"] for row in trail] == ["gmail_connected"]
    assert trail[0]["usage_action"] is None, "a connection is a change, not output"
    assert "her@firm.com" not in trail[0]["task"]


def test_offline_deployments_answer_503_not_500(as_user, configured, monkeypatch):
    as_user(OWNER)
    monkeypatch.setattr(gmail_oauth, "make_state", lambda uid: "s")
    monkeypatch.setattr(gmail_oauth, "read_state", lambda state, *, user_id: None)
    resp = client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "s"})
    assert resp.status_code == 503 and "switched off" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# sheet
# --------------------------------------------------------------------------- #

def test_put_sheet_validates_the_reference_then_checks_and_stores(as_user, store, trail, monkeypatch):
    as_user(OWNER)
    resp = client.put("/api/inbox/sheet", json={"ref": "nonsense"})
    assert resp.status_code == 400 and "Google Sheet" in resp.json()["detail"]
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: SheetCheck("not_shared", ""))
    resp = client.put("/api/inbox/sheet", json={"ref": f"https://docs.google.com/spreadsheets/d/{SID}/edit"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == STATUS_KEYS
    assert body["sheet"] == {**body["sheet"], "id": SID, "check": "not_shared"}
    assert [row["action"] for row in trail] == ["sheet_set"]
    assert "not_shared" in trail[0]["task"]


def test_sheet_check_reruns_the_check_and_answers_400_without_a_sheet(as_user, store, monkeypatch):
    as_user(OWNER)
    resp = client.post("/api/inbox/sheet/check")
    assert resp.status_code == 400 and "No sheet" in resp.json()["detail"]
    store.connections[OWNER["id"]] = {"sheet": {"id": SID, "check": "not_shared"}}
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: SheetCheck("ok", "Her inbox"))
    body = client.post("/api/inbox/sheet/check").json()
    assert body["sheet"]["check"] == "ok" and body["sheet"]["title"] == "Her inbox"


def test_a_sheets_outage_is_502_with_the_reason(as_user, store, monkeypatch):
    as_user(OWNER)
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: (_ for _ in ()).throw(
        sheet_writer.SheetsUnavailable("Sheets metadata failed after 3 attempts: timed out")))
    resp = client.put("/api/inbox/sheet", json={"ref": SID})
    assert resp.status_code == 502 and "after 3 attempts" in resp.json()["detail"]


def test_disconnect_keeps_the_sheet_and_answers_the_status_shape(as_user, store, trail):
    as_user(OWNER)
    store.connections[OWNER["id"]] = _connected_doc()
    body = client.post("/api/inbox/disconnect").json()
    assert set(body) == STATUS_KEYS | {"google_revoked"}
    assert body["google_revoked"] is False, "offline, so Google never confirmed a revoke"
    assert body["gmail"]["connected"] is False and body["sheet"]["id"] == SID
    assert "refresh_token_enc" not in store.connections[OWNER["id"]]
    assert [row["action"] for row in trail] == ["gmail_disconnected"]


# --------------------------------------------------------------------------- #
# cron
# --------------------------------------------------------------------------- #

def test_cron_refuses_without_a_key_then_with_the_wrong_key(monkeypatch, trail):
    monkeypatch.delenv("INBOX_CRON_KEY", raising=False)
    assert client.post("/api/inbox/cron/poll").status_code == 503
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    assert client.post("/api/inbox/cron/poll", headers={"x-cron-key": "wrong"}).status_code == 403
    assert trail == []


def test_an_idle_fire_records_no_trail_row(monkeypatch, store, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com", raising=False)
    store.users["her@legalsoft.com"] = {"id": OWNER["id"], "email": "her@legalsoft.com"}
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(user_id=uid))
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok" and body["ok"] == 1 and body["users"][0]["new_rows"] == 0
    assert trail == [], "288 idle fires a day must not be 288 rows"


def test_a_fire_with_rows_records_one_job_and_names_nobody(monkeypatch, store, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com,nobody@legalsoft.com", raising=False)
    store.users["her@legalsoft.com"] = {"id": OWNER["id"], "email": "her@legalsoft.com"}
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(
        user_id=uid, new_rows=3, messages_read=3, backfill_state="running"))
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})
    body = resp.json()
    assert resp.status_code == 200 and body["no_account"] == 1
    assert len(body["users"]) == 1 and "user-aaaa" not in resp_text(body) and "@" not in resp_text(body)
    assert [row["action"] for row in trail] == ["cron_poll"]
    assert trail[0]["usage_action"] == "session" and "3 rows added" in trail[0]["task"]


def test_a_fire_holding_the_lease_is_skipped_and_said_so(monkeypatch, store, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com", raising=False)
    store.users["her@legalsoft.com"] = {"id": OWNER["id"], "email": "her@legalsoft.com"}
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(
        user_id=uid, skipped="previous fire still running"))
    body = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"}).json()
    assert body["skipped"] == 1 and body["users"][0]["skipped"] == "previous fire still running"
    assert trail == []


def test_every_user_failing_is_502_and_some_is_207(monkeypatch, store, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "a@legalsoft.com,b@legalsoft.com", raising=False)
    store.users["a@legalsoft.com"] = {"id": "ua", "email": "a@legalsoft.com"}
    store.users["b@legalsoft.com"] = {"id": "ub", "email": "b@legalsoft.com"}
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(
        user_id=uid, ok=False, error="Gmail profile was refused: HTTP 403"))
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})
    assert resp.status_code == 502 and resp.json()["status"] == "failed"
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(
        user_id=uid, ok=(uid == "ua"), new_rows=1 if uid == "ua" else 0, error=None if uid == "ua" else "boom"))
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})
    assert resp.status_code == 207 and resp.json()["status"] == "partial"
    assert trail[-1]["status"] == "partial"


def test_the_cron_budget_is_bounded(monkeypatch):
    from app.routers import inbox

    monkeypatch.delenv("INBOX_CRON_BUDGET_SECONDS", raising=False)
    assert inbox._cron_budget_seconds() == 270.0
    monkeypatch.setenv("INBOX_CRON_BUDGET_SECONDS", "5000")
    assert inbox._cron_budget_seconds() == 840.0
    monkeypatch.setenv("INBOX_CRON_BUDGET_SECONDS", "nonsense")
    assert inbox._cron_budget_seconds() == 270.0


# --------------------------------------------------------------------------- #
# The ledger and the registry
# --------------------------------------------------------------------------- #

def test_the_ledger_and_the_cron_registry_know_these_routes():
    from app.routers.tests import test_route_tenancy_conformance as ledger
    from app.services.cron_registry import CRON_REGISTRY

    for method, path in (
        ("POST", "/api/inbox/oauth/start"), ("POST", "/api/inbox/oauth/complete"),
        ("PUT", "/api/inbox/sheet"), ("POST", "/api/inbox/sheet/check"),
        ("POST", "/api/inbox/disconnect"),
    ):
        assert ledger.ROUTE_LEDGER[(method, path)] == (ledger.INBOX_USER_ONLY, ledger.INTERNAL_ONLY)
    assert ledger.ROUTE_LEDGER[("GET", "/api/inbox/status")] == (ledger.TENANT_SCOPED, ledger.INTERNAL_ONLY)
    assert ledger.ROUTE_LEDGER[("POST", "/api/inbox/cron/poll")] == (ledger.CRON_SECRET, ledger.INTERNAL_ONLY)
    entry = next(e for e in CRON_REGISTRY if e["id"] == "inbox-poll-5min")
    assert entry["agent_id"] == "a12" and entry["endpoint"] == "POST /api/inbox/cron/poll"
    assert entry["expected"] == {"cron": "*/5 * * * *", "timezone": "Asia/Kolkata"}


def test_next_poll_is_five_minutes_after_the_last(as_user, store):
    as_user(OWNER)
    at = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)
    store.connections[OWNER["id"]] = {**_connected_doc(), "last_poll": {"at": at.isoformat(), "ok": True, "messages_read": 0, "error": None}}
    body = client.get("/api/inbox/status").json()
    assert body["next_poll_at"] == (at + timedelta(minutes=5)).isoformat()


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (tester pass): the token never leaves on any route, the
# cron envelope and trail carry no mail facts from a REAL fire, one owner's
# writes never touch another's document, and the role is re-derived from the
# live list on every request rather than trusted from the token.
# --------------------------------------------------------------------------- #

import copy  # noqa: E402

from .test_pipeline import _message, grant, mailbox, model, sheet  # noqa: E402,F401 — shared fakes

PLAIN_REFRESH = "1//0gPLAINREFRESHTOKEN-never-returned"


def _sealed_doc(key: str) -> dict:
    from cryptography.fernet import Fernet as _F

    doc = _connected_doc()
    doc["refresh_token_enc"] = _F(key.encode()).encrypt(PLAIN_REFRESH.encode()).decode()
    return doc


def _assert_no_token(text: str, sealed: str) -> None:
    assert "refresh_token" not in text
    assert sealed not in text and PLAIN_REFRESH not in text


def test_no_route_ever_answers_with_the_token_sealed_or_plain(as_user, store, configured, monkeypatch, trail):
    as_user(OWNER)
    doc = _sealed_doc(settings.inbox_token_key)
    sealed = doc["refresh_token_enc"]
    store.connections[OWNER["id"]] = copy.deepcopy(doc)

    def fake_connect(user_id, *, code, state, email):
        store.connections[user_id] = copy.deepcopy(doc)
        return store.connections[user_id]
    monkeypatch.setattr(pipeline, "connect", fake_connect)
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: SheetCheck("ok", "Her inbox"))

    bodies = [
        client.get("/api/inbox/status"),
        client.post("/api/inbox/oauth/start"),
        client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "s"}),
        client.put("/api/inbox/sheet", json={"ref": SID}),
        client.post("/api/inbox/sheet/check"),
    ]
    for resp in bodies:
        assert resp.status_code == 200, (resp.request.url, resp.text)
        _assert_no_token(resp.text, sealed)
    # The token is still stored after all of that, so the checks above were
    # made against a document that really held it.
    assert store.connections[OWNER["id"]]["refresh_token_enc"] == sealed
    resp = client.post("/api/inbox/disconnect")
    assert resp.status_code == 200
    _assert_no_token(resp.text, sealed)
    for row in trail:
        _assert_no_token(str(row), sealed)


def test_a_real_fire_through_the_cron_leaves_no_address_subject_or_summary_in_envelope_or_trail(
    monkeypatch, store, trail, mailbox, sheet, model, grant
):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com", raising=False)
    store.users["her@legalsoft.com"] = {"id": OWNER["id"], "email": "her@legalsoft.com"}
    store.connections[OWNER["id"]] = _connected_doc()
    mailbox.messages = {
        "m1": _message("m1", subject="Priya Rathore offer letter", body="Offer for Priya."),
        "m2": _message("m2", subject="GARBLE Kapoor escalation", body="Kapoor is unhappy."),
    }
    mailbox.history_added = ["m1", "m2"]

    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["users"][0]["new_rows"] == 2 and len(sheet.appended) == 2, "the fire really ran"
    carried = resp.text + " ".join(str(row) for row in trail)
    for secret in ("her@firm.com", "her@legalsoft.com", "gm@rathorelegal.in", "@",
                   "Priya", "Rathore", "Kapoor", "two paralegals", OWNER["id"]):
        assert secret not in carried, f"{secret!r} reached the cron envelope or the trail"
    assert [row["action"] for row in trail] == ["cron_poll"]


def test_the_cron_key_is_the_only_credential_the_poll_accepts(as_user, monkeypatch, trail):
    as_user(OWNER)  # a signed-in owner is not a scheduler
    monkeypatch.delenv("INBOX_CRON_KEY", raising=False)
    assert client.post("/api/inbox/cron/poll").status_code == 503
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    assert client.post("/api/inbox/cron/poll").status_code == 403
    assert client.post("/api/inbox/cron/poll", headers={"x-cron-key": ""}).status_code == 403
    assert client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key "}).status_code == 403
    assert trail == []


def test_one_owners_writes_never_touch_another_owners_document(as_user, store, monkeypatch):
    other = {**OWNER, "id": "user-cccc", "email": "second@legalsoft.com"}
    store.connections[other["id"]] = _connected_doc()
    store.messages[f"{other['id']}__x"] = {"user_id": other["id"], "message_id": "x", "status": "ok"}
    theirs = copy.deepcopy(store.connections[other["id"]])
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: SheetCheck("ok", "Mine"))

    as_user(OWNER)
    store.connections[OWNER["id"]] = _connected_doc()
    assert client.put("/api/inbox/sheet", json={"ref": "1zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"}).status_code == 200
    assert client.post("/api/inbox/sheet/check").status_code == 200
    body = client.post("/api/inbox/disconnect").json()

    assert body["sheet"]["id"] == "1zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    assert store.connections[other["id"]] == theirs
    assert f"{other['id']}__x" in store.messages
    as_user(other)
    assert client.get("/api/inbox/status").json()["sheet"]["id"] == SID


def test_the_role_follows_the_live_list_not_the_token(store, monkeypatch):
    from app.security import create_token

    monkeypatch.setattr(settings, "inbox_triage_emails", "colleague@legalsoft.com", raising=False)
    headers = {"Authorization": f"Bearer {create_token('user-live', 'colleague@legalsoft.com')}"}
    store.connections["user-live"] = _connected_doc()
    assert client.get("/api/inbox/status", headers=headers).json()["enabled"] is True

    monkeypatch.setattr(settings, "inbox_triage_emails", "", raising=False)
    monkeypatch.setattr(firestore_repo, "get_inbox_connection",
                        lambda uid: pytest.fail("a removed owner's document must not be read"))
    body = client.get("/api/inbox/status", headers=headers).json()
    assert body["enabled"] is False and body["gmail"]["address"] is None
    resp = client.post("/api/inbox/disconnect", headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"] == "Inbox Triage user only"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (review fixes): the caller's verified address reaches the
# checks, disconnect reports the revoke, de-listed users are disconnected by
# the cron, and odd input is a refusal rather than a 500.
# --------------------------------------------------------------------------- #

def test_sheet_routes_check_ownership_against_the_callers_signed_in_address(as_user, store, monkeypatch):
    as_user(OWNER)
    seen: list[str] = []
    monkeypatch.setattr(sheet_writer, "check",
                        lambda sid, *, caller_email: seen.append(caller_email) or SheetCheck("not_yours", ""))
    body = client.put("/api/inbox/sheet", json={"ref": SID}).json()
    assert body["sheet"]["check"] == "not_yours" and body["sheet"]["title"] is None
    assert client.post("/api/inbox/sheet/check").json()["sheet"]["check"] == "not_yours"
    assert seen == [OWNER["email"], OWNER["email"]]


def test_an_mr_sheet_is_answered_mr_source(as_user, store, monkeypatch):
    from marketing_research_agent import sources_registry

    as_user(OWNER)
    monkeypatch.setattr(sources_registry, "find_source", lambda sid: {"id": sid})
    monkeypatch.setattr(sheet_writer, "check", lambda sid, **kw: pytest.fail("Google must not be asked"))
    body = client.put("/api/inbox/sheet", json={"ref": SID}).json()
    assert body["sheet"]["check"] == "mr_source"


def test_oauth_routes_carry_the_callers_address(as_user, configured, store, monkeypatch):
    as_user(OWNER)
    url = client.post("/api/inbox/oauth/start").json()["url"]
    assert "login_hint=her%40legalsoft.com" in url
    got: dict = {}

    def fake_connect(user_id, *, code, state, email):
        got["email"] = email
        store.connections[user_id] = _connected_doc()
        return store.connections[user_id]
    monkeypatch.setattr(pipeline, "connect", fake_connect)
    assert client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "s"}).status_code == 200
    assert got == {"email": OWNER["email"]}


def test_a_mailbox_mismatch_is_a_502_sentence(as_user, configured, store, monkeypatch):
    from inbox_triage_agent.gmail_oauth import ExchangeFailed

    as_user(OWNER)
    monkeypatch.setattr(pipeline, "connect", lambda user_id, **kw: (_ for _ in ()).throw(
        ExchangeFailed("That Google account is not the one you are signed in with.")))
    resp = client.post("/api/inbox/oauth/complete", json={"code": "c", "state": "s"})
    assert resp.status_code == 502 and "signed in with" in resp.json()["detail"]
    assert store.connections == {}


def test_disconnect_says_true_when_google_confirmed_the_revoke(as_user, store, trail, monkeypatch):
    as_user(OWNER)
    store.connections[OWNER["id"]] = _connected_doc()
    monkeypatch.setattr(gmail_oauth, "open_", lambda sealed: "plain")
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: token == "plain")
    body = client.post("/api/inbox/disconnect").json()
    assert body["google_revoked"] is True and body["gmail"]["connected"] is False
    assert "Google permission revoked" in trail[0]["task"]


def test_the_cron_disconnects_a_user_taken_off_the_list(monkeypatch, store, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    monkeypatch.setattr(settings, "inbox_triage_emails", "her@legalsoft.com", raising=False)
    store.users["her@legalsoft.com"] = {"id": OWNER["id"], "email": "her@legalsoft.com"}
    store.connections[OWNER["id"]] = _connected_doc()
    store.connections["user-removed"] = _connected_doc()
    revoked: list[str] = []
    monkeypatch.setattr(gmail_oauth, "open_", lambda sealed: "plain-" + sealed)
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: revoked.append(token) or True)
    monkeypatch.setattr(pipeline, "fire", lambda uid, *, email, budget_seconds: FireReport(user_id=uid))
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "test-only-key"})
    body = resp.json()
    assert resp.status_code == 200 and body["delisted"] == 1
    assert "refresh_token_enc" not in store.connections["user-removed"]
    assert store.connections[OWNER["id"]]["refresh_token_enc"] == "sealed"
    assert revoked == ["plain-sealed"]
    assert "user-removed" not in resp.text
    assert [row["action"] for row in trail] == ["cron_poll"], "a disconnect is not an idle fire"


def test_a_non_ascii_cron_key_is_403_not_500(monkeypatch, trail):
    monkeypatch.setenv("INBOX_CRON_KEY", "test-only-key")
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": "clé-ü".encode("utf-8")})
    assert resp.status_code == 403
    resp = client.post("/api/inbox/cron/poll", headers={"x-cron-key": b"\xe9\xff"})
    assert resp.status_code == 403
    assert trail == []


def test_a_non_ascii_state_is_400_not_500(as_user, configured, store, trail):
    as_user(OWNER)
    state = gmail_oauth.make_state(OWNER["id"])
    head, _, signature = state.rpartition(".")
    resp = client.post("/api/inbox/oauth/complete", json={"code": "c", "state": f"{head}.é{signature[1:]}"})
    assert resp.status_code == 400 and "not valid" in resp.json()["detail"]
    assert trail == [] and store.connections == {}
