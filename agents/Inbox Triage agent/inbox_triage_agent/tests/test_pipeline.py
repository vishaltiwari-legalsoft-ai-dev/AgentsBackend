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
from inbox_triage_agent import (
    gmail_client, gmail_oauth, pipeline, sheet_writer, summarise, worktree,
)
from inbox_triage_agent.gmail_client import HistoryExpired, Message, MessageGone
from inbox_triage_agent.gmail_oauth import RevokedGrant, Tokens
from inbox_triage_agent.sheet_layout import (
    CATEGORY_LABELS, COL_ACTION, COL_CATEGORY, COL_DEADLINE, COL_MESSAGE_ID, COL_STATUS,
    COL_SUMMARY, HEADERS, WORKTREE_HEADERS, message_link,
)
from inbox_triage_agent.sheet_writer import SheetCheck, SheetsUnavailable
from inbox_triage_agent.summarise import ModelCallFailed, ModelUnavailable
from inbox_triage_agent.triage import TEAM_TIMEZONE, NEEDS_REVIEW, Rejected, Verdict

#: 0-based positions in a row of agent cells, from the layout's own constants.
ID, CAT, SUMMARY, ACTION, DEADLINE = (
    COL_MESSAGE_ID - 1, COL_CATEGORY - 1, COL_SUMMARY - 1, COL_ACTION - 1, COL_DEADLINE - 1,
)
ASKS = CATEGORY_LABELS["action_required"]
UNREAD = CATEGORY_LABELS[NEEDS_REVIEW]

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

    def get_users_by_ids(self, user_ids):
        """``self.users`` is keyed by address for readability; the real read
        is by id, and a deleted user is simply absent."""
        self.events.append("users")
        wanted = set(user_ids)
        return {u["id"]: dict(u) for u in self.users.values() if u["id"] in wanted}


def _message(
    message_id: str, subject: str = "Paralegal search", body: str = "Need two by Friday.",
    *, thread: str = "", sender: str = "gm@rathorelegal.in",
) -> Message:
    return Message(
        id=message_id, thread_id=thread or ("t-" + message_id),
        received_at=datetime(2026, 9, 17, 10, 5, tzinfo=TEAM_TIMEZONE),
        from_=sender, to="her@firm.com",
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
        #: ``message id -> thread id`` as the LISTING reports it; a message
        #: the listing never mentions is not in here.
        self.threads: dict[str, str] = {}
        #: What the SENT label lists, in the same ``(id, thread id)`` shape,
        #: and the times a stamp read answers with.
        self.sent: list[tuple[str, str]] = []
        self.sent_times: dict[str, datetime] = {}
        self.stamped: list[str] = []
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

    def thread_of(self, message_id: str) -> str:
        """The thread id Gmail's listing carries for a message."""
        if message_id in self.threads:
            return self.threads[message_id]
        message = self.messages.get(message_id)
        return message.thread_id if message else ""

    def list_inbox_pairs(self, svc, *, after_epoch, page_token=None, max_results=100):
        self.events.append("list_pairs")
        start = int(page_token or 0)
        page = self.listing[start:start + max_results]
        next_token = str(start + max_results) if start + max_results < len(self.listing) else None
        return [(m, self.thread_of(m)) for m in page], next_token, len(self.listing)

    def list_sent_pairs(self, svc, *, after_epoch, page_token=None, max_results=500):
        self.events.append("list_sent")
        start = int(page_token or 0)
        page = self.sent[start:start + max_results]
        next_token = str(start + max_results) if start + max_results < len(self.sent) else None
        return list(page), next_token, len(self.sent)

    def stamp(self, svc, message_id):
        """``messages.get(format="minimal")`` — the time, and nothing else."""
        self.events.append("stamp:" + message_id)
        self.stamped.append(message_id)
        if message_id not in self.sent_times:
            raise MessageGone(message_id)
        return self.sent_times[message_id]

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
    """What ``sheet_writer`` would do: an id map, appends, updates, the
    Action column the one-time re-triage reads, and a real grid — her Status
    column included — so the Worktree is built from cells, not from a stub."""

    def __init__(self):
        self.rows: dict[str, int] = {}
        #: ids whose row has a category but an empty Action (legacy rows)
        self.blank_actions: list[str] = []
        self.appended: list[list[str]] = []
        self.updated: dict[int, list[str]] = {}
        self.checks = 0
        self.check_result = SheetCheck("ok", "Her inbox", "", "ours")
        self.checked_for: list[str] = []
        self.events: list[str] = []
        #: 1-based row number -> the eleven cells A..K, hers included
        self.grid: dict[int, list[str]] = {}
        self.worktree: list[list[str]] = []
        self.worktree_writes = 0

    def _put(self, row: int, values: list[str]) -> None:
        cells = self.grid.setdefault(row, [""] * len(HEADERS))
        for index, value in enumerate(values):
            cells[index] = value

    def set_status(self, message_id: str, value: str) -> None:
        """Her Status cell, set the way she would set it."""
        self._put(self.rows[message_id], [])
        self.grid[self.rows[message_id]][COL_STATUS - 1] = value

    def read_inbox(self, spreadsheet_id, *, svc=None):
        self.events.append("read_inbox")
        return [list(self.grid[row]) for row in sorted(self.grid)]

    def write_worktree(self, spreadsheet_id, rows, *, previous_rows=0, svc=None):
        self.events.append("write_worktree")
        self.worktree_writes += 1
        self.worktree = [list(row) for row in rows]
        return len(rows)

    def worktree_column(self, name: str) -> list[str]:
        return [row[WORKTREE_HEADERS.index(name)] for row in self.worktree]

    def check(self, spreadsheet_id, *, caller_email):
        self.checks += 1
        self.checked_for.append(caller_email)
        return self.check_result

    def id_rows(self, spreadsheet_id, *, svc=None):
        self.events.append("id_rows")
        return dict(self.rows)

    def blank_action_ids(self, spreadsheet_id, *, svc=None):
        self.events.append("blank_action_ids")
        return list(self.blank_actions)

    def append(self, spreadsheet_id, rows, *, svc=None):
        if not rows:
            return []
        self.events.append("append")
        first = 2 + len(self.rows)
        numbers = []
        for offset, row in enumerate(rows):
            self.rows[row[ID]] = first + offset
            numbers.append(first + offset)
            self.appended.append(row)
            self._put(first + offset, row)
        return numbers

    def update(self, spreadsheet_id, row_values, *, svc=None):
        if row_values:
            self.events.append("update")
        self.updated.update(row_values)
        by_row = {row: mid for mid, row in self.rows.items()}
        for row, values in row_values.items():
            self._put(row, values)
            if values[ACTION] and by_row.get(row) in self.blank_actions:
                self.blank_actions.remove(by_row[row])
        return len(row_values)


def _reply(
    summary="Rathore Legal asks for a shortlist of two paralegals in Pune.",
    deadline="2026-09-18",
    category="action_required",
    action="Send Rathore Legal a shortlist of two paralegals by 18 Sep.",
):
    return json.dumps({"summary": summary, "action": action, "deadline": deadline, "category": category})


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
    "list_inbox_messages", "save_inbox_messages", "delete_inbox_messages", "get_users_by_ids",
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
    for name in ("profile", "history_since", "list_inbox", "list_inbox_pairs",
                 "list_sent_pairs", "stamp", "fetch"):
        monkeypatch.setattr(gmail_client, name, getattr(fake, name))
    return fake


@pytest.fixture()
def sheet(monkeypatch) -> FakeSheet:
    fake = FakeSheet()
    monkeypatch.setattr(sheet_writer, "service", lambda: "sheets-service")
    monkeypatch.setattr(sheet_writer, "service_account_email", lambda: "hub@project.iam.gserviceaccount.com")
    for name in ("check", "id_rows", "blank_action_ids", "append", "update",
                 "read_inbox", "write_worktree"):
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
                  "checked_for": EMAIL, "worktree": "ours"},
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
    assert [row[ID] for row in sheet.appended] == ["new1", "new2", "old1", "old2"]
    assert sheet.appended[0][CAT] == ASKS and sheet.appended[0][DEADLINE] == "2026-09-18"
    assert sheet.appended[0][ACTION] == "Send Rathore Legal a shortlist of two paralegals by 18 Sep."
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
    assert [row[ID] for row in sheet.appended] == ["m2", "m3"]


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
    assert row[CAT] == UNREAD and row[SUMMARY] == "" and row[ACTION] == "" and row[DEADLINE] == ""
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
    assert sheet.updated[9][CAT] == ASKS and sheet.updated[9][ID] == "odd"
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
    assert sheet.appended[0][CAT] == UNREAD and sheet.appended[1][CAT] == ASKS


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
    # 100s less the write reserve and the Worktree reserve = 60s of fetching.
    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=100.0)

    assert report.unreached is True and report.ok is True
    assert report.new_rows == 2 and mailbox.fetched == ["m0", "m1"]
    assert [row[ID] for row in sheet.appended] == ["m0", "m1"], "what was done is written"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "800", "an unfinished pass keeps the old checkpoint"
    assert connected.connections[UID]["backfill"]["state"] == "running"
    # The Worktree's reserve is kept back from the same budget, so a work
    # pass that ran itself out does not also cost the tab its rebuild.
    assert report.worktree_rows == 2 and sheet.worktree_writes == 1


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
    # 110s less both reserves = 70s: three fetches (the third ends at 90s).
    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=130.0)

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
    payload = pipeline.status_payload(UID, now=NOW)
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


def test_a_checked_sheet_stores_the_worktree_standing_set_up_reported(store, sheet):
    """Every answer set-up gives about the Worktree tab reaches the document.

    The fire's Worktree pass gates on ``sheet.worktree``; a standing that
    stays only in :class:`SheetCheck` means the tab is created and styled and
    then never filled, which is exactly what both live sheets did."""
    assert pipeline.set_sheet(UID, SID, email=EMAIL)["sheet"]["worktree"] == "ours"

    sheet.check_result = SheetCheck("ok", "Her inbox", "", "claimed")
    assert pipeline.recheck_sheet(UID, email=EMAIL)["sheet"]["worktree"] == "claimed"

    # A sheet that is not the caller's has no standing at all, and the stored
    # one must not survive as a stale "ours" the pass would write through.
    sheet.check_result = SheetCheck("not_yours", "")
    doc = pipeline.recheck_sheet(UID, email=EMAIL)
    assert doc["sheet"]["check"] == "not_yours" and doc["sheet"]["worktree"] == ""

    # Same for a sheet refused before Google is asked anything.
    from marketing_research_agent import config as mr_config

    doc = pipeline.set_sheet(UID, str(mr_config.SHEETS_SPREADSHEET_ID), email=EMAIL)
    assert doc["sheet"]["check"] == "mr_source" and doc["sheet"]["worktree"] == ""


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


def test_status_for_a_user_who_never_connected_is_enabled_with_nulls(store, sheet):
    payload = pipeline.status_payload("anyone", now=NOW)
    assert payload == {
        "enabled": True, "service_account_email": "hub@project.iam.gserviceaccount.com",
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
    payload = pipeline.status_payload(UID, now=NOW)
    assert payload["enabled"] is True
    assert payload["service_account_email"] == "hub@project.iam.gserviceaccount.com"
    assert payload["gmail"] == {"connected": True, "address": "her@firm.com", "connected_at": doc["gmail"]["connected_at"]}
    assert payload["sheet"]["id"] == SID and payload["sheet"]["check"] == "ok"
    assert payload["backfill"] == {"state": "running", "done": 0, "total": None}
    assert payload["next_poll_at"] == (NOW + timedelta(minutes=3)).isoformat()
    assert payload["rows_24h"] == 5 and payload["needs_review"] == 2
    assert "refresh_token_enc" not in json.dumps(payload)
    doc["last_poll"] = None
    assert pipeline.status_payload(UID, now=NOW)["next_poll_at"] == NOW.isoformat()


# --------------------------------------------------------------------------- #
# summarise — the model seam
# --------------------------------------------------------------------------- #

def test_a_valid_reply_is_a_verdict_and_nothing_of_the_email_is_logged(caplog):
    llm = FakeLLM()
    message = _message("m1", subject="Paralegal search", body="Need two by Friday, ping Priya.")
    import logging

    with caplog.at_level(logging.INFO, logger="agentos.inbox.summarise"):
        verdict = summarise.summarise(message, llm=llm)
    assert isinstance(verdict, Verdict) and verdict.category == "action_required"
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
        "timeout": summarise.OPENROUTER_TIMEOUT_SECONDS, "max_tokens": 400,
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
    assert [row[ID] for row in sheet.appended] == ["n1", "n2", "b1"], "no row was written twice"
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
    assert [row[ID] for row in sheet.appended] == ["n1", "n2"]
    assert connected.connections[UID]["checkpoint"] == before["checkpoint"], "the checkpoint did not move"
    assert connected.connections[UID]["lease_until"] is None, "the lease is cleared even for an unmapped error"

    monkeypatch.setattr(firestore_repo, "save_inbox_messages", connected.save_inbox_messages)
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 0
    assert [row[ID] for row in sheet.appended] == ["n1", "n2"], "duplicate-over-drop must not become a duplicate row"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "950"


def test_reconnecting_after_a_disconnect_appends_nothing_already_on_the_sheet(
    store, mailbox, sheet, model, grant, consent
):
    store.connections[UID] = _connected_doc()
    mailbox.messages = {m: _message(m) for m in ("a", "b", "c")}
    mailbox.listing = ["c", "b", "a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert [row[ID] for row in sheet.appended] == ["c", "b", "a"]

    pipeline.disconnect(UID)
    assert store.connections[UID]["sheet"]["id"] == SID and store.messages == {}
    doc = pipeline.connect(UID, code="c", state="s", email=EMAIL)
    assert doc["backfill"]["state"] == "running", "the kept sheet is still ok, so the backfill restarts"
    fetched_before = list(mailbox.fetched)

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert report.ok and report.new_rows == 0
    assert [row[ID] for row in sheet.appended] == ["c", "b", "a"], "a reconnect must not re-append her rows"
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

    # 105s less the write and Worktree reserves = 40s of work: two fetches.
    report = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=105.0)

    backfill = connected.connections[UID]["backfill"]
    assert report.ok and report.unreached is True
    assert report.backfill_state == "running" and report.new_rows == 2
    assert (backfill["cursor"], backfill["done"]) == (None, 0), "a page cut short does not advance"
    assert [row[ID] for row in sheet.appended] == ["o0", "o1"]


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


def test_purge_without_access_disconnects_exactly_the_named_users(connected, monkeypatch):
    import time as _time

    connected.connections["user-gone"] = _connected_doc()
    connected.messages["user-gone__x"] = {"user_id": "user-gone", "message_id": "x"}
    revoked: list[str] = []
    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: revoked.append(token) or True)
    assert pipeline.purge_without_access(["user-gone"], deadline=_time.monotonic() + 60) == 1
    assert "refresh_token_enc" not in connected.connections["user-gone"]
    assert "gmail" not in connected.connections["user-gone"] and connected.messages == {}
    assert connected.connections[UID]["refresh_token_enc"] == "sealed"
    assert revoked == ["plain-sealed"]


def test_purge_without_access_stops_at_the_deadline_and_leaves_the_rest_connected(connected, monkeypatch):
    import time as _time

    monkeypatch.setattr(gmail_oauth, "revoke", lambda token: pytest.fail("past the deadline"))
    assert pipeline.purge_without_access([UID], deadline=_time.monotonic() - 1) == 0
    assert connected.connections[UID]["refresh_token_enc"] == "sealed"


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


# --------------------------------------------------------------------------- #
# Pinned 2026-09-19: the Action column ships to a sheet that already has rows.
# The first live sheet had 83 rows in the first release's layout; the first
# fire after the deploy must migrate it in place, append nothing it already
# holds, and fill Action on the old rows through the ordinary summarise path.
# --------------------------------------------------------------------------- #

def _real_sheet(monkeypatch, grid):
    """The REAL ``sheet_writer`` over the grid fake from the writer tests:
    set-up, migration, the id read and the writes all run for real."""
    from inbox_triage_agent.tests.test_sheet_writer import FakeDrive
    from marketing_research_agent import sources_registry

    monkeypatch.setattr(sheet_writer, "service", lambda: grid)
    monkeypatch.setattr(sheet_writer, "drive_service", lambda: FakeDrive({
        "owners": [{"emailAddress": EMAIL}], "permissions": [],
    }))
    monkeypatch.setattr(sources_registry, "find_source", lambda sid: None)


def test_the_first_fire_after_the_deploy_migrates_the_live_sheet_and_re_offered_mail_appends_nothing(
    store, mailbox, model, grant, monkeypatch
):
    from inbox_triage_agent.sheet_layout import HEADERS
    from inbox_triage_agent.tests.test_sheet_writer import legacy_sheet

    grid = legacy_sheet(83)
    before = [list(r) for r in grid.grid["Inbox"]]
    _real_sheet(monkeypatch, grid)
    # A fresh ``ok`` check: the hourly re-check would NOT run on its own —
    # the id read's header check is what triggers the migration.
    store.connections[UID] = _connected_doc()
    store.connections[UID]["backfill"]["state"] = "done"
    ids = [f"m{i}" for i in range(83)]
    mailbox.messages = {m: _message(m) for m in ids}
    mailbox.history_added = list(ids)  # every message already on the sheet, offered again

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok, report.error
    assert report.new_rows == 0 and "values.append" not in grid.names(), "dedupe held across the migration"
    assert len(grid.grid["Inbox"]) == 84
    assert grid.row("Inbox", 0, 11) == list(HEADERS)
    for r in range(1, 84):
        assert grid.row("Inbox", r, 11)[9:] == (before[r] + ["", ""])[8:10], "her Status and Notes, intact"
    # The one-time re-triage: 50 this fire, through the same model path.
    assert report.retriaged_rows == 50 and report.retried_rows == 0
    filled = [r for r in range(1, 84) if grid.cell("Inbox", r, ACTION)]
    assert len(filled) == 50
    assert grid.cell("Inbox", filled[0], ACTION) == "Send Rathore Legal a shortlist of two paralegals by 18 Sep."
    assert grid.cell("Inbox", filled[0], CAT) == ASKS
    assert connected_retriage(store)["state"] == "running"

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 0 and "values.append" not in grid.names()
    assert report.retriaged_rows == 33
    assert all(grid.cell("Inbox", r, ACTION) for r in range(1, 84))
    assert connected_retriage(store) == {"state": "done", "skipped": []}
    assert model.invocations == 83, "one call per old row, no re-asks"

    grid.calls.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=10))
    reads = [kw["ranges"] for name, kw in grid.calls if name == "values.batchGet"]
    assert reads == [["Inbox!A1:Z1", "Inbox!I:I"]], "once done, the Action column is never read again"


def connected_retriage(store) -> dict:
    return store.connections[UID]["retriage"]


def test_the_retriage_leaves_gone_and_unreadable_rows_as_they_were_and_never_asks_again(
    connected, mailbox, sheet, model
):
    connected.connections[UID]["backfill"]["state"] = "done"
    sheet.rows = {"a": 2, "gone": 3, "odd": 4}
    sheet.blank_actions = ["a", "gone", "odd"]
    mailbox.messages = {"a": _message("a"), "odd": _message("odd", subject="GARBLE")}

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.retriaged_rows == 1
    assert set(sheet.updated) == {2}, "a row the model could not read keeps what it had"
    assert sheet.updated[2][ACTION] and sheet.updated[2][CAT] == ASKS
    assert connected.connections[UID]["retriage"] == {"state": "done", "skipped": ["gone", "odd"]}
    asked = model.invocations
    sheet.events.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert "blank_action_ids" not in sheet.events and model.invocations == asked


def test_a_sheet_with_no_old_rows_finishes_the_retriage_in_one_read(connected, mailbox, sheet, model):
    connected.connections[UID]["backfill"]["state"] = "done"
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.retriaged_rows == 0 and model.invocations == 0
    assert connected.connections[UID]["retriage"] == {"state": "done", "skipped": []}
    assert sheet.events.count("blank_action_ids") == 1


def test_setting_a_sheet_starts_a_fresh_retriage(connected, sheet):
    connected.connections[UID]["retriage"] = {"state": "done", "skipped": ["x"]}
    doc = pipeline.set_sheet(UID, SID, email=EMAIL)
    assert doc["retriage"] == {"state": "running", "skipped": []}


def test_a_header_set_up_cannot_fix_fails_the_fire_loudly_with_nothing_written(
    connected, mailbox, sheet, monkeypatch
):
    def mismatched(sid, *, svc=None):
        raise sheet_writer.LayoutMismatch(
            "The Inbox tab's header row does not match the agent's columns; nothing was written."
        )
    monkeypatch.setattr(sheet_writer, "id_rows", mismatched)
    mailbox.messages = {"n1": _message("n1")}
    mailbox.history_added = ["n1"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert not report.ok and "header row does not match" in report.error
    assert sheet.checks == 1, "set-up was re-run once before giving up"
    assert sheet.appended == [] and sheet.updated == {} and mailbox.fetched == []
    doc = connected.connections[UID]
    assert doc["last_poll"]["ok"] is False and doc["checkpoint"]["history_id"] == "800"


# --------------------------------------------------------------------------- #
# The Worktree — one process a thread, written only when it changed
# --------------------------------------------------------------------------- #

def test_the_worktree_groups_the_rows_into_one_process_a_thread(connected, mailbox, sheet):
    mailbox.messages = {
        "a": _message("a", subject="Paralegal search", thread="t1"),
        "b": _message("b", subject="Re: Paralegal search", thread="t1"),
        "c": _message("c", subject="Invoice INV-2231", thread="t2"),
    }
    mailbox.history_added = ["a", "b", "c"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 3 and report.worktree_rows == 2
    assert sheet.worktree_writes == 1
    assert sheet.worktree_column("Process") == ["Invoice INV-2231", "Paralegal search"]
    assert sheet.worktree_column("Mails") == ["1", "2"]
    assert sheet.worktree_column("Status") == [worktree.STATUS_WAITING_ON_US] * 2
    assert sheet.worktree_column("Due") == ["2026-09-18", "2026-09-18"]
    assert sheet.worktree_column("Waiting since") == ["1 day", "1 day"]
    assert sheet.worktree_column("Latest") == [
        message_link("c"), message_link("b"),
    ], "the newest message of each thread"
    state = connected.connections[UID]["worktree"]
    assert (state["state"], state["rows"], state["total"], state["error"]) == ("ours", 2, 2, None)
    assert state["hash"]


def test_the_worktree_is_rebuilt_when_mail_lands_or_the_day_turns_and_never_otherwise(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.worktree_writes == 1

    # Five minutes on, no mail: not even a read. This is the common fire.
    sheet.events.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert "read_inbox" not in sheet.events and sheet.worktree_writes == 1

    # Two hours on it is rebuilt — but nothing moved, so nothing is written.
    sheet.events.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))
    assert "read_inbox" in sheet.events and sheet.worktree_writes == 1
    assert "write_worktree" not in sheet.events


def test_a_thread_she_has_marked_done_leaves_the_worktree(connected, mailbox, sheet):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.worktree_column("Process") == ["Paralegal search"]

    sheet.set_status("a", "Done")
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))

    assert sheet.worktree == [] and sheet.worktree_writes == 2
    assert connected.connections[UID]["worktree"]["rows"] == 0


def test_a_tracked_message_carries_what_grouping_needs_and_no_mailbox_address(
    connected, mailbox, sheet
):
    mailbox.messages = {
        "a": _message("a", thread="t9"),
        "b": _message("b", thread="t9", sender=f"Her Name <{EMAIL}>"),
    }
    mailbox.history_added = ["a", "b"]

    pipeline.fire(UID, email=EMAIL, now=NOW)

    theirs = connected.messages[f"{UID}__a"]
    assert theirs["thread_id"] == "t9" and theirs["from_me"] is False
    assert theirs["received_at"].startswith("2026-09-17T10:05")
    assert theirs["subject"] == "Paralegal search"
    assert theirs["category"] == "action_required" and theirs["deadline"] == "2026-09-18"
    assert theirs["action"].startswith("Send Rathore Legal")
    assert theirs["status"] == "ok", "the retry bookkeeping is still there"
    assert connected.messages[f"{UID}__b"]["from_me"] is True, "her own address means we sent it"
    assert not any("@" in str(v) for v in theirs.values()), "no mailbox address is stored"


def test_a_message_the_model_could_not_read_keeps_its_facts_when_the_mail_is_gone(
    connected, mailbox, sheet
):
    """A placeholder must not blank what a real message already wrote."""
    mailbox.messages = {"a": _message("a", subject="GARBLE", thread="t1")}
    mailbox.history_added = ["a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    connected.messages[f"{UID}__a"]["subject"] = "Paralegal search"  # an earlier good pass

    del mailbox.messages["a"]  # she deleted it before the retry
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    tracked = connected.messages[f"{UID}__a"]
    assert tracked["retry_due"] is False
    assert tracked["subject"] == "Paralegal search", "the placeholder wrote no facts at all"


def test_a_worktree_tab_of_hers_is_never_written_to_and_is_recorded(connected, mailbox, sheet):
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "claimed")
    connected.connections[UID]["sheet"]["worktree"] = "claimed"
    mailbox.messages = {"a": _message("a")}
    mailbox.history_added = ["a"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1, "her Inbox rows still flow"
    assert sheet.worktree_writes == 0 and "read_inbox" not in sheet.events
    assert connected.connections[UID]["worktree"]["state"] == "claimed"


def test_the_worktree_waits_until_set_up_has_looked_at_the_tab(connected, mailbox, sheet):
    """A connection stored before the Worktree existed: nothing is assumed
    until the hourly check has reported on the tab."""
    connected.connections[UID]["sheet"].pop("worktree")
    mailbox.messages = {"a": _message("a")}
    mailbox.history_added = ["a"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1
    assert sheet.worktree_writes == 0 and "read_inbox" not in sheet.events
    assert not connected.connections[UID]["worktree"]


def test_a_sheet_set_up_end_to_end_builds_the_worktree_on_its_first_fire_with_mail(
    store, sheet, mailbox, model, grant, consent
):
    """The whole path with nothing seeded by hand: consent, set the sheet,
    then one fire with new mail. The tab must have rows at the end of it.

    Every other Worktree test starts from ``_connected_doc``, which writes
    ``sheet.worktree`` itself — so the connection made the way a real user
    makes one is the only thing that pins the standing actually being
    stored, and the pass actually running rather than declining."""
    pipeline.connect(UID, code="c", state="s", email=EMAIL)
    pipeline.set_sheet(UID, SID, email=EMAIL)
    mailbox.messages = {
        "a": _message("a", subject="Paralegal search", thread="t1"),
        "b": _message("b", subject="Invoice INV-2231", thread="t2"),
    }
    mailbox.history_added = ["a", "b"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 2
    assert report.worktree_rows == 2 and sheet.worktree_writes == 1
    assert sorted(sheet.worktree_column("Process")) == ["Invoice INV-2231", "Paralegal search"]
    doc = store.connections[UID]
    assert doc["worktree"]["state"] == "ours" and doc["worktree"]["rows"] == 2
    assert doc["thread_backfill"]["state"] == "done"
    assert doc["sent_backfill"]["state"] == "done"


def test_a_live_sheet_still_mid_thread_walk_gets_its_worktree_on_that_same_fire(
    connected, mailbox, sheet, monkeypatch
):
    """The shape of the two live sheets: rows already written and tracked
    before thread ids were stored. The walk is bounded per fire, so it is
    still ``running`` when the pass reaches the tab — and the tab must be
    built from the threads that ARE resolved, with the rest counted as
    ``untagged``, rather than withheld until the walk ends."""
    mailbox.messages = {
        "a": _message("a", subject="Paralegal search", thread="t1"),
        "b": _message("b", subject="Invoice INV-2231", thread="t2"),
    }
    mailbox.history_added = ["a", "b"]
    pipeline.fire(UID, email=EMAIL, now=NOW)  # the rows land on the sheet
    for key in (f"{UID}__a", f"{UID}__b"):  # ...tracked the way phase 0 tracked
        connected.messages[key].pop("thread_id")
    for key in ("thread_backfill", "sent_backfill", "worktree"):
        connected.connections[UID].pop(key, None)
    mailbox.listing = ["a", "b"]
    mailbox.threads = {"a": "t1", "b": "t2"}
    monkeypatch.setattr(gmail_client, "LIST_THREAD_PAGE_MAX", 1)
    monkeypatch.setattr(pipeline, "THREAD_BACKFILL_PAGES_PER_FIRE", 1)
    sheet.worktree_writes = 0

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))

    assert connected.connections[UID]["thread_backfill"]["state"] == "running"
    assert report.threads_tagged == 1
    assert sheet.worktree_writes == 1 and report.worktree_rows == 1
    assert sheet.worktree_column("Process") == ["Paralegal search"]
    assert connected.connections[UID]["worktree"]["untagged"] == 1


def test_a_refused_worktree_write_does_not_fail_the_fire_and_is_retried(
    connected, mailbox, sheet, monkeypatch
):
    def refused(*_args, **_kwargs):
        raise SheetsUnavailable("Sheets worktree write was refused: HTTP 400")

    monkeypatch.setattr(sheet_writer, "write_worktree", refused)
    mailbox.messages = {"a": _message("a")}
    mailbox.history_added = ["a"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok is True and report.new_rows == 1
    assert [row[ID] for row in sheet.appended] == ["a"], "the Inbox row is written regardless"
    doc = connected.connections[UID]
    assert doc["last_poll"]["ok"] is True and doc["last_poll"]["error"] is None
    assert "HTTP 400" in doc["worktree"]["error"]


# --------------------------------------------------------------------------- #
# The one-time thread-id backfill
# --------------------------------------------------------------------------- #

def _tracked_without_a_thread(store, count: int, *, prefix: str = "old") -> None:
    """Rows tracked before thread ids were stored — the two live sheets."""
    for index in range(count):
        store.messages[f"{UID}__{prefix}{index}"] = {
            "user_id": UID, "message_id": f"{prefix}{index}", "status": "ok",
            "attempts": 1, "sheet_row": 2 + index, "retry_due": False,
        }


def test_the_thread_id_backfill_is_bounded_resumable_and_runs_exactly_once(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"  # mail backfill already finished
    _tracked_without_a_thread(connected, 12)
    mailbox.listing = [f"old{i}" for i in range(12)]
    mailbox.threads = {f"old{i}": f"t{i // 4}" for i in range(12)}
    monkeypatch.setattr(gmail_client, "LIST_THREAD_PAGE_MAX", 5)
    monkeypatch.setattr(pipeline, "THREAD_BACKFILL_PAGES_PER_FIRE", 1)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert first.threads_tagged == 5
    state = connected.connections[UID]["thread_backfill"]
    assert (state["state"], state["cursor"], state["tagged"]) == ("running", "5", 5)
    assert connected.messages[f"{UID}__old0"]["thread_id"] == "t0"
    assert "thread_id" not in connected.messages[f"{UID}__old11"], "not reached yet"

    second = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert second.threads_tagged == 5
    assert connected.connections[UID]["thread_backfill"]["cursor"] == "10"

    third = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=10))
    assert third.threads_tagged == 2
    state = connected.connections[UID]["thread_backfill"]
    assert (state["state"], state["tagged"], state["unresolved"]) == ("done", 12, 0)
    assert {d["thread_id"] for d in connected.messages.values()} == {"t0", "t1", "t2"}

    mailbox.events.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=15))
    assert "list_pairs" not in mailbox.events, "done means done; it never runs again"


def test_a_message_the_listing_never_mentions_is_counted_not_guessed(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    _tracked_without_a_thread(connected, 3)
    mailbox.listing = ["old0", "old1"]  # old2 was archived, or is older than the window
    mailbox.threads = {"old0": "t0", "old1": "t0"}

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    state = connected.connections[UID]["thread_backfill"]
    assert (state["state"], state["unresolved"]) == ("done", 1)
    assert report.threads_tagged == 2
    assert "thread_id" not in connected.messages[f"{UID}__old2"]


def test_the_backfill_walks_the_same_window_the_mail_backfill_used(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    _tracked_without_a_thread(connected, 1)
    mailbox.listing = ["old0"]
    mailbox.threads = {"old0": "t0"}
    seen: list[int] = []
    real_pairs = mailbox.list_inbox_pairs

    def watched(svc, *, after_epoch, page_token=None, max_results=100):
        seen.append(after_epoch)
        return real_pairs(svc, after_epoch=after_epoch, page_token=page_token,
                          max_results=max_results)

    monkeypatch.setattr(gmail_client, "list_inbox_pairs", watched)
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert seen == [connected.connections[UID]["backfill"]["since_epoch"]]


def test_a_fresh_connection_has_nothing_to_backfill_and_says_so_at_once(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.events.clear()

    pipeline.fire(UID, email=EMAIL, now=NOW)

    assert "list_pairs" not in mailbox.events
    state = connected.connections[UID]["thread_backfill"]
    assert (state["state"], state["tagged"], state["unresolved"]) == ("done", 0, 0)


# --------------------------------------------------------------------------- #
# Reading what she SENT — markers only, and what they change
# --------------------------------------------------------------------------- #

def test_mail_she_sent_flips_the_thread_and_is_stored_as_a_marker_and_nothing_more(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("s1", "t1")]
    mailbox.sent_times = {"s1": datetime(2026, 9, 18, 9, 30, tzinfo=TEAM_TIMEZONE)}

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert (report.sent_marked, report.sent_stamped) == (1, 1)
    assert sheet.worktree_column("Status") == [worktree.STATUS_WAITING_ON_THEM]
    assert sheet.worktree_column("Mails") == ["2"]
    assert sheet.worktree_column("Latest") == [message_link("a")]
    marker = connected.messages[f"{UID}__s1"]
    assert marker == {
        "user_id": UID, "message_id": "s1", "kind": "sent", "thread_id": "t1",
        "from_me": True, "received_at": "2026-09-18T09:30:00+05:30",
    }, "id, thread, side and time — no subject, no recipient, no body, no row"


def test_a_reply_she_sent_days_ago_that_nobody_answered_is_a_chase(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("s1", "t1")]
    # She answered the day after it arrived, and has heard nothing since.
    mailbox.sent_times = {"s1": datetime(2026, 9, 18, 9, 30, tzinfo=TEAM_TIMEZONE)}

    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(days=5))

    assert sheet.worktree_column("Status") == [worktree.STATUS_CHASING]
    assert sheet.worktree_column("Waiting since") == ["5 days"]


def test_sent_mail_in_a_thread_we_never_ingested_is_dropped_on_the_floor(
    connected, mailbox, sheet
):
    """It has no subject to name it and no triage to describe it, so keeping
    it would buy a blank row and a Firestore document for nothing."""
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("s1", "t1"), ("s2", "some-other-thread")]
    mailbox.sent_times = {
        "s1": datetime(2026, 9, 18, 9, 30, tzinfo=TEAM_TIMEZONE),
        "s2": datetime(2026, 9, 18, 9, 31, tzinfo=TEAM_TIMEZONE),
    }

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.sent_marked == 1
    assert f"{UID}__s2" not in connected.messages
    assert "s2" not in mailbox.stamped, "and it is never even asked about"


def test_sent_ingestion_waits_for_the_thread_ids_it_needs_to_join_on(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    _tracked_without_a_thread(connected, 8)
    mailbox.listing = [f"old{i}" for i in range(8)]
    mailbox.threads = {f"old{i}": "t0" for i in range(8)}
    mailbox.sent = [("s1", "t0")]
    mailbox.sent_times = {"s1": datetime(2026, 9, 18, 9, 30, tzinfo=TEAM_TIMEZONE)}
    monkeypatch.setattr(gmail_client, "LIST_THREAD_PAGE_MAX", 4)
    monkeypatch.setattr(pipeline, "THREAD_BACKFILL_PAGES_PER_FIRE", 1)

    mailbox.events.clear()
    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert connected.connections[UID]["thread_backfill"]["state"] == "running"
    assert "list_sent" not in mailbox.events and first.sent_marked == 0
    assert connected.connections[UID]["sent_backfill"]["state"] == "not_started"

    # The fire that finishes the thread ids is the one that may read SENT.
    second = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert connected.connections[UID]["thread_backfill"]["state"] == "done"
    assert second.sent_marked == 1
    assert connected.connections[UID]["sent_backfill"]["state"] == "done"


def test_the_sent_walk_is_bounded_resumable_and_then_only_watches_the_last_day(
    connected, mailbox, sheet, monkeypatch
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [(f"s{i}", "t1") for i in range(10)]
    mailbox.sent_times = {
        f"s{i}": datetime(2026, 9, 18, 9, i, tzinfo=TEAM_TIMEZONE) for i in range(10)
    }
    monkeypatch.setattr(pipeline, "SENT_BACKFILL_PAGES_PER_FIRE", 1)

    def one_page(svc, *, after_epoch, page_token=None, max_results=500):
        return mailbox.list_sent_pairs(svc, after_epoch=after_epoch,
                                       page_token=page_token, max_results=4)
    monkeypatch.setattr(gmail_client, "list_sent_pairs", one_page)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    state = connected.connections[UID]["sent_backfill"]
    assert (first.sent_marked, state["state"], state["cursor"]) == (4, "running", "4")

    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert connected.connections[UID]["sent_backfill"]["cursor"] == "8"

    third = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=10))
    state = connected.connections[UID]["sent_backfill"]
    assert (state["state"], state["markers"], state["stamped"]) == ("done", 10, 10)
    assert third.sent_marked == 2

    # Done: every later build watches the last day, for one call.
    mailbox.events.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))
    assert mailbox.events.count("list_sent") == 1


def test_the_last_day_watch_picks_up_a_reply_she_sends_after_the_walk_finished(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.worktree_column("Status") == [worktree.STATUS_WAITING_ON_US]
    assert connected.connections[UID]["sent_backfill"]["state"] == "done"

    mailbox.sent = [("s1", "t1")]  # she answered it
    mailbox.sent_times = {"s1": datetime(2026, 9, 18, 12, 0, tzinfo=TEAM_TIMEZONE)}
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))

    assert sheet.worktree_column("Status") == [worktree.STATUS_WAITING_ON_THEM]


def test_the_stamp_reads_are_capped_per_fire_and_resume(
    connected, mailbox, sheet, monkeypatch
):
    """The one call-per-message step in either backfill, so it is the one
    that is bounded tightest."""
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [(f"s{i}", "t1") for i in range(5)]
    mailbox.sent_times = {
        f"s{i}": datetime(2026, 9, 18, 9, i, tzinfo=TEAM_TIMEZONE) for i in range(5)
    }
    monkeypatch.setattr(pipeline, "SENT_STAMPS_PER_FIRE", 2)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert first.sent_marked == 5 and first.sent_stamped == 2
    assert connected.connections[UID]["sent_backfill"]["unstamped"] == 3
    assert len(mailbox.stamped) == 2

    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert connected.connections[UID]["sent_backfill"]["unstamped"] == 1
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=10))
    state = connected.connections[UID]["sent_backfill"]
    assert (state["unstamped"], state["stamped"]) == (0, 5)
    assert sorted(mailbox.stamped) == [f"s{i}" for i in range(5)]


def test_a_sent_message_she_has_since_deleted_is_remembered_and_never_asked_again(
    connected, mailbox, sheet
):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("s1", "t1")]
    mailbox.sent_times = {}  # the stamp read answers 404

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.sent_marked == 1 and report.sent_stamped == 0
    assert connected.messages[f"{UID}__s1"]["gone"] is True
    assert connected.connections[UID]["sent_backfill"]["unstamped"] == 0
    assert sheet.worktree_column("Status") == [worktree.STATUS_WAITING_ON_US], \
        "a marker with no time changes nothing"

    mailbox.stamped.clear()
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=2))
    assert mailbox.stamped == [], "gone means gone"


def test_a_mail_she_sent_to_herself_keeps_its_row_and_is_not_marked_twice(
    connected, mailbox, sheet
):
    """Gmail files a note-to-self under both labels. The row wins: it is the
    one with a subject, a category and an Action."""
    mailbox.messages = {"a": _message("a", thread="t1", sender=f"Her Name <{EMAIL}>")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("a", "t1")]
    mailbox.sent_times = {"a": datetime(2026, 9, 17, 10, 5, tzinfo=TEAM_TIMEZONE)}

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.sent_marked == 0
    tracked = connected.messages[f"{UID}__a"]
    assert tracked["subject"] == "Paralegal search" and tracked["from_me"] is True
    assert "kind" not in tracked, "the row was never turned into a marker"
    assert sheet.worktree_column("Mails") == ["1"]


def test_a_disconnect_clears_the_markers_with_everything_else(connected, mailbox, sheet):
    mailbox.messages = {"a": _message("a", thread="t1")}
    mailbox.history_added = ["a"]
    mailbox.sent = [("s1", "t1")]
    mailbox.sent_times = {"s1": datetime(2026, 9, 18, 9, 30, tzinfo=TEAM_TIMEZONE)}
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert f"{UID}__s1" in connected.messages

    pipeline.disconnect(UID)

    assert connected.messages == {}
