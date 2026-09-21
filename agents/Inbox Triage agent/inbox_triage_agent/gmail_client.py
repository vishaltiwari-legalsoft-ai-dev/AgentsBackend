"""The Gmail reads: profile, inbox listing, history since a checkpoint, and one
message parsed into the facts the row and the model need.

Every client is built with ``timed_http`` so a stalled call has a deadline of
its own, and every ``execute`` rides through ``execute_with_retry`` so
Google's back-pressure is a delay rather than a failed fire. The body text
is decoded here and handed on; it is never logged and never stored — the
model sees it once and the sheet gets a summary.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.google_http import execute_with_retry, timed_http

from . import refuse_if_offline
from .triage import TEAM_TIMEZONE, html_to_text

#: Socket deadline for each Gmail call — see ``app.services.google_http``.
GMAIL_TIMEOUT_SECONDS = 30
INBOX_LABEL = "INBOX"
#: Mail the connected mailbox SENT. Read for thread ids and timestamps only —
#: never fetched in full, never summarised, never written to the sheet. It is
#: what makes "who sent the last message" a fact instead of a guess.
SENT_LABEL = "SENT"
#: Gmail lists up to 500 ids per page; 100 keeps one page inside one fire's
#: fetch budget so an unfinished page is re-listed cheaply next time.
LIST_PAGE_MAX = 100
#: The thread-id backfill fetches nothing and summarises nothing, so it takes
#: Gmail's whole page: 500 ``(id, thread id)`` pairs for one call's quota.
LIST_THREAD_PAGE_MAX = 500
#: History pages one fire will follow. More than this between two five-minute
#: fires means the checkpoint is not worth trusting; the caller re-lists.
HISTORY_PAGE_CAP = 20


class GmailUnavailable(RuntimeError):
    """A Gmail call could not be completed, or was refused."""


class HistoryExpired(RuntimeError):
    """The checkpoint is too old for ``history.list`` (Gmail answers 404) or
    the history is too long to follow — the caller falls back to listing."""


class MessageGone(RuntimeError):
    """The message no longer exists (deleted between listing and fetch)."""


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    received_at: datetime  # Gmail's internalDate, in triage.TEAM_TIMEZONE
    from_: str
    to: str
    date_header: str
    subject: str
    body_text: str


def service(creds):
    refuse_if_offline("Gmail")
    from googleapiclient.discovery import build

    return build(
        "gmail", "v1", http=timed_http(creds, GMAIL_TIMEOUT_SECONDS), cache_discovery=False
    )


def _status_of(exc: BaseException) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _run(request, *, what: str, on_404: type[Exception] | None = None):
    """``execute`` with the retry, and every refusal mapped to this module's
    exceptions so callers never see a raw ``HttpError``."""
    from googleapiclient.errors import HttpError

    try:
        return execute_with_retry(request, what=f"Gmail {what}", unavailable=GmailUnavailable)
    except HttpError as exc:
        status = _status_of(exc)
        if status == 404 and on_404 is not None:
            raise on_404(f"Gmail {what}: not found") from exc
        raise GmailUnavailable(f"Gmail {what} was refused: HTTP {status}") from exc


def profile(service) -> dict:
    """``{email, history_id, messages_total}`` — the address the row's link
    opens and the checkpoint a fresh connection starts from."""
    data = _run(service.users().getProfile(userId="me"), what="profile")
    return {
        "email": str(data.get("emailAddress") or ""),
        "history_id": str(data.get("historyId") or ""),
        "messages_total": int(data.get("messagesTotal") or 0),
    }


def _list_pairs(
    service, *, label: str, what: str, after_epoch: int, page_token: str | None,
    max_results: int,
) -> tuple[list[tuple[str, str]], str | None, int]:
    """One page of ``(message id, thread id)`` under ``label``, newer than
    ``after_epoch`` (seconds), newest first, with ``(next_page_token,
    result_size_estimate)``.

    The listing already carries ``threadId`` on every entry — this is the one
    Gmail read that answers "which thread is this message in?" in bulk, at up
    to :data:`LIST_THREAD_PAGE_MAX` messages per call instead of one
    ``messages.get`` each. That is what makes both backfills a matter of
    pages rather than thousands of calls."""
    data = _run(
        service.users().messages().list(
            userId="me",
            labelIds=[label],
            q=f"after:{int(after_epoch)}",
            maxResults=max(1, min(int(max_results), 500)),
            pageToken=page_token or None,
        ),
        what=what,
    )
    pairs = [
        (str(m["id"]), str(m.get("threadId") or ""))
        for m in data.get("messages") or [] if m.get("id")
    ]
    return pairs, (data.get("nextPageToken") or None), int(data.get("resultSizeEstimate") or 0)


def list_inbox_pairs(
    service, *, after_epoch: int, page_token: str | None = None, max_results: int = LIST_PAGE_MAX
) -> tuple[list[tuple[str, str]], str | None, int]:
    """Inbox label only, which includes Promotions and Social and excludes
    Spam and Trash. See :func:`_list_pairs`."""
    return _list_pairs(
        service, label=INBOX_LABEL, what="inbox listing", after_epoch=after_epoch,
        page_token=page_token, max_results=max_results,
    )


def list_sent_pairs(
    service, *, after_epoch: int, page_token: str | None = None,
    max_results: int = LIST_THREAD_PAGE_MAX,
) -> tuple[list[tuple[str, str]], str | None, int]:
    """The same page, over mail the connected mailbox SENT. Ids and thread
    ids only: nothing here reads a subject, a recipient or a body."""
    return _list_pairs(
        service, label=SENT_LABEL, what="sent listing", after_epoch=after_epoch,
        page_token=page_token, max_results=max_results,
    )


def stamp(service, message_id: str) -> datetime:
    """When one message arrived, and nothing else.

    ``format="minimal"`` is the smallest thing Gmail will answer with: ids,
    labels and ``internalDate``, no headers and no body. It is what a sent
    marker needs and the most a sent marker is ever allowed to know — the
    subject, the recipients and the text of her own mail are never read.
    Raises :class:`MessageGone` if it no longer exists."""
    data = _run(
        service.users().messages().get(userId="me", id=message_id, format="minimal"),
        what="message stamp",
        on_404=MessageGone,
    )
    internal_ms = int(data.get("internalDate") or 0)
    if not internal_ms:
        raise MessageGone(f"Gmail message stamp: {message_id} carries no date")
    return datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).astimezone(TEAM_TIMEZONE)


def list_inbox(
    service, *, after_epoch: int, page_token: str | None = None, max_results: int = LIST_PAGE_MAX
) -> tuple[list[str], str | None, int]:
    """:func:`list_inbox_pairs` with the thread ids dropped — what the mail
    passes want. One HTTP path, so there is nothing for the two to disagree
    about."""
    pairs, token, estimate = list_inbox_pairs(
        service, after_epoch=after_epoch, page_token=page_token, max_results=max_results
    )
    return [message_id for message_id, _thread_id in pairs], token, estimate


def history_since(service, history_id: str) -> tuple[list[str], str]:
    """``(message ids added to the inbox since the checkpoint, new checkpoint)``.

    A 404 means Gmail no longer holds history back to that id — it keeps
    roughly a week — and is raised as :class:`HistoryExpired` so the caller
    re-lists a window instead. An unterminated pager is raised the same way:
    the ``historyId`` on every page is the mailbox's CURRENT id, so stopping
    early and saving it would silently skip whatever the unread pages held.
    """
    added: list[str] = []
    seen: set[str] = set()
    token: str | None = None
    latest = str(history_id)
    for _ in range(HISTORY_PAGE_CAP):
        data = _run(
            service.users().history().list(
                userId="me",
                startHistoryId=str(history_id),
                historyTypes=["messageAdded"],
                labelId=INBOX_LABEL,
                pageToken=token,
            ),
            what="history",
            on_404=HistoryExpired,
        )
        latest = str(data.get("historyId") or latest)
        for record in data.get("history") or []:
            for item in record.get("messagesAdded") or []:
                message = item.get("message") or {}
                message_id = str(message.get("id") or "")
                labels = message.get("labelIds") or []
                if not message_id or message_id in seen:
                    continue
                if labels and INBOX_LABEL not in labels:
                    continue  # already archived; not an inbox row
                seen.add(message_id)
                added.append(message_id)
        token = data.get("nextPageToken") or None
        if not token:
            return added, latest
    raise HistoryExpired(
        f"Gmail history ran past {HISTORY_PAGE_CAP} pages; re-listing instead"
    )


def fetch(service, message_id: str) -> Message:
    data = _run(
        service.users().messages().get(userId="me", id=message_id, format="full"),
        what="message fetch",
        on_404=MessageGone,
    )
    return parse_message(data)


# --------------------------------------------------------------------------- #
# Parsing — pure, so the shapes Gmail sends are pinned offline
# --------------------------------------------------------------------------- #

_CHARSET = re.compile(r'charset="?([\w.-]+)"?', re.IGNORECASE)


def parse_message(data: dict) -> Message:
    """A ``users.messages.get(format=full)`` resource → :class:`Message`.

    Body: ``text/plain`` parts first; otherwise ``text/html`` through
    :func:`triage.html_to_text`; multipart walked recursively; any part with a
    filename is an attachment and is skipped. A header Gmail did not send
    reads as an empty string, never as a failure.
    """
    payload = data.get("payload") or {}
    headers = _headers(payload)
    internal_ms = int(data.get("internalDate") or 0)
    received_at = (
        datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).astimezone(TEAM_TIMEZONE)
        if internal_ms
        else datetime.now(tz=TEAM_TIMEZONE)
    )
    return Message(
        id=str(data.get("id") or ""),
        thread_id=str(data.get("threadId") or ""),
        received_at=received_at,
        from_=headers.get("from", ""),
        to=headers.get("to", ""),
        date_header=headers.get("date", ""),
        subject=headers.get("subject", ""),
        body_text=_body_text(payload),
    )


def _headers(payload: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(header.get("value") or "")
    return out


def _body_text(payload: dict) -> str:
    plain: list[str] = []
    html: list[str] = []
    _walk(payload, plain, html)
    if plain:
        return "\n".join(plain)
    if html:
        return html_to_text("\n".join(html))
    return ""


def _walk(part: dict, plain: list[str], html: list[str]) -> None:
    if part.get("filename"):
        return  # an attachment, whatever its MIME type says
    mime = str(part.get("mimeType") or "").lower()
    data = (part.get("body") or {}).get("data")
    if data and mime == "text/plain":
        plain.append(_decode(data, part))
    elif data and mime == "text/html":
        html.append(_decode(data, part))
    for sub in part.get("parts") or []:
        _walk(sub, plain, html)


def _decode(data: str, part: dict) -> str:
    """One body part as text. A part whose data is not valid base64url reads
    as empty: the message is still a row (headers, and whatever other parts
    decode), because raising here would stop the checkpoint at this message
    and fail every later fire for the inbox."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError, TypeError):
        return ""
    charset = "utf-8"
    for header in part.get("headers") or []:
        if str(header.get("name") or "").lower() == "content-type":
            match = _CHARSET.search(str(header.get("value") or ""))
            if match:
                charset = match.group(1)
            break
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
