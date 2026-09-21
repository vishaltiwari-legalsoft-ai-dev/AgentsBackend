"""The consent state, the token vault, the credentials, and the Gmail reads —
all offline. Google is a fake request object or a stubbed ``httpx.post``;
the only test that leaves ``INBOX_OFFLINE`` does so with the transport
stubbed first, the way the MR transport suite does."""

from __future__ import annotations

import base64
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from googleapiclient.errors import HttpError

import app  # noqa: F401 — registers the agent roots on sys.path
from app.config import settings
from inbox_triage_agent import InboxOffline, gmail_client, gmail_oauth
from inbox_triage_agent.gmail_client import (
    GmailUnavailable, HistoryExpired, MessageGone, parse_message,
)
from inbox_triage_agent.gmail_oauth import (
    BadState, ExchangeFailed, OAuthNotConfigured, RevokedGrant, TokenKeyMissing,
    TokenRefreshFailed,
)

USER = "user-1111"


@pytest.fixture()
def oauth_client(monkeypatch):
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid-test", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec-test", raising=False)
    monkeypatch.setattr(settings, "app_public_url", "https://console.example/", raising=False)


@pytest.fixture()
def token_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "inbox_token_key", key, raising=False)
    return key


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------- #
# Consent URL and state
# --------------------------------------------------------------------------- #

def test_auth_url_sends_google_to_the_frontend_and_asks_for_a_refresh_token(oauth_client):
    url, state = gmail_oauth.auth_url(USER)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "redirect_uri=https%3A%2F%2Fconsole.example%2Foauth%2Fgoogle" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert f"state={state}" in url
    assert "sec-test" not in url


def test_state_is_bound_to_the_user_who_started_it(oauth_client):
    state = gmail_oauth.make_state(USER)
    gmail_oauth.read_state(state, user_id=USER)  # no raise
    with pytest.raises(BadState, match="different account"):
        gmail_oauth.read_state(state, user_id="user-2222")


def test_state_expires_after_ten_minutes(oauth_client):
    state = gmail_oauth.make_state(USER, now=1_000_000)
    gmail_oauth.read_state(state, user_id=USER, now=1_000_000 + 599)
    with pytest.raises(BadState, match="expired"):
        gmail_oauth.read_state(state, user_id=USER, now=1_000_000 + 601)


def test_a_tampered_or_garbage_state_is_refused(oauth_client):
    state = gmail_oauth.make_state(USER)
    head, _, signature = state.rpartition(".")
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    with pytest.raises(BadState, match="not valid"):
        gmail_oauth.read_state(f"{head}.{flipped}", user_id=USER)
    with pytest.raises(BadState, match="not valid"):
        gmail_oauth.read_state("junk", user_id=USER)
    with pytest.raises(BadState):
        gmail_oauth.read_state("", user_id=USER)


def test_a_state_for_another_user_cannot_be_forged_by_editing_the_id(oauth_client):
    state = gmail_oauth.make_state("user-2222")
    forged = state.replace("user-2222", USER, 1)
    with pytest.raises(BadState, match="not valid"):
        gmail_oauth.read_state(forged, user_id=USER)


def test_a_missing_oauth_client_is_a_named_failure(monkeypatch):
    monkeypatch.setattr(settings, "inbox_google_client_id", "", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "", raising=False)
    with pytest.raises(OAuthNotConfigured, match="INBOX_GOOGLE_CLIENT_ID"):
        gmail_oauth.auth_url(USER)


# --------------------------------------------------------------------------- #
# The vault
# --------------------------------------------------------------------------- #

def test_seal_and_open_round_trip_and_the_sealed_form_hides_the_token(token_key):
    sealed = gmail_oauth.seal("1//refresh-token-plain")
    assert sealed != "1//refresh-token-plain"
    assert "refresh-token-plain" not in sealed
    assert gmail_oauth.open_(sealed) == "1//refresh-token-plain"


def test_a_missing_or_malformed_key_is_named_before_anything_is_stored(monkeypatch):
    monkeypatch.setattr(settings, "inbox_token_key", "", raising=False)
    with pytest.raises(TokenKeyMissing, match="INBOX_TOKEN_KEY"):
        gmail_oauth.require_token_key()
    with pytest.raises(TokenKeyMissing):
        gmail_oauth.seal("anything")
    monkeypatch.setattr(settings, "inbox_token_key", "not-a-fernet-key", raising=False)
    with pytest.raises(TokenKeyMissing, match="not a valid Fernet key"):
        gmail_oauth.seal("anything")


def test_a_token_sealed_under_a_rotated_key_reads_as_revoked(monkeypatch, token_key):
    sealed = gmail_oauth.seal("1//refresh")
    monkeypatch.setattr(settings, "inbox_token_key", Fernet.generate_key().decode(), raising=False)
    with pytest.raises(RevokedGrant, match="connect again"):
        gmail_oauth.open_(sealed)


# --------------------------------------------------------------------------- #
# The exchange
# --------------------------------------------------------------------------- #

def test_complete_checks_the_state_and_then_refuses_to_call_out_while_offline(
    oauth_client, monkeypatch
):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("called Google offline"))
    with pytest.raises(BadState):
        gmail_oauth.complete(USER, code="c", state="junk")
    state = gmail_oauth.make_state(USER)
    with pytest.raises(InboxOffline):
        gmail_oauth.complete(USER, code="c", state=state)


class _Resp:
    def __init__(self, status: int, payload: dict, text: str = ""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def exchange(oauth_client, monkeypatch):
    """Leave offline mode with ``httpx.post`` stubbed FIRST, so nothing goes out."""
    import httpx

    sent: dict = {}

    def install(resp: _Resp):
        def fake_post(url, data=None, timeout=None, **kw):
            sent.update({"url": url, "data": data, "timeout": timeout})
            return resp
        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.delenv("INBOX_OFFLINE", raising=False)
        return sent
    return install


def test_complete_exchanges_the_code_with_a_deadline(exchange):
    sent = exchange(_Resp(200, {
        "access_token": "ya29.x", "refresh_token": "1//r", "scope": gmail_oauth.SCOPE,
    }))
    tokens = gmail_oauth.complete(USER, code="the-code", state=gmail_oauth.make_state(USER))
    assert tokens.refresh_token == "1//r" and tokens.access_token == "ya29.x"
    assert sent["url"] == gmail_oauth.TOKEN_ENDPOINT
    assert sent["timeout"] == gmail_oauth.EXCHANGE_TIMEOUT_SECONDS
    assert sent["data"]["redirect_uri"] == "https://console.example/oauth/google"
    assert sent["data"]["grant_type"] == "authorization_code"


@pytest.mark.parametrize("payload, phrase", [
    ({"access_token": "a", "refresh_token": "r", "scope": "openid"}, "read access to Gmail"),
    ({"access_token": "a", "scope": gmail_oauth.SCOPE}, "refresh token"),
])
def test_complete_refuses_a_grant_that_cannot_poll(exchange, payload, phrase):
    exchange(_Resp(200, payload))
    with pytest.raises(ExchangeFailed, match=phrase):
        gmail_oauth.complete(USER, code="c", state=gmail_oauth.make_state(USER))


def test_complete_reports_googles_refusal_as_a_sentence(exchange):
    exchange(_Resp(400, {"error": "invalid_grant"}, text='{"error":"invalid_grant"}'))
    with pytest.raises(ExchangeFailed, match="refused the sign-in code"):
        gmail_oauth.complete(USER, code="c", state=gmail_oauth.make_state(USER))


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #

class _Creds:
    def __init__(self, error: Exception | None = None, valid: bool = False):
        self.valid, self._error, self.refreshed = valid, error, 0

    def refresh(self, request):
        self.refreshed += 1
        if self._error:
            raise self._error


def test_refresh_maps_invalid_grant_to_revoked_and_other_failures_to_retryable(monkeypatch):
    from google.auth.exceptions import RefreshError

    monkeypatch.delenv("INBOX_OFFLINE", raising=False)
    with pytest.raises(RevokedGrant, match="connect again"):
        gmail_oauth.refresh(_Creds(RefreshError("invalid_grant: Token has been expired or revoked.")))
    with pytest.raises(TokenRefreshFailed):
        gmail_oauth.refresh(_Creds(RefreshError("internal_failure: try later")))
    fine = _Creds()
    gmail_oauth.refresh(fine)
    assert fine.refreshed == 1


def test_refresh_is_skipped_for_a_valid_token_and_refused_offline():
    still_valid = _Creds(valid=True)
    gmail_oauth.refresh(still_valid)  # offline, but nothing to mint
    assert still_valid.refreshed == 0
    with pytest.raises(InboxOffline):
        gmail_oauth.refresh(_Creds())


def test_credentials_carry_the_readonly_scope_and_the_agents_own_client(oauth_client):
    creds = gmail_oauth.credentials("1//r", access_token="ya29.x")
    assert creds.refresh_token == "1//r"
    assert creds.token == "ya29.x"
    assert creds.scopes == [gmail_oauth.SCOPE]
    assert creds.client_id == "cid-test"


# --------------------------------------------------------------------------- #
# A fake Gmail service, the shape googleapiclient builds
# --------------------------------------------------------------------------- #

def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=f"status {status}"), b"body")


class _Req:
    def __init__(self, result=None, error: Exception | None = None, on_execute=None):
        self._result, self._error, self._on_execute = result, error, on_execute

    def execute(self):
        if self._on_execute:
            self._on_execute()
        if self._error:
            raise self._error
        return self._result


class FakeGmail:
    """``users().messages().list/get``, ``users().history().list`` and
    ``users().getProfile`` over dicts. Every request built is in ``calls``;
    every ``execute()`` — which is what the retry repeats — in ``executions``."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.executions: list[str] = []
        self.profile = {"emailAddress": "her@firm.com", "historyId": "500", "messagesTotal": 42}
        self.mail: dict[str, dict] = {}
        self.list_pages: list[dict] = []
        self.history_pages: list[dict] = []
        self.history_error: Exception | None = None
        self.get_error: Exception | None = None

    def users(self):
        return self

    def _req(self, name: str, result=None, error: Exception | None = None) -> _Req:
        return _Req(result, error, on_execute=lambda: self.executions.append(name))

    def getProfile(self, userId):
        self.calls.append(("getProfile", {"userId": userId}))
        return self._req("getProfile", self.profile)

    def messages(self):
        return SimpleNamespace(list=self._list, get=self._get)

    def history(self):
        return SimpleNamespace(list=self._history)

    def _list(self, **kw):
        self.calls.append(("messages.list", kw))
        index = int(kw.get("pageToken") or 0)
        return self._req("messages.list", self.list_pages[index] if index < len(self.list_pages) else {})

    def _get(self, **kw):
        self.calls.append(("messages.get", kw))
        if self.get_error:
            return self._req("messages.get", error=self.get_error)
        if kw["id"] not in self.mail:
            return self._req("messages.get", error=_http_error(404))
        return self._req("messages.get", self.mail[kw["id"]])

    def _history(self, **kw):
        self.calls.append(("history.list", kw))
        if self.history_error:
            return self._req("history.list", error=self.history_error)
        index = int(kw.get("pageToken") or 0)
        return self._req("history.list", self.history_pages[index] if index < len(self.history_pages) else {})


def test_profile_reads_the_address_and_the_checkpoint():
    gmail = FakeGmail()
    assert gmail_client.profile(gmail) == {
        "email": "her@firm.com", "history_id": "500", "messages_total": 42,
    }


def test_list_inbox_asks_for_the_inbox_label_after_an_epoch_and_pages():
    gmail = FakeGmail()
    gmail.list_pages = [
        {"messages": [{"id": "m3"}, {"id": "m2"}], "nextPageToken": "1", "resultSizeEstimate": 3},
        {"messages": [{"id": "m1"}], "resultSizeEstimate": 3},
    ]
    ids, token, estimate = gmail_client.list_inbox(gmail, after_epoch=1_700_000_000, max_results=2)
    assert (ids, token, estimate) == (["m3", "m2"], "1", 3)
    _, kw = gmail.calls[-1]
    assert kw["labelIds"] == ["INBOX"] and kw["q"] == "after:1700000000" and kw["maxResults"] == 2
    ids, token, _ = gmail_client.list_inbox(gmail, after_epoch=1_700_000_000, page_token="1")
    assert (ids, token) == (["m1"], None)


def test_the_listing_carries_the_thread_id_which_is_what_the_backfill_reads():
    """One ``messages.list`` answers "which thread is this in?" for a whole
    page — the reason the thread-id backfill is pages, not one call a message."""
    gmail = FakeGmail()
    gmail.list_pages = [{
        "messages": [
            {"id": "m3", "threadId": "t1"},
            {"id": "m2", "threadId": "t1"},
            {"id": "m1"},  # Gmail left it out: no thread id, and none invented
        ],
        "resultSizeEstimate": 3,
    }]
    pairs, token, estimate = gmail_client.list_inbox_pairs(
        gmail, after_epoch=1_700_000_000, max_results=gmail_client.LIST_THREAD_PAGE_MAX
    )
    assert (pairs, token, estimate) == ([("m3", "t1"), ("m2", "t1"), ("m1", "")], None, 3)
    _, kw = gmail.calls[-1]
    assert kw["maxResults"] == 500, "the backfill takes Gmail's whole page"
    assert kw["labelIds"] == ["INBOX"], "the same window the mail passes walk"
    ids, _, _ = gmail_client.list_inbox(gmail, after_epoch=1_700_000_000)
    assert ids == ["m3", "m2", "m1"], "the mail passes see the same page without the threads"


def test_the_sent_listing_asks_for_the_sent_label_and_nothing_else():
    gmail = FakeGmail()
    gmail.list_pages = [{"messages": [{"id": "s1", "threadId": "t1"}], "resultSizeEstimate": 1}]
    pairs, token, _ = gmail_client.list_sent_pairs(gmail, after_epoch=1_700_000_000)
    assert (pairs, token) == ([("s1", "t1")], None)
    _, kw = gmail.calls[-1]
    assert kw["labelIds"] == ["SENT"] and kw["q"] == "after:1700000000"
    assert kw["maxResults"] == 500
    assert "format" not in kw, "a listing reads no content, so there is none to ask for"


def test_a_stamp_reads_the_time_and_only_the_time():
    gmail = FakeGmail()
    gmail.mail["s1"] = {
        "id": "s1", "threadId": "t1", "labelIds": ["SENT"],
        "internalDate": "1789000000000",
    }
    when = gmail_client.stamp(gmail, "s1")
    _, kw = gmail.calls[-1]
    assert kw["format"] == "minimal", "no headers, no body — the subject is never read"
    assert when.tzinfo is not None and when.year == 2026


def test_a_stamp_for_a_message_she_deleted_is_gone_not_a_failed_fire():
    gmail = FakeGmail()
    with pytest.raises(MessageGone):
        gmail_client.stamp(gmail, "vanished")
    gmail.mail["s2"] = {"id": "s2", "threadId": "t1"}  # no internalDate at all
    with pytest.raises(MessageGone):
        gmail_client.stamp(gmail, "s2")


def test_history_collects_inbox_additions_dedupes_and_returns_the_new_checkpoint():
    gmail = FakeGmail()
    gmail.history_pages = [
        {"historyId": "610", "nextPageToken": "1", "history": [
            {"messagesAdded": [{"message": {"id": "a", "labelIds": ["INBOX", "UNREAD"]}}]},
            {"messagesAdded": [{"message": {"id": "a", "labelIds": ["INBOX"]}}]},
            {"messagesAdded": [{"message": {"id": "archived", "labelIds": ["CATEGORY_PROMOTIONS"]}}]},
        ]},
        {"historyId": "610", "history": [
            {"messagesAdded": [{"message": {"id": "b"}}]},
        ]},
    ]
    added, checkpoint = gmail_client.history_since(gmail, "600")
    assert added == ["a", "b"] and checkpoint == "610"
    _, kw = gmail.calls[0]
    assert kw["startHistoryId"] == "600" and kw["historyTypes"] == ["messageAdded"]
    assert kw["labelId"] == "INBOX"


def test_an_expired_checkpoint_is_the_fallback_signal_not_a_failure():
    gmail = FakeGmail()
    gmail.history_error = _http_error(404)
    with pytest.raises(HistoryExpired):
        gmail_client.history_since(gmail, "1")
    gmail.history_error = _http_error(403)
    with pytest.raises(GmailUnavailable, match="HTTP 403"):
        gmail_client.history_since(gmail, "1")


def test_a_history_that_never_ends_is_re_listed_rather_than_half_saved():
    gmail = FakeGmail()
    gmail.history_pages = [
        {"historyId": "700", "nextPageToken": str(i + 1), "history": []}
        for i in range(gmail_client.HISTORY_PAGE_CAP + 2)
    ]
    with pytest.raises(HistoryExpired, match="pages"):
        gmail_client.history_since(gmail, "600")


def test_fetch_maps_a_missing_message_and_a_refusal():
    gmail = FakeGmail()
    with pytest.raises(MessageGone):
        gmail_client.fetch(gmail, "nope")
    gmail.get_error = _http_error(403)
    with pytest.raises(GmailUnavailable):
        gmail_client.fetch(gmail, "nope")


def test_a_transient_status_is_retried_then_becomes_unavailable():
    gmail = FakeGmail()
    gmail.history_error = _http_error(503)
    with pytest.raises(GmailUnavailable, match="after 3 attempts"):
        gmail_client.history_since(gmail, "1")
    assert gmail.executions.count("history.list") == 3, "one request, executed three times"


def test_the_client_refuses_to_build_offline():
    with pytest.raises(InboxOffline):
        gmail_client.service(object())


# --------------------------------------------------------------------------- #
# Message parsing
# --------------------------------------------------------------------------- #

def _b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


def _headers(**values) -> list[dict]:
    """``from_=`` → the ``From`` header, and so on — Gmail sends them capitalised."""
    return [{"name": name.rstrip("_").title(), "value": value} for name, value in values.items()]


def test_plain_text_message_with_headers_and_ist_time():
    data = {
        "id": "m1", "threadId": "t1", "internalDate": "1789000000000",  # 2026-09-10 UTC
        "payload": {
            "mimeType": "text/plain",
            "headers": _headers(from_="A <a@b.com>", to="r@firm.com",
                                date="Thu, 10 Sep 2026 10:00:00 +0000", subject="Hi"),
            "body": {"data": _b64("Please reply by Friday.")},
        },
    }
    message = parse_message(data)
    assert (message.id, message.thread_id, message.subject) == ("m1", "t1", "Hi")
    assert message.from_ == "A <a@b.com>" and message.to == "r@firm.com"
    assert message.body_text == "Please reply by Friday."
    assert message.received_at.tzinfo is not None
    assert message.received_at.utcoffset().total_seconds() == 5.5 * 3600
    assert message.received_at == datetime.fromtimestamp(1789000000, tz=message.received_at.tzinfo)


def test_html_only_message_is_reduced_to_visible_text():
    data = {"id": "m2", "payload": {
        "mimeType": "text/html",
        "headers": _headers(subject="Offer"),
        "body": {"data": _b64("<html><style>p{}</style><body><p>Sign by <b>Monday</b></p></body></html>")},
    }}
    assert parse_message(data).body_text == "Sign by Monday"


def test_multipart_prefers_plain_and_skips_attachments_even_when_they_are_text():
    data = {"id": "m3", "payload": {
        "mimeType": "multipart/mixed",
        "headers": _headers(subject="CV"),
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Plain part")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>Html part</p>")}},
            ]},
            {"mimeType": "text/plain", "filename": "cv.txt", "body": {"data": _b64("SECRET CV TEXT")}},
            {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "x"}},
        ],
    }}
    message = parse_message(data)
    assert message.body_text == "Plain part"
    assert "SECRET" not in message.body_text and "Html" not in message.body_text


def test_missing_headers_and_missing_body_read_as_empty_not_as_errors():
    message = parse_message({"id": "m4", "payload": {"mimeType": "multipart/mixed", "parts": []}})
    assert (message.from_, message.to, message.date_header, message.subject) == ("", "", "", "")
    assert message.body_text == ""
    assert message.received_at.tzinfo is not None


def test_a_declared_charset_is_honoured():
    data = {"id": "m5", "payload": {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
        "body": {"data": _b64("Café", "latin-1")},
    }}
    assert parse_message(data).body_text == "Café"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (tester pass): the parse shapes the brief named, and the
# shared retry the Gmail and Sheets clients both ride on.
# --------------------------------------------------------------------------- #

def test_an_attachment_nested_two_levels_down_is_still_skipped():
    data = {"id": "m6", "payload": {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/related", "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Only html here</p>")}},
                ]},
                {"mimeType": "text/html", "filename": "forwarded.html",
                 "body": {"data": _b64("<p>ATTACHED HTML</p>")}},
                {"mimeType": "text/plain", "filename": "notes.txt",
                 "body": {"data": _b64("ATTACHED NOTES")}},
            ]},
        ],
    }}
    body = parse_message(data).body_text
    assert body == "Only html here"
    assert "ATTACHED" not in body, "an attachment is skipped however deep it sits"


def test_every_plain_part_is_kept_in_order():
    data = {"id": "m7", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("First part.")}},
        {"mimeType": "text/plain", "body": {"data": _b64("Second part.")}},
    ]}}
    assert parse_message(data).body_text == "First part.\nSecond part."


@pytest.mark.parametrize("text", ["a", "ab", "abc", "abcd", "subject?>>>", "ÿþ??~~"])
def test_base64url_without_padding_and_with_url_safe_characters_decodes(text):
    # ``?>>>`` and ``ÿþ`` encode to '-' / '_' in the url-safe alphabet, and every
    # length mod 3 is covered, so every padding Gmail strips is restored.
    encoded = _b64(text)
    assert "=" not in encoded
    data = {"id": "m8", "payload": {"mimeType": "text/plain", "body": {"data": encoded}}}
    assert parse_message(data).body_text == text


def test_url_safe_alphabet_is_actually_exercised():
    assert "-" in _b64("subject?>>>") and "_" in _b64("ÿþ??~~")


def test_a_completely_empty_resource_is_a_message_of_empty_strings():
    message = parse_message({})
    assert (message.id, message.thread_id, message.subject, message.body_text) == ("", "", "", "")
    assert message.received_at.tzinfo is not None


def test_an_undecodable_body_part_does_not_poison_the_message():
    """A part whose data is not valid base64 must not raise out of the parser:
    ``fetch`` runs inside the fire, the error class is not one the fire maps,
    and since the checkpoint never moves past it, the same message would kill
    every later fire for this inbox. Headers must survive; the body reads as
    whatever could be decoded (here: nothing)."""
    data = {"id": "m9", "payload": {
        "mimeType": "multipart/alternative",
        "headers": _headers(subject="Still has a subject"),
        "parts": [{"mimeType": "text/plain", "body": {"data": "abcde"}}],  # 5 chars: never valid
    }}
    message = parse_message(data)
    assert message.subject == "Still has a subject"
    assert message.body_text == ""


class _Seq:
    """``execute`` answers from a script: exceptions are raised, values returned."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def execute(self):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Down(RuntimeError):
    pass


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_the_shared_retry_rides_out_each_transient_status(status):
    from app.services.google_http import execute_with_retry

    request = _Seq(_http_error(status), {"ok": 1})
    assert execute_with_retry(request, what="X", unavailable=_Down) == {"ok": 1}
    assert request.calls == 2


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), ConnectionResetError("reset")])
def test_the_shared_retry_rides_out_socket_failures(error):
    from app.services.google_http import execute_with_retry

    request = _Seq(error, error, {"ok": 1})
    assert execute_with_retry(request, what="X", unavailable=_Down) == {"ok": 1}
    assert request.calls == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_the_shared_retry_re_raises_an_answer_untouched_on_the_first_try(status):
    from app.services.google_http import execute_with_retry

    original = _http_error(status)
    request = _Seq(original, {"never": "reached"})
    with pytest.raises(HttpError) as caught:
        execute_with_retry(request, what="X", unavailable=_Down)
    assert caught.value is original and request.calls == 1


def test_the_shared_retry_ends_in_the_callers_own_class_with_the_last_cause():
    from app.services.google_http import execute_with_retry

    last = _http_error(503)
    request = _Seq(_http_error(502), _http_error(500), last, {"never": "reached"})
    with pytest.raises(_Down, match=r"^Gmail thing failed after 3 attempts") as caught:
        execute_with_retry(request, what="Gmail thing", unavailable=_Down)
    assert caught.value.__cause__ is last and request.calls == 3


def test_a_programming_error_is_not_retried_or_renamed():
    from app.services.google_http import execute_with_retry

    request = _Seq(KeyError("bug"), {"never": "reached"})
    with pytest.raises(KeyError):
        execute_with_retry(request, what="X", unavailable=_Down)
    assert request.calls == 1


def test_mr_still_sees_the_same_transport_through_its_re_exports():
    """The lift must leave MR's names pointing at the one implementation, so a
    fix in ``google_http`` reaches MR and MR's own seams keep working."""
    from app.services import google_http
    from marketing_research_agent.sources import sheets_source as ss

    for name in ("execute_with_retry", "cached_credentials", "refresh_if_stale", "timed_http",
                 "_TimedRequest", "_is_transient"):
        assert getattr(ss, name) is getattr(google_http, name), name
    assert ss._RETRYABLE_STATUS == google_http._RETRYABLE_STATUS
    assert ss._RETRY_ATTEMPTS == google_http._RETRY_ATTEMPTS == 3
    with pytest.raises(ss.SheetsUnavailable, match=r"^Sheets tab read failed after 3 attempts"):
        ss._execute(_Seq(TimeoutError(), TimeoutError(), TimeoutError()), what="tab read")


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (review fixes): login_hint, non-ASCII state, the revoke.
# --------------------------------------------------------------------------- #

def test_auth_url_hints_the_signed_in_account_and_keeps_state_last(oauth_client):
    url, state = gmail_oauth.auth_url(USER, login_hint="her@legalsoft.com")
    assert "login_hint=her%40legalsoft.com" in url
    assert url.endswith(f"state={state}")
    assert "login_hint" not in gmail_oauth.auth_url(USER)[0]


@pytest.mark.parametrize("tamper", ["signature", "user"])
def test_a_non_ascii_state_is_bad_state_not_a_type_error(oauth_client, tamper):
    state = gmail_oauth.make_state(USER)
    head, _, signature = state.rpartition(".")
    if tamper == "signature":
        forged = f"{head}.é{signature[1:]}"
        with pytest.raises(BadState, match="not valid"):
            gmail_oauth.read_state(forged, user_id=USER)
    else:
        with pytest.raises(BadState, match="different account"):
            gmail_oauth.read_state(state, user_id="usér-ü")


class _RevokeResp:
    def __init__(self, status: int):
        self.status_code = status


@pytest.fixture()
def revoke_calls(monkeypatch):
    """Scripted ``httpx.post`` for the revoke; offline mode left AFTER the stub."""
    import httpx

    calls: list[dict] = []
    script: list = []

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "data": data, "timeout": timeout})
        answer = script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _RevokeResp(answer)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("INBOX_OFFLINE", raising=False)
    return calls, script


def test_revoke_posts_the_token_with_a_deadline_and_is_true_only_on_200(revoke_calls):
    calls, script = revoke_calls
    script.append(200)
    assert gmail_oauth.revoke("1//r") is True
    assert calls == [{"url": gmail_oauth.REVOKE_ENDPOINT, "data": {"token": "1//r"},
                      "timeout": gmail_oauth.REVOKE_TIMEOUT_SECONDS}]


def test_revoke_a_4xx_is_false_at_once_and_a_5xx_or_transport_error_is_tried_once_more(revoke_calls):
    import httpx

    calls, script = revoke_calls
    script.append(400)
    assert gmail_oauth.revoke("1//r") is False and len(calls) == 1
    calls.clear()
    script.extend([503, 200])
    assert gmail_oauth.revoke("1//r") is True and len(calls) == 2
    calls.clear()
    script.extend([httpx.ConnectTimeout("slow"), httpx.ConnectError("down")])
    assert gmail_oauth.revoke("1//r") is False and len(calls) == gmail_oauth.REVOKE_ATTEMPTS


def test_revoke_offline_or_without_a_token_calls_nobody_and_says_false(monkeypatch, caplog):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("revoke went out"))
    assert gmail_oauth.revoke("1//secret-refresh") is False  # INBOX_OFFLINE=1
    monkeypatch.delenv("INBOX_OFFLINE", raising=False)
    assert gmail_oauth.revoke("") is False
    assert "secret-refresh" not in caplog.text
