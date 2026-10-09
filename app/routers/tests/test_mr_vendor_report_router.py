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
                "MR_VENDOR_REPORT", "MR_REPORT_TEMPLATES", "RENDERER_URL", "RENDERER_TOKEN",
                "MR_RUN_RETENTION_PER_KIND"):
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
    # Team templates off: the template line says the built-in, not nothing.
    assert body["template"] == {"kind": "builtin", "number": None, "set_by": None,
                                "set_by_name": None, "set_at": None}


# --- build -------------------------------------------------------------------------------

def test_the_build_returns_the_report_as_data_with_its_run_metadata(vendor_on):
    run = _build()
    s = run["structured"]
    assert run["kind"] == "vendor_report" and run["reused"] is False
    assert run["sweep_date"] == "2026-09-02" and run["built_at"]
    assert run["template"] == {"kind": "builtin", "number": None, "id": None}
    assert run["links"] == {"html": f"/api/mr/vendor-report/{run['id']}/html",
                            "pdf": f"/api/mr/vendor-report/{run['id']}/pdf"}
    assert s["portfolio"]["total_budget"] == 95_600 and s["portfolio"]["total_leads"] == 18
    assert [round(m["gap_pct"], 1) for m in s["movers"]] == [58.8, 30.9, 24.0, -33.3,
                                                              -74.0, -76.6]
    assert any(m["metric"] == "show_rate_pct" and len(m["vendors"]) == 11
               for m in s["missing"])

    again = _build()
    assert again["reused"] is True and again["id"] == run["id"]


def test_a_reused_build_records_no_second_output(vendor_on, trail_rows):
    """Building the same month twice hands back the stored run the second time:
    one report, so one trail row and one Home "generate", not two."""
    first = _build()
    again = _build()
    assert first["reused"] is False and again["reused"] is True
    assert again["id"] == first["id"]
    outputs = [r for r in trail_rows if r["action"] == "report:vendor_report"]
    assert len(outputs) == 1 and outputs[0]["usage_action"] == "generate"
    assert outputs[0]["run_id"] == first["id"]
    assert len(trail_rows) == 1
    # A different month is new work again.
    assert _build({"year_month": "2026-08"})["reused"] is False
    assert len(trail_rows) == 2


def test_no_year_month_builds_the_newest_month(vendor_on):
    assert _build({})["structured"]["year_month"] == "2026-09"


@pytest.mark.parametrize("body, status, code, fragment", [
    ({"year_month": "2026-13"}, 422, "invalid_month", "is not a month"),
    ({"year_month": 202609}, 422, "invalid_month", "<int>"),
    ({"year_month": "2026-05"}, 422, "empty_month",
     "The last pull has no vendor figures for May 2026. Pull the workbook, then build again."),
    ({"year_month": "2026-09", "template": "v7"}, 422, "invalid_template", "builtin"),
])
def test_a_build_it_cannot_honour_is_a_422_saying_why(vendor_on, body, status, code,
                                                       fragment):
    resp = client.post("/api/mr/vendor-report", json=body)
    assert resp.status_code == status, resp.text
    payload = resp.json()
    assert payload["code"] == code
    assert fragment in payload["detail"] and payload["reason"] == payload["detail"]


def test_template_builtin_is_accepted_explicitly(vendor_on):
    assert _build({"year_month": "2026-09", "template": "builtin"})["template"] == {
        "kind": "builtin", "number": None, "id": None}


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


# =========================================================================== #
# Team report templates (/api/mr/report-templates*)
# =========================================================================== #
# ``extract_layout`` is stubbed in every test that reaches it: no model, no
# network. Uploads are real bytes through the real sniffer.

from marketing_research_agent import report_templates as rt  # noqa: E402
from marketing_research_agent import template_extract as tx  # noqa: E402
from marketing_research_agent import vendor_report_render as vrr  # noqa: E402

TEMPLATE_ROUTES = [
    ("POST", "/api/mr/report-templates"),
    ("POST", "/api/mr/report-templates/extract"),
    ("POST", "/api/mr/report-templates/check-html"),
    ("POST", "/api/mr/report-templates/preview"),
    ("POST", "/api/mr/report-templates/builtin/activate"),
    ("GET", "/api/mr/report-templates/starter.html"),
]
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF"
LAYOUT = {"theme": {"colors": {"ink": "#101010"}},
          "sections": [{"type": "header"}, {"type": "vendor_scorecard"},
                       {"type": "data_gaps"}]}
HTML = (b"<!DOCTYPE html><html><head><title>Ours</title></head><body>"
        b"<h1>Spend {{total_spend}}</h1><div>{{table:vendor_scorecard}}</div></body></html>")
COLLEAGUE = {"id": "u-colleague", "email": "colleague@legalsoft.com"}


@pytest.fixture()
def templates_on(monkeypatch):
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")
    monkeypatch.setenv("MR_REPORT_TEMPLATES", "1")


@pytest.fixture()
def shared(monkeypatch):
    monkeypatch.setenv("MR_WORKSPACE_SHARED", "1")
    monkeypatch.setenv("MR_WORKSPACE_ID", "team-ws")


@pytest.fixture()
def trail_rows(monkeypatch):
    from app.services import run_tracking

    rows: list[dict] = []
    monkeypatch.setattr(run_tracking, "record_activity",
                        lambda user, **kw: rows.append({"user": user.get("email"), **kw}))
    return rows


@pytest.fixture()
def reader(monkeypatch):
    """Stands in for the model: records each call, answers from a queue."""
    calls: list[dict] = []
    answers: list = []

    def fake(data, kind, *, filename, activity=None):
        calls.append({"kind": kind, "filename": filename, "size": len(data)})
        answer = answers.pop(0) if answers else _reading()
        if isinstance(answer, Exception):
            raise answer
        if activity is not None:
            activity.note(f"Read sample report {filename!r}")
        return answer

    monkeypatch.setattr(tx, "extract_layout", fake)
    return calls, answers


def _reading(layout=None) -> dict:
    return {"layout": layout or LAYOUT,
            "unsupported": [{"title": "Social reach", "description": "a follower chart"}],
            "matched_count": 3, "model": "stub", "notes": [{"kind": "policy", "detail": "x"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02, "calls": 1},
            "source": {"filename": "s.pdf", "kind": "pdf", "pages": 1, "images": 1},
            "prompt_version": "test"}


def _upload(path: str, name: str, data: bytes, mime: str = "application/octet-stream"):
    return client.post(path, files={"file": (name, data, mime)})


def _save(body: dict) -> dict:
    resp = client.post("/api/mr/report-templates", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _listing() -> dict:
    resp = client.get("/api/mr/report-templates")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the switch ------------------------------------------------------------------------

@pytest.mark.parametrize("vendor, templates", [("0", "0"), ("1", "0"), ("0", "1")])
def test_template_routes_are_dark_unless_both_switches_are_on(monkeypatch, vendor, templates):
    monkeypatch.setenv("MR_VENDOR_REPORT", vendor)
    monkeypatch.setenv("MR_REPORT_TEMPLATES", templates)
    body = _listing()
    assert body["enabled"] is False and body["versions"] == [] and body["active"] is None
    for method, path in TEMPLATE_ROUTES:
        resp = client.request(method, path, json={})
        assert resp.status_code == 404, (method, path, resp.text)


def test_an_anonymous_caller_gets_401_on_every_template_route(templates_on, unauthenticated):
    unauthenticated()
    for method, path in TEMPLATE_ROUTES + [("GET", "/api/mr/report-templates")]:
        assert client.request(method, path, json={}).status_code == 401, path


# --- the listing -------------------------------------------------------------------------

def test_the_listing_carries_the_builtin_live_examples_and_the_allowance(templates_on):
    body = _listing()
    assert body["enabled"] is True
    assert body["active"]["kind"] == "builtin" and body["active"]["id"] == "builtin"
    assert body["versions"] == []
    assert body["readings_left_today"] == 10
    assert body["examples_from"] == {"year_month": "2026-09", "label": "September 2026"}
    by_token = {p["token"]: p for p in body["placeholders"]}
    assert by_token["{{total_spend}}"]["example"] == "$2,737"
    assert by_token["{{total_spend}}"]["kind"] == "scalar"
    assert by_token["{{table:vendor_scorecard}}"]["kind"] == "table"
    assert all(set(p) == {"token", "title", "description", "kind", "example"}
               for p in body["placeholders"])
    assert by_token["{{table:vendor_scorecard}}"]["title"] == "Vendor scorecard"
    assert by_token["{{total_spend}}"]["title"] == "Total Spend"
    assert body["default_layout"] == vrr.layout_to_dict(vrr.DEFAULT_LAYOUT)
    assert body["limits"] == {"readings_per_day": 10, "readings_reset": "00:00 UTC",
                              "pdf_max_bytes": 10 * 1024 * 1024, "pdf_max_pages": 10,
                              "image_max_bytes": 5 * 1024 * 1024, "image_max_side": 4096,
                              "html_max_bytes": 512 * 1024}


def test_with_no_vendor_data_the_examples_are_null_not_invented(templates_on, tmp_path,
                                                                 monkeypatch):
    empty = tmp_path / "empty-snaps"
    empty.mkdir()
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(empty))
    body = _listing()
    assert body["examples_from"] is None
    assert all(p["example"] is None for p in body["placeholders"])


# --- reading a sample (extract) ------------------------------------------------------------

def test_a_sample_is_read_into_a_layout_with_a_preview_in_real_figures(
        templates_on, reader, trail_rows):
    calls, _ = reader
    resp = _upload("/api/mr/report-templates/extract", "../q3 sample.html", PDF)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert calls == [{"kind": "pdf", "filename": "q3 sample.html", "size": len(PDF)}]
    assert body["source_kind"] == "pdf"
    assert body["upload"]["kind"] == "pdf" and len(body["upload"]["sha256"]) == 64
    assert body["upload"]["filename"] == "q3 sample.html"      # named .html, read as PDF
    assert body["layout"] == LAYOUT and body["matched_count"] == 3
    assert body["unsupported"] == [{"title": "Social reach", "description": "a follower chart"}]
    assert body["notes"] == [{"kind": "policy", "detail": "x"}]
    assert body["preview_html"].startswith("<!DOCTYPE html>")
    assert "Vendor scorecard" in body["preview_html"]
    assert "Biggest movers vs. benchmark" not in body["preview_html"]
    assert "a preview of an unsaved template" in body["preview_html"]
    assert body["preview_unavailable_reason"] is None
    assert body["readings_left_today"] == 9
    assert [r["action"] for r in trail_rows] == ["template_extract"]
    assert trail_rows[0]["usage_action"] == "generate"
    assert _listing()["versions"] == []                       # reading saves nothing


def test_an_html_upload_to_extract_is_checked_and_costs_no_reading(templates_on, reader,
                                                                    trail_rows):
    calls, _ = reader
    resp = _upload("/api/mr/report-templates/extract", "t.html", HTML, "text/html")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_kind"] == "html" and body["can_save"] is True
    assert calls == [] and trail_rows == []
    assert _listing()["readings_left_today"] == 10


@pytest.mark.parametrize("name, data, fragment", [
    ("logo.gif", b"GIF89a\x01\x00\x01\x00", "GIF"),
    ("deck.pptx", b"PK\x03\x04\x14\x00", "zip or Office"),
    ("empty.pdf", b"", "empty"),
])
def test_a_file_that_is_not_a_sample_is_refused_before_any_reading(
        templates_on, reader, name, data, fragment):
    calls, _ = reader
    resp = _upload("/api/mr/report-templates/extract", name, data)
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_file" and fragment in resp.json()["reason"]
    assert calls == [] and _listing()["readings_left_today"] == 10


def test_an_oversized_upload_is_refused_without_reading_it_all(templates_on, reader):
    calls, _ = reader
    resp = _upload("/api/mr/report-templates/extract", "big.pdf",
                   b"%PDF-1.7" + b"0" * (10 * 1024 * 1024))
    assert resp.status_code == 422 and resp.json()["code"] == "invalid_file"
    assert calls == []


def test_ten_readings_a_day_per_workspace_and_a_failed_billed_call_counts(
        templates_on, reader, trail_rows, caplog):
    calls, answers = reader
    answers.append(tx.TemplateExtractionError(
        "The AI reader did not answer within 150 seconds.", code="timeout",
        usage={"input_tokens": 900, "output_tokens": 0, "cost_usd": 0.01, "calls": 1}))
    answers.append(tx.TemplateExtractionError(
        "The AI reader is offline here.", code="offline"))
    with caplog.at_level("WARNING", logger="agentos.mr"):
        billed = _upload("/api/mr/report-templates/extract", "a.pdf", PDF)
    assert billed.status_code == 503
    assert billed.json() == {"code": "timeout",
                             "reason": "The AI reader did not answer within 150 seconds.",
                             "detail": "The AI reader did not answer within 150 seconds.",
                             "billed": True, "readings_left_today": 9}
    assert "workspace=u1" in caplog.text and "code=timeout" in caplog.text
    unbilled = _upload("/api/mr/report-templates/extract", "a.pdf", PDF)
    assert unbilled.status_code == 503 and unbilled.json()["billed"] is False
    assert unbilled.json()["readings_left_today"] == 9         # never reached the provider
    assert trail_rows == []                                     # no output, no dashboard tick
    for i in range(9):
        assert _upload("/api/mr/report-templates/extract", "a.pdf", PDF).status_code == 200
    assert _listing()["readings_left_today"] == 0
    refused = _upload("/api/mr/report-templates/extract", "a.pdf", PDF)
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert refused.json()["readings_left_today"] == 0 and "00:00 UTC" in refused.json()["reason"]
    assert len(calls) == 11                                     # the 12th never ran
    assert len(trail_rows) == 9


def test_the_allowance_is_the_workspaces_not_each_members(templates_on, reader, shared,
                                                          as_caller):
    for _ in range(10):
        assert _upload("/api/mr/report-templates/extract", "a.pdf", PDF).status_code == 200
    as_caller(COLLEAGUE)
    assert _upload("/api/mr/report-templates/extract", "a.pdf", PDF).status_code == 429


@pytest.mark.parametrize("code, status", [
    ("no_key", 503), ("offline", 503), ("provider_error", 503), ("timeout", 503),
    ("refused", 502), ("truncated", 502), ("invalid_output", 502),
    ("unreadable", 422), ("encrypted", 422), ("too_large", 422), ("too_many_pages", 422),
    ("cost_ceiling", 422), ("nothing_matched", 422), ("something_new", 502),
])
def test_every_reader_failure_has_a_status_and_a_reason(templates_on, reader, code, status):
    _, answers = reader
    unsupported = [{"title": "Social", "description": ""}] if code == "nothing_matched" else None
    answers.append(tx.TemplateExtractionError(f"reason for {code}", code=code,
                                              unsupported=unsupported))
    resp = _upload("/api/mr/report-templates/extract", "a.pdf", PDF)
    assert resp.status_code == status
    body = resp.json()
    assert body["code"] == code and body["reason"] == f"reason for {code}"
    assert ("unsupported" in body) is (unsupported is not None)


# --- checking HTML, previewing a hand-arranged layout ----------------------------------------

def test_check_html_answers_errors_or_a_preview_and_never_counts_a_reading(templates_on):
    ok = _upload("/api/mr/report-templates/check-html", "t.html", HTML, "text/html").json()
    assert ok["can_save"] is True and ok["errors"] == []
    assert ok["placeholders_used"] == ["{{total_spend}}", "{{table:vendor_scorecard}}"]
    assert ok["preview_html"].startswith('<!DOCTYPE html><html lang="en"><head>' + rt.CSP_META)
    assert "$2,737" in ok["preview_html"]
    assert ok["upload"]["kind"] == "html" and ok["source_kind"] == "html"

    bad = _upload("/api/mr/report-templates/check-html", "t.html",
                  b"<p>{{totl_spend}}</p><script>x</script>", "text/html").json()
    assert bad["can_save"] is False and bad["preview_html"] is None
    assert bad["errors"][0]["suggestion"] == "{{total_spend}}"
    assert bad["removed"]["scripts"] == 1
    assert _listing()["readings_left_today"] == 10

    pdf = _upload("/api/mr/report-templates/check-html", "t.html", PDF)
    assert pdf.status_code == 422 and pdf.json()["code"] == "not_html"


def test_a_hand_arranged_layout_previews_or_is_refused_with_the_reason(templates_on):
    resp = client.post("/api/mr/report-templates/preview", json={"layout": LAYOUT})
    assert resp.status_code == 200 and "Vendor scorecard" in resp.json()["preview_html"]
    for layout, fragment in (({"sections": [{"type": "pie"}]}, "unknown section type"),
                             (None, "must be an object"),
                             ({"theme": {"colors": {"ink": "red}</style>"}},
                               "sections": [{"type": "header"}]}, "must be a colour")):
        resp = client.post("/api/mr/report-templates/preview", json={"layout": layout})
        assert resp.status_code == 422 and resp.json()["code"] == "invalid_layout"
        assert fragment in resp.json()["reason"]


# --- saving and switching ------------------------------------------------------------------

def test_any_member_can_save_and_switch_the_workspace_template(templates_on, shared, as_caller,
                                                               trail_rows):
    first = _save({"source_kind": "pdf", "filename": "C:\\docs\\Q3 sample.pdf",
                   "layout": LAYOUT})
    v1 = first["version"]
    assert v1["number"] == 1 and v1["kind"] == "layout" and v1["source_kind"] == "pdf"
    assert v1["filename"] == "Q3 sample.pdf" and v1["created_by"] == USER["email"]
    assert first["active"] == v1

    as_caller(COLLEAGUE)                                   # not an admin, not the author
    listed = _listing()
    assert [v["id"] for v in listed["versions"]] == [v1["id"]]
    v2 = _save({"source_kind": "html", "filename": "ours.html",
                "html": HTML.decode()})["version"]
    assert v2["number"] == 2 and v2["created_by"] == COLLEAGUE["email"]

    resp = client.post(f"/api/mr/report-templates/{v1['id']}/activate")
    assert resp.status_code == 200, resp.text
    active = resp.json()["active"]
    assert active["id"] == v1["id"] and active["number"] == 1
    assert active["created_by"] == USER["email"] and active["set_by"] == COLLEAGUE["email"]
    listed = _listing()
    assert listed["active"]["id"] == v1["id"]
    assert [(v["number"], v["active"]) for v in listed["versions"]] == [(2, False), (1, True)]

    again = client.post(f"/api/mr/report-templates/{v1['id']}/activate")
    assert again.json()["active"]["set_at"] == active["set_at"]     # nothing changed
    back = client.post("/api/mr/report-templates/builtin/activate").json()["active"]
    assert back["kind"] == "builtin" and back["set_by"] == COLLEAGUE["email"]
    assert [r["action"] for r in trail_rows] == ["template_save", "template_save",
                                                 "template_activate", "template_activate"]
    assert all(r["usage_action"] is None for r in trail_rows)     # audit, not output

    periods = client.get("/api/mr/vendor-report/periods").json()
    assert periods["template"] == {"kind": "builtin", "number": None,
                                   "set_by": COLLEAGUE["email"],
                                   "set_by_name": COLLEAGUE["email"],   # no profile name
                                   "set_at": back["set_at"]}


def test_a_version_from_another_workspace_is_not_found(templates_on, as_caller):
    v = _save({"source_kind": "builder", "layout": LAYOUT})["version"]
    as_caller(OTHER)                                       # unshared: a different workspace
    resp = client.post(f"/api/mr/report-templates/{v['id']}/activate")
    assert resp.status_code == 404 and resp.json()["code"] == "not_found"
    assert _listing()["versions"] == [] and _listing()["active"]["kind"] == "builtin"
    assert client.post("/api/mr/report-templates/nope/activate").status_code == 404


def test_the_save_rechecks_html_and_stores_only_the_sanitized_text(templates_on):
    tampered = ('<p onclick="steal()">{{total_spend}}</p><script>alert(1)</script>'
                '<img src="https://evil.example/x.png"><a href="javascript:x">j</a>')
    saved = _save({"source_kind": "html", "html": tampered})["version"]
    from marketing_research_agent import runs
    stored = runs.find_template_version("u1", saved["id"])["html"]
    for gone in ("onclick", "<script", "evil.example", "javascript"):
        assert gone not in stored
    assert "{{total_spend}}" in stored

    for body, code in (
            ({"source_kind": "html", "html": "<p>{{nope}}</p>"}, "template_invalid"),
            ({"source_kind": "html", "html": "<p>Spend was $2,737.</p>"}, "template_invalid"),
            ({"source_kind": "html", "html": "<p>{{total_spend}}</p>", "layout": LAYOUT},
             "invalid_template"),
            ({"source_kind": "pdf", "layout": LAYOUT, "html": "<p>x</p>"}, "invalid_template"),
            ({"source_kind": "docx", "layout": LAYOUT}, "invalid_template"),
            ({"source_kind": "builder", "layout": {"sections": [{"type": "header",
                                                                 "options": {"kpis": 5}}]}},
             "invalid_layout"),
            ({"source_kind": "image",
              "layout": {"theme": {"colors": {"ink": "#000}</style><script>"}},
                         "sections": [{"type": "header"}]}}, "invalid_layout")):
        resp = client.post("/api/mr/report-templates", json=body)
        assert resp.status_code == 422, (body, resp.text)
        assert resp.json()["code"] == code, (body, resp.json())
    bad = client.post("/api/mr/report-templates",
                      json={"source_kind": "html", "html": "<p>{{nope}}</p>"}).json()
    assert bad["errors"][0]["placeholder"] == "{{nope}}"
    assert [v["number"] for v in _listing()["versions"]] == [1]


def test_the_starter_downloads_as_a_file_that_passes_its_own_check(templates_on):
    resp = client.get("/api/mr/report-templates/starter.html")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == (
        'attachment; filename="vendor-report-template-starter.html"')
    assert rt.check_html(resp.content).can_save


# --- building with the team template ---------------------------------------------------------

def test_a_build_uses_the_team_template_and_its_document_renders_through_it(templates_on):
    v = _save({"source_kind": "pdf", "layout": LAYOUT})["version"]
    run = _build()
    assert run["template"] == {"kind": "layout", "number": 1, "id": v["id"]}
    html = client.get(f"/api/mr/vendor-report/{run['id']}/html")
    assert html.status_code == 200
    assert html.headers["content-security-policy"].startswith("default-src 'none'")
    assert "Vendor scorecard" in html.text and "Biggest movers" not in html.text
    assert "Template: team template, version 1." in html.text
    assert client.get("/api/mr/vendor-report/periods").json()["template"]["number"] == 1


def test_an_html_team_template_builds_and_renders_with_its_csp(templates_on):
    _save({"source_kind": "html", "html": HTML.decode()})
    run = _build()
    assert run["template"]["kind"] == "html"
    doc = client.get(f"/api/mr/vendor-report/{run['id']}/html").text
    assert doc.startswith('<!DOCTYPE html><html lang="en"><head>' + rt.CSP_META)
    assert "Spend $2,737" in doc


def test_a_failing_team_template_is_a_409_and_the_builtin_rebuild_says_why(
        templates_on, monkeypatch, trail_rows):
    v = _save({"source_kind": "pdf", "layout": LAYOUT})["version"]
    trail_rows.clear()
    real = rt.render_with_template

    def team_breaks(report, version):
        if version.get("id") == v["id"]:
            raise rt.TemplateRenderError("This template can't be rendered: it broke.")
        return real(report, version)

    monkeypatch.setattr(rt, "render_with_template", team_breaks)
    resp = client.post("/api/mr/vendor-report", json={"year_month": "2026-09"})
    assert resp.status_code == 409
    assert resp.json() == {"code": "template_failed",
                           "reason": "This template can't be rendered: it broke.",
                           "detail": "This template can't be rendered: it broke.",
                           "can_use_builtin": True,
                           "template": {"kind": "layout", "number": 1, "id": v["id"]}}
    assert trail_rows == []                                 # nothing was built
    from marketing_research_agent import runs
    assert runs.list_runs("u1", kind="vendor_report") == []

    rebuilt = _build({"year_month": "2026-09", "template": "builtin"})
    assert rebuilt["template"]["kind"] == "builtin"
    assert rebuilt["template"]["fallback"]["number"] == 1
    doc = client.get(f"/api/mr/vendor-report/{rebuilt['id']}/html").text
    assert doc.count(vrr.FALLBACK_SENTENCE) == 2


def test_a_report_built_with_a_team_template_is_not_quietly_rerendered_in_another(
        templates_on, monkeypatch):
    _save({"source_kind": "pdf", "layout": LAYOUT})
    run_id = _build()["id"]
    monkeypatch.setenv("MR_REPORT_TEMPLATES", "0")         # the kill switch
    for suffix in ("html", "pdf"):
        resp = client.get(f"/api/mr/vendor-report/{run_id}/{suffix}")
        assert resp.status_code == 409, suffix
        assert resp.json()["code"] == "template_failed"
        assert "switched off" in resp.json()["reason"] and resp.json()["can_use_builtin"]
    # Off, a new build is the plain built-in — the team template is ignored.
    assert _build({"year_month": "2026-09"})["template"] == {
        "kind": "builtin", "number": None, "id": None}


# --- 2026-10-09 security audit -------------------------------------------------------------

AUDIT_REPRO = b"<svg><style>" + b"<b><div>" * 65530 + b"</style></svg>{{total_spend}}"


def test_the_audit_repro_is_refused_fast_on_every_path_in(templates_on, reader):
    """72-84 s of html5ever CPU before the fix, on all three routes: the JSON
    save never sniffs, and the sniffer passes anything not STARTING with <svg."""
    calls, _ = reader
    import time

    prefixed = (b"<p>x</p><svg><style>" + b"<b><div>" * 65529
                + b"</style></svg>{{total_spend}}")         # 512 KB, passes the sniffer
    started = time.perf_counter()                  # the bare repro: the sniffer says SVG
    resp = _upload("/api/mr/report-templates/check-html", "t.html", AUDIT_REPRO, "text/html")
    assert time.perf_counter() - started < 2.0
    assert resp.status_code == 422 and resp.json()["code"] == "invalid_file"
    for path in ("check-html", "extract"):
        started = time.perf_counter()
        resp = _upload(f"/api/mr/report-templates/{path}", "t.html", prefixed, "text/html")
        assert time.perf_counter() - started < 2.0, path
        assert resp.status_code == 200 and resp.json()["can_save"] is False, path
        assert "formatting tags" in resp.json()["errors"][0]["message"]
    started = time.perf_counter()
    resp = client.post("/api/mr/report-templates",
                       json={"source_kind": "html", "html": prefixed.decode()})
    assert time.perf_counter() - started < 2.0
    assert resp.status_code == 422 and resp.json()["code"] == "template_invalid"
    assert calls == [] and _listing()["versions"] == []


_MULTIPART = "multipart/form-data; boundary=xyz"


@pytest.mark.parametrize("path, limit, ctype", [
    ("/api/mr/report-templates/extract", mr_router._BODY_LIMITS["extract"], _MULTIPART),
    ("/api/mr/report-templates/check-html", mr_router._BODY_LIMITS["check-html"], _MULTIPART),
    ("/api/mr/report-templates/preview", mr_router._BODY_LIMITS["preview"], "application/json"),
    ("/api/mr/report-templates", mr_router._BODY_LIMITS["save"], "application/json"),
    ("/api/mr/report-templates/builtin/activate", mr_router._BODY_LIMITS["activate"], None),
])
def test_an_oversized_body_is_refused_before_it_is_read(templates_on, path, limit, ctype):
    declared = client.post(path, content=b"x", headers={
        "content-type": ctype or "application/json", "content-length": str(limit + 1)})
    assert declared.status_code == 413 and declared.json()["code"] == "too_large"
    if ctype is None:
        return                             # a route with no body never reads one

    pulled: list[int] = []

    def chunks():                          # no Content-Length: a chunked stream
        head = (b"--xyz\r\nContent-Disposition: form-data; name=\"file\"; "
                b"filename=\"a.pdf\"\r\n\r\n")
        yield head if ctype == _MULTIPART else b'{"html": "'
        for _ in range(limit // 65536 + 8):
            pulled.append(1)
            yield b"a" * 65536

    streamed = client.post(path, content=chunks(), headers={"content-type": ctype})
    assert streamed.status_code == 413 and streamed.json()["code"] == "too_large"


@pytest.mark.parametrize("path, ctype", [
    ("/api/mr/report-templates/extract", _MULTIPART),
    ("/api/mr/report-templates/check-html", _MULTIPART),
    ("/api/mr/report-templates", "application/json"),
])
def test_the_streamed_cap_stops_reading_at_the_limit(templates_on, path, ctype):
    """The test client buffers a request body itself, so this drives the route's
    handler directly with a receive() that never ends and counts what it pulls."""
    import asyncio

    from fastapi import Request

    from app.main import app as fastapi_app

    # The MR router's own route object: FastAPI 0.141 mounts included routers
    # lazily, so ``app.routes`` holds no per-route objects to look up.
    route = next(r for r in mr_router.router.routes
                 if "/api" + getattr(r, "path", "") == path and "POST" in r.methods)
    pulled = 0

    head = (b"--xyz\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n\r\n"
            if ctype == _MULTIPART else b'{"html": "')

    async def endless():
        nonlocal pulled
        pulled += 1
        body = head if pulled == 1 else b"a" * 65536
        return {"type": "http.request", "body": body, "more_body": True}

    from contextlib import AsyncExitStack

    async def drive():
        async with AsyncExitStack() as outer, AsyncExitStack() as inner:
            scope = {"type": "http", "method": "POST", "path": path, "root_path": "",
                     "query_string": b"", "headers": [(b"content-type", ctype.encode())],
                     "app": fastapi_app, "fastapi_middleware_astack": outer,
                     "fastapi_inner_astack": inner}
            return await route.get_route_handler()(Request(scope, endless))

    resp = asyncio.run(drive())
    assert resp.status_code == 413
    assert pulled <= route.body_limit // 65536 + 2


def test_a_burst_of_40_simultaneous_readings_admits_exactly_10(templates_on, reader):
    import threading

    calls, _ = reader
    gate = threading.Barrier(40, timeout=20)
    statuses: list[int] = []

    def one():
        gate.wait()
        statuses.append(_upload("/api/mr/report-templates/extract", "a.pdf", PDF).status_code)

    threads = [threading.Thread(target=one) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(statuses) == [200] * 10 + [429] * 30
    assert len(calls) == 10
    assert _listing()["readings_left_today"] == 0


def test_a_checker_that_cannot_run_is_a_503_and_nothing_is_saved(templates_on, monkeypatch):
    def unavailable(raw):
        raise rt.TemplateCheckUnavailable("the template checker did not start")

    monkeypatch.setattr(rt, "check_html", unavailable)
    resp = _upload("/api/mr/report-templates/check-html", "t.html", HTML, "text/html")
    assert resp.status_code == 503 and resp.json()["code"] == "check_unavailable"
    resp = client.post("/api/mr/report-templates",
                       json={"source_kind": "html", "html": HTML.decode()})
    assert resp.status_code == 503 and resp.json()["code"] == "check_unavailable"
    assert _listing()["versions"] == []


# --- contract additions for the template UI (2026-10-09) ------------------------------------

def test_the_listing_offers_the_builtin_layout_to_arrange_from(templates_on, monkeypatch):
    body = _listing()
    assert body["default_layout"]["sections"][0] == {"type": "header", "title": None,
                                                     "options": {}}
    assert vrr.layout_from_dict(body["default_layout"]) is not None
    monkeypatch.setenv("MR_REPORT_TEMPLATES", "0")
    assert _listing()["default_layout"] is None


def test_a_versions_layout_is_one_read_and_scoped_to_the_workspace(templates_on, as_caller):
    layout_v = _save({"source_kind": "pdf", "layout": LAYOUT})["version"]
    html_v = _save({"source_kind": "html", "html": HTML.decode()})["version"]

    resp = client.get(f"/api/mr/report-templates/{layout_v['id']}/layout")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["id"], body["kind"], body["number"]) == (layout_v["id"], "layout", 1)
    assert [s["type"] for s in body["layout"]["sections"]] == [
        "header", "vendor_scorecard", "data_gaps"]
    assert body["layout"]["theme"]["colors"]["ink"] == "#101010"

    builtin = client.get("/api/mr/report-templates/builtin/layout").json()
    assert builtin["kind"] == "builtin"
    assert builtin["layout"] == vrr.layout_to_dict(vrr.DEFAULT_LAYOUT)

    resp = client.get(f"/api/mr/report-templates/{html_v['id']}/layout")
    assert resp.status_code == 422 and resp.json()["code"] == "not_a_layout"
    assert client.get("/api/mr/report-templates/nope/layout").status_code == 404

    as_caller(OTHER)                                  # unshared: another workspace
    resp = client.get(f"/api/mr/report-templates/{layout_v['id']}/layout")
    assert resp.status_code == 404 and resp.json()["code"] == "not_found"


def test_the_layout_route_is_dark_with_the_feature(monkeypatch):
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")
    assert client.get("/api/mr/report-templates/builtin/layout").status_code == 404


def test_a_missing_preview_says_why_in_a_code_the_console_can_act_on(
        templates_on, reader, tmp_path, monkeypatch):
    ok = client.post("/api/mr/report-templates/preview", json={"layout": LAYOUT}).json()
    assert ok["preview_html"] and ok["preview_unavailable_code"] is None

    bad = _upload("/api/mr/report-templates/check-html", "t.html", b"<p>{{nope}}</p>",
                  "text/html").json()
    assert bad["preview_unavailable_code"] == "template_failed"

    def broken(report, version):
        raise rt.TemplateRenderError("it broke")

    monkeypatch.setattr(rt, "render_with_template", broken)
    failed = client.post("/api/mr/report-templates/preview", json={"layout": LAYOUT}).json()
    assert (failed["preview_unavailable_code"], failed["preview_unavailable_reason"]) == (
        "template_failed", "it broke")
    monkeypatch.undo()
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")
    monkeypatch.setenv("MR_REPORT_TEMPLATES", "1")
    monkeypatch.setattr(tx, "extract_layout", lambda data, kind, **kw: _reading())

    from marketing_research_agent import snapshots

    def store_down(*a, **k):
        raise snapshots.SnapshotStoreError("down")

    monkeypatch.setattr(snapshots, "vendor_sweep", store_down)
    down = _upload("/api/mr/report-templates/extract", "a.pdf", PDF).json()
    assert down["preview_unavailable_code"] == "store_unavailable" and down["layout"]

    monkeypatch.undo()
    monkeypatch.setenv("MR_VENDOR_REPORT", "1")
    monkeypatch.setenv("MR_REPORT_TEMPLATES", "1")
    empty = tmp_path / "no-snaps"
    empty.mkdir()
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(empty))
    nodata = _upload("/api/mr/report-templates/check-html", "t.html", HTML, "text/html").json()
    assert nodata["can_save"] is True
    assert nodata["preview_unavailable_code"] == "no_data" and nodata["preview_html"] is None


def test_history_shows_display_names_and_falls_back_to_the_email(templates_on, shared,
                                                                 as_caller, monkeypatch):
    from app.services import firestore_repo

    names = {USER["id"]: {"name": "Tara  Lee"}, COLLEAGUE["id"]: {}}
    monkeypatch.setattr(firestore_repo, "get_users_by_ids",
                        lambda ids: {i: names[i] for i in ids if i in names})
    v1 = _save({"source_kind": "pdf", "layout": LAYOUT})["version"]
    assert v1["created_by_name"] == "Tara Lee" and v1["set_by_name"] == "Tara Lee"
    assert v1["created_by"] == USER["email"]                   # the email stays as it was

    as_caller(COLLEAGUE)                                       # a profile with no name
    _save({"source_kind": "builder", "layout": LAYOUT})
    active = client.post(f"/api/mr/report-templates/{v1['id']}/activate").json()["active"]
    assert active["created_by_name"] == "Tara Lee"             # the author's, kept
    assert active["set_by_name"] == COLLEAGUE["email"]         # the switcher's, by email
    listed = _listing()
    assert listed["active"]["set_by_name"] == COLLEAGUE["email"]
    assert [v["created_by_name"] for v in listed["versions"]] == [COLLEAGUE["email"],
                                                                  "Tara Lee"]
    template = client.get("/api/mr/vendor-report/periods").json()["template"]
    assert template["set_by_name"] == COLLEAGUE["email"] and template["number"] == 1

    def unreadable(ids):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(firestore_repo, "get_users_by_ids", unreadable)
    saved = _save({"source_kind": "builder", "layout": LAYOUT})["version"]
    assert saved["created_by_name"] == COLLEAGUE["email"]      # cosmetic: never blocks
