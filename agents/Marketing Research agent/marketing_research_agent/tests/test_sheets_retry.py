"""Transient Sheets failures must not fail a whole cron fire.

The MR cron reads the workbook in two Sheets calls — a ``spreadsheets().get``
for the tab list and one ``values().batchGet`` across every tab. The batch call
is big enough to occasionally overrun its 30s deadline, and there was no retry
at all, so one blip failed the fire and that tick's snapshot never happened.
Live rate on 2026-08-26: 3 failed fires out of 71, all
``workbook unreadable: The read operation timed out``.

These tests pin the three things that matter: a blip is ridden out, a real
error is not disguised as one, and the retry is bounded rather than unbounded.
"""
from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from marketing_research_agent.sources import sheets_source as ss


class _Resp:
    """Minimal stand-in for the ``resp`` on a googleapiclient HttpError."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = f"status {status}"


class _Request:
    """A Sheets request whose ``execute`` fails a set number of times first."""

    def __init__(self, failures: list[BaseException], payload=None) -> None:
        self._failures = list(failures)
        self._payload = payload if payload is not None else {"ok": True}
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return self._payload


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """The backoff is real seconds in production and must not be here."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def test_a_socket_timeout_is_ridden_out():
    """The exact live failure: httplib2 raises TimeoutError on deadline."""
    request = _Request([TimeoutError("The read operation timed out")])

    assert ss._execute(request, what="batch read") == {"ok": True}
    assert request.calls == 2, "should have retried exactly once"


def test_two_timeouts_still_succeed_on_the_third_attempt():
    request = _Request([TimeoutError("timed out"), TimeoutError("timed out")])

    assert ss._execute(request, what="batch read") == {"ok": True}
    assert request.calls == ss._RETRY_ATTEMPTS


def test_retries_are_bounded_and_end_in_sheets_unavailable():
    """An outage must stop, not spin — the cron fires again in 3 minutes."""
    request = _Request([TimeoutError("timed out")] * 10)

    with pytest.raises(ss.SheetsUnavailable) as exc:
        ss._execute(request, what="batch read")

    assert request.calls == ss._RETRY_ATTEMPTS
    assert "after 3 attempts" in str(exc.value)


@pytest.mark.parametrize("status", sorted(ss._RETRYABLE_STATUS))
def test_googles_back_pressure_is_retried(status: int):
    request = _Request([HttpError(_Resp(status), b"slow down")])

    assert ss._execute(request, what="batch read") == {"ok": True}
    assert request.calls == 2


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_real_error_is_raised_immediately_and_unchanged(status: int):
    """403 is an answer, not a blip.

    Retrying it burns the cron's budget arriving at the same place, and — more
    importantly — callers distinguish "the API refused us" from "the workbook is
    empty". That distinction has to survive this wrapper, so the original
    HttpError propagates rather than becoming a SheetsUnavailable.
    """
    original = HttpError(_Resp(status), b"nope")
    request = _Request([original])

    with pytest.raises(HttpError) as exc:
        ss._execute(request, what="tab read")

    assert exc.value is original
    assert request.calls == 1, "a non-transient error must not be retried"


def test_a_programming_error_is_not_swallowed():
    request = _Request([ValueError("bad range")])

    with pytest.raises(ValueError):
        ss._execute(request, what="tab read")
    assert request.calls == 1


def test_a_clean_call_does_not_retry():
    request = _Request([])

    assert ss._execute(request, what="workbook metadata") == {"ok": True}
    assert request.calls == 1
