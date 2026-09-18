"""Deadlines and bounded retry for every Google API client this service builds.

Lifted out of ``marketing_research_agent.sources.sheets_source`` (2026-09-18)
when the Inbox Triage agent (a12) needed the same four things for Gmail and
for a Sheets *writer*: ADC resolved once per process, a token refresh that
does not inherit google-auth's 120s default, an httplib2 transport with a
socket deadline, and an ``execute()`` that rides out Google's own
back-pressure without disguising a real refusal. Copying 180 lines into a
second agent would have been two places for the next deadline bug to hide.
``sheets_source`` imports and re-exports these names, so its callers and its
tests are unchanged.

Why deadlines at all: every Google call here runs inside a *sync* FastAPI
handler, i.e. on anyio's worker threadpool — 40 slots for the whole process.
A call that stalls does not merely fail slowly, it takes a slot out of
circulation; ~40 stalled calls wedge the service including ``/api/health``.
So each transport gets a deadline we chose, not one we inherited.
``tests/test_google_client_deadline_sweep.py`` holds every ``build(...)`` in
the repo to that.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Sequence

logger = logging.getLogger("agentos.google_http")

#: Token mint/refresh against oauth2.googleapis.com (or the metadata server):
#: a tiny request with no reason to be slow. google-auth's own default is 120s.
AUTH_TIMEOUT_SECONDS = 10


class _TimedRequest:
    """google-auth transport wrapper that stamps our deadline onto token calls.

    ``creds.refresh(request)`` never passes a timeout, so google-auth's 120s
    default applies. Wrapping the transport is the only seam that reaches it.
    """

    def __init__(self, inner: Callable, timeout: float):
        self._inner = inner
        self._timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        return self._inner(
            url, method=method, body=body, headers=headers,
            timeout=self._timeout if timeout is None else timeout, **kwargs,
        )


# ADC resolution is not free — it reads the key file or queries the Cloud Run
# metadata server — and the MR sheet fetcher used to redo it for *every tab*,
# so one 6-tab ingest paid for it six times. Credentials are safe to share:
# the httplib2 transport built on top of them is what is per-client (below).
_creds_cache: dict[tuple[str, ...], object] = {}
_creds_lock = threading.Lock()
_refresh_lock = threading.Lock()


def cached_credentials(scopes: Sequence[str]):
    """ADC credentials for a scope set, resolved once per process."""
    key = tuple(sorted(scopes))
    creds = _creds_cache.get(key)
    if creds is not None:
        return creds
    import google.auth

    with _creds_lock:
        creds = _creds_cache.get(key)
        if creds is None:
            creds, _ = google.auth.default(scopes=list(key))
            _creds_cache[key] = creds
    return creds


def refresh_if_stale(creds) -> None:
    """Mint a token only when the cached one is missing or expired."""
    if getattr(creds, "valid", False):
        return
    from google.auth.transport.requests import Request

    with _refresh_lock:
        if getattr(creds, "valid", False):  # another thread beat us to it
            return
        creds.refresh(_TimedRequest(Request(), AUTH_TIMEOUT_SECONDS))


def timed_http(creds, timeout: float):
    """An authorised httplib2 transport carrying an explicit socket timeout.

    ``build(credentials=...)`` constructs its own transport internally, so
    passing ``http=`` is the only way to state a deadline. A *fresh*
    ``httplib2.Http`` per client is deliberate and not an oversight: httplib2
    is not thread-safe, and these clients are used from FastAPI worker threads.
    Building one costs nothing (the discovery document is bundled statically in
    google-api-python-client, so `build` opens no socket).
    """
    import google_auth_httplib2
    import httplib2

    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))


#: HTTP statuses worth another attempt — Google's own back-pressure and its
#: transient 5xx. A 403 or 404 is an answer, not a blip, and retrying it just
#: spends the cron's budget arriving at the same place.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Attempts per call, including the first. Three is chosen against the
#: callers, not in the abstract: the MR cron fires every few minutes and the
#: a12 poll every five, so the worst case here (3 x a 30s deadline plus
#: backoff, ~95s) still finishes inside one fire's budget.
_RETRY_ATTEMPTS = 3

#: Waits *between* attempts, so len() is _RETRY_ATTEMPTS - 1. Short on purpose:
#: a socket timeout has already spent 30s, and the thing being ridden out is a
#: blip rather than an outage.
_RETRY_BACKOFF_SECONDS = (1.0, 3.0)


def _is_transient(exc: BaseException) -> bool:
    """Whether *exc* is the kind of failure a second attempt might survive.

    ``TimeoutError`` covers ``socket.timeout``, which is what httplib2 raises
    when the deadline in :func:`timed_http` expires — and it is what the MR cron
    actually hits ("workbook unreadable: The read operation timed out", roughly
    4% of fires). ``OSError`` catches the connection resets and DNS blips
    underneath it; ``TimeoutError`` is already an ``OSError``, but naming it
    keeps the intent legible.
    """
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        return getattr(exc.resp, "status", None) in _RETRYABLE_STATUS
    return isinstance(exc, (TimeoutError, OSError))


def execute_with_retry(request, *, what: str, unavailable: type[Exception]):
    """``request.execute()`` that rides out a transient failure.

    Retries are deliberately *not* pushed down into ``num_retries`` on
    ``execute()``: that handles retryable HTTP statuses but not the socket
    timeout raised by the transport, which is the failure actually being seen.

    A non-transient error is re-raised immediately and unchanged — callers
    distinguish "the API refused us" from "the workbook is empty" and that
    distinction must survive this wrapper. When every attempt fails,
    ``unavailable`` is raised with the count and the last cause, so the caller
    gets its own exception class rather than a generic one.
    """
    last: BaseException | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return request.execute()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless transient
            if not _is_transient(exc):
                raise
            last = exc
            if attempt < len(_RETRY_BACKOFF_SECONDS):
                logger.warning(
                    "%s failed (%s); retrying in %ss (attempt %d/%d)",
                    what, exc, _RETRY_BACKOFF_SECONDS[attempt],
                    attempt + 2, _RETRY_ATTEMPTS,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    raise unavailable(f"{what} failed after {_RETRY_ATTEMPTS} attempts: {last}") from last
