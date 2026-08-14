"""Transport deadlines and honest failure for the Sheets read path.

Two bug classes, both of which were invisible from the call site:

1. Every Google client was built without a stated deadline, and these run in
   sync handlers on anyio's 40-slot worker threadpool — a stalled sheet pull
   removes a slot from the whole service, /api/health included.
2. ``fetch_official_totals`` returned ``{}`` on *any* exception, so a 429 or a
   revoked share was indistinguishable from "this workbook has no Overall tab".
   The headline figures just vanished, with no error anywhere.

Fully offline: ADC is stubbed, no client here ever opens a socket (`build` reads
the discovery document bundled inside google-api-python-client).
"""
from __future__ import annotations

import pytest

from marketing_research_agent.sources import sheets_source as ss


class _FakeCreds:
    """Stands in for ADC. ``AuthorizedHttp`` only stores it at build time."""

    valid = True
    token = "fake-token"

    def refresh(self, request):  # pragma: no cover - only if `valid` goes False
        raise AssertionError("no token minting in tests")


@pytest.fixture()
def stub_adc(monkeypatch):
    """Resolve ADC to a fake, and count how often it is asked."""
    calls: list[tuple] = []

    def fake_default(scopes=None):
        calls.append(tuple(scopes or ()))
        return _FakeCreds(), "test-project"

    import google.auth

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(ss, "_creds_cache", {})
    return calls


# --------------------------------------------------------------------------- #
# F2 — every client carries a deadline
# --------------------------------------------------------------------------- #

def test_sheets_client_carries_an_explicit_timeout(stub_adc):
    svc = ss._sheets_service()
    # `_http` is the AuthorizedHttp; `.http` is the httplib2 transport underneath.
    assert svc._http.http.timeout == ss.SHEETS_TIMEOUT_SECONDS


def test_the_timeout_is_ours_not_googles_implicit_default(stub_adc):
    """googleapiclient applies an implicit 60s socket timeout when it builds its
    own transport. That is not a deadline anyone chose, and it is far longer
    than a healthy Sheets call. Pin that we set our own, tighter, number."""
    from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC

    assert ss.SHEETS_TIMEOUT_SECONDS < DEFAULT_HTTP_TIMEOUT_SEC
    assert ss._sheets_service()._http.http.timeout == ss.SHEETS_TIMEOUT_SECONDS


def test_every_transport_deadline_is_finite_and_sane():
    for name in ("SHEETS_TIMEOUT_SECONDS", "AUTH_TIMEOUT_SECONDS", "EXPORT_TIMEOUT_SECONDS"):
        value = getattr(ss, name)
        assert isinstance(value, (int, float)) and 0 < value <= 120, name


def test_each_client_gets_its_own_transport(stub_adc):
    """httplib2 is not thread-safe and these clients are used from worker
    threads, so sharing one transport across them would be a data race."""
    first, second = ss._sheets_service(), ss._sheets_service()
    assert first._http.http is not second._http.http


def test_adc_is_resolved_once_not_once_per_tab(stub_adc):
    """A 6-tab ingest used to re-run ADC six times (and mint six tokens)."""
    for _ in range(5):
        ss._sheets_service()
    assert len(stub_adc) == 1


def test_token_is_not_reminted_while_it_is_still_valid(stub_adc):
    creds = ss.cached_credentials([ss.DRIVE_SCOPE])
    assert creds.valid
    ss.refresh_if_stale(creds)  # _FakeCreds.refresh raises if this misfires


def test_auth_transport_overrides_google_auths_120s_default():
    captured: dict = {}

    def inner(url, method="GET", body=None, headers=None, timeout=None, **kw):
        captured["timeout"] = timeout

    ss._TimedRequest(inner, ss.AUTH_TIMEOUT_SECONDS)("https://oauth2.googleapis.com/token")
    assert captured["timeout"] == ss.AUTH_TIMEOUT_SECONDS


def test_csv_export_fetcher_is_bounded(stub_adc, monkeypatch):
    captured: dict = {}

    class _Resp:
        text = "a,b\n1,2\n"

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    ss._default_fetcher("sheet1", "0")
    assert captured["timeout"] == ss.EXPORT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# B2 / contract C-4 — failure is not emptiness
# --------------------------------------------------------------------------- #

_ROLLUP_TITLE = "Marketing 2026 Overall Report"
_ROLLUP_ROWS = [
    ["All", "July (Performance)", "July (Investment)"],
    ["Spend", "$45,461.20", "#N/A"],
]


def test_official_totals_raise_when_the_workbook_cannot_be_listed(monkeypatch):
    boom = RuntimeError("HttpError 429: Quota exceeded for reads")
    monkeypatch.setattr(ss, "list_tabs", lambda sid, service=None: (_ for _ in ()).throw(boom))
    with pytest.raises(ss.SheetsUnavailable) as excinfo:
        ss.fetch_official_totals("sheet1", 2026, service=object())
    # The message names the real cause — that is the entire point of the change.
    assert "429" in str(excinfo.value)
    assert excinfo.value.__cause__ is boom


def test_official_totals_raise_when_the_rollup_tab_cannot_be_read(monkeypatch):
    boom = RuntimeError("HttpError 403: caller does not have permission")
    monkeypatch.setattr(ss, "list_tabs", lambda sid, service=None: [{"gid": 2, "title": _ROLLUP_TITLE}])
    monkeypatch.setattr(
        ss, "fetch_tab_values",
        lambda sid, title, service=None: (_ for _ in ()).throw(boom),
    )
    with pytest.raises(ss.SheetsUnavailable) as excinfo:
        ss.fetch_official_totals("sheet1", 2026, service=object())
    assert _ROLLUP_TITLE in str(excinfo.value) and "403" in str(excinfo.value)


def test_official_totals_return_empty_only_when_there_is_genuinely_no_rollup(monkeypatch):
    """The honest empty case must survive: read fine, nothing to report."""
    monkeypatch.setattr(ss, "list_tabs", lambda sid, service=None: [{"gid": 1, "title": "Meta 360 RA"}])
    monkeypatch.setattr(ss, "fetch_tab_values", lambda sid, title, service=None: [["Meta 360 RA"]])
    assert ss.fetch_official_totals("sheet1", 2026, service=object()) == {}


def test_a_transient_failure_is_distinguishable_from_emptiness(monkeypatch):
    """The bug in one assertion: these two situations must not look the same."""
    monkeypatch.setattr(ss, "list_tabs", lambda sid, service=None: [{"gid": 1, "title": "Vendor"}])
    monkeypatch.setattr(ss, "fetch_tab_values", lambda sid, title, service=None: [["Vendor"]])
    empty = ss.fetch_official_totals("sheet1", 2026, service=object())

    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    with pytest.raises(ss.SheetsUnavailable):
        ss.fetch_official_totals("sheet1", 2026, service=object())

    assert empty == {}  # ...and the failure above did NOT also produce {}


def test_official_spend_inherits_the_raise(monkeypatch):
    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(RuntimeError("timed out")),
    )
    with pytest.raises(ss.SheetsUnavailable):
        ss.fetch_official_spend("sheet1", 2026, service=object())


def test_sheets_unavailable_is_catchable_as_a_runtime_error():
    """Callers catching broadly (the MR router does) keep working."""
    assert issubclass(ss.SheetsUnavailable, RuntimeError)


def test_official_totals_still_parse_the_rollup_when_everything_works(monkeypatch):
    monkeypatch.setattr(ss, "list_tabs", lambda sid, service=None: [{"gid": 2, "title": _ROLLUP_TITLE}])
    monkeypatch.setattr(ss, "fetch_tab_values", lambda sid, title, service=None: _ROLLUP_ROWS)
    assert ss.fetch_official_totals("sheet1", 2026, service=object()) == {
        "2026-07": {"spend": 45461.20}
    }


def test_all_trackers_report_both_causes_when_both_paths_fail(monkeypatch):
    """The xlsx export is a real fallback for "Sheets API not enabled". But if
    it fails too, reporting only its error sends a debugger down the wrong road.
    """
    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(RuntimeError("SHEETS_API_DISABLED")),
    )

    def bad_xlsx(spreadsheet_id):
        raise RuntimeError("XLSX_EXPORT_404")

    with pytest.raises(ss.SheetsUnavailable) as excinfo:
        ss.fetch_all_trackers("sheet1", 2026, service=object(), xlsx_fetcher=bad_xlsx)
    message = str(excinfo.value)
    assert "SHEETS_API_DISABLED" in message and "XLSX_EXPORT_404" in message


def test_all_trackers_still_fall_back_to_xlsx_when_that_path_works(monkeypatch):
    """The fallback is not collateral damage of the honest-failure change."""
    import io

    import openpyxl

    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(RuntimeError("api off")),
    )
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Meta 360 RA"
    for row in [
        ["Meta 360 RA", "July (Performance)", "July (Investment)"],
        ["Spend", "$100.00", ""],
        ["Leads", "5", ""],
    ]:
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    found = ss.fetch_all_trackers(
        "sheet1", 2026, service=object(), xlsx_fetcher=lambda sid: buf.getvalue()
    )
    assert [f["tab"] for f in found] == ["Meta 360 RA"]
