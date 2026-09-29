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
    COL_SUMMARY, HEADERS, WORKTREE_HEADERS, landed, message_link, place, pushed,
)
from inbox_triage_agent.sheet_writer import SheetCheck, SheetsUnavailable, Written
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

    def take_inbox_lease(self, user_id, *, now, until, owner=""):
        """The real primitive's contract, with the real rule: one atomic
        read-and-take, the document back on success, ``None`` when held."""
        doc = self.connections.get(user_id)
        self.lease_attempts.append(until.isoformat())
        if doc is None or not firestore_repo.inbox_lease_free(doc, now):
            return None
        doc["lease_until"] = until.isoformat()
        doc["lease_owner"] = owner
        self.events.append("lease")
        return copy.deepcopy(doc)

    def renew_inbox_lease(self, user_id, *, owner, held, until):
        """Extended only by the fire whose token is on it — the real rule."""
        doc = self.connections.get(user_id)
        if doc is None or not firestore_repo.inbox_lease_held_by(doc, owner, held):
            self.events.append("renew refused")
            return False
        doc["lease_until"] = until.isoformat()
        self.events.append("renew")
        return True

    def release_inbox_lease(self, user_id, *, owner, held):
        doc = self.connections.get(user_id)
        if doc is None or not firestore_repo.inbox_lease_held_by(doc, owner, held):
            self.events.append("release refused")
            return False
        doc["lease_until"], doc["lease_owner"] = None, None
        self.events.append("release")
        return True

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
    at: datetime = datetime(2026, 9, 17, 10, 5, tzinfo=TEAM_TIMEZONE),
) -> Message:
    return Message(
        id=message_id, thread_id=thread or ("t-" + message_id),
        received_at=at,
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
    """What ``sheet_writer`` would do: an id map, the one write a fire makes
    (rows rewritten where their ids are, new rows placed newest first by the
    layout's own ``place``), the Action column the one-time re-triage reads,
    and a real grid — her Status column included — so the Worktree is built
    from cells, not from a stub."""

    def __init__(self):
        self.rows: dict[str, int] = {}
        #: ids whose row has a category but an empty Action (legacy rows)
        self.blank_actions: list[str] = []
        #: the new rows handed to the writer, in the order they were handed
        self.inserted: list[list[str]] = []
        #: row (as it stood when written) -> the cells it was rewritten with
        self.updated: dict[int, list[str]] = {}
        self.checks = 0
        self.check_result = SheetCheck("ok", "Her inbox", "", "ours", "already")
        self.checked_for: list[str] = []
        #: whether each check was allowed to run the one-time reorder
        self.reorders: list[bool] = []
        self.check_holds: list = []
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

    def check(self, spreadsheet_id, *, caller_email, reorder=False, hold=None):
        self.checks += 1
        self.checked_for.append(caller_email)
        self.reorders.append(reorder)
        #: what a poll hands the writer so the sort can prove the lease
        self.check_holds.append(hold)
        return self.check_result

    def top_to_bottom(self) -> list[str]:
        """The message ids in sheet order, first data row first."""
        return [mid for mid, _row in sorted(self.rows.items(), key=lambda item: item[1])]

    def id_rows(self, spreadsheet_id, *, svc=None):
        self.events.append("id_rows")
        return dict(self.rows)

    def blank_action_ids(self, spreadsheet_id, *, svc=None):
        self.events.append("blank_action_ids")
        return list(self.blank_actions)

    def write_rows(self, spreadsheet_id, new_rows, updates=None, *, svc=None, hold=None):
        updates = dict(updates or {})
        if not new_rows and not updates:
            return Written()
        if hold is not None:
            hold.prove()  # as the real writer does, before it reads a position
        self.events.append("write_rows")
        for message_id, values in updates.items():
            row = self.rows.get(message_id)
            if row is None:
                continue  # she deleted it; reported as missing below
            self._put(row, values)
            self.updated[row] = values
            if values[ACTION] and message_id in self.blank_actions:
                self.blank_actions.remove(message_id)
        last = max([1, *self.rows.values(), *self.grid])
        dates = [""] + [(self.grid.get(row) or [""])[0] for row in range(2, last + 1)]
        blocks = place(dates, [row[0] for row in new_rows], end=last)
        self.rows = {mid: pushed(row - 1, blocks) + 1 for mid, row in self.rows.items()}
        self.grid = {pushed(row - 1, blocks) + 1: cells for row, cells in self.grid.items()}
        numbers = landed(blocks, len(new_rows))
        for values, row in zip(new_rows, numbers):
            self.rows[values[ID]] = row
            self.inserted.append(values)
            self._put(row, values)
        return Written(
            new_rows=numbers,
            updated={mid: self.rows[mid] for mid in updates if mid in self.rows},
            missing=[mid for mid in updates if mid not in self.rows],
        )


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
    "take_inbox_lease", "renew_inbox_lease", "release_inbox_lease",
    "list_connected_inbox_user_ids",
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
    for name in ("check", "id_rows", "blank_action_ids", "write_rows",
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
                  "checked_for": EMAIL, "worktree": "ours", "ordering": "already"},
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
    assert mailbox.fetched == [] and sheet.inserted == []


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

def test_new_mail_is_worked_before_the_backfill_and_the_checkpoint_after_the_write(
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
    assert events.index("write_rows") < events.index("messages")
    assert events.index("write_rows") < [i for i, e in enumerate(events) if e.startswith("save:") and "checkpoint" in e][0]
    assert events.index("id_rows") < events.index("history")
    doc = connected.connections[UID]
    assert doc["checkpoint"]["history_id"] == "950"
    assert doc["backfill"] == {**doc["backfill"], "state": "done", "cursor": None, "done": 4, "total": 4}
    assert doc["last_poll"] == {"at": NOW.isoformat(), "ok": True, "messages_read": 4, "error": None}
    assert doc["recent_fires"] == [{"at": NOW.isoformat(), "rows": 4}]
    assert doc["lease_until"] is None
    assert [row[ID] for row in sheet.inserted] == ["new1", "new2", "old1", "old2"]
    assert sheet.inserted[0][CAT] == ASKS and sheet.inserted[0][DEADLINE] == "2026-09-18"
    assert sheet.inserted[0][ACTION] == "Send Rathore Legal a shortlist of two paralegals by 18 Sep."
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
    assert [row[ID] for row in sheet.inserted] == ["m2", "m3"]


def test_a_message_deleted_between_listing_and_fetch_is_simply_not_a_row(connected, mailbox, sheet):
    mailbox.history_added = ["gone"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 0 and sheet.inserted == []


# --------------------------------------------------------------------------- #
# needs_review and retries
# --------------------------------------------------------------------------- #

def test_an_unreadable_message_gets_its_row_and_at_most_three_later_tries(connected, mailbox, sheet, model):
    mailbox.messages = {"odd": _message("odd", subject="GARBLE")}
    mailbox.history_added = ["odd"]

    first = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert first.new_rows == 1 and first.needs_review_added == 1
    row = sheet.inserted[0]
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

def test_an_expired_checkpoint_re_lists_under_a_fresh_checkpoint(connected, mailbox, sheet):
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
    assert mailbox.fetched == [] and sheet.inserted == []
    assert pipeline.fire(UID, email=EMAIL, now=NOW).skipped == "gmail not connected"


def test_a_model_that_is_down_fails_the_fire_loudly_with_no_rows(connected, mailbox, sheet, model):
    model.always_fail = True
    mailbox.messages = {m: _message(m) for m in ("a", "b", "c", "d")}
    mailbox.history_added = ["a", "b", "c", "d"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "abandoned" in report.error
    assert sheet.inserted == [] and connected.messages == {}
    assert mailbox.fetched == ["a", "b", "c"], "three failures in a row is the trip"
    doc = connected.connections[UID]
    assert doc["last_poll"]["ok"] is False and doc["checkpoint"]["history_id"] == "800"
    assert doc["lease_until"] is None


def test_one_flaky_call_is_a_needs_review_row_not_a_failed_fire(connected, mailbox, sheet, model):
    mailbox.messages = {"a": _message("a", subject="BOOM"), "b": _message("b")}
    mailbox.history_added = ["a", "b"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok and report.new_rows == 2 and report.needs_review_added == 1
    assert sheet.inserted[0][CAT] == UNREAD and sheet.inserted[1][CAT] == ASKS


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
    assert [row[ID] for row in sheet.inserted] == ["m0", "m1"], "what was done is written"
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
        seen.append(sorted(patch))
        return real_save(user_id, patch, clear=clear)
    monkeypatch.setattr(firestore_repo, "save_inbox_connection", spying_save)
    monkeypatch.setattr(sheet_writer, "id_rows", lambda sid, svc=None: (_ for _ in ()).throw(
        sheet_writer.SheetsUnavailable("Sheets id column read failed after 3 attempts")))
    report = pipeline.fire(UID, email=EMAIL, now=NOW)
    assert report.ok is False and "Sheets" in report.error
    assert connected.lease_attempts == [(NOW + timedelta(seconds=pipeline.LEASE_SECONDS)).isoformat()]
    assert connected.events[0] == "lease" and connected.events[-1] == "release"
    doc = connected.connections[UID]
    assert doc["lease_until"] is None and doc["lease_owner"] is None
    assert not any("lease_until" in patch for patch in seen), \
        "the lease is cleared by its owner's release, never by a blind save"
    assert doc["last_poll"]["ok"] is False


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
        "sheet": {"id": None, "url": None, "title": None, "check": None, "checked_at": None,
                  "ordering": None, "ordering_note": None},
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
    real_id_rows, real_write = sheet.id_rows, sheet.write_rows
    real_messages, real_save = store.save_inbox_messages, store.save_inbox_connection

    def id_rows(sid, *, svc=None):
        timeline.append("id_rows")
        return real_id_rows(sid, svc=svc)

    def write_rows(sid, new_rows, updates=None, *, svc=None, hold=None):
        timeline.append(f"write:{len(new_rows)}+{len(updates or {})}")
        return real_write(sid, new_rows, updates, svc=svc, hold=hold)

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
        (sheet_writer, "id_rows"): id_rows, (sheet_writer, "write_rows"): write_rows,
        (firestore_repo, "save_inbox_messages"): save_messages,
        (firestore_repo, "save_inbox_connection"): save_connection,
    }


def test_a_whole_fire_reads_ids_then_writes_then_persists_then_checkpoints_and_a_refire_writes_nothing(
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
        "id_rows", "write:3+0", "persist:messages", "persist:checkpoint",
    ], "the sheet is read first, written once, and only then is anything persisted"

    # The same mail offered again (history still reports it; the backfill is
    # done): the id map read at the start of the fire makes it a no-op.
    timeline.clear()
    connected.connections[UID]["checkpoint"]["history_id"] = "800"
    second = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert second.ok and second.new_rows == 0 and second.messages_read == 0
    assert timeline[0] == "id_rows" and "write:0+0" in timeline
    assert [row[ID] for row in sheet.inserted] == ["n1", "n2", "b1"], "no row was written twice"
    assert mailbox.fetched == ["n1", "n2", "b1"], "nothing already on the sheet is re-read"


def test_a_fire_that_died_between_write_and_persist_duplicates_nothing_next_time(
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
    assert [row[ID] for row in sheet.inserted] == ["n1", "n2"]
    assert connected.connections[UID]["checkpoint"] == before["checkpoint"], "the checkpoint did not move"
    assert connected.connections[UID]["lease_until"] is None, "the lease is cleared even for an unmapped error"

    monkeypatch.setattr(firestore_repo, "save_inbox_messages", connected.save_inbox_messages)
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 0
    assert [row[ID] for row in sheet.inserted] == ["n1", "n2"], "duplicate-over-drop must not become a duplicate row"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "950"


def test_reconnecting_after_a_disconnect_writes_nothing_already_on_the_sheet(
    store, mailbox, sheet, model, grant, consent
):
    store.connections[UID] = _connected_doc()
    mailbox.messages = {m: _message(m) for m in ("a", "b", "c")}
    mailbox.listing = ["c", "b", "a"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert [row[ID] for row in sheet.inserted] == ["c", "b", "a"]

    pipeline.disconnect(UID)
    assert store.connections[UID]["sheet"]["id"] == SID and store.messages == {}
    doc = pipeline.connect(UID, code="c", state="s", email=EMAIL)
    assert doc["backfill"]["state"] == "running", "the kept sheet is still ok, so the backfill restarts"
    fetched_before = list(mailbox.fetched)

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert report.ok and report.new_rows == 0
    assert [row[ID] for row in sheet.inserted] == ["c", "b", "a"], "a reconnect must not write her rows again"
    assert mailbox.fetched == fetched_before, "rows already on the sheet are not re-read either"
    assert store.connections[UID]["backfill"]["state"] == "done"


def test_the_fallback_takes_a_fresh_checkpoint_before_listing_from_the_checkpoints_time_less_a_day(
    connected, mailbox, sheet, monkeypatch
):
    checkpoint_at = datetime.fromisoformat(connected.connections[UID]["checkpoint"]["updated_at"])
    # The last attempt is later than the checkpoint, and failed: not the anchor.
    connected.connections[UID]["last_poll"] = {
        "at": (NOW - timedelta(minutes=1)).isoformat(), "ok": False, "messages_read": 0,
        "error": "Gmail history was refused: HTTP 503",
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
    assert listed_after == [int(checkpoint_at.timestamp()) - 86400]
    assert connected.connections[UID]["checkpoint"]["history_id"] == "1200"


def test_the_fallback_on_a_connection_that_never_polled_counts_back_from_the_checkpoint_time(
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
    assert sheet.inserted == [] and connected.messages == {}
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
    assert [row[ID] for row in sheet.inserted] == ["o0", "o1"]


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
    assert doc["lease_until"] is None and sheet.inserted == []


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
    assert sheet.inserted == [] and mailbox.fetched == []


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


def test_a_fire_that_starts_while_another_is_writing_its_rows_touches_neither_mail_nor_sheet(
    connected, mailbox, sheet, monkeypatch
):
    """The write is one read of where the rows are and one batch addressed by
    what was read. A second fire between the two would move the rows under
    it, so inside the lease it must not get as far as the sheet at all."""
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    inner: list = []
    seen: dict = {}
    real_write = sheet.write_rows

    def write_with_an_overlap(sid, new_rows, updates=None, *, svc=None, hold=None):
        if not seen:  # the first fire's write only: a fire let in must not start another
            seen["mail"], seen["sheet"] = None, None
            mail, cells = len(mailbox.events), len(sheet.events)
            inner.append(pipeline.fire(
                UID, email=EMAIL, now=NOW + timedelta(seconds=pipeline.LEASE_SECONDS - 1)))
            seen["mail"], seen["sheet"] = mailbox.events[mail:], sheet.events[cells:]
        return real_write(sid, new_rows, updates, svc=svc, hold=hold)
    monkeypatch.setattr(sheet_writer, "write_rows", write_with_an_overlap)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert first.ok and first.new_rows == 1
    assert len(inner) == 1 and inner[0].skipped == "previous fire still running"
    assert seen == {"mail": [], "sheet": []}, "the second fire read no mail and asked the sheet nothing"
    assert sheet.events.count("write_rows") == 1 and [row[ID] for row in sheet.inserted] == ["m1"]
    assert connected.events.count("lease") == 1 and connected.events.count("messages") == 1


def test_a_fire_that_starts_during_the_one_time_reorder_is_skipped_and_never_asks_for_a_second_sort(
    connected, mailbox, sheet, monkeypatch
):
    """The sort moves every row. It runs inside the fire, under the lease,
    so a poll that arrives while it is running does not run one of its own."""
    connected.connections[UID]["sheet"].pop("ordering")  # stored before the rule existed
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "applied")
    inner: list = []
    overlapped: list = []

    def check_with_an_overlap(sid, *, caller_email, reorder=False, hold=None):
        if not overlapped:  # the first fire's check only
            overlapped.append(True)
            inner.append(pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(seconds=30)))
        return sheet.check(sid, caller_email=caller_email, reorder=reorder, hold=hold)
    monkeypatch.setattr(sheet_writer, "check", check_with_an_overlap)

    first = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert first.ok and first.skipped is None
    assert len(inner) == 1 and inner[0].skipped == "previous fire still running"
    assert sheet.reorders == [True], "one check, by the fire that holds the lease"
    assert [type(hold).__name__ for hold in sheet.check_holds] == ["_Lease"], \
        "and the sort is handed that lease to prove before it moves a row"
    assert connected.connections[UID]["sheet"]["ordering"] == "applied"
    assert connected.events.count("lease") == 1


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
# fire after the deploy must migrate it in place, add no row it already
# holds, and fill Action on the old rows through the ordinary summarise path.
# It is also the first fire to meet the sheet since rows became newest-first,
# so the same set-up pass sorts it once: rows are compared by Message ID here,
# never by where they sit.
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


def test_the_first_fire_after_the_deploy_migrates_the_live_sheet_and_re_offered_mail_adds_no_row(
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
    assert report.new_rows == 0 and len(grid.grid["Inbox"]) == 84, "dedupe held across the migration"
    assert not any(
        "insertDimension" in request and request["insertDimension"]["range"]["dimension"] == "ROWS"
        for name, kw in grid.calls if name.startswith("batchUpdate")
        for request in kw["body"]["requests"]
    ), "no row was inserted"
    assert grid.row("Inbox", 0, 11) == list(HEADERS)
    hers = {row[7]: (row + ["", ""])[8:10] for row in before[1:]}  # legacy H is the id
    for r in range(1, 84):
        cells = grid.row("Inbox", r, 11)
        assert cells[9:] == hers[cells[ID]], "her Status and Notes, intact and on the same message"
    # Newest first by the dates the rows held when they were sorted (the
    # re-triage below rewrites the Date cell from this test's one fixture).
    newest_first = sorted(before[1:], key=lambda row: (row[0], row[7]), reverse=True)
    assert [grid.cell("Inbox", r, ID) for r in range(1, 84)] == [row[7] for row in newest_first]
    assert grid.names().count("batchUpdate:order") == 1
    # The one-time re-triage: 50 this fire, through the same model path.
    assert report.retriaged_rows == 50 and report.retried_rows == 0
    filled = [r for r in range(1, 84) if grid.cell("Inbox", r, ACTION)]
    assert len(filled) == 50
    assert grid.cell("Inbox", filled[0], ACTION) == "Send Rathore Legal a shortlist of two paralegals by 18 Sep."
    assert grid.cell("Inbox", filled[0], CAT) == ASKS
    assert connected_retriage(store)["state"] == "running"

    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 0 and len(grid.grid["Inbox"]) == 84
    assert report.retriaged_rows == 33
    assert grid.names().count("batchUpdate:order") == 1, "sorted once, not once a fire"
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
    assert sheet.inserted == [] and sheet.updated == {} and mailbox.fetched == []
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
    assert [row[ID] for row in sheet.inserted] == ["a"], "the Inbox row is written regardless"
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


# --------------------------------------------------------------------------- #
# Pinned 2026-09-29: newest mail is the first data row; sheets from before
# that are put in the same order once; and a run of failed fires cannot move
# the point the recovery starts from.
# --------------------------------------------------------------------------- #

def _at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TEAM_TIMEZONE)


def test_new_mail_is_the_first_data_row_and_two_in_one_fire_keep_newest_first(
    connected, mailbox, sheet
):
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {
        "mon": _message("mon", at=_at(14)), "tue": _message("tue", at=_at(15)),
    }
    mailbox.history_added = ["mon", "tue"]  # history hands them over oldest first
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.top_to_bottom() == ["tue", "mon"]

    mailbox.messages.update({
        "wed-am": _message("wed-am", at=_at(16, 8)), "wed-pm": _message("wed-pm", at=_at(16, 17)),
    })
    mailbox.history_added = ["wed-am", "wed-pm"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert report.ok and report.new_rows == 2
    assert sheet.top_to_bottom() == ["wed-pm", "wed-am", "tue", "mon"]
    assert sheet.rows["wed-pm"] == 2, "the newest message is the first data row"
    assert connected.messages[f"{UID}__wed-pm"]["sheet_row"] == 2
    assert connected.messages[f"{UID}__wed-am"]["sheet_row"] == 3


def test_two_messages_inside_one_minute_are_still_newest_first(connected, mailbox, sheet):
    """The Date cell is to the minute; the order of a fire's own rows is
    decided on the full timestamp before they are handed to the sheet."""
    connected.connections[UID]["backfill"]["state"] = "done"
    minute = _at(16, 10, 5)
    mailbox.messages = {
        "first": _message("first", at=minute.replace(second=10)),
        "second": _message("second", at=minute.replace(second=50)),
    }
    mailbox.history_added = ["first", "second"]

    pipeline.fire(UID, email=EMAIL, now=NOW)

    assert sheet.inserted[0][0] == sheet.inserted[1][0] == "2026-09-16 10:05"
    assert sheet.top_to_bottom() == ["second", "first"]


def test_the_backfill_goes_under_the_new_mail_not_over_it(connected, mailbox, sheet):
    mailbox.messages = {
        "today": _message("today", at=_at(17)),
        "old1": _message("old1", at=_at(3)), "old2": _message("old2", at=_at(2)),
    }
    mailbox.history_added = ["today"]
    mailbox.listing = ["today", "old1", "old2"]
    pipeline.fire(UID, email=EMAIL, now=NOW)
    assert sheet.top_to_bottom() == ["today", "old1", "old2"]

    # Mail arrives while a later page of the backfill is still being walked.
    mailbox.messages.update({
        "tomorrow": _message("tomorrow", at=_at(18)), "old3": _message("old3", at=_at(1)),
    })
    connected.connections[UID]["backfill"].update(state="running", cursor=None)
    mailbox.history_added = ["tomorrow"]
    mailbox.listing = ["tomorrow", "today", "old1", "old2", "old3"]
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))

    assert sheet.top_to_bottom() == ["tomorrow", "today", "old1", "old2", "old3"]


def test_a_retried_row_and_new_mail_in_the_same_fire_each_reach_their_own_row(
    store, mailbox, model, grant, monkeypatch
):
    """With the real writer. The row to rewrite is found at row 3 when the
    fire starts; the new mail then goes in above it. Addressed by the number,
    the rewrite would land on the row above the one that was meant."""
    from inbox_triage_agent.tests.test_sheet_writer import _body, _fill, _row, current_sheet

    grid = _fill(current_sheet(), [
        _row("keep", "2026-09-16 09:00", notes="hers"),
        _row("odd", "2026-09-15 09:00", status="In progress", notes="chase Friday", mine="mine"),
        _row("last", "2026-09-14 09:00"),
    ])
    for col, value in ((CAT, UNREAD), (SUMMARY, ""), (ACTION, "")):
        grid.put("Inbox", 2, col, value)
    before = {row[ID]: row for row in _body(grid)}
    _real_sheet(monkeypatch, grid)
    store.connections[UID] = _connected_doc()
    store.connections[UID]["backfill"]["state"] = "done"
    store.connections[UID]["retriage"] = {"state": "done", "skipped": []}
    store.messages[f"{UID}__odd"] = {
        "user_id": UID, "message_id": "odd", "status": "needs_review", "attempts": 1,
        "retry_due": True, "sheet_row": 3,
    }
    mailbox.messages = {
        "odd": _message("odd", subject="Now readable", at=_at(15)),
        "new": _message("new", at=_at(18, 8)),
    }
    mailbox.history_added = ["new"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1 and report.retried_rows == 1
    body = _body(grid)
    assert [row[ID] for row in body] == ["new", "keep", "odd", "last"]
    rows = {row[ID]: row for row in body}
    assert rows["odd"][CAT] == ASKS and rows["odd"][SUMMARY], "the retried row was rewritten"
    assert rows["odd"][9:] == ["In progress", "chase Friday", "", "mine"], "and her cells on it kept"
    assert rows["keep"] == before["keep"] and rows["last"] == before["last"], \
        "no other row was written to"
    assert store.messages[f"{UID}__odd"]["sheet_row"] == 4
    assert store.messages[f"{UID}__new"]["sheet_row"] == 2
    assert [name for name in grid.names() if name.startswith("batchUpdate")] == ["batchUpdate:rows"], \
        "one write for the whole fire"


def test_a_sheet_not_yet_newest_first_is_checked_by_its_next_fire_and_only_a_fire_may_reorder(
    connected, mailbox, sheet
):
    # A connection stored before the rule existed: no ``ordering`` on it.
    connected.connections[UID]["sheet"].pop("ordering")
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "applied")

    pipeline.fire(UID, email=EMAIL, now=NOW)

    assert sheet.checks == 1 and sheet.reorders == [True], "not left for the hourly check"
    assert connected.connections[UID]["sheet"]["ordering"] == "applied"
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert sheet.checks == 1, "settled: nothing to check again until the hour is up"

    # From the panel a check can land in the middle of a fire, so it is never
    # allowed to move rows — and what it leaves pending, the next fire does.
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "pending")
    pipeline.recheck_sheet(UID, email=EMAIL)
    pipeline.set_sheet(UID, SID, email=EMAIL)
    assert sheet.reorders == [True, False, False]
    assert connected.connections[UID]["sheet"]["ordering"] == "pending"
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "applied")
    pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=10))
    assert sheet.reorders == [True, False, False, True]
    assert connected.connections[UID]["sheet"]["ordering"] == "applied"


def test_a_sheet_that_could_not_be_sorted_gets_its_mail_and_the_sort_waits_an_hour(
    connected, mailbox, sheet
):
    """The owner's decision: mail keeps flowing. A sheet Sheets will not sort
    is not a reason to stop writing to it, and it is not asked again on
    every five-minute fire."""
    note = (
        "The Inbox tab has not been put in newest-first order yet: it has merged cells at "
        "K3:K4, and Google Sheets cannot sort rows that are merged together. Unmerge them "
        "(Format > Merge cells > Unmerge). New mail is still added at the top. The agent "
        "tries the sort again every hour."
    )
    connected.connections[UID]["sheet"]["ordering"] = "pending"
    connected.connections[UID]["backfill"]["state"] = "done"
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "blocked", note)
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.skipped is None and report.error is None
    assert report.new_rows == 1 and [row[ID] for row in sheet.inserted] == ["m1"], "mail landed"
    doc = connected.connections[UID]
    assert doc["sheet"]["check"] == "ok"
    assert (doc["sheet"]["ordering"], doc["sheet"]["ordering_note"]) == ("blocked", note)
    assert doc["last_poll"]["ok"] is True and doc["last_poll"]["error"] is None, \
        "the read did not fail, and is not reported as if it had"
    assert doc["checkpoint"]["history_id"] == mailbox.history_id
    shown = pipeline.status_payload(UID, now=NOW)["sheet"]
    assert (shown["ordering"], shown["ordering_note"]) == ("blocked", note), "where the panel reads"

    # The fires of the next hour write their mail and ask the sheet nothing
    # about its order.
    assert pipeline.ORDER_RETRY_SECONDS == 3600
    checked_at = pipeline._parse_iso(doc["sheet"]["checked_at"])
    for minutes in (5, 30, 59):
        mailbox.messages[f"n{minutes}"] = _message(f"n{minutes}")
        mailbox.history_added = [f"n{minutes}"]
        later = pipeline.fire(UID, email=EMAIL, now=checked_at + timedelta(minutes=minutes))
        assert later.ok and later.new_rows == 1
    assert sheet.checks == 1 and sheet.reorders == [True]

    # After it, the sort is tried again, by a fire, under its lease.
    sheet.check_result = SheetCheck("ok", "Her inbox", "", "ours", "applied")
    pipeline.fire(UID, email=EMAIL, now=checked_at + timedelta(minutes=61))
    assert sheet.checks == 2 and sheet.reorders == [True, True]
    doc = connected.connections[UID]
    assert (doc["sheet"]["ordering"], doc["sheet"]["ordering_note"]) == ("applied", "")
    assert pipeline.status_payload(UID, now=NOW)["sheet"]["ordering_note"] is None


def test_a_connection_stored_before_the_note_existed_reads_as_no_note(connected, sheet):
    stored = connected.connections[UID]["sheet"]
    assert "ordering_note" not in stored
    shown = pipeline.status_payload(UID, now=NOW)["sheet"]
    assert shown["ordering"] == "already" and shown["ordering_note"] is None
    stored.pop("ordering")
    assert pipeline.status_payload(UID, now=NOW)["sheet"]["ordering"] is None


def _listing_that_honours_after(mailbox, monkeypatch) -> list[int]:
    """The fake listing, made to do what Gmail's ``after:`` does: leave out
    what is older. Returns the ``after`` of every call."""
    asked: list[int] = []

    def list_inbox(svc, *, after_epoch, page_token=None, max_results=100):
        asked.append(after_epoch)
        listing = [m for m in mailbox.listing
                   if mailbox.messages[m].received_at.timestamp() > after_epoch]
        start = int(page_token or 0)
        token = str(start + max_results) if start + max_results < len(listing) else None
        return listing[start:start + max_results], token, len(listing)
    monkeypatch.setattr(gmail_client, "list_inbox", list_inbox)
    return asked


def test_after_days_of_failed_fires_the_fallback_re_lists_from_the_last_poll_that_got_through(
    connected, mailbox, sheet, model, monkeypatch
):
    """2026-09-22: the model provider refused every call for 44 hours. Each
    failed fire stamped ``last_poll``; had Gmail let go of the history in that
    time, the re-listing would have begun a day before the LAST FAILED fire
    and the first day and more of the outage would never have been a row."""
    got_through = NOW - timedelta(minutes=5)
    connected.connections[UID]["backfill"]["state"] = "done"
    assert connected.connections[UID]["checkpoint"]["updated_at"] == got_through.isoformat()
    # Thirteen messages across three days of outage, one every five hours.
    outage = {
        f"m{i:02d}": _message(f"m{i:02d}", at=(NOW + timedelta(hours=1 + 5 * i)).astimezone(TEAM_TIMEZONE))
        for i in range(13)
    }
    mailbox.messages = dict(outage)
    mailbox.history_added = sorted(outage)
    model.always_fail = True
    for hours in range(0, 72, 6):
        failed = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(hours=hours))
        assert failed.ok is False and "abandoned" in failed.error
    doc = connected.connections[UID]
    assert doc["last_poll"]["at"] == (NOW + timedelta(hours=66)).isoformat(), \
        "the last ATTEMPT walked forward with every failure"
    assert doc["checkpoint"] == {"history_id": "800", "updated_at": got_through.isoformat()}, \
        "the last poll that got through did not"
    assert sheet.inserted == []

    # The provider is back, and Gmail no longer holds history that far back.
    model.always_fail = False
    mailbox.history_expired = True
    mailbox.history_id = "5000"
    mailbox.listing = sorted(outage, reverse=True)  # newest first, as Gmail lists
    monkeypatch.setattr(gmail_client, "LIST_THREAD_PAGE_MAX", 2)  # seven pages
    asked = _listing_that_honours_after(mailbox, monkeypatch)
    back = NOW + timedelta(hours=72)

    report = pipeline.fire(UID, email=EMAIL, now=back)

    assert report.ok and report.new_rows == 13, "every message of the outage is a row"
    assert set(asked) == {int(got_through.timestamp()) - 86400}, \
        "listed from the last poll that got through, less a day — not from the last attempt"
    assert len(asked) == 7, "and to the end of the listing, not to a page limit"
    assert sheet.top_to_bottom() == sorted(outage, reverse=True), "newest first"
    doc = connected.connections[UID]
    assert doc["checkpoint"] == {"history_id": "5000", "updated_at": back.isoformat()}
    assert doc["last_poll"]["ok"] is True


def test_a_recovery_too_big_for_one_fire_keeps_its_anchor_and_the_next_fire_finishes_it(
    connected, mailbox, sheet, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    real_fetch = mailbox.fetch

    def slow_fetch(svc, message_id):
        clock.t += 30.0
        return real_fetch(svc, message_id)
    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)
    connected.connections[UID]["backfill"]["state"] = "done"
    anchor = dict(connected.connections[UID]["checkpoint"])
    mailbox.history_expired = True
    mailbox.history_id = "1200"
    mailbox.messages = {f"m{i}": _message(f"m{i}", at=_at(17, 18 + i)) for i in range(5)}
    mailbox.listing = ["m4", "m3", "m2", "m1", "m0"]
    asked = _listing_that_honours_after(mailbox, monkeypatch)

    # 125s less both reserves = 60s of work: two fetches.
    first = pipeline.fire(UID, email=EMAIL, now=NOW, budget_seconds=125.0)

    assert first.ok and first.unreached and first.new_rows == 2
    assert connected.connections[UID]["checkpoint"] == anchor, \
        "a poll that did not get through all of it does not become the new starting point"

    clock.t = 5000.0
    second = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5), budget_seconds=1000.0)

    assert second.ok and second.new_rows == 3 and not second.unreached
    assert asked[0] == asked[-1] == int(datetime.fromisoformat(anchor["updated_at"]).timestamp()) - 86400
    assert mailbox.fetched == ["m4", "m3", "m2", "m1", "m0"], "what was done is not read again"
    assert sheet.top_to_bottom() == ["m4", "m3", "m2", "m1", "m0"], \
        "the older mail of the second fire goes UNDER the newer mail of the first"
    assert connected.connections[UID]["checkpoint"]["history_id"] == "1200"


def test_a_re_listing_cut_short_by_the_budget_never_saves_a_fresh_checkpoint(
    connected, mailbox, sheet, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    connected.connections[UID]["backfill"]["state"] = "done"
    anchor = dict(connected.connections[UID]["checkpoint"])
    mailbox.history_expired = True
    mailbox.history_id = "1200"
    mailbox.messages = {f"m{i}": _message(f"m{i}", at=_at(10 + i)) for i in range(4)}
    mailbox.listing = ["m3", "m2", "m1", "m0"]
    monkeypatch.setattr(gmail_client, "LIST_THREAD_PAGE_MAX", 2)
    real_list = mailbox.list_inbox

    def slow_list(svc, *, after_epoch, page_token=None, max_results=100):
        clock.t += 200.0
        return real_list(svc, after_epoch=after_epoch, page_token=page_token, max_results=max_results)
    monkeypatch.setattr(gmail_client, "list_inbox", slow_list)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.unreached, "said, not hidden"
    assert connected.connections[UID]["checkpoint"] == anchor, \
        "mail that was never even listed must not end up behind the checkpoint"

    # And the listing says so itself, whatever the caller then does with it.
    work = pipeline._Work(user_id=UID, gmail="gmail-service", llm=None, deadline=clock.t + 100.0)
    ids, checkpoint = pipeline._new_mail_ids(work, connected.connections[UID], NOW)
    assert ids == ["m3", "m2"] and checkpoint is None and work.unreached
    work = pipeline._Work(user_id=UID, gmail="gmail-service", llm=None, deadline=clock.t + 1000.0)
    ids, checkpoint = pipeline._new_mail_ids(work, connected.connections[UID], NOW)
    assert ids == ["m3", "m2", "m1", "m0"] and checkpoint == "1200" and not work.unreached


def test_a_connection_with_no_checkpoint_time_counts_back_from_when_it_was_connected(
    connected, mailbox, sheet, monkeypatch
):
    """An older document may hold a checkpoint with no ``updated_at``; the
    field is read with a default, and never falls back to the last attempt."""
    connected.connections[UID]["backfill"]["state"] = "done"
    connected.connections[UID]["checkpoint"] = {"history_id": "800"}
    connected.connections[UID]["last_poll"] = {
        "at": (NOW - timedelta(minutes=1)).isoformat(), "ok": False, "messages_read": 0,
        "error": "The model failed on 3 messages in a row",
    }
    connected_at = datetime.fromisoformat(connected.connections[UID]["gmail"]["connected_at"])
    mailbox.history_expired = True
    asked = _listing_that_honours_after(mailbox, monkeypatch)

    pipeline.fire(UID, email=EMAIL, now=NOW)

    assert asked == [int(connected_at.timestamp()) - 86400]


# --------------------------------------------------------------------------- #
# Pinned 2026-09-30: what the independent verification of the newest-first
# change found, at the level of a whole fire. The lease is proved before a
# row is written and cleared only by the fire that owns it; a row batch whose
# reply is lost is one copy on the sheet; a sheet that cannot be sorted still
# gets its mail.
# --------------------------------------------------------------------------- #

def _sheet_of_three(monkeypatch, store):
    """The real writer over a newest-first sheet of three, her cells on each."""
    from inbox_triage_agent.tests.test_sheet_writer import newest_first_sheet

    grid = newest_first_sheet()
    _real_sheet(monkeypatch, grid)
    monkeypatch.setattr(sheet_writer.time, "sleep", lambda _seconds: None)  # the waits between passes
    store.connections[UID] = _connected_doc()
    store.connections[UID]["backfill"]["state"] = "done"
    store.connections[UID]["retriage"] = {"state": "done", "skipped": []}
    return grid


def test_a_row_write_whose_reply_was_lost_is_one_copy_on_the_sheet_and_the_fire_records_it(
    store, mailbox, model, grant, monkeypatch
):
    """Seen live (R1) with the fire's own writer: the batch was applied, the
    reply lost, the batch sent again - the new message twice, another
    message gone, and her notes on the wrong message, reported as a success."""
    from inbox_triage_agent.tests.test_sheet_writer import _body, _lose_the_reply

    grid = _sheet_of_three(monkeypatch, store)
    for col, value in ((CAT, UNREAD), (SUMMARY, ""), (ACTION, "")):
        grid.put("Inbox", 2, col, value)  # "b" is waiting for another try
    before = {row[ID]: row for row in _body(grid)}
    store.messages[f"{UID}__b"] = {
        "user_id": UID, "message_id": "b", "status": "needs_review", "attempts": 1,
        "retry_due": True, "sheet_row": 3,
    }
    mailbox.messages = {
        "b": _message("b", subject="Now readable", at=_at(18)),
        "new": _message("new", at=_at(21, 8)),
    }
    mailbox.history_added = ["new"]
    mailbox.history_id = "950"
    requests_sent = _lose_the_reply(grid)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.error is None
    assert report.new_rows == 1 and report.retried_rows == 1
    assert len(requests_sent) == 1 and requests_sent[0].sent == 1, "sent once"
    body = _body(grid)
    assert [row[ID] for row in body] == ["new", "c", "b", "a"], "one copy, and nobody written over"
    rows = {row[ID]: row for row in body}
    for message_id in ("c", "b", "a"):
        assert rows[message_id][9:] == before[message_id][9:], \
            f"her cells on {message_id!r} are still her cells on {message_id!r}"
    assert rows["b"][CAT] == ASKS and rows["b"][SUMMARY], "the retried row was rewritten"
    assert rows["c"][:9] == before["c"][:9] and rows["a"][:9] == before["a"][:9]
    # What the fire persists is what is on the sheet.
    assert store.messages[f"{UID}__new"]["sheet_row"] == 2
    assert store.messages[f"{UID}__b"]["sheet_row"] == 4
    doc = store.connections[UID]
    assert doc["last_poll"]["ok"] is True and doc["checkpoint"]["history_id"] == "950"
    assert doc["lease_until"] is None

    # And the next fire, offered the same mail, writes nothing.
    calls = len(grid.calls)
    store.connections[UID]["checkpoint"]["history_id"] = "800"
    again = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert again.ok and again.new_rows == 0
    assert not any(name.startswith("batchUpdate") for name in grid.names()[calls:])
    assert [row[ID] for row in _body(grid)] == ["new", "c", "b", "a"]


def test_a_row_write_that_never_gets_an_answer_fails_the_fire_and_the_next_one_writes_the_mail(
    store, mailbox, model, grant, monkeypatch
):
    from inbox_triage_agent.tests.test_sheet_writer import _body

    grid = _sheet_of_three(monkeypatch, store)
    mailbox.messages = {"new": _message("new", at=_at(21, 8))}
    mailbox.history_added = ["new"]
    mailbox.history_id = "950"
    grid.fail["batchUpdate:rows"] = TimeoutError("The read operation timed out")

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok is False and "got no answer" in report.error and SID not in report.error
    assert store.messages == {}, "nothing is recorded that is not on the sheet"
    doc = store.connections[UID]
    assert doc["checkpoint"]["history_id"] == "800", "the mail is still ahead of the next fire"
    assert doc["last_poll"]["ok"] is False and doc["lease_until"] is None

    del grid.fail["batchUpdate:rows"]
    report = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(minutes=5))
    assert report.ok and report.new_rows == 1
    assert [row[ID] for row in _body(grid)] == ["new", "c", "b", "a"]


def test_a_fire_still_writing_when_its_lease_runs_out_is_not_joined_by_the_next_one(
    store, mailbox, model, grant, monkeypatch
):
    """Every Sheets call may take 3 x 30 s plus back-off, and the fire's
    budget does not count them: by the time a fire writes, the lease it took
    when it started can be all but spent. The next poll then found the lease
    free while this one was between its position read and its batch.

    With the fire's own writer: the first lease has a second left when the
    write begins, the reads take two, and the next poll arrives just before
    the batch is sent."""
    from inbox_triage_agent.tests.test_sheet_writer import _body

    grid = _sheet_of_three(monkeypatch, store)
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    mailbox.messages = {"m1": _message("m1", at=_at(21, 8))}
    mailbox.history_added = ["m1"]
    real_fetch, real_read, real_batch = mailbox.fetch, grid._values_batch_get, grid._batch_update
    inner: list = []

    def slow_fetch(svc, message_id):
        clock.t += pipeline.LEASE_SECONDS - 1
        return real_fetch(svc, message_id)

    def slow_read(**kw):
        if "valueRenderOption" in kw:  # the writer's position read
            clock.t += 2
        return real_read(**kw)

    def batch_with_the_next_poll_arriving(**kw):
        if any("insertDimension" in r for r in kw["body"]["requests"]) and not inner:
            inner.append(None)
            later = NOW + timedelta(seconds=pipeline.LEASE_SECONDS + 1)
            inner[0] = pipeline.fire(UID, email=EMAIL, now=later)
        return real_batch(**kw)

    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)
    grid._values_batch_get, grid._batch_update = slow_read, batch_with_the_next_poll_arriving

    first = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert first.ok and first.new_rows == 1
    assert len(inner) == 1 and inner[0].skipped == "previous fire still running", (
        f"a second fire ran to the end inside the first one's write "
        f"(new_rows={inner[0].new_rows}, skipped={inner[0].skipped!r}); "
        f"the sheet now reads {[row[ID] for row in _body(grid)]}"
    )
    assert [row[ID] for row in _body(grid)] == ["m1", "c", "b", "a"]
    assert grid.names().count("batchUpdate:rows") == 1
    assert store.events.count("lease") == 1 and store.events.count("renew") >= 1
    assert store.connections[UID]["lease_until"] is None, "its own lease, cleared at the end"


def _taken_over(store: FakeStore, *, at: datetime) -> str:
    """The next poll takes the lease, as ``take_inbox_lease`` would."""
    until = at + timedelta(seconds=pipeline.LEASE_SECONDS)
    assert store.take_inbox_lease(UID, now=at, until=until, owner="the-next-fire") is not None, \
        "the lease had run out, so it was there to take"
    return until.isoformat()


def test_a_fire_that_overran_does_not_clear_the_lease_of_the_fire_that_replaced_it(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    real_persist = connected.save_inbox_messages
    theirs: list[str] = []

    def persist_during_which_the_lease_changes_hands(user_id, docs):
        # The rows are written. The lease ran out and the next poll took it.
        theirs.append(_taken_over(connected, at=NOW + timedelta(seconds=2 * pipeline.LEASE_SECONDS)))
        return real_persist(user_id, docs)

    monkeypatch.setattr(firestore_repo, "save_inbox_messages", persist_during_which_the_lease_changes_hands)

    pipeline.fire(UID, email=EMAIL, now=NOW)

    doc = connected.connections[UID]
    assert theirs and (doc["lease_until"], doc["lease_owner"]) == (theirs[0], "the-next-fire"), (
        "the finished fire cleared a lease that was no longer its own, so a third fire "
        "can start while the second is still writing"
    )
    assert connected.events[-1] == "release refused"
    third = pipeline.fire(UID, email=EMAIL, now=NOW + timedelta(seconds=2 * pipeline.LEASE_SECONDS + 5))
    assert third.skipped == "previous fire still running"


def test_a_fire_whose_lease_was_taken_over_before_it_wrote_writes_nothing_and_records_nothing(
    connected, mailbox, sheet, monkeypatch, caplog
):
    """It read its mail and paid for its summaries; none of that is a reason
    to write under a lease that is another fire's. The fire that holds the
    lease reads the same mail."""
    import logging

    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    before = copy.deepcopy(connected.connections[UID])
    real_write = sheet.write_rows
    theirs: list[str] = []

    def write_after_the_lease_changed_hands(sid, new_rows, updates=None, *, svc=None, hold=None):
        theirs.append(_taken_over(connected, at=NOW + timedelta(seconds=2 * pipeline.LEASE_SECONDS)))
        return real_write(sid, new_rows, updates, svc=svc, hold=hold)

    monkeypatch.setattr(sheet_writer, "write_rows", write_after_the_lease_changed_hands)

    with caplog.at_level(logging.WARNING):
        report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.skipped == pipeline.SKIPPED_LEASE_LOST and report.error is None
    assert sheet.inserted == [] and "write_rows" not in sheet.events, "not a row"
    assert connected.messages == {}, "not a tracking document"
    doc = connected.connections[UID]
    assert (doc["lease_until"], doc["lease_owner"]) == (theirs[0], "the-next-fire")
    for key in ("checkpoint", "last_poll", "backfill", "needs_review", "recent_fires"):
        assert doc.get(key) == before.get(key), f"{key} belongs to the fire that holds the lease"
    assert "its lease was taken over" in caplog.text and UID not in caplog.text


def test_a_disconnect_in_the_middle_of_a_fire_stops_that_fire_from_writing(
    connected, mailbox, sheet, monkeypatch
):
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    real_write = sheet.write_rows

    def write_after_she_disconnected(sid, new_rows, updates=None, *, svc=None, hold=None):
        pipeline.disconnect(UID)
        return real_write(sid, new_rows, updates, svc=svc, hold=hold)

    monkeypatch.setattr(sheet_writer, "write_rows", write_after_she_disconnected)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.skipped == pipeline.SKIPPED_LEASE_LOST
    assert sheet.inserted == [] and connected.messages == {}
    doc = connected.connections[UID]
    assert "checkpoint" not in doc and "gmail" not in doc, "what the disconnect removed stays removed"
    assert doc["lease_until"] is None and doc["lease_owner"] is None


def test_the_lease_is_extended_from_the_fires_own_clock_and_only_when_it_has_to_be(
    connected, mailbox, sheet, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    real_fetch = mailbox.fetch
    seen: list[str] = []
    real_renew = connected.renew_inbox_lease

    def slow_fetch(svc, message_id):
        clock.t += 100.0
        return real_fetch(svc, message_id)

    def renew(user_id, *, owner, held, until):
        seen.append((held, until.isoformat()))
        return real_renew(user_id, owner=owner, held=held, until=until)

    monkeypatch.setattr(gmail_client, "fetch", slow_fetch)
    monkeypatch.setattr(firestore_repo, "renew_inbox_lease", renew)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1
    assert seen == [(
        (NOW + timedelta(seconds=pipeline.LEASE_SECONDS)).isoformat(),
        (NOW + timedelta(seconds=100 + pipeline.LEASE_SECONDS)).isoformat(),
    )], "once, before the write: from when the write began, and enough for the tab's rewrite too"
    assert sheet.worktree_writes == 1


def test_the_worktree_is_not_rewritten_by_a_fire_that_lost_its_lease_after_its_rows_landed(
    connected, mailbox, sheet, monkeypatch
):
    """The rows are on the sheet and are this fire's to record. The tab's
    rewrite is a write like any other, and is left to the lease's holder."""
    clock = _Clock()
    monkeypatch.setattr(pipeline.time, "monotonic", clock.monotonic)
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    real_read = sheet.read_inbox

    def read_that_outlasts_the_lease(sid, *, svc=None):
        clock.t += 2 * pipeline.LEASE_SECONDS
        _taken_over(connected, at=NOW + timedelta(seconds=2 * pipeline.LEASE_SECONDS))
        return real_read(sid, svc=svc)

    monkeypatch.setattr(sheet_writer, "read_inbox", read_that_outlasts_the_lease)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.new_rows == 1 and [row[ID] for row in sheet.inserted] == ["m1"]
    assert sheet.worktree_writes == 0 and report.worktree_rows == 0
    doc = connected.connections[UID]
    assert doc["worktree"]["error"] == pipeline.SKIPPED_LEASE_LOST
    assert connected.messages[f"{UID}__m1"]["sheet_row"] == 2, "the rows it wrote are recorded"
    assert doc["lease_owner"] == "the-next-fire"


def test_the_lease_rules_are_the_ones_the_store_applies():
    held = firestore_repo.inbox_lease_held_by
    mine = (NOW + timedelta(seconds=60)).isoformat()
    theirs = (NOW + timedelta(seconds=400)).isoformat()
    assert held({"lease_owner": "abc", "lease_until": mine}, "abc", mine)
    assert held({"lease_owner": "abc", "lease_until": NOW.isoformat()}, "abc", NOW.isoformat()), \
        "a lease that ran out and that nobody took is still its owner's to extend"
    assert not held({"lease_owner": "xyz", "lease_until": mine}, "abc", mine)
    assert not held({"lease_owner": "abc", "lease_until": theirs}, "abc", mine), \
        "taken by a fire that writes no token: the time moved, so it changed hands"
    assert not held({"lease_owner": "abc", "lease_until": None}, "abc", mine), "cleared"
    for nobody in ({"lease_owner": None, "lease_until": mine}, {"lease_until": mine}, {}, None):
        assert not held(nobody, "abc", mine), \
            "a lease taken before owners existed is nobody's to extend or clear"
    assert not held({"lease_owner": "", "lease_until": mine}, "", mine), "no token holds nothing"
    assert not held({"lease_owner": "abc", "lease_until": ""}, "abc", "")


class _Transaction:
    """As much of a Firestore transaction as ``firestore.transactional``
    drives: writes are held until the commit, and a commit is all of them."""

    _max_attempts = 1
    _read_only = False

    def __init__(self, docs: dict):
        self.docs, self.pending, self._id = docs, [], None

    def _clean_up(self):
        self.pending, self._id = [], None

    def _begin(self, retry_id=None):
        self._id = b"txn"

    def _rollback(self):
        self.pending = []

    def _commit(self):
        for key, patch in self.pending:
            self.docs[key].update(patch)
        return []

    def update(self, ref, patch):
        self.pending.append((ref.key, dict(patch)))


class _Document:
    def __init__(self, docs: dict, key: str):
        self.docs, self.key = docs, key

    def get(self, transaction=None):
        from types import SimpleNamespace

        doc = self.docs.get(self.key)
        return SimpleNamespace(exists=doc is not None, to_dict=lambda: copy.deepcopy(doc))


class _Database:
    def __init__(self, docs: dict):
        self.docs = docs

    def collection(self, name):
        from types import SimpleNamespace

        assert name == firestore_repo.INBOX_CONNECTIONS
        return SimpleNamespace(document=lambda key: _Document(self.docs, key))

    def transaction(self):
        return _Transaction(self.docs)


def test_the_stores_own_lease_primitives_extend_and_clear_only_the_callers_lease(monkeypatch):
    """The real ``firestore_repo`` functions, over a transaction that holds
    its writes until the commit. No Firestore is reached."""
    docs = {UID: {"gmail": {"connected": True}, "lease_until": None}}
    monkeypatch.setattr(firestore_repo, "_db", lambda: _Database(docs))
    first = NOW + timedelta(seconds=300)

    taken = firestore_repo.take_inbox_lease(UID, now=NOW, until=first, owner="fire-1")
    assert taken["lease_owner"] == "fire-1" and taken["lease_until"] == first.isoformat()
    assert (docs[UID]["lease_until"], docs[UID]["lease_owner"]) == (first.isoformat(), "fire-1")
    assert firestore_repo.take_inbox_lease(
        UID, now=NOW + timedelta(seconds=10), until=first, owner="fire-2") is None
    assert docs[UID]["lease_owner"] == "fire-1", "a lease that is held is not taken"

    # Extended by its owner, and by nobody else.
    second = NOW + timedelta(seconds=500)
    assert not firestore_repo.renew_inbox_lease(
        UID, owner="fire-2", held=first.isoformat(), until=second)
    assert not firestore_repo.renew_inbox_lease(
        UID, owner="fire-1", held=second.isoformat(), until=second), "not the time it wrote"
    assert docs[UID]["lease_until"] == first.isoformat(), "a refusal writes nothing"
    assert firestore_repo.renew_inbox_lease(
        UID, owner="fire-1", held=first.isoformat(), until=second)
    assert (docs[UID]["lease_until"], docs[UID]["lease_owner"]) == (second.isoformat(), "fire-1")

    # It runs out and the next fire takes it: the first can do nothing to it.
    later = NOW + timedelta(seconds=600)
    third = later + timedelta(seconds=300)
    assert firestore_repo.take_inbox_lease(UID, now=later, until=third, owner="fire-2") is not None
    assert not firestore_repo.renew_inbox_lease(
        UID, owner="fire-1", held=second.isoformat(), until=third)
    assert not firestore_repo.release_inbox_lease(UID, owner="fire-1", held=second.isoformat())
    assert (docs[UID]["lease_until"], docs[UID]["lease_owner"]) == (third.isoformat(), "fire-2")

    # Cleared by its owner.
    assert firestore_repo.release_inbox_lease(UID, owner="fire-2", held=third.isoformat())
    assert docs[UID]["lease_until"] is None and docs[UID]["lease_owner"] is None
    assert not firestore_repo.release_inbox_lease(UID, owner="fire-2", held=third.isoformat())

    # No document, no lease.
    assert not firestore_repo.renew_inbox_lease("nobody", owner="x", held="y", until=third)
    assert not firestore_repo.release_inbox_lease("nobody", owner="x", held="y")
    assert firestore_repo.take_inbox_lease("nobody", now=NOW, until=first, owner="x") is None


def test_a_lease_taken_by_a_revision_that_writes_no_owner_is_not_written_under_or_cleared(
    connected, mailbox, sheet, monkeypatch
):
    """While two revisions serve at once, a fire of the older one takes the
    lease by writing the time alone; the token on the document is still this
    fire's. The time is not."""
    connected.connections[UID]["backfill"]["state"] = "done"
    mailbox.messages = {"m1": _message("m1")}
    mailbox.history_added = ["m1"]
    real_write = sheet.write_rows
    theirs = (NOW + timedelta(seconds=2 * pipeline.LEASE_SECONDS)).isoformat()

    def write_during_which_the_lease_changes_hands(sid, new_rows, updates=None, *, svc=None, hold=None):
        connected.connections[UID]["lease_until"] = theirs
        return real_write(sid, new_rows, updates, svc=svc, hold=hold)

    monkeypatch.setattr(sheet_writer, "write_rows", write_during_which_the_lease_changes_hands)

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.skipped == pipeline.SKIPPED_LEASE_LOST and sheet.inserted == []
    assert connected.connections[UID]["lease_until"] == theirs, (
        "the finished fire cleared a lease that was no longer its own, so a third fire "
        "can start while the second is still writing"
    )


def test_a_sheet_that_cannot_be_sorted_still_gets_its_mail_and_says_why_where_the_panel_reads(
    store, mailbox, model, grant, monkeypatch
):
    """Seen live (B4), with the fire's own writer: a vertical merge below the
    header made Sheets refuse the sort, the fire failed, and so did every
    fire after it - no mail at all for that user."""
    from inbox_triage_agent.tests.test_sheet_writer import MERGE_K3_K4, _body, split_sheet

    grid = split_sheet()
    grid.merges = [dict(MERGE_K3_K4)]
    order = [row[ID] for row in _body(grid)]
    before = {row[ID]: row for row in _body(grid)}
    _real_sheet(monkeypatch, grid)
    store.connections[UID] = _connected_doc()
    store.connections[UID]["sheet"].pop("ordering")  # connected before the rule
    store.connections[UID]["backfill"]["state"] = "done"
    store.connections[UID]["retriage"] = {"state": "done", "skipped": []}
    mailbox.messages = {"today": _message("today", at=_at(29, 10))}
    mailbox.history_added = ["today"]

    report = pipeline.fire(UID, email=EMAIL, now=NOW)

    assert report.ok and report.error is None and report.new_rows == 1
    body = _body(grid)
    assert [row[ID] for row in body] == ["today", *order], \
        "new mail at the top, and the sheet in the order it was in"
    assert {row[ID]: row for row in body if row[ID] != "today"} == before
    assert "batchUpdate:order" not in grid.names()
    shown = pipeline.status_payload(UID, now=NOW)
    assert shown["sheet"]["check"] == "ok" and shown["sheet"]["ordering"] == "blocked"
    assert shown["sheet"]["ordering_note"] == (
        "The Inbox tab has not been put in newest-first order yet: it has merged cells at "
        "K3:K4, and Google Sheets cannot sort rows that are merged together. Unmerge them "
        "(Format > Merge cells > Unmerge). New mail is still added at the top. The agent "
        "tries the sort again every hour."
    )
    assert shown["last_poll"]["ok"] is True and shown["last_poll"]["error"] is None

    # She unmerges. Within the hour nothing is asked; after it the tab is sorted.
    grid.merges = []
    mailbox.history_added = []
    checked_at = pipeline._parse_iso(store.connections[UID]["sheet"]["checked_at"])
    pipeline.fire(UID, email=EMAIL, now=checked_at + timedelta(minutes=30))
    assert "batchUpdate:order" not in grid.names()
    pipeline.fire(UID, email=EMAIL, now=checked_at + timedelta(minutes=61))
    assert grid.names().count("batchUpdate:order") == 1
    ids = [row[ID] for row in _body(grid)]
    assert ids[:3] == ["today", "n4", "n3"] and ids[-1] == "b7"
    shown = pipeline.status_payload(UID, now=NOW)["sheet"]
    assert (shown["ordering"], shown["ordering_note"]) == ("applied", None)
