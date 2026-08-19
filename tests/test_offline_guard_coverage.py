"""The suite's own guards, asserted instead of assumed.

``backend/conftest.py`` claims four protections and every one of them exists
because a test run once reached production: a targets test wrote into the live
``mr_config/targets`` doc, a GD test uploaded fixtures to the live bucket, and a
wave-1 agent in this very pass burned real OpenRouter credit with a script that
went around them. Nothing asserted the guards themselves, so a refactor that
quietly neutered one would show up as a production write, not a red test.

Everything here is inert: the guards are observed, never exercised against a
real service.
"""
from __future__ import annotations

import os

import pytest


# --------------------------------------------------------------------------- #
# The four guards conftest.py documents
# --------------------------------------------------------------------------- #

def test_the_agent_offline_flags_are_on_for_every_test():
    for flag in ("MR_OFFLINE", "SEO_OFFLINE", "BLOG_OFFLINE", "BROWSER_OFFLINE"):
        assert os.environ.get(flag) == "1", f"{flag} is not set for this run"


def test_firestore_is_a_loud_failure_not_a_live_connection():
    """The default. A test that legitimately needs Firestore monkeypatches
    ``_db`` itself and that override wins — this is what everyone else gets."""
    from app.services import firestore_repo

    with pytest.raises(RuntimeError) as excinfo:
        firestore_repo._db()
    assert "blocked in tests" in str(excinfo.value)


def test_the_openrouter_key_reads_empty_so_no_llm_path_can_spend():
    """A developer's real key in .env must not turn a test run into a bill."""
    from app.config import settings

    assert not settings.openrouter_api_key
    assert not os.environ.get("OPENROUTER_API_KEY")


def test_cloud_storage_reports_itself_unconfigured():
    from app.services import storage

    assert storage.is_configured() is False


def test_the_llm_entry_point_refuses_rather_than_calling_out(monkeypatch):
    """The guard's mechanism, not just its symptom: every network entry point in
    ``openrouter`` resolves the key first, so a blank key is a raise before a
    socket, on every one of them."""
    from app.services import openrouter

    with pytest.raises(RuntimeError) as excinfo:
        openrouter.get_llm()
    message = str(excinfo.value).lower()
    assert "openrouter_api_key" in message
    assert "missing" in message  # names the config, does not invent a client


# --------------------------------------------------------------------------- #
# The hole that used to be in guard #1, found by running a contract test one
# layer deeper than the existing MR router tests do.
#
# ``fetch_all_trackers`` falls back to Google's spreadsheet export endpoint when
# the Sheets API path fails, and the two default fetchers below consulted no
# offline flag at all — they resolved ADC, minted a real token against the
# service account, and pulled the live workbook. Reproduced from a test run:
# stubbing only ``list_tabs`` (the API seam every existing test stubs) still
# fetched the real 15-vendor workbook over the network in ~28s.
#
# Fixed: both fetchers now raise ``CredentialMissingError("offline mode")``
# before resolving credentials, so offline is a property of the module rather
# than of every caller remembering to stub it. Was a strict xfail recording the
# defect; it is a plain assertion now that the guard is real.
# --------------------------------------------------------------------------- #

def test_mr_offline_stops_the_sheets_export_fetchers(monkeypatch):
    """No HTTP request may be attempted while MR_OFFLINE=1.

    The proof itself stays offline: ADC and httpx are both stubbed, so this
    records the *attempt* rather than making it.
    """
    import httpx

    import google.auth
    from marketing_research_agent.sources import sheets_source as ss

    class _FakeCreds:
        valid = True
        token = "fake-token"

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_FakeCreds(), "p"))
    monkeypatch.setattr(ss, "_creds_cache", {})
    monkeypatch.setenv("MR_OFFLINE", "1")

    attempted: list[str] = []

    class _Resp:
        text, content = "", b""

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "get", lambda url, **kw: (attempted.append(url), _Resp())[1])

    from marketing_research_agent.sources.base import CredentialMissingError

    for call in (lambda: ss._default_xlsx_fetcher("sheet-1"),
                 lambda: ss._default_fetcher("sheet-1", "0")):
        with pytest.raises(CredentialMissingError, match="offline mode"):
            call()

    assert attempted == [], (
        "offline, and still called out to Google: " + "; ".join(attempted)
    )
