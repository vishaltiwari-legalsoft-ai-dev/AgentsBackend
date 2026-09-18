"""Inbox Triage (a12) — one recruiter's Gmail inbox, one row per message in a
sheet she owns.

Read-only against Gmail (``gmail.readonly``), per-user OAuth, never a shared
mailbox credential. The sheet is her dashboard; Firestore is the record and
the checkpoint. The hub never stores a mail body.

``INBOX_OFFLINE=1`` — the test suite's default, set in ``backend/conftest.py``
— makes every external seam (the token exchange and refresh, the Gmail and
Sheets clients, the model) raise :class:`InboxOffline` before a socket is
opened. A test reaches those paths through fakes at the module seams, never by
clearing the flag.
"""

from __future__ import annotations

import hashlib
import os

AGENT_ID = "a12"
AGENT_NAME = "Inbox Triage"


class InboxOffline(RuntimeError):
    """An external call was attempted while ``INBOX_OFFLINE=1``."""


def offline() -> bool:
    return os.environ.get("INBOX_OFFLINE") == "1"


def refuse_if_offline(what: str) -> None:
    """The guard every external seam calls first."""
    if offline():
        raise InboxOffline(f"{what} is unavailable while INBOX_OFFLINE=1.")


def user_label(user_id: str) -> str:
    """A stable, non-reversible handle for logs and the cron envelope. The id
    is internal, but neither the scheduler's logs nor ours need ids at all."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]
