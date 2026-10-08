"""The vendor performance report routes (/api/mr/vendor-report*) and the shared
PDF renderer client they — and the board PDF — call.

Offline throughout: snapshots are the real 2026-09-02 / 2026-08-31 docs on a
temp disk store, runs and targets go to ``tmp_path``, the renderer address is
unset unless a test opts in (and then it is ``.invalid`` with ``httpx.post``
stubbed), and the Google ID token is stubbed by the directory conftest — no
test here can reach Firestore, the renderer or Google.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

os.environ["MR_OFFLINE"] = "1"

from app.routers import marketing_research as mr_router
from app.routers.tests.conftest import DEFAULT_CALLER, FAKE_ID_TOKEN, client
from app.services import pdf_renderer

USER = dict(DEFAULT_CALLER)
OTHER = {"id": "u-other", "email": "other@legalsoft.com"}

FIXTURE = (Path(__file__).resolve().parents[3] / "agents" / "Marketing Research agent"
           / "marketing_research_agent" / "tests" / "fixtures"
           / "vendor_sweep_2026-09-02.json")


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    for doc in json.loads(FIXTURE.read_text(encoding="utf-8"))["docs"]:
        (snap_dir / f"{doc['vendor_slug']}_{doc['date']}.json").write_text(
            json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(snap_dir))
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    for var in ("MR_WORKSPACE_ID", "MR_CRON_USER_ID", "MR_WORKSPACE_SHARED",
                "MR_VENDOR_REPORT", "RENDERER_URL", "RENDERER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    from marketing_research_agent import goals
    goals.invalidate_targets_cache()
    as_caller(USER)


@pytest.fixture()
def vendor_on(monkeypatch):
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")


@pytest.fixture()
def renderer_on(monkeypatch):
    monkeypatch.setenv("RENDERER_URL", "http://renderer.invalid")
    monkeypatch.setenv("RENDERER_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setattr(mr_router, "_RENDERER_BACKOFF_SECONDS", 0)


def _stub_renderer(monkeypatch, *responses):
    calls: list[dict] = []
    queue = list(responses)

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "post", post)
    return calls


def _pdf_bytes() -> bytes:
    return b"%PDF-1.7\n" + b"0" * 200 + b"\n%%EOF"


def _build(body: dict | None = None) -> dict:
    r = client.post("/api/mr/vendor-report", json=body or {"year_month": "2026-09"})
    assert r.status_code == 200, r.text
    return r.json()


# --- the switch, and who may call ----------------------------------------------------

def test_the_vendor_routes_are_dark_by_default_but_the_periods_route_says_so(monkeypatch):
    run_id = None
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")
    run_id = _build()["id"]
    monkeypatch.delenv("MR_VENDOR_REPORT")

    assert client.post("/api/mr/vendor-report", json={}).status_code == 404
    for suffix in ("html", "pdf"):
        resp = client.get(f"/api/mr/vendor-report/{run_id}/{suffix}")
        assert resp.status_code == 404 and resp.json()["detail"] == "Not Found"
    # The console hides the band from THIS answer — no failed click needed.
    periods = client.get("/api/mr/vendor-report/periods")
    assert periods.status_code == 200
    assert periods.json()["enabled"] is False and periods.json()["months"] == []


def test_an_anonymous_caller_gets_401_whether_the_feature_is_on_or_off(
        monkeypatch, unauthenticated):
    unauthenticated()
    for on in (False, True):
        if on:
            monkeypatch.setenv("MR_VENDOR_REPORT", "1")
        assert client.post("/api/mr/vendor-report", json={}).status_code == 401
        assert client.get("/api/mr/vendor-report/periods").status_code == 401
        assert client.get("/api/mr/vendor-report/abc/html").status_code == 401


# --- periods ---------------------------------------------------------------------------

def test_periods_lists_sweep_months_newest_first_with_pdf_availability(vendor_on, monkeypatch):
    body = client.get("/api/mr/vendor-report/periods").json()
    assert body["enabled"] is True
    assert body["months"] == [{"year_month": "2026-09", "label": "September 2026"},
                              {"year_month": "2026-08", "label": "August 2026"}]
    assert body["pdf_available"] is False
    reason = body["pdf_unavailable_reason"]
    assert "RENDERER_URL and RENDERER_TOKEN are unset" in reason
    # Browsers print a sandboxed frame only at its visible size, so the reason
    # points at the web page and never promises a browser-printed PDF.
    assert "opens as a web page" in reason and "Print" not in reason

    monkeypatch.setenv("RENDERER_URL", "http://renderer.invalid")
    monkeypatch.setenv("RENDERER_TOKEN", "x")
    body = client.get("/api/mr/vendor-report/periods").json()
    assert body["pdf_available"] is True and body["pdf_unavailable_reason"] is None


# --- build -------------------------------------------------------------------------------

def test_the_build_returns_the_report_as_data_with_its_run_metadata(vendor_on):
    run = _build()
    s = run["structured"]
    assert run["kind"] == "vendor_report" and run["reused"] is False
    assert run["sweep_date"] == "2026-09-02" and run["built_at"]
    assert run["template"] == {"kind": "builtin"}
    assert run["links"] == {"html": f"/api/mr/vendor-report/{run['id']}/html",
                            "pdf": f"/api/mr/vendor-report/{run['id']}/pdf"}
    assert s["portfolio"]["total_budget"] == 95_600 and s["portfolio"]["total_leads"] == 18
    assert [round(m["gap_pct"], 1) for m in s["movers"]] == [58.8, 30.9, 24.0, -33.3,
                                                              -74.0, -76.6]
    assert any(m["metric"] == "show_rate_pct" and len(m["vendors"]) == 11
               for m in s["missing"])

    again = _build()
    assert again["reused"] is True and again["id"] == run["id"]


def test_no_year_month_builds_the_newest_month(vendor_on):
    assert _build({})["structured"]["year_month"] == "2026-09"


@pytest.mark.parametrize("body, status, fragment", [
    ({"year_month": "2026-13"}, 422, "is not a month"),
    ({"year_month": 202609}, 422, "<int>"),
    ({"year_month": "2026-05"}, 422,
     "The last pull has no vendor figures for May 2026. Pull the workbook, then build again."),
    ({"year_month": "2026-09", "template": "v7"}, 422, "builtin"),
])
def test_a_build_it_cannot_honour_is_a_422_saying_why(vendor_on, body, status, fragment):
    resp = client.post("/api/mr/vendor-report", json=body)
    assert resp.status_code == status, resp.text
    assert fragment in resp.json()["detail"]


def test_template_builtin_is_accepted_explicitly(vendor_on):
    assert _build({"year_month": "2026-09", "template": "builtin"})["template"] == {
        "kind": "builtin"}


# --- the run rail ----------------------------------------------------------------------

def test_a_vendor_run_lists_reads_back_and_points_at_its_own_pdf_route(vendor_on):
    run = _build()
    listed = {r["id"]: r for r in client.get("/api/mr/runs").json()}
    assert listed[run["id"]]["kind"] == "vendor_report"
    assert listed[run["id"]]["period"] == "September 2026"
    assert client.get(f"/api/mr/runs/{run['id']}").json()["id"] == run["id"]
    resp = client.get(f"/api/mr/runs/{run['id']}/pdf")
    assert resp.status_code == 404 and "/api/mr/vendor-report/" in resp.json()["detail"]
    resp = client.post("/api/mr/reports/vendor_report")
    assert resp.status_code == 422 and "POST /api/mr/vendor-report" in resp.json()["detail"]


def test_another_workspaces_vendor_run_is_not_found(vendor_on, as_caller):
    run_id = _build()["id"]
    as_caller(OTHER)
    for suffix in ("html", "pdf"):
        assert client.get(f"/api/mr/vendor-report/{run_id}/{suffix}").status_code == 404
    assert client.get("/api/mr/vendor-report/does-not-exist/html").status_code == 404


def test_a_board_run_is_not_a_vendor_document(vendor_on, monkeypatch):
    from marketing_research_agent import runs
    runs.save_run({"id": "boardrun1", "kind": "board_report", "user_id": USER["id"],
                   "generated_at": "2026-10-01T00:00:00+00:00", "structured": {}})
    assert client.get("/api/mr/vendor-report/boardrun1/html").status_code == 404


# --- the document ---------------------------------------------------------------------

def test_the_html_is_one_self_contained_document(vendor_on):
    run_id = _build()["id"]
    resp = client.get(f"/api/mr/vendor-report/{run_id}/html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert 'filename="mr-vendor-report-2026-09-02.html"' in resp.headers["content-disposition"]
    html = resp.text.lower()
    for banned in ("<script", "<link", "@import", "http://", "https://"):
        assert banned not in html, banned
    assert "biggest movers vs. benchmark" in html and "template: built-in" in html


def test_a_stored_run_from_another_generator_is_a_422_not_a_500(vendor_on):
    from marketing_research_agent import runs
    runs.save_run({"id": "oldvendor", "kind": "vendor_report", "user_id": USER["id"],
                   "generated_at": "2026-10-01T00:00:00+00:00",
                   "structured": {"generator": "mr-vendor-report/0"}})
    resp = client.get("/api/mr/vendor-report/oldvendor/html")
    assert resp.status_code == 422 and "POST /api/mr/vendor-report" in resp.json()["detail"]


# --- the PDF, through the shared renderer client ------------------------------------

def test_an_unconfigured_renderer_is_a_503_the_user_can_act_on(vendor_on):
    run_id = _build()["id"]
    resp = client.get(f"/api/mr/vendor-report/{run_id}/pdf")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "RENDERER_URL and RENDERER_TOKEN are unset" in detail
    assert f"/api/mr/vendor-report/{run_id}/html" in detail and "Print" not in detail


def test_the_pdf_call_carries_the_google_identity_and_the_shared_secret(
        vendor_on, renderer_on, monkeypatch, _no_live_identity_token):
    pdf = _pdf_bytes()
    calls = _stub_renderer(monkeypatch, httpx.Response(200, content=pdf))
    run_id = _build()["id"]

    resp = client.get(f"/api/mr/vendor-report/{run_id}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.content == pdf
    assert 'filename="mr-vendor-report-2026-09-02.pdf"' in resp.headers["content-disposition"]
    assert calls[0]["url"] == "http://renderer.invalid/pdf"
    assert calls[0]["headers"] == {"X-Renderer-Token": "test-token-not-a-real-secret",
                                   "Authorization": f"Bearer {FAKE_ID_TOKEN}"}
    assert calls[0]["json"]["v"] == 1 and calls[0]["timeout"] is not None
    # The token's audience is the renderer's own URL.
    assert _no_live_identity_token == ["http://renderer.invalid"]


def test_the_audience_and_secret_are_stripped(vendor_on, monkeypatch, _no_live_identity_token):
    monkeypatch.setenv("RENDERER_URL", "  http://renderer.invalid/  ")
    monkeypatch.setenv("RENDERER_TOKEN", "  padded-secret \n")
    monkeypatch.setattr(mr_router, "_RENDERER_BACKOFF_SECONDS", 0)
    calls = _stub_renderer(monkeypatch, httpx.Response(200, content=_pdf_bytes()))
    run_id = _build()["id"]
    assert client.get(f"/api/mr/vendor-report/{run_id}/pdf").status_code == 200
    assert _no_live_identity_token == ["http://renderer.invalid"]
    assert calls[0]["headers"]["X-Renderer-Token"] == "padded-secret"


def test_no_identity_token_is_a_503_and_nothing_is_sent_unauthenticated(
        vendor_on, renderer_on, monkeypatch):
    def boom(audience):
        raise RuntimeError("no metadata server and no service-account key")

    monkeypatch.setattr(pdf_renderer, "_fetch_id_token", boom)
    calls = _stub_renderer(monkeypatch, httpx.Response(200, content=_pdf_bytes()))
    run_id = _build()["id"]

    resp = client.get(f"/api/mr/vendor-report/{run_id}/pdf")

    assert resp.status_code == 503
    assert "could not obtain a Google identity token" in resp.json()["detail"]
    assert calls == [], "a request was sent without the ID token"
    # Same rule on the board PDF: it is the same client.
    monkeypatch.setattr(pdf_renderer, "_fetch_id_token", lambda a: "   ")
    with pytest.raises(pdf_renderer.RendererError) as exc:
        pdf_renderer.render_pdf("<html></html>", label="t")
    assert exc.value.status == 503 and calls == []


def test_a_403_is_the_renderer_refusing_our_identity_distinct_from_a_401(
        vendor_on, renderer_on, monkeypatch):
    run_id = _build()["id"]

    _stub_renderer(monkeypatch, httpx.Response(403, text="Forbidden"))
    resp = client.get(f"/api/mr/vendor-report/{run_id}/pdf")
    assert resp.status_code == 502
    forbidden = resp.json()["detail"]
    assert "refused the backend's identity" in forbidden

    _stub_renderer(monkeypatch, httpx.Response(401, json={"error": "unauthorized"}))
    unauthorized = client.get(f"/api/mr/vendor-report/{run_id}/pdf").json()["detail"]
    assert "RENDERER_TOKEN here does not match" in unauthorized
    assert unauthorized != forbidden

    _stub_renderer(monkeypatch, httpx.Response(500, text="boom"))
    server_error = client.get(f"/api/mr/vendor-report/{run_id}/pdf").json()["detail"]
    assert "answered 500" in server_error and "identity" not in server_error


def test_the_board_pdf_goes_through_the_same_identity_path(renderer_on, monkeypatch):
    """One helper for both documents: the board PDF route reaches the renderer
    only via ``pdf_renderer.render_pdf``, so it carries the ID token too."""
    seen = {}

    def fake_render(html, *, label, html_hint="", backoff_seconds=None):
        seen["label"], seen["hint"] = label, html_hint
        raise pdf_renderer.RendererError(502, "stub")

    monkeypatch.setattr(pdf_renderer, "render_pdf", fake_render)
    with pytest.raises(Exception) as exc:
        mr_router._render_pdf_via_service("<html></html>", run_id="r1")
    assert getattr(exc.value, "status_code", None) == 502
    assert seen == {"label": "r1", "hint": mr_router._BOARD_HTML_HINT}
