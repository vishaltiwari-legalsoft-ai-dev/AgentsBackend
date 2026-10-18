"""Every Google client the app builds must carry a deadline, and every loop
around one must have a ceiling.

These calls run in sync handlers, i.e. on anyio's worker threadpool — 40 slots
for the whole process. A stalled Google call does not just fail slowly, it takes
a slot out of circulation; enough of them wedge the service, /api/health
included. googleapiclient does apply an implicit 60s socket timeout of its own,
but (a) nobody chose that number, (b) it is a *socket* timeout, so a server
dribbling bytes outlives it, and (c) it does nothing at all about the two
unbounded loops here: Drive paging and chunked download.

Fully offline — ADC is stubbed and no client opens a socket (`build` reads the
discovery document bundled inside google-api-python-client).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import drive_source, storage


class _FakeCreds:
    valid = True
    token = "fake-token"


@pytest.fixture()
def stub_adc(monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_FakeCreds(), "p"))


# --------------------------------------------------------------------------- #
# Drive
# --------------------------------------------------------------------------- #

def test_drive_client_carries_an_explicit_timeout(stub_adc):
    svc = drive_source.build_drive_service()
    assert svc._http.http.timeout == drive_source.DRIVE_TIMEOUT_SECONDS


def test_drive_timeout_is_tighter_than_the_implicit_default(stub_adc):
    from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC

    assert 0 < drive_source.DRIVE_TIMEOUT_SECONDS < DEFAULT_HTTP_TIMEOUT_SEC


def test_each_drive_client_gets_its_own_transport(stub_adc):
    """httplib2 is not thread-safe; these are used from worker threads."""
    a, b = drive_source.build_drive_service(), drive_source.build_drive_service()
    assert a._http.http is not b._http.http


class _PagingService:
    """files().list() that never stops handing out a nextPageToken."""

    def __init__(self, pages: int | None):
        self.pages, self.calls = pages, 0

    def files(self):
        return self

    def list(self, **kw):
        self.calls += 1
        more = self.pages is None or self.calls < self.pages
        return _Exec({
            "files": [{"id": f"f{self.calls}", "name": f"n{self.calls}", "mimeType": "image/png"}],
            "nextPageToken": "tok" if more else None,
        })


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def test_drive_paging_stops_instead_of_looping_forever():
    """`while True:` with a server that always returns a page token was an
    infinite loop holding a threadpool slot."""
    service = _PagingService(pages=None)
    with pytest.raises(RuntimeError) as excinfo:
        drive_source._list_children(service, "folder-1")
    assert service.calls == drive_source.MAX_LIST_PAGES
    # Refuses rather than silently returning a partial folder as if complete.
    assert "partial" in str(excinfo.value).lower()


def test_drive_paging_still_returns_normally_under_the_cap():
    service = _PagingService(pages=3)
    children = drive_source._list_children(service, "folder-1")
    assert len(children) == 3 and service.calls == 3


class _Downloader:
    """MediaIoBaseDownload double that never reports done, writing as it goes."""

    def __init__(self, buf, request, chunksize):
        self.buf, self.chunksize = buf, chunksize

    def next_chunk(self):
        self.buf.write(b"\0" * self.chunksize)
        return None, False


def test_drive_download_is_capped_instead_of_looping_forever(monkeypatch):
    import googleapiclient.http as gh

    captured: dict = {}

    def fake_downloader(buf, request, chunksize=gh.DEFAULT_CHUNK_SIZE):
        captured["chunksize"] = chunksize
        return _Downloader(buf, request, chunksize)

    monkeypatch.setattr(gh, "MediaIoBaseDownload", fake_downloader)

    class _Svc:
        def files(self):
            return self

        def get_media(self, **kw):
            return object()

    with pytest.raises(RuntimeError) as excinfo:
        drive_source._download_file(_Svc(), "file-1")
    assert "limit" in str(excinfo.value).lower()
    # And we ask for a chunk small enough to fit inside the socket deadline,
    # rather than googleapiclient's 100MB default.
    assert captured["chunksize"] == drive_source.DOWNLOAD_CHUNK_BYTES
    assert captured["chunksize"] < gh.DEFAULT_CHUNK_SIZE


def test_folder_walk_has_a_depth_ceiling(monkeypatch):
    """Drive folders cannot cycle, but shortcuts can point back up the tree."""
    depth_seen: list[int] = []

    def one_subfolder(service, parent_id):
        depth_seen.append(len(depth_seen))
        return [{"id": "sub", "name": "sub", "mimeType": drive_source._FOLDER_MIME}]

    monkeypatch.setattr(drive_source, "_list_children", one_subfolder)
    written = drive_source._download_folder_recursive(
        object(), "root", Path("."), resolve_type=lambda n: "carousel"
    )
    assert written == 0
    assert len(depth_seen) == drive_source.MAX_FOLDER_DEPTH


# --------------------------------------------------------------------------- #
# Cloud Storage
# --------------------------------------------------------------------------- #

class _Blob:
    def __init__(self, log: dict):
        self.log = log

    def download_as_bytes(self, **kw):
        self.log["download"] = kw
        return b"bytes"

    def upload_from_string(self, data, **kw):
        self.log["upload"] = kw

    def exists(self, **kw):
        self.log["exists"] = kw
        return True

    def delete(self, **kw):
        self.log["delete"] = kw

    def generate_signed_url(self, **kw):
        return "https://signed.example/x"

    name = "brandx/creatives/a.png"


class _Bucket:
    def __init__(self, log: dict):
        self.log = log

    def blob(self, path):
        return _Blob(self.log)

    def list_blobs(self, **kw):
        self.log["list"] = kw
        return [_Blob(self.log)]


@pytest.fixture()
def gcs(monkeypatch):
    log: dict = {}
    monkeypatch.setattr(storage, "_storage", lambda: type("C", (), {"bucket": lambda s, n: _Bucket(log)})())
    monkeypatch.setattr(storage, "_signing_kwargs", dict)
    monkeypatch.setattr(storage.settings, "gcs_bucket_name", "test-bucket")
    return log


def test_gcs_download_states_its_timeout(gcs):
    storage.download_bytes("gs://test-bucket/a/b.png")
    assert gcs["download"]["timeout"] == storage._TRANSFER_TIMEOUT_SECONDS


def test_gcs_upload_states_its_timeout(gcs):
    storage._upload("a/b.png", b"x", "image/png")
    assert gcs["upload"]["timeout"] == storage._TRANSFER_TIMEOUT_SECONDS


def test_gcs_brand_asset_upload_states_its_timeout(gcs):
    storage.upload_brand_asset("b1", "logos", "l.png", b"x", "image/png")
    assert gcs["upload"]["timeout"] == storage._TRANSFER_TIMEOUT_SECONDS


def test_gcs_reference_index_read_states_its_timeouts(gcs):
    storage.read_reference_index()
    assert gcs["exists"]["timeout"] == storage._METADATA_TIMEOUT_SECONDS
    assert gcs["download"]["timeout"] == storage._TRANSFER_TIMEOUT_SECONDS


def test_gcs_bulk_delete_states_its_timeouts(gcs):
    assert storage.delete_all_brand_kit_blobs() == 1
    assert gcs["list"]["timeout"] == storage._METADATA_TIMEOUT_SECONDS
    assert gcs["delete"]["timeout"] == storage._METADATA_TIMEOUT_SECONDS


def test_gcs_deadlines_are_finite_and_sane():
    for name in (
        "_METADATA_TIMEOUT_SECONDS", "_TRANSFER_TIMEOUT_SECONDS", "_AUTH_TIMEOUT_SECONDS",
    ):
        value = getattr(storage, name)
        assert isinstance(value, (int, float)) and 0 < value <= 120, name


def test_signing_token_refresh_overrides_google_auths_120s_default(monkeypatch):
    from google.auth.transport import requests as google_requests

    captured: dict = {}

    def fake_call(self, url, method="GET", body=None, headers=None, timeout=120, **kw):
        captured["timeout"] = timeout

    monkeypatch.setattr(google_requests.Request, "__call__", fake_call)
    storage._TimedRequest()("https://metadata.google.internal/token")
    assert captured["timeout"] == storage._AUTH_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# The shared retry (``google_http.execute_with_retry``): three attempts with
# 1s + 3s between them, as every caller has had since 2026-09-18 — and, only
# when a caller passes a deadline, no attempt that could not finish before it.
# Pinned 2026-10-19: Marketing Research's sheet reads pass no deadline and
# must keep the old behaviour; the Inbox Triage fire passes its own share.
# --------------------------------------------------------------------------- #

class _FailingRequest:
    def __init__(self, error: Exception):
        self.error, self.executes = error, 0

    def execute(self):
        self.executes += 1
        raise self.error


class _Unavailable(RuntimeError):
    pass


@pytest.fixture()
def retry_clock(monkeypatch):
    from app.services import google_http

    clock = {"t": 0.0, "slept": []}
    monkeypatch.setattr(google_http.time, "monotonic", lambda: clock["t"])

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["t"] += seconds
    monkeypatch.setattr(google_http.time, "sleep", sleep)
    return clock


def test_execute_with_retry_without_a_deadline_makes_three_attempts_as_before(retry_clock):
    from app.services import google_http

    req = _FailingRequest(TimeoutError("timed out"))
    with pytest.raises(_Unavailable, match="Sheets read failed after 3 attempts: timed out") as caught:
        google_http.execute_with_retry(req, what="Sheets read", unavailable=_Unavailable)
    assert req.executes == 3 and retry_clock["slept"] == [1.0, 3.0]
    assert caught.value.attempts == 3 and caught.value.deadline_hit is False


def test_execute_with_retry_never_starts_an_attempt_that_cannot_finish_before_the_deadline(retry_clock):
    from app.services import google_http

    # 32s left and a 30s socket deadline: the 1s wait fits (31 ≤ 32), the 3s
    # wait after the second failure does not (1 + 3 + 30 > 32) — two attempts.
    req = _FailingRequest(TimeoutError("timed out"))
    with pytest.raises(_Unavailable) as caught:
        google_http.execute_with_retry(
            req, what="Sheets read", unavailable=_Unavailable, deadline=32.0, attempt_seconds=30.0,
        )
    assert req.executes == 2 and retry_clock["slept"] == [1.0]
    assert str(caught.value) == "Sheets read failed after 2 attempts (stopped before the deadline): timed out"
    assert caught.value.attempts == 2 and caught.value.deadline_hit is True

    # Nothing left for a second attempt: one, and said in the singular.
    retry_clock["t"], retry_clock["slept"] = 0.0, []
    req = _FailingRequest(TimeoutError("timed out"))
    with pytest.raises(_Unavailable, match="failed after 1 attempt "):
        google_http.execute_with_retry(
            req, what="Sheets read", unavailable=_Unavailable, deadline=5.0, attempt_seconds=30.0,
        )
    assert req.executes == 1 and retry_clock["slept"] == []

    # A deadline far away changes nothing.
    req = _FailingRequest(TimeoutError("timed out"))
    with pytest.raises(_Unavailable, match="failed after 3 attempts: "):
        google_http.execute_with_retry(
            req, what="Sheets read", unavailable=_Unavailable, deadline=10_000.0, attempt_seconds=30.0,
        )
    assert req.executes == 3


def test_execute_with_retry_re_raises_a_refusal_at_once_with_or_without_a_deadline(retry_clock):
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    from app.services import google_http

    for kwargs in ({}, {"deadline": 1000.0, "attempt_seconds": 30.0}):
        req = _FailingRequest(HttpError(SimpleNamespace(status=403, reason="forbidden"), b"body"))
        with pytest.raises(HttpError):
            google_http.execute_with_retry(req, what="Sheets read", unavailable=_Unavailable, **kwargs)
        assert req.executes == 1 and retry_clock["slept"] == []
