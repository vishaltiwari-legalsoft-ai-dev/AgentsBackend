"""The fire and the connection lifecycle, driven against fakes at every seam:
an in-memory store with Firestore's merge semantics, a fake mailbox behind
``gmail_client``, a fake sheet behind ``sheet_writer``, and a fake model
behind ``summarise.llm``. Order, duplicates, retries, the budget, the lease
and the failure modes are asserted from what the fakes recorded."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

import app  # noqa: F401 — registers the agent roots on sys.path
from app.config import settings
from app.services import firestore_repo
from inbox_triage_agent import gmail_client, gmail_oauth, pipeline, sheet_writer, summarise
from inbox_triage_agent.gmail_client import HistoryExpired, Message, MessageGone
from inbox_triage_agent.gmail_oauth import RevokedGrant, Tokens
from inbox_triage_agent.sheet_writer import SheetCheck
from inbox_triage_agent.summarise import ModelCallFailed, ModelUnavailable
from inbox_triage_agent.triage import INBOX_TZ, NEEDS_REVIEW, Rejected, Verdict

UID = "user-aaaa"
#: The caller's signed-in address — also the fake mailbox's own address, so
#: a connect is for her own inbox unless a test says otherwise.
EMAIL = "her@firm.com"
SID = "1abcdefghijklmnopqrstuvwxyz0123456789"
NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


class FakeStore:
    """The a12 section of ``firestore_repo``, with merge and clear semantics."""

    def __init__(self):
        self.connections: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.events: list[str] = []
        self.lease_attempts: list[str] = []

    def get_inbox_connection(self, user_id):
        return copy.deepcopy(self.connections.get(user_id))

    def save_inbox_connection(self, user_id, patch, *, clear=()):
        doc = self.connections.setdefault(user_id, {})
        _deep_merge(doc, copy.deepcopy(patch))
        for name in clear:
            doc.pop(name, None)
        doc["user_id"] = user_id
        self.events.append("save:" + ",".join(sorted(patch)))
        return copy.deepcopy(doc)

    def delete_inbox_connection(self, user_id):
        self.connections.pop(user_id, None)

    def take_inbox_lease(self, user_id, *, now, until):
        """The real primitive's contract, with the real rule: one atomic
        read-and-take, the document back on success, ``None`` when held."""
        doc = self.connections.get(user_id)
        self.lease_attempts.append(until.isoformat())
        if doc is None or not firestore_repo.inbox_lease_free(doc, now):
            return None
        doc["lease_until"] = until.isoformat()
        self.events.append("lease")
        return copy.deepcopy(doc)

    def list_connected_inbox_user_ids(self):
        return [uid for uid, d in self.connections.items() if (d.get("gmail") or {}).get("connected")]

    def list_inbox_messages(self, user_id, *, retry_due=None):
        return [
            copy.deepcopy(d) for d in self.messages.values()
            if d["user_id"] == user_id and (retry_due is None or d.get("retry_due") == retry_due)
        ]

    def save_inbox_messages(self, user_id, docs):
        for message_id, fields in docs.items():
            key = f"{user_id}__{message_id}"
            self.messages.setdefault(key, {}).update(
                {**fields, "user_id": user_id, "message_id": message_id}
            )
        self.events.append("messages")
        return len(docs)

    def delete_inbox_messages(self, user_id):
        keys = [k for k, d in self.messages.items() if d["user_id"] == user_id]
        for key in keys:
            del self.messages[key]
        return len(keys)

    def get_user_by_email(self, email):
        return self.users.get(email.lower())


def _message(message_id: str, subject: str = "Paralegal search", body: str = "Need two by Friday.") -> Message:
    return Message(
        id=message_id, thread_id="t-" + message_id,
        received_at=datetime(2026, 9, 17, 10, 5, tzinfo=INBOX_TZ),
        from_="gm@rathorelegal.in", to="her@firm.com",
        date_header="Thu, 17 Sep 2026 10:05:00 +0530", subject=subject, body_text=body,
    )


class FakeMailbox:
    """What ``gmail_client`` would read: history since a checkpoint, the
    newest-first 90-day listing, and the messages themselves."""

    def __init__(self):
        self.messages: dict[str, Message] = {}
        self.history_added: list[str] = []
        self.history_expired = False
        self.history_id = "900"
        self.listing: list[str] = []
        self.fetched: list[str] = []
        self.profile_calls = 0
        self.events: list[str] = []

    def profile(self, svc):
        self.profile_calls += 1
        return {"email": "her@firm.com", "history_id": self.history_id, "messages_total": len(self.messages)}

    def history_since(self, svc, history_id):
        self.events.append("history")
        if self.history_expired:
            raise HistoryExpired("Gmail history: not found")
        return list(self.history_added), self.history_id

    def list_inbox(self, svc, *, after_epoch, page_token=None, max_results=100):
        self.events.append("list")
        start = int(page_token or 0)
        page = self.listing[start:start + max_results]
        next_token = str(start + max_results) if start + max_results < len(self.listing) else None
        return page, next_token, len(self.listing)

    def fetch(self, svc, message_id):
        if message_id not in self.messages:
            raise MessageGone(message_id)
        self.fetched.append(message_id)
        self.events.append("fetch:" + message_id)
        return self.messages[message_id]


class FakeSheet:
    """What ``sheet_writer`` would do: an id map, appends, updates."""

    def __init__(self):
        self.rows: dict[str, int] = {}
        self.appended: list[list[str]] = []
        self.updated: dict[int, list[str]] = {}
        self.checks = 0
        self.check_result = SheetCheck("ok", "Her inbox")
        self.checked_for: list[str] = []
        self.events: list[str] = []

    def check(self, spreadsheet_id, *, caller_email):
        self.checks += 1
        self.checked_for.append(caller_email)
        return self.check_result

    def id_rows(self, spreadsheet_id, *, svc=None):
        self.events.append("id_rows")
        return dict(self.rows)

    def append(self, spreadsheet_id, rows, *, svc=None):
        if not rows:
            return []
        self.events.append("append")
        first = 2 + len(self.rows)
        numbers = []
        for offset, row in enumerate(rows):
            self.rows[row[7]] = first + offset
            numbers.append(first + offset)
            self.appended.append(row)
        return numbers

    def update(self, spreadsheet_id, row_values, *, svc=None):
        if row_values:
            self.events.append("update")
        self.updated.update(row_values)
        return len(row_values)


def _reply(summary="The sender asks for two paralegals.", deadline="2026-09-18", category="role_to_fill"):
    return json.dumps({"summary": summary, "deadline": deadline, "category": category})


class FakeLLM:
    """Answers by subject: ``GARBLE`` → not JSON, ``BOOM`` → the call raises."""

    def __init__(self):
        self.invocations = 0
        self.always_fail = False

    def invoke(self, conversation):
        self.invocations += 1
        if self.always_fail:
            raise RuntimeError("provider down")
        user_text = conversation[1].content
        if "Subject: BOOM" in user_text:
            raise RuntimeError("provider hiccup")
        if "Subject: GARBLE" in user_text:
            return type("R", (), {"content": "I cannot say.", "usage_metadata": None})()
        return type("R", (), {"content": _reply(), "usage_metadata": {"input_tokens": 300, "output_tokens": 40}})()


#: Every ``firestore_repo`` function the a12 code calls — patched as a set so
#: nothing can fall through to the real (offline-guarded) client.
STORE_SEAMS = (
    "get_inbox_connection", "save_inbox_connection", "delete_inbox_connection",
    "list_inbox_messages", "save_inbox_messages", "delete_inbox_messages", "get_user_by_email",
    "take_inbox_lease", "list_connected_inbox_user_ids",
)


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    for name in STORE_SEAMS:
        monkeypatch.setattr(firestore_repo, name, getattr(fake, name))
    return fake


@pytest.fixture()
def mailbox(monkeypatch) -> FakeMailbox:
    fake = FakeMailbox()
    monkeypatch.setattr(gmail_client, "service", lambda creds: "gmail-service")
    for name in ("profile", "history_since", "list_inbox", "fetch"):
        monkeypatch.setattr(gmail_client, name, getattr(fake, name))
    return fake


@pytest.fixture()
def sheet(monkeypatch) -> FakeSheet:
    fake = FakeSheet()
    monkeypatch.setattr(sheet_writer, "service", lambda: "sheets-service")
    monkeypatch.setattr(sheet_writer, "service_account_email", lambda: "hub@project.iam.gserviceaccount.com")
    for name in ("check", "id_rows", "append", "update"):
        monkeypatch.setattr(sheet_writer, name, getattr(fake, name))
    from marketing_research_agent import sources_registry

    monkeypatch.setattr(sources_registry, "find_source", lambda sid: None)
    return fake


@pytest.fixture()
def model(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(summarise, "build_llm", lambda: fake)
    return fake


@pytest.fixture()
def grant(monkeypatch):
    """A stored grant that opens and refreshes cleanly; ``revoke()`` flips it."""
    state = {"revoked": False}
    monkeypatch.setattr(gmail_oauth, "open_", lambda sealed: "plain-" + sealed)
    monkeypatch.setattr(gmail_oauth, "credentials", lambda plain, access_token=None: ("creds", plain))

    def refresh(creds):
        if state["revoked"]:
            raise RevokedGrant("Google no longer honours this Gmail connection — connect again.")
    monkeypatch.setattr(gmail_oauth, "refresh", refresh)
    return state


def _connected_doc(*, sheet_check="ok", checked_at=NOW - timedelta(minutes=5), history_id="800") -> dict:
    return {
        "gmail": {"connected": True, "address": "her@firm.com", "connected_at": (NOW - timedelta(days=1)).isoformat(), "revoked_at": None},
        "refresh_token_enc": "sealed",
        "checkpoint": {"history_id": history_id, "updated_at": (NOW - timedelta(minutes=5)).isoformat()},
        "sheet": {"id": SID, "url": "https://docs.google.com/spreadsheets/d/x/edit", "title": "Her inbox",
                  "check": sheet_check, "checked_at": checked_at.isoformat() if checked_at else None,
                  "checked_for": EMAIL},
        "backfill": {"state": "running", "cursor": None, "done": 0, "total": None,
                     "since_epoch": int((NOW - timedelta(days=90)).timestamp())},
        "needs_review": 0, "recent_fires": [], "last_poll": None, "lease_until": None,
    }


@pytest.fixture()
def connected(store, mailbox, sheet, model, grant) -> FakeStore:
    store.connections[UID] = _connected_doc()
    return store


# --------------------------------------------------------------------------- #
# Skips
# --------------------------------------------------------------------------- #

def test_a_fire_for_nobody_or_for_an_unconnected_user_skips(store, sheet, mailbox, model):
    assert pipeline.fire("nobody", email=EMAIL, now=NOW).skipped == "gmail not connected"
    store.connections[UID] = {"gmail": {"connected": False}}
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped == "gmail not connected"
    assert mailbox.fetched == [] and sheet.appended == []


def test_no_sheet_or_a_failed_check_skips_and_a_failed_check_is_retried(connected, sheet):
    connected.connections[UID]["sheet"] = {}
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped == "no sheet set"
    connected.connections[UID] = _connected_doc(sheet_check="not_shared")
    sheet.check_result = SheetCheck("not_shared", "")
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.skipped == "sheet check: not_shared"
    assert sheet.checks == 1, "a failed check is re-run every fire"


def test_a_check_older_than_an_hour_is_repeated_and_a_fresh_ok_one_is_not(connected, sheet):
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.checks == 0
    connected.connections[UID]["sheet"]["checked_at"] = (NOW - timedelta(hours=2)).isoformat()
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.checks == 1


# --------------------------------------------------------------------------- #
# Order and duplicates
# --------------------------------------------------------------------------- #

def test_new_mail_is_worked_before_the_backfill_and_the_checkpoint_after_the_append(
    connected, mailbox, sheet
):
    mailbox.messages = {m: _message(m) for m in ("new1", "new2", "old1", "old2")}
    mailbox.history_added = ["new1", "new2"]
    mailbox.history_id = "950"
    mailbox.listing = ["new2", "new1", "old1", "old2"]
    mailbox.events = sheet.events = connected.events = events = []

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 4 and report.messages_read == 4
    fetches = [e for e in events if e.startswith("fetch:")]
    assert fetches == ["fetch:new1", "fetch:new2", "fetch:old1", "fetch:old2"]
    assert events.index("append") < events.index("messages")
    assert events.index("append") < [i for i, e in enumerate(events) if e.startswith("save:") and "checkpoint" in e][0]
    assert events.index("id_rows") < events.index("history")
    doc = connected.connections[UID]
    assert doc["checkpoint"]["history_id"] == "950"
    assert doc["backfill"] == {**doc["backfill"], "state": "done", "cursor": None, "done": 4, "total": 4}
    assert doc["last_poll"] == {"at": NOW.isoformat(), "ok": True, "messages_read": 4, "error": None}
    assert doc["recent_fires"] == [{"at": NOW.isoformat(), "rows": 4}]
    assert doc["lease_until"] is None
    assert [row[7] for row in sheet.appended] == ["new1", "new2", "old1", "old2"]
    assert sheet.appended[0][3] == "role_to_fill" and sheet.appended[0][5] == "2026-09-18"
    tracked = connected.messages[f"{UID}__new1"]
    assert tracked["status"] == "ok" and tracked["retry_due"] is False and tracked["sheet_row"] == 2


def test_the_id_map_makes_a_duplicate_row_impossible(connected, mailbox, sheet):
    sheet.rows = {"m1": 2}
    mailbox.messages = {m: _message(m) for m in ("m1", "m2", "m3")}
    mailbox.history_added = ["m1", "m2"]
    mailbox.listing = ["m3", "m2", "m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.new_rows == 2
    assert mailbox.fetched == ["m2", "m3"]
    assert [row[7] for row in sheet.appended] == ["m2", "m3"]


def test_a_message_deleted_between_listing_and_fetch_is_simply_not_a_row(connected, mailbox, sheet):
    mailbox.history_added = ["gone"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 0 and sheet.appended == []


# --------------------------------------------------------------------------- #
# needs_review and retries
# --------------------------------------------------------------------------- #

def test_an_unreadable_message_gets_its_row_and_at_most_three_later_tries(connected, mailbox, sheet, model):
    mailbox.messages = {"odd": _message("odd", subject="GARBLE")}
    mailbox.history_added = ["odd"]

    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert first.new_rows == 1 and first.needs_review_added == 1
    row = sheet.appended[0]
    assert row[3] == NEEDS_REVIEW and row[4] == "" and row[5] == ""
    tracked = connected.messages[f"{UID}__odd"]
    assert (tracked["status"], tracked["attempts"], tracked["retry_due"]) == ("needs_review", 1, True)
    assert connected.connections[UID]["needs_review"] == 1
    assert model.invocations == 2, "one re-ask, then needs_review"

    mailbox.history_added = []
    for later_fire in (2, 3, 4):
        pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5 * later_fire))
        tracked = connected.messages[f"{UID}__odd"]
        assert tracked["attempts"] == later_fire
    assert tracked["retry_due"] is False
    assert mailbox.fetched == ["odd"] * 4

    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=1))
    assert mailbox.fetched == ["odd"] * 4, "a fourth later try must not happen"
    assert sheet.updated == {}, "a row that stayed needs_review is never rewritten"
    assert connected.connections[UID]["needs_review"] == 1


def test_a_recovered_retry_updates_the_row_found_by_id_not_by_remembered_number(connected, mailbox, sheet, model):
    mailbox.messages = {"odd": _message("odd", subject="GARBLE")}
    mailbox.history_added = ["odd"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    # She sorted the sheet: the row moved from 2 to 9.
    sheet.rows = {"odd": 9}
    connected.messages[f"{UID}__odd"]["sheet_row"] = 2
    mailbox.messages["odd"] = _message("odd", subject="Now readable")
    mailbox.history_added = []

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert report.retried_rows == 1 and report.new_rows == 0
    assert list(sheet.updated) == [9]
    assert sheet.updated[9][3] == "role_to_fill" and sheet.updated[9][7] == "odd"
    tracked = connected.messages[f"{UID}__odd"]
    assert (tracked["status"], tracked["attempts"], tracked["retry_due"], tracked["sheet_row"]) == ("ok", 2, False, 9)
    assert connected.connections[UID]["needs_review"] == 0


def test_a_tracked_row_she_deleted_stops_being_retried(connected, mailbox, sheet):
    connected.messages[f"{UID}__lost"] = {
        "user_id": UID, "message_id": "lost", "status": "needs_review", "attempts": 1,
        "retry_due": True, "sheet_row": 5,
    }
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert connected.messages[f"{UID}__lost"]["retry_due"] is False
    assert mailbox.fetched == []


# --------------------------------------------------------------------------- #
# Checkpoint fallback, revocation, the model being down
# --------------------------------------------------------------------------- #

def test_an_expired_checkpoint_re_lists_the_last_day_under_a_fresh_checkpoint(connected, mailbox, sheet):
    mailbox.history_expired = True
    mailbox.history_id = "1200"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.listing = ["m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 1
    assert mailbox.profile_calls == 1
    assert connected.connections[UID]["checkpoint"]["history_id"] == "1200"


def test_a_revoked_grant_marks_the_connection_disconnected_with_the_reason(connected, mailbox, sheet, grant):
    grant["revoked"] = True
    mailbox.history_added = ["m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "connect again" in report.error
    doc = connected.connections[UID]
    assert doc["gmail"]["connected"] is False and doc["gmail"]["revoked_at"] == NOW.isoformat()
    assert doc["gmail"]["address"] == "her@firm.com", "the address stays so the panel can name it"
    assert "refresh_token_enc" not in doc
    assert doc["last_poll"]["ok"] is False and "connect again" in doc["last_poll"]["error"]
    assert doc["lease_until"] is None
    assert mailbox.fetched == [] and sheet.appended == []
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped == "gmail not connected"


def test_a_model_that_is_down_fails_the_fire_loudly_with_no_rows(connected, mailbox, sheet, model):
    model.always_fail = True
    mailbox.messages = {m: _message(m) for m in ("a", "b", "c", "d")}
    mailbox.history_added = ["a", "b", "c", "d"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "abandoned" in report.error
    assert sheet.appended == [] and connected.messages == {}
    assert mailbox.fetched == ["a", "b", "c"], "three failures in a row is the trip"
    doc = connected.connections[UID]
    assert doc["last_poll"]["ok"] is False and doc["checkpoint"]["history_id"] == "800"
    assert doc["lease_until"] is None


def test_one_flaky_call_is_a_needs_review_row_not_a_failed_fire(connected, mailbox, sheet, model):
    mailbox.messages = {"a": _message("a", subject="BOOM"), "b": _message("b")}
    mailbox.history_added = ["a", "b"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 2 and report.needs_review_added == 1
    assert sheet.appended[0][3] == NEEDS_REVIEW and sheet.appended[1][3] == "role_to_fill"


def test_no_model_key_fails_before_any_mail_is_read(connected, mailbox, sheet, monkeypatch):
    monkeypatch.setattr(summarise, "build_llm", lambda: (_ for _ in ()).throw(
        ModelUnavailable('Missing required configuration "openrouter_api_key".')))
    mailbox.history_added = ["m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "openrouter_api_key" in report.error
    assert mailbox.fetched == [] and mailbox.events == []


# --------------------------------------------------------------------------- #
# Budget and lease
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def test_the_budget_stops_the_loop_keeps_the_checkpoint_and_reports_partial(connected, mailbox, sheet, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    real_fetch = mailbox.fetch

    def slow_fetch(svc, message_id):
        clock.t += 30.0
        return real_fetch(svc, message_id)
    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)

    mailbox.messages = {f"m{i}": _message(f"m{i}") for i in range(12)}
    mailbox.history_added = [f"m{i}" for i in range(12)]
    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=100.0)  # 80s of work after the reserve

    assert report.unreached is True and report.ok is True
    assert report.new_rows == 3 and mailbox.fetched == ["m0", "m1", "m2"]
    assert [row[7] for row in sheet.appended] == ["m0", "m1", "m2"], "what was done is written"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "800", "an unfinished pass keeps the old checkpoint"
    assert connected.connections[UID]["backfill"]["state"] == "running"


def test_the_backfill_only_moves_its_cursor_past_a_page_it_finished(connected, mailbox, sheet, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    real_fetch = mailbox.fetch

    def slow_fetch(svc, message_id):
        clock.t += 30.0
        return real_fetch(svc, message_id)
    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)
    monkeypatch.setattr(gmail_client, "LIST_PAGE_MAX", 2)

    mailbox.messages = {f"o{i}": _message(f"o{i}") for i in range(6)}
    mailbox.listing = [f"o{i}" for i in range(6)]
    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=110.0)  # 90s: three fetches

    backfill = connected.connections[UID]["backfill"]
    assert mailbox.fetched == ["o0", "o1", "o2"]
    assert (backfill["cursor"], backfill["done"]) == ("2", 2), "page two was cut short, so it stays"
    assert backfill["total"] == 6 and report.unreached

    clock.t = 5000.0
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5), budget_seconds=1000.0)
    assert mailbox.fetched == ["o0", "o1", "o2", "o3", "o4", "o5"], "the cut page was re-listed and o2 skipped"
    assert connected.connections[UID]["backfill"]["state"] == "done"


def test_the_backfill_is_capped_per_fire(connected, mailbox, sheet, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_PER_FIRE", 3)
    mailbox.messages = {f"o{i}": _message(f"o{i}") for i in range(5)}
    mailbox.listing = [f"o{i}" for i in range(5)]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert mailbox.fetched == ["o0", "o1", "o2"]
    assert connected.connections[UID]["backfill"]["state"] == "running"


def test_a_fire_holding_the_lease_makes_the_next_one_skip(connected, mailbox, sheet):
    connected.connections[UID]["lease_until"] = (NOW + timedelta(seconds=120)).isoformat()
    mailbox.history_added = ["m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.skipped == "previous fire still running"
    assert mailbox.events == [] and sheet.events == []
    connected.connections[UID]["lease_until"] = (NOW - timedelta(seconds=1)).isoformat()
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped is None


def test_the_lease_is_taken_at_the_start_and_cleared_even_when_the_fire_fails(connected, mailbox, sheet, monkeypatch):
    seen: list = []
    real_save = connected.save_inbox_connection

    def spying_save(user_id, patch, *, clear=()):
        seen.append(patch.get("lease_until", "absent"))
        return real_save(user_id, patch, clear=clear)
    monkeypatch.setattr(firestore_repo, "save_inbox_connection", spying_save)
    monkeypatch.setattr(sheet_writer, "id_rows", lambda sid, svc=None: (_ for _ in ()).throw(
        sheet_writer.SheetsUnavailable("Sheets id column read failed after 3 attempts")))
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "Sheets" in report.error
    assert connected.lease_attempts == [(NOW + timedelta(seconds=pipeline.LEASE_SECONDS)).isoformat()]
    assert seen[-1] is None
    assert connected.connections[UID]["last_poll"]["ok"] is False


# --------------------------------------------------------------------------- #
# Lifecycle: connect, sheet, disconnect, status
# --------------------------------------------------------------------------- #

@pytest.fixture()
def consent(monkeypatch, mailbox):
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", Fernet.generate_key().decode(), raising=False)
    monkeypatch.setattr(gmail_oauth, "complete", lambda uid, *, code, state: Tokens("1//refresh-plain", "ya29"))
    mailbox.history_id = "700"


def test_connect_seals_the_token_takes_the_checkpoint_and_reads_no_mail(store, sheet, consent, mailbox):
    doc = pipeline.connect(UID, code="c", state="s", email=EMAIL)
    assert doc["gmail"]["connected"] is True and doc["gmail"]["address"] == "her@firm.com"
    assert doc["checkpoint"]["history_id"] == "700"
    assert doc["backfill"]["state"] == "not_started" and doc["backfill"]["done"] == 0
    assert doc["refresh_token_enc"] != "1//refresh-plain" and "refresh-plain" not in doc["refresh_token_enc"]
    assert gmail_oauth.open_(doc["refresh_token_enc"]) == "1//refresh-plain"
    assert mailbox.fetched == [] and mailbox.events == []
    payload = pipeline.status_payload(UID, enabled=True, now=NOW)
    assert "refresh_token_enc" not in json.dumps(payload) and "refresh-plain" not in json.dumps(payload)


def test_setting_a_sheet_checks_it_and_starts_the_backfill_once_gmail_is_connected(store, sheet, consent):
    with pytest.raises(ValueError, match="Google Sheet link"):
        pipeline.set_sheet(UID, "not a sheet", email=EMAIL)
    doc = pipeline.set_sheet(UID, f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0", email=EMAIL)
    assert doc["sheet"]["id"] == SID and doc["sheet"]["check"] == "ok" and doc["sheet"]["title"] == "Her inbox"
    assert doc.get("backfill", {}).get("state") in (None, "not_started")
    doc = pipeline.connect(UID, code="c", state="s", email=EMAIL)
    assert doc["backfill"]["state"] == "running"
    assert sheet.checks == 1


def test_a_recheck_without_a_sheet_says_so(store):
    with pytest.raises(ValueError, match="No sheet"):
        pipeline.recheck_sheet(UID, email=EMAIL)


def test_disconnect_deletes_token_and_tracking_zeroes_counters_and_keeps_the_sheet(connected):
    connected.messages[f"{UID}__x"] = {"user_id": UID, "message_id": "x", "status": "ok"}
    connected.messages["other__x"] = {"user_id": "other", "message_id": "x", "status": "ok"}
    connected.connections[UID]["needs_review"] = 3
    doc = pipeline.disconnect(UID).doc
    assert "refresh_token_enc" not in doc and "gmail" not in doc and "checkpoint" not in doc
    assert doc["sheet"]["id"] == SID
    assert doc["needs_review"] == 0 and doc["recent_fires"] == [] and doc["backfill"]["state"] == "not_started"
    assert list(connected.messages) == ["other__x"]


def test_status_for_an_outsider_is_disabled_nulls_and_never_reads_the_store(monkeypatch):
    monkeypatch.setattr(firestore_repo, "get_inbox_connection",
                        lambda uid: pytest.fail("the store must not be read for an outsider"))
    payload = pipeline.status_payload("anyone", enabled=False, now=NOW)
    assert payload == {
        "enabled": False, "service_account_email": "",
        "gmail": {"connected": False, "address": None, "connected_at": None},
        "sheet": {"id": None, "url": None, "title": None, "check": None, "checked_at": None},
        "backfill": {"state": None, "done": 0, "total": None},
        "last_poll": {"at": None, "ok": None, "messages_read": 0, "error": None},
        "next_poll_at": None, "rows_24h": 0, "needs_review": 0, "generated_at": NOW.isoformat(),
    }


def test_status_for_the_owner_is_one_read_with_the_next_poll_computed(connected, sheet):
    doc = connected.connections[UID]
    doc["last_poll"] = {"at": (NOW - timedelta(minutes=2)).isoformat(), "ok": True, "messages_read": 3, "error": None}
    doc["recent_fires"] = [
        {"at": (NOW - timedelta(hours=23)).isoformat(), "rows": 5},
        {"at": (NOW - timedelta(hours=25)).isoformat(), "rows": 9},
    ]
    doc["needs_review"] = 2
    payload = pipeline.status_payload(UID, enabled=True, now=NOW)
    assert payload["enabled"] is True
    assert payload["service_account_email"] == "hub@project.iam.gserviceaccount.com"
    assert payload["gmail"] == {"connected": True, "address": "her@firm.com", "connected_at": doc["gmail"]["connected_at"]}
    assert payload["sheet"]["id"] == SID and payload["sheet"]["check"] == "ok"
    assert payload["backfill"] == {"state": "running", "done": 0, "total": None}
    assert payload["next_poll_at"] == (NOW + timedelta(minutes=3)).isoformat()
    assert payload["rows_24h"] == 5 and payload["needs_review"] == 2
    assert "refresh_token_enc" not in json.dumps(payload)
    doc["last_poll"] = None
    assert pipeline.status_payload(UID, enabled=True, now=NOW)["next_poll_at"] == NOW.isoformat()


# --------------------------------------------------------------------------- #
# summarise — the model seam
# --------------------------------------------------------------------------- #

def test_a_valid_reply_is_a_verdict_and_nothing_of_the_email_is_logged(caplog):
    llm = FakeLLM()
    message = _message("m1", subject="Paralegal search", body="Need two by Friday, ping Priya.")
    import logging

    with caplog.at_level(logging.INFO, logger="agentos.inbox.summarise"):
        verdict = summarise.summarise(message, llm=llm)
    assert isinstance(verdict, Verdict) and verdict.category == "role_to_fill"
    assert llm.invocations == 1
    for secret in ("Paralegal", "Priya", "Friday", "rathorelegal"):
        assert secret not in caplog.text


def test_an_invalid_reply_is_asked_once_more_with_the_reason_then_rejected():
    class TwoStep(FakeLLM):
        def __init__(self, second):
            super().__init__()
            self.second, self.conversations = second, []

        def invoke(self, conversation):
            self.invocations += 1
            self.conversations.append(conversation)
            content = "nope" if self.invocations == 1 else self.second
            return type("R", (), {"content": content, "usage_metadata": None})()

    llm = TwoStep(_reply())
    assert isinstance(summarise.summarise(_message("m1"), llm=llm), Verdict)
    assert llm.invocations == 2
    re_ask = llm.conversations[1][-1].content
    assert "was not JSON" in re_ask and "Need two" not in re_ask

    llm = TwoStep("still nope")
    verdict = summarise.summarise(_message("m1"), llm=llm)
    assert isinstance(verdict, Rejected) and verdict.reason == "The reply was not JSON."


def test_a_call_that_raises_is_model_call_failed():
    with pytest.raises(ModelCallFailed):
        summarise.summarise(_message("m1", subject="BOOM"), llm=FakeLLM())


def test_the_model_is_built_as_a12_with_the_caps_and_refuses_offline(monkeypatch):
    from inbox_triage_agent import InboxOffline

    with pytest.raises(InboxOffline):
        summarise.build_llm()
    captured: dict = {}
    monkeypatch.setattr(summarise, "get_llm", lambda **kw: captured.update(kw) or "llm")
    monkeypatch.delenv("INBOX_OFFLINE", raising=False)
    assert summarise.build_llm() == "llm"
    assert captured == {
        "temperature": 0.0, "fast": True, "agent_id": "a12",
        "timeout": summarise.OPENROUTER_TIMEOUT_SECONDS, "max_tokens": 300,
    }


def test_no_key_is_model_unavailable_naming_the_setting(monkeypatch):
    monkeypatch.delenv("INBOX_OFFLINE", raising=False)
    with pytest.raises(ModelUnavailable, match="openrouter_api_key"):
        summarise.build_llm()  # the repo-root guard blanks the key



# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (tester pass): whole-fire order, re-fire idempotence,
# reconnect, the fallback window, the lease under an unmapped error, a budget
# cut inside a backfill page, and that nothing of a message reaches any log.
# --------------------------------------------------------------------------- #

def _timeline_patches(store: FakeStore, sheet: FakeSheet) -> tuple[list[str], dict]:
    """One timeline across the seams that matter for write-then-persist."""
    timeline: list[str] = []
    real_id_rows, real_append, real_update = sheet.id_rows, sheet.append, sheet.update
    real_messages, real_save = store.save_inbox_messages, store.save_inbox_connection

    def id_rows(sid, *, svc=None):
        timeline.append("id_rows")
        return real_id_rows(sid, svc=svc)

    def append(sid, rows, *, svc=None):
        timeline.append(f"append:{len(rows)}")
        return real_append(sid, rows, svc=svc)

    def update(sid, row_values, *, svc=None):
        timeline.append(f"update:{len(row_values)}")
        return real_update(sid, row_values, svc=svc)

    def save_messages(user_id, docs):
        timeline.append("persist:messages")
        return real_messages(user_id, docs)

    def save_connection(user_id, patch, *, clear=()):
        if "checkpoint" in patch:
            timeline.append("persist:checkpoint")
        elif "last_poll" in patch:
            timeline.append("persist:poll")
        return real_save(user_id, patch, clear=clear)

    return timeline, {
        (sheet_writer, "id_rows"): id_rows, (sheet_writer, "append"): append,
        (sheet_writer, "update"): update,
        (firestore_repo, "save_inbox_messages"): save_messages,
        (firestore_repo, "save_inbox_connection"): save_connection,
    }


def test_a_whole_fire_reads_ids_then_appends_then_persists_then_checkpoints_and_a_refire_appends_nothing(
    connected, mailbox, sheet, monkeypatch
):
    mailbox.messages = {m: _message(m) for m in ("n1", "n2", "b1")}
    mailbox.history_added = ["n1", "n2"]
    mailbox.history_id = "950"
    mailbox.listing = ["n2", "n1", "b1"]
    timeline, patches = _timeline_patches(connected, sheet)
    for (module, name), fn in patches.items():
        monkeypatch.setattr(module, name, fn)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert first.ok and first.new_rows == 3
    assert timeline == [
        "id_rows", "append:3", "update:0", "persist:messages", "persist:checkpoint",
    ], "the sheet is read first, written once, and only then is anything persisted"

    # The same mail offered again (history still reports it; the backfill is
    # done): the id map read at the start of the fire makes it a no-op.
    timeline.clear()
    connected.connections[UID]["checkpoint"]["history_id"] = "800"
    second = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert second.ok and second.new_rows == 0 and second.messages_read == 0
    assert timeline[0] == "id_rows" and "append:0" in timeline
    assert [row[7] for row in sheet.appended] == ["n1", "n2", "b1"], "no row was written twice"
    assert mailbox.fetched == ["n1", "n2", "b1"], "nothing already on the sheet is re-read"


def test_a_fire_that_died_between_append_and_persist_duplicates_nothing_next_time(
    connected, mailbox, sheet, monkeypatch
):
    mailbox.messages = {m: _message(m) for m in ("n1", "n2")}
    mailbox.history_added = ["n1", "n2"]
    mailbox.history_id = "950"
    before = copy.deepcopy(connected.connections[UID])

    def dies(user_id, docs):
        raise RuntimeError("instance killed between write and persist")
    monkeypatch.setattr(firestore_repo, "save_inbox_messages", dies)
    with pytest.raises(RuntimeError, match="killed"):
        pipeline.fire(UID, email=EMAIL, now=NOW)
    assert [row[7] for row in sheet.appended] == ["n1", "n2"]
    assert connected.connections[UID]["checkpoint"] == before["checkpoint"], "the checkpoint did not move"
    assert connected.connections[UID]["lease_until"] is None, "the lease is cleared even for an unmapped error"

    monkeypatch.setattr(firestore_repo, "save_inbox_messages", connected.save_inbox_messages)
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 0
    assert [row[7] for row in sheet.appended] == ["n1", "n2"], "duplicate-over-drop must not become a duplicate row"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "950"


def test_reconnecting_after_a_disconnect_appends_nothing_already_on_the_sheet(
    store, mailbox, sheet, model, grant, consent
):
    store.connections[UID] = _connected_doc()
    mailbox.messages = {m: _message(m) for m in ("a", "b", "c")}
    mailbox.listing = ["c", "b", "a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert [row[7] for row in sheet.appended] == ["c", "b", "a"]

    pipeline.disconnect(UID)
    assert store.connections[UID]["sheet"]["id"] == SID and store.messages == {}
    doc = pipeline.connect(UID, code="c", state="s", email=EMAIL)
    assert doc["backfill"]["state"] == "running", "the kept sheet is still ok, so the backfill restarts"
    fetched_before = list(mailbox.fetched)

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert report.ok and report.new_rows == 0
    assert [row[7] for row in sheet.appended] == ["c", "b", "a"], "a reconnect must not re-append her rows"
    assert mailbox.fetched == fetched_before, "rows already on the sheet are not re-read either"
    assert store.connections[UID]["backfill"]["state"] == "done"


def test_the_fallback_takes_a_fresh_checkpoint_before_listing_from_last_poll_less_a_day(
    connected, mailbox, sheet, monkeypatch
):
    last_poll_at = NOW - timedelta(minutes=5)
    connected.connections[UID]["last_poll"] = {
        "at": last_poll_at.isoformat(), "ok": True, "messages_read": 0, "error": None,
    }
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.history_expired = True
    mailbox.history_id = "1200"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.listing = ["m1"]
    order: list[str] = []
    listed_after: list[int] = []
    real_profile, real_list = mailbox.profile, mailbox.list_inbox

    def profile(svc):
        order.append("profile")
        return real_profile(svc)

    def list_inbox(svc, *, after_epoch, page_token=None, max_results=100):
        order.append("list")
        listed_after.append(after_epoch)
        return real_list(svc, after_epoch=after_epoch, page_token=page_token, max_results=max_results)
    monkeypatch.setattr(gmail_client, "profile", profile)
    monkeypatch.setattr(gmail_client, "list_inbox", list_inbox)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1
    assert order == ["profile", "list"], "the fresh history id is taken BEFORE the re-listing"
    assert listed_after == [int(last_poll_at.timestamp()) - 86400]
    assert connected.connections[UID]["checkpoint"]["history_id"] == "1200"


def test_the_fallback_without_a_last_poll_counts_the_day_back_from_the_checkpoint_time(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    checkpoint_at = datetime.fromisoformat(connected.connections[UID]["checkpoint"]["updated_at"])
    mailbox.history_expired = True
    listed_after: list[int] = []
    real_list = mailbox.list_inbox

    def list_inbox(svc, *, after_epoch, page_token=None, max_results=100):
        listed_after.append(after_epoch)
        return real_list(svc, after_epoch=after_epoch, page_token=page_token, max_results=max_results)
    monkeypatch.setattr(gmail_client, "list_inbox", list_inbox)

    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert listed_after == [int(checkpoint_at.timestamp()) - 86400]


def test_the_lease_is_cleared_when_the_fire_dies_of_an_unmapped_error(connected, mailbox, sheet, monkeypatch):
    mailbox.history_added = ["m1"]
    monkeypatch.setattr(gmail_client, "history_since", lambda svc, hid: (_ for _ in ()).throw(
        KeyError("a programming error")))
    with pytest.raises(KeyError):
        pipeline.fire(UID, email=EMAIL, now=NOW)
    assert connected.connections[UID]["lease_until"] is None
    assert sheet.appended == [] and connected.messages == {}
    # And the next fire is not locked out by it.
    monkeypatch.setattr(gmail_client, "history_since", mailbox.history_since)
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped is None


def test_a_budget_cut_inside_a_backfill_page_reports_partial_and_keeps_the_cursor(
    connected, mailbox, sheet, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    real_fetch = mailbox.fetch

    def slow_fetch(svc, message_id):
        clock.t += 30.0
        return real_fetch(svc, message_id)
    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)
    monkeypatch.setattr(gmail_client, "LIST_PAGE_MAX", 5)
    mailbox.messages = {f"o{i}": _message(f"o{i}") for i in range(5)}
    mailbox.listing = [f"o{i}" for i in range(5)]

    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=80.0)  # 60s of work: two fetches

    backfill = connected.connections[UID]["backfill"]
    assert report.ok and report.unreached is True
    assert report.backfill_state == "running" and report.new_rows == 2
    assert (backfill["cursor"], backfill["done"]) == (None, 0), "a page cut short does not advance"
    assert [row[7] for row in sheet.appended] == ["o0", "o1"]


def test_nothing_of_a_message_reaches_any_log_during_a_whole_fire(connected, mailbox, sheet, model, caplog):
    import logging

    mailbox.messages = {
        "ok": _message("ok", subject="Paralegal search Priya", body="Call Priya at 98100 before Friday."),
        "odd": _message("odd", subject="GARBLE Rathore offer", body="Offer letter for Rathore."),
        "boom": _message("boom", subject="BOOM Kapoor escalation", body="Kapoor is unhappy."),
    }
    mailbox.history_added = ["ok", "odd", "boom"]
    with caplog.at_level(logging.DEBUG):
        report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 3 and report.needs_review_added == 2
    text = caplog.text
    for secret in ("Priya", "98100", "Rathore", "Kapoor", "Paralegal", "rathorelegal",
                   "her@firm.com", "two paralegals"):
        assert secret not in text, f"{secret!r} leaked into the logs"


def test_a_sheet_that_stopped_being_shared_between_checks_fails_the_fire_on_the_panel_not_by_raising(
    connected, mailbox, sheet, monkeypatch
):
    """The sheet check is re-run hourly, so for up to an hour a fire can meet
    a sheet she has since un-shared. The real ``sheet_writer.id_rows`` lets
    the 403 out as a raw ``HttpError`` (see test_sheet_writer); ``fire`` only
    maps ``SheetsUnavailable``. Contract (pipeline.fire docstring): a reason
    the panel should show lands in ``last_poll`` and the report, never as a
    raise."""
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    def refused(sid, *, svc=None):
        raise HttpError(SimpleNamespace(status=403, reason="forbidden"), b"The caller does not have permission")
    monkeypatch.setattr(sheet_writer, "id_rows", refused)
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok is False and report.error
    doc = connected.connections[UID]
    assert doc["last_poll"]["ok"] is False and doc["last_poll"]["error"]
    assert doc["lease_until"] is None and sheet.appended == []


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (review fixes): the sheet and the mailbox must be the
# caller's, disconnect really revokes, de-listed users are disconnected, the
# lease is one atomic take, and nothing identifying reaches a log or an error.
# --------------------------------------------------------------------------- #

def test_marketing_researchs_primary_tracker_and_connected_sheets_are_refused_with_nothing_asked(
    store, sheet, monkeypatch
):
    from marketing_research_agent import config as mr_config
    from marketing_research_agent import sources_registry

    doc = pipeline.set_sheet(UID, mr_config.SHEETS_SPREADSHEET_ID, email=EMAIL)
    assert doc["sheet"]["check"] == "mr_source" and doc["sheet"]["title"] == ""
    monkeypatch.setattr(sources_registry, "find_source",
                        lambda sid: {"id": sid, "label": "Leads"} if sid == SID else None)
    doc = pipeline.set_sheet(UID, SID, email=EMAIL)
    assert doc["sheet"]["check"] == "mr_source"
    assert sheet.checks == 0, "Google is not even asked about an MR sheet"
    assert doc.get("backfill", {}).get("state") in (None, "not_started")


def test_an_unreadable_mr_registry_fails_loudly_and_stores_nothing(store, sheet, monkeypatch):
    from marketing_research_agent import sources_registry

    monkeypatch.setattr(sources_registry, "find_source", lambda sid: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(pipeline.SheetsUnavailable, match="nothing was written"):
        pipeline.set_sheet(UID, SID, email=EMAIL)
    assert sheet.checks == 0 and UID not in store.connections


def test_the_check_is_made_for_the_callers_address_and_a_refusal_stores_no_title(store, sheet):
    sheet.check_result = SheetCheck("not_yours", "Another person's sheet")
    doc = pipeline.set_sheet(UID, SID, email="Her@Firm.com")
    assert sheet.checked_for == ["Her@Firm.com"]
    assert doc["sheet"]["check"] == "not_yours" and doc["sheet"]["title"] == ""
    assert doc["sheet"]["checked_for"] == "her@firm.com"


def test_the_fire_re_proves_ownership_for_a_check_made_for_someone_else_and_writes_nothing_if_not_yours(
    connected, mailbox, sheet
):
    connected.connections[UID]["sheet"]["checked_for"] = "previous.owner@firm.com"
    sheet.check_result = SheetCheck("not_yours", "")
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.checked_for == [EMAIL]
    assert report.skipped == "sheet check: not_yours"
    assert sheet.appended == [] and mailbox.fetched == []


def test_the_hourly_recheck_re_proves_ownership_and_an_mr_sheet_is_caught_there_too(
    connected, mailbox, sheet, monkeypatch
):
    from marketing_research_agent import sources_registry

    connected.connections[UID]["sheet"]["checked_at"] = (NOW - timedelta(hours=2)).isoformat()
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.checked_for == [EMAIL]

    connected.connections[UID]["sheet"]["checked_at"] = (NOW - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(sources_registry, "find_source", lambda sid: {"id": sid})
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.skipped == "sheet check: mr_source" and sheet.checks == 1


def test_connecting_another_mailbox_is_refused_revoked_and_stores_nothing(
    store, sheet, consent, mailbox, monkeypatch
):
    from inbox_triage_agent.gmail_oauth import ExchangeFailed

    revoked: list[str] = []
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: revoked.append(token) or True)
    sealed: list = []
    monkeypatch.setattr(gmail_oauth, "seal", lambda plain: sealed.append(plain) or "x")
    with pytest.raises(ExchangeFailed) as caught:
        pipeline.connect(UID, code="c", state="s", email="not.her@legalsoft.com")
    message = str(caught.value)
    assert "her@firm.com" not in message, "the other mailbox address is never echoed"
    assert "not.her@legalsoft.com" in message
    assert revoked == ["1//refresh-plain"] and sealed == [] and store.connections == {}


def test_connecting_her_own_mailbox_matches_case_insensitively(store, sheet, consent, mailbox):
    doc = pipeline.connect(UID, code="c", state="s", email="HER@Firm.COM")
    assert doc["gmail"]["connected"] is True


def test_disconnect_revokes_at_google_then_clears_and_says_whether_google_confirmed(connected, monkeypatch):
    posted: list[str] = []
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: posted.append(token) or True)
    result = pipeline.disconnect(UID)
    assert posted == ["plain-sealed"] and result.google_revoked is True
    assert "refresh_token_enc" not in connected.connections[UID]

    connected.connections[UID] = _connected_doc()
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: False)
    result = pipeline.disconnect(UID)
    assert result.google_revoked is False
    assert "refresh_token_enc" not in result.doc and "gmail" not in result.doc, "cleared regardless"


def test_disconnect_with_an_unopenable_or_missing_token_still_clears_and_says_false(connected, monkeypatch):
    monkeypatch.setattr(gmail_oauth, "open_", lambda sealed: (_ for _ in ()).throw(RevokedGrant("rotated")))
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: pytest.fail("nothing to revoke"))
    assert pipeline.disconnect(UID).google_revoked is False
    assert pipeline.disconnect(UID).google_revoked is False  # now there is no token at all


def test_purge_delisted_disconnects_only_users_off_the_list(connected, monkeypatch):
    connected.connections["user-gone"] = _connected_doc()
    connected.messages["user-gone__x"] = {"user_id": "user-gone", "message_id": "x"}
    revoked: list[str] = []
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: revoked.append(token) or True)
    assert pipeline.purge_delisted({UID}) == 1
    assert "refresh_token_enc" not in connected.connections["user-gone"]
    assert "gmail" not in connected.connections["user-gone"] and connected.messages == {}
    assert connected.connections[UID]["refresh_token_enc"] == "sealed"
    assert revoked == ["plain-sealed"]
    assert pipeline.purge_delisted({UID}) == 0, "already disconnected — nothing to do"


def test_the_lease_rule_is_the_one_the_store_applies():
    free = firestore_repo.inbox_lease_free
    assert free(None, NOW) and free({}, NOW) and free({"lease_until": None}, NOW)
    assert free({"lease_until": "garbage"}, NOW)
    assert free({"lease_until": NOW.isoformat()}, NOW)
    assert not free({"lease_until": (NOW + timedelta(seconds=1)).isoformat()}, NOW)
    assert not free({"lease_until": (NOW + timedelta(seconds=1)).replace(tzinfo=None).isoformat()}, NOW)


def test_two_overlapping_fires_cannot_both_take_the_lease(connected, mailbox, sheet, monkeypatch):
    """The second fire starts while the first is mid-flight (inside the id
    read): the atomic take refuses it, and it never reads mail or the sheet."""
    inner: list = []
    real_id_rows = sheet.id_rows

    def id_rows_with_an_overlap(sid, *, svc=None):
        if not inner:
            inner.append(pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(seconds=10)))
        return real_id_rows(sid, svc=svc)
    monkeypatch.setattr(sheet_writer, "id_rows", id_rows_with_an_overlap)
    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert first.ok and first.skipped is None
    assert inner[0].skipped == "previous fire still running"
    assert len(connected.lease_attempts) == 2 and connected.events.count("lease") == 1
    assert connected.connections[UID]["lease_until"] is None


def test_a_raw_sheets_refusal_reaches_the_panel_without_the_sheet_id_and_the_log_without_the_user(
    connected, mailbox, sheet, monkeypatch, caplog
):
    import logging
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    uri = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/Inbox%21H%3AH"
    monkeypatch.setattr(sheet_writer, "id_rows", lambda sid, *, svc=None: (_ for _ in ()).throw(
        HttpError(SimpleNamespace(status=403, reason="forbidden"), b"denied", uri=uri)))
    with caplog.at_level(logging.DEBUG):
        report = pipeline.fire(UID, email=EMAIL, now=NOW)
    error = connected.connections[UID]["last_poll"]["error"]
    assert report.error == error and "HTTP 403" in error
    for leaked in (SID, "googleapis", uri):
        assert leaked not in error and leaked not in caplog.text
    assert UID not in caplog.text, "logs carry the hashed label, never the user id"
