"""The mail log turned into a list of open work. Pure rules; no I/O, no model.

A **process is one Gmail thread**. Phase 1 derives everything from facts the
agent already has — the thread id it now stores per message, the row the
triage already wrote, and her own Status cell — with plain rules that can be
read off this module and argued with. Nothing here calls a model, and nothing
here guesses: a fact that is missing produces an empty cell, never a filler.

The rules, in one place:

* **Title** — the thread's earliest subject with ``Re:``/``Fwd:``/``RE[2]:``
  and friends stripped. Earliest, because that is the subject Gmail itself
  shows for the thread, so the name here matches what she sees there.
* **Type** — the most demanding category present in the thread, ranked by
  :data:`TYPE_PRIORITY`. A thread that starts as an update and turns into a
  request is an *Action required* thread; ``needs_review`` ranks last because
  it is an absence of information, not a kind of work.
* **Status** — decided by who sent the LAST message. Her own connected
  address means the ball is with the other side (``Waiting on them``);
  anyone else's means it is with her (``Waiting on us``). Either side can go
  quiet, and silence of :data:`CHASE_SILENCE_DAYS` days or more says so in
  the same column: ``Chasing`` when they owe her the reply, and
  ``Overdue reply`` when she owes them one and has marked nothing in the
  thread Done or Ignore. Four words, one column, both of them alarms.
* **Next action** — the Action text of the most recent message that has one
  and that she has not marked Done or Ignore. The code-stamped "no action"
  wording is not an action.
* **Waiting since** — whole days between the last message and today, in
  :data:`triage.TEAM_TIMEZONE`, written as plain language.
* **Due** — the earliest deadline in the thread that is still open (its row
  is not Done or Ignore). Overdue ones are kept: an obligation that has
  passed is still hers to close.
* **Mails** / **Latest** — how many of the thread's messages the agent knows
  about, and a link to the most recent one that is on the sheet (a sent
  marker has no row and no subject, so there is nothing of it to open).

**Sent mail is markers, not rows.** The agent reads the ids, thread ids and
timestamps of what she sent — never a subject, a recipient or a body — so
that "who sent the last message" is a fact. A marker
(:func:`from_marker`) is a :class:`ThreadMessage` with ``on_sheet`` false and
nothing in it but a time: it decides the Status, the Waiting-since and the
Mails count, and contributes nothing to the Title, Type, Next action or Due.
A marker whose timestamp has not been read yet is not a message at all — it
is dropped, so a half-ingested thread reads exactly as it did before rather
than wrongly.

What never becomes a process: a thread whose every message is a newsletter,
promotion or automated alert (:data:`NOISE_CATEGORIES`) — one alert inside a
real conversation does not hide it, but a thread made only of them is not
work; and a thread with no row on the sheet at all, which is mail she sent
into a conversation the agent never ingested and can therefore neither name
nor describe. What stops being one: a thread whose every row she has marked
Done or Ignore, and a thread silent for :data:`CLOSED_AFTER_SILENT_DAYS`
days.

Phase 2 (a model merging several threads into one process, a "where it
stands" line, a suggested next step) replaces :func:`group` and
:func:`describe` and nothing else: every rule below reads a
:class:`ThreadMessage` and writes a :class:`Process`, so a model that emits
the same :class:`Process` fits without touching the sheet, the store or the
pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .sheet_layout import (
    CATEGORY_KEYS, CATEGORY_LABELS, COL_CATEGORY, COL_DATE, COL_DEADLINE, COL_FROM,
    COL_MESSAGE_ID, COL_STATUS, COL_ACTION, COL_SUBJECT, NO_ACTION, STATUS_DONE,
    WORKTREE_HEADERS, message_link,
)
from .triage import NEEDS_REVIEW, TEAM_TIMEZONE

#: Silence this long is something to act on, whichever side owes the reply.
CHASE_SILENCE_DAYS = 3
#: A thread nobody has touched for this long is over, whatever its rows say.
#: 30 days: longer than any follow-up cadence either live inbox shows, and
#: short enough that the tab stays a work list rather than an archive.
CLOSED_AFTER_SILENT_DAYS = 30
#: Her Status values that close a row. ``Done`` is the layout's; ``Ignore``
#: is the fourth dropdown option and means the same thing here.
CLOSED_ROW_STATUSES = frozenset({STATUS_DONE.lower(), "ignore"})
#: Categories that are never work on their own.
NOISE_CATEGORIES = frozenset({"newsletter_promo", "notification"})
#: Most demanding first. The thread's Type is the first of these it contains.
TYPE_PRIORITY: tuple[str, ...] = (
    "action_required", "reply_needed", "finance", "meeting",
    "fyi", "other", "notification", "newsletter_promo", NEEDS_REVIEW,
)

STATUS_WAITING_ON_US = "Waiting on us"
STATUS_WAITING_ON_THEM = "Waiting on them"
#: They owe us a reply and have been quiet: go after them.
STATUS_CHASING = "Chasing"
#: WE owe them a reply and have been quiet, and she has marked nothing in the
#: thread Done or Ignore — so this is not a thing she decided to let go, it is
#: a thing she has not got to. It is the same alarm as a chase, pointed the
#: other way, and it is the one the live inboxes will actually raise.
STATUS_OVERDUE_REPLY = "Overdue reply"
#: Both alarms. These sort above everything and share the red row.
CHASE_STATUSES = frozenset({STATUS_CHASING, STATUS_OVERDUE_REPLY})

#: The most rows the tab is ever given. The sort puts what needs chasing on
#: top, so a truncation loses the quietest end of the list — and the count is
#: recorded on the connection document either way, never guessed at.
MAX_PROCESSES = 200

#: ``Re:``, ``Fwd:``, ``FW:``, ``RE[2]:``, ``Antwort:``, ``Re :`` … repeated.
_REPLY_PREFIX = re.compile(
    r"^\s*(?:re|aw|antwort|fwd?|vs|sv|rif|res|odp|encaminhada)\s*(?:\[\d+\])?\s*:\s*",
    re.IGNORECASE,
)
_ANGLE_ADDRESS = re.compile(r"<([^<>]+)>")


def strip_reply_prefixes(subject: str) -> str:
    """``"Re: Fwd: RE[2]: Lease"`` → ``"Lease"``. Whitespace collapsed."""
    text = " ".join(str(subject or "").split())
    while True:
        stripped = _REPLY_PREFIX.sub("", text, count=1)
        if stripped == text:
            return text.strip()
        text = stripped


def email_address(header: str) -> str:
    """The bare address out of a ``From``/``To`` header, lowercased. A header
    with no angle brackets is taken whole, because that is what a bare
    address looks like. ``""`` when there is nothing to read."""
    text = str(header or "").strip()
    match = _ANGLE_ADDRESS.search(text)
    if match:
        text = match.group(1)
    text = text.strip().strip("<>").strip()
    return text.lower() if "@" in text else ""


def sent_by(sender_header: str, connected_address: str) -> bool:
    """Did the connected mailbox itself send this? Address comparison only —
    a display name is not identity."""
    wanted = email_address(connected_address) or str(connected_address or "").strip().lower()
    address = email_address(sender_header)
    return bool(wanted) and bool(address) and address == wanted


@dataclass(frozen=True)
class ThreadMessage:
    """One tracked message, as the rules need it. ``user_status`` is her own
    Status cell, verbatim; ``category`` is a triage key (or ``""`` for a row
    that was never triaged).

    ``on_sheet`` is false for a **sent marker**: mail she sent, known only by
    its id, thread and time. It carries no subject, no category, no action and
    no deadline — not because they were dropped, but because they were never
    read."""

    message_id: str
    thread_id: str
    received_at: datetime | None
    subject: str
    category: str
    action: str | None
    deadline: date | None
    from_me: bool
    user_status: str = ""
    on_sheet: bool = True

    @property
    def closed(self) -> bool:
        return str(self.user_status or "").strip().lower() in CLOSED_ROW_STATUSES

    @property
    def open_action(self) -> str:
        text = str(self.action or "").strip()
        if not text or text == NO_ACTION or self.closed:
            return ""
        return text


@dataclass(frozen=True)
class Process:
    """One row of the Worktree tab, before it is turned into cells."""

    thread_id: str
    title: str
    type_label: str
    status: str
    next_action: str
    waiting_days: int
    due: date | None
    mails: int
    latest_link: str

    @property
    def chasing(self) -> bool:
        """Is this one of the two alarms — someone owes a reply and the
        thread has gone quiet? Which side owes it is in :attr:`status`."""
        return self.status in CHASE_STATUSES

    @property
    def waiting_since(self) -> str:
        if self.waiting_days <= 0:
            return "today"
        return "1 day" if self.waiting_days == 1 else f"{self.waiting_days} days"

    def sort_key(self) -> tuple:
        """Both alarms first — whoever owes the reply — then anything with a
        due date (earliest first, so overdue leads), then the rest
        longest-waiting first. The title breaks every tie, so two builds of
        the same facts produce byte-identical rows, which is what lets the
        pipeline skip a rewrite."""
        bucket = 0 if self.chasing else (1 if self.due is not None else 2)
        due_key = self.due.toordinal() if self.due is not None else 0
        return (bucket, due_key, -self.waiting_days, self.title.lower(), self.thread_id)


def group(messages: list[ThreadMessage]) -> dict[str, list[ThreadMessage]]:
    """Messages by thread id, each thread oldest first. A message with no
    thread id belongs to no process yet (its id has not been backfilled) and
    is dropped here — the caller counts those separately."""
    threads: dict[str, list[ThreadMessage]] = {}
    for message in messages:
        thread_id = str(message.thread_id or "").strip()
        if thread_id:
            threads.setdefault(thread_id, []).append(message)
    for thread in threads.values():
        thread.sort(key=_chronological)
    return threads


def _chronological(message: ThreadMessage) -> tuple:
    """Oldest first. A message whose Date cell could not be read sorts before
    every dated one rather than pretending to be now."""
    received = message.received_at
    return (1, received.timestamp(), message.message_id) if received else (0, 0.0, message.message_id)


def rows_of(thread: list[ThreadMessage]) -> list[ThreadMessage]:
    """The thread's messages that have a row on the sheet — everything the
    triage actually read. The rest are sent markers."""
    return [m for m in thread if m.on_sheet]


def is_noise(thread: list[ThreadMessage]) -> bool:
    """Every row of it is a newsletter, a promotion or an automated alert,
    and she never wrote into it. Answering something makes it a
    conversation, whatever its rows were classified as."""
    rows = rows_of(thread)
    if not rows:
        return False  # judged by :func:`has_row` instead
    return all(m.category in NOISE_CATEGORIES for m in rows) and not any(
        m.from_me for m in thread
    )


def has_row(thread: list[ThreadMessage]) -> bool:
    """Is any of this on the sheet? A thread of nothing but sent markers is
    mail she sent into a conversation the agent never ingested: it has no
    subject to name it and no triage to describe it, so it is not a process."""
    return any(m.on_sheet for m in thread)


def is_closed(thread: list[ThreadMessage], *, today: date) -> bool:
    """Her bookkeeping lives on rows, so "all done" is judged on rows; going
    quiet is judged on the whole thread, her own replies included."""
    if not thread:
        return True
    rows = rows_of(thread)
    if rows and all(m.closed for m in rows):
        return True
    return _waiting_days(thread[-1], today=today) >= CLOSED_AFTER_SILENT_DAYS


def _waiting_days(last: ThreadMessage, *, today: date) -> int:
    if last.received_at is None:
        return 0
    return max(0, (today - last.received_at.date()).days)


def _title(thread: list[ThreadMessage]) -> str:
    for message in thread:  # oldest first — Gmail names a thread the same way
        title = strip_reply_prefixes(message.subject)
        if title:
            return title
    for message in reversed(thread):
        title = strip_reply_prefixes(message.subject)
        if title:
            return title
    return "(no subject)"


def _type_label(thread: list[ThreadMessage]) -> str:
    present = {m.category for m in thread if m.category}
    for category in TYPE_PRIORITY:
        if category in present:
            return CATEGORY_LABELS.get(category, category)
    return ""


def _status(last: ThreadMessage, thread: list[ThreadMessage], *, waiting_days: int) -> str:
    """Who owes the next move, and whether the wait has become an alarm.

    Pinned, in full: the LAST message decides the side. Sent by the connected
    mailbox → they owe us, and ``CHASE_SILENCE_DAYS`` of silence makes it a
    ``Chasing``. Sent by anyone else → we owe them, and the same silence makes
    it an ``Overdue reply`` — unless she has marked some row of the thread
    Done or Ignore, which means she has dealt with it and does not need
    telling twice."""
    quiet = waiting_days >= CHASE_SILENCE_DAYS
    if last.from_me:
        return STATUS_CHASING if quiet else STATUS_WAITING_ON_THEM
    if quiet and not any(m.closed for m in rows_of(thread)):
        return STATUS_OVERDUE_REPLY
    return STATUS_WAITING_ON_US


def _next_action(thread: list[ThreadMessage]) -> str:
    for message in reversed(thread):  # newest first
        action = message.open_action
        if action:
            return action
    return ""


def _due(thread: list[ThreadMessage]) -> date | None:
    open_deadlines = [m.deadline for m in thread if m.deadline and not m.closed]
    return min(open_deadlines) if open_deadlines else None


def _latest_link(thread: list[ThreadMessage]) -> str:
    """The newest message of the thread that is on the sheet. Gmail opens the
    whole conversation from any message in it, and a sent marker has no row
    and no link of its own, so this always resolves to something she can
    open."""
    for message in reversed(rows_of(thread)):
        return message_link(message.message_id)
    return ""


def describe(thread_id: str, thread: list[ThreadMessage], *, today: date) -> Process:
    """One thread's :class:`Process`. The caller has already decided the
    thread has a row and is neither noise nor closed."""
    last = thread[-1]
    waiting_days = _waiting_days(last, today=today)
    return Process(
        thread_id=thread_id,
        title=_title(thread),
        type_label=_type_label(thread),
        status=_status(last, thread, waiting_days=waiting_days),
        next_action=_next_action(thread),
        waiting_days=waiting_days,
        due=_due(thread),
        mails=len(thread),
        latest_link=_latest_link(thread),
    )


@dataclass(frozen=True)
class Worktree:
    """What one build produced. ``total`` is every open process;
    ``processes`` is the sorted prefix that fits :data:`MAX_PROCESSES`."""

    processes: list[Process]
    total: int
    #: Tracked messages with no thread id yet — the backfill has not reached
    #: them, so they are in no process. Reported, never silently dropped.
    untagged: int = 0
    #: Sent markers that made it into a process, i.e. how much of the Status
    #: column is standing on read facts rather than on the absence of them.
    sent_used: int = 0


def build(messages: list[ThreadMessage], *, today: date) -> Worktree:
    """Every open process, most in need of chasing first."""
    untagged = sum(1 for m in messages if not str(m.thread_id or "").strip())
    processes: list[Process] = []
    sent_used = 0
    for thread_id, thread in group(messages).items():
        if not has_row(thread) or is_noise(thread) or is_closed(thread, today=today):
            continue
        processes.append(describe(thread_id, thread, today=today))
        sent_used += sum(1 for m in thread if not m.on_sheet)
    processes.sort(key=Process.sort_key)
    return Worktree(
        processes=processes[:MAX_PROCESSES], total=len(processes),
        untagged=untagged, sent_used=sent_used,
    )


# --------------------------------------------------------------------------- #
# Reading the Inbox tab back, and writing the Worktree tab's cells
# --------------------------------------------------------------------------- #

#: How ``sheet_layout.agent_values`` writes the Date cell.
DATE_CELL_FORMAT = "%Y-%m-%d %H:%M"
#: 0-based positions of the cells a process is derived from, by name, so a
#: column that moves moves them with it.
_CELL = {
    name: col - 1 for name, col in (
        ("date", COL_DATE), ("sender", COL_FROM), ("subject", COL_SUBJECT),
        ("category", COL_CATEGORY), ("action", COL_ACTION), ("deadline", COL_DEADLINE),
        ("message_id", COL_MESSAGE_ID), ("user_status", COL_STATUS),
    )
}


def _cell(cells: list[str], name: str) -> str:
    index = _CELL[name]
    return str(cells[index]).strip() if index < len(cells) else ""


def _received_at(cell: str) -> datetime | None:
    try:
        return datetime.strptime(cell, DATE_CELL_FORMAT).replace(tzinfo=TEAM_TIMEZONE)
    except ValueError:
        return None


def _deadline(cell: str) -> date | None:
    try:
        return date.fromisoformat(cell)
    except ValueError:
        return None


def from_row(
    cells: list[str], *, connected_address: str, thread_ids: dict[str, str]
) -> ThreadMessage | None:
    """One Inbox row → a :class:`ThreadMessage`, or ``None`` when the row
    carries no message id (a blank row, or one she typed herself).

    The Inbox tab is the live record: she may correct a Deadline or an Action
    by hand, and the Worktree follows her. The one fact no column holds is the
    thread id, which comes from ``thread_ids`` — the message's tracking
    document. A cell that cannot be read (a Date she reformatted, a Category
    she renamed) reads as missing, never as a guess."""
    message_id = _cell(cells, "message_id")
    if not message_id:
        return None
    label = _cell(cells, "category")
    action = _cell(cells, "action")
    return ThreadMessage(
        message_id=message_id,
        thread_id=str(thread_ids.get(message_id) or ""),
        received_at=_received_at(_cell(cells, "date")),
        subject=_cell(cells, "subject"),
        category=CATEGORY_KEYS.get(label, "other" if label else ""),
        action=action or None,
        deadline=_deadline(_cell(cells, "deadline")),
        from_me=sent_by(_cell(cells, "sender"), connected_address),
        user_status=_cell(cells, "user_status"),
    )


#: Marks a tracking document as a sent marker rather than a sheet row. A
#: document written before sent mail was read has no ``kind`` at all, and is
#: a row — the field is only ever added, never back-filled onto old ones.
KIND_SENT = "sent"


def is_sent_marker(doc: dict) -> bool:
    return str((doc or {}).get("kind") or "") == KIND_SENT


def from_marker(doc: dict) -> ThreadMessage | None:
    """A sent marker's tracking document → a :class:`ThreadMessage` that is
    nothing but a time and a side.

    ``None`` when the marker has no timestamp yet: the listing found it but
    the stamp read has not reached it. Dropping it is deliberate — a marker
    with no time cannot be compared against anything, and a message that
    sorts as "oldest" would silently answer "who sent the last one?" wrongly.
    The thread then reads exactly as it did before the marker existed, and
    flips the moment the stamp lands."""
    message_id = str((doc or {}).get("message_id") or "")
    thread_id = str((doc or {}).get("thread_id") or "")
    received_at = _stamp(doc.get("received_at"))
    if not message_id or not thread_id or received_at is None:
        return None
    return ThreadMessage(
        message_id=message_id, thread_id=thread_id, received_at=received_at,
        subject="", category="", action=None, deadline=None,
        from_me=True, user_status="", on_sheet=False,
    )


def _stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TEAM_TIMEZONE)


def values(process: Process) -> list[str]:
    """One process as cells, in :data:`sheet_layout.WORKTREE_HEADERS` order.
    Everything is a string written RAW: a Process title is a subject line,
    which is third-party text, and ``USER_ENTERED`` would evaluate one that
    begins with ``=``."""
    cells = {
        "Process": process.title,
        "Type": process.type_label,
        "Status": process.status,
        "Next action": process.next_action,
        "Waiting since": process.waiting_since,
        "Due": process.due.isoformat() if process.due else "",
        "Mails": str(process.mails),
        "Latest": process.latest_link,
    }
    return [cells[name] for name in WORKTREE_HEADERS]
