"""The agent's Sheets writes must carry a deadline.

A write that hangs is worse than a read that hangs: the caller cannot tell
whether the rows landed, so the agent cannot honestly report what it did. And
like every other Google call in this codebase it runs on one of anyio's 40
worker threadpool slots, so hanging costs the whole service, not just this run.

Offline: ADC is stubbed and `build` opens no socket (the discovery document
ships inside google-api-python-client).
"""
from __future__ import annotations

import pytest

from browser_agent import tools


class _FakeCreds:
    valid = True
    token = "fake-token"


@pytest.fixture()
def stub_adc(monkeypatch):
    import google.auth

    from marketing_research_agent.sources import sheets_source as ss

    granted: list[tuple] = []

    def fake_default(scopes=None):
        granted.append(tuple(scopes or ()))
        return _FakeCreds(), "test-project"

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(ss, "_creds_cache", {})
    return granted


def test_write_client_carries_an_explicit_timeout(stub_adc):
    svc = tools._write_service()
    assert svc._http.http.timeout == tools.SHEETS_WRITE_TIMEOUT_SECONDS


def test_write_timeout_is_tighter_than_the_implicit_default(stub_adc):
    from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC

    assert 0 < tools.SHEETS_WRITE_TIMEOUT_SECONDS < DEFAULT_HTTP_TIMEOUT_SEC


def test_write_client_keeps_its_own_write_scope(stub_adc):
    """Sharing MR's transport plumbing must not leak MR's read-only scope onto
    the write path, nor this write scope back onto MR's readers."""
    tools._write_service()
    assert stub_adc == [(tools.WRITE_SCOPE,)]


def test_mr_read_path_stays_read_only_scoped(stub_adc):
    """"MR can only read" is a property of the credential, not of care taken at
    the call site — keep it literally true."""
    from marketing_research_agent.sources import sheets_source as ss

    ss._sheets_service()
    granted = {scope for call in stub_adc for scope in call}
    assert granted == {ss.SHEETS_SCOPE, ss.DRIVE_SCOPE}
    assert all(scope.endswith(".readonly") for scope in granted)
    assert tools.WRITE_SCOPE not in granted


def test_each_write_client_gets_its_own_transport(stub_adc):
    a, b = tools._write_service(), tools._write_service()
    assert a._http.http is not b._http.http


def test_append_still_reports_a_sheets_failure_honestly(stub_adc, monkeypatch):
    """The transport change must not turn a refusal into a silent no-op."""
    def boom():
        raise RuntimeError("HttpError 403: caller does not have permission")

    monkeypatch.setattr(tools, "_write_service", boom)
    monkeypatch.setattr(tools, "service_account_email", lambda: "sa@example.iam")
    with pytest.raises(tools.ToolError) as excinfo:
        tools.sheet_append("https://docs.google.com/spreadsheets/d/abc123/edit", [["a"]])
    assert "Editor" in str(excinfo.value)
