"""The two contracts that were written down before they were wired up.

C-2 and C-4 were each specified in prose and then implemented by different
agents, in different waves, on opposite sides of a seam. Both sides have their
own tests and both suites are green — which proves each half does what its
author intended, and nothing at all about whether the two halves meet.

  C-2  ``geo_poll.poll_step`` emits ``terminal`` / ``terminal_reason``  →
       ``app/routers/geo.py`` puts them on the wire  →
       ``components/console/geo/pollLoop.ts`` stops the loop on them.
       Covered separately: geo_poll's unit tests (the values), pollLoop's unit
       tests (the decisions). Uncovered: that the HTTP response in between
       actually carries the fields, under the names the console reads.

  C-4  ``sheets_source.fetch_official_totals`` raises ``SheetsUnavailable``  →
       ``marketing_research._pull_and_swap`` keeps the previous figures.
       Covered separately: sheets_source raising (test_sheets_transport), the
       router degrading (test_mr_router, using a plain ``RuntimeError``).
       Uncovered: that the *real* exception class travels that path — a
       consumer narrowing its ``except`` clause, or the producer moving to a
       type that is no longer a RuntimeError, breaks the pair while both
       suites stay green.

Fully offline: no Google client is built, no engine is polled, no LLM is called.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ["SEO_OFFLINE"] = "1"
os.environ["MR_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers the agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user

client = TestClient(fastapi_app)

USER = {"id": "u-contract", "email": "t@legalsoft.com", "is_admin": False,
        "is_creator": True, "session_id": "", "timezone": "UTC"}

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "newfrontend"


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    """Save/restore, never an import-time write — see
    tests/test_dependency_override_hygiene.py for why that matters."""
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


# =========================================================================== #
# C-2 — the GEO poll's stop signal, backend module → HTTP → console loop
# =========================================================================== #

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}

# What the console needs in order to stop. Named here so a rename on either
# side is a failure and not a silently-ignored `undefined` in the browser.
C2_FIELDS = ("done", "total", "calls_used_today", "daily_cap", "capped",
             "terminal", "terminal_reason")


@pytest.fixture()
def geo(monkeypatch, tmp_path):
    """The real geo modules, with only the paid engine adapters faked."""
    from final_geo_agent import geo_engines
    from seo_geo_agent import insights

    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "geo_state"))
    # A real OPENROUTER_API_KEY in a developer .env must never turn this into a
    # live paid poll.
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False,
                                 "chatgpt": False, "aio": False})
    return geo_engines


def _answer(engine, prompt, *, error: str = ""):
    from final_geo_agent.geo_engines import EngineAnswer

    return EngineAnswer(
        engine=engine, model="fake",
        text="" if error else f"Legal Soft leads on: {prompt}",
        citations=[], error=error,
    )


def _put_prompts(n=2):
    prompts = [{"id": f"p{i}", "text": f"best legal va {i}", "intent": "category",
                "stage": "consideration", "enabled": True} for i in range(1, n + 1)]
    resp = client.put(f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": prompts})
    assert resp.status_code == 200, resp.text


def _step(**body):
    return client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                       json={"runs": 1, "batch_size": 10, **body})


def test_healthy_poll_response_carries_the_stop_signal_on_the_wire(geo, monkeypatch):
    """The console reads these off the HTTP body, not off a Python dict."""
    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(2)
    resp = _step()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in C2_FIELDS:
        assert field in body, f"{field} never reached the wire: {sorted(body)}"
    assert body["terminal"] is False
    assert body["terminal_reason"] is None


def test_a_dead_engine_key_reaches_the_console_as_terminal_plus_a_reason(geo, monkeypatch):
    """The failure the contract exists for. `done` counts only non-errored
    answers, so without `terminal` the console's loop would never satisfy
    `done >= total` and would spend the whole daily cap discovering that."""
    monkeypatch.setattr(
        geo, "poll_engine",
        lambda e, p: _answer(e, p, error="HTTP 401: invalid api key"),
    )
    _put_prompts(2)
    body = _step().json()
    assert body["terminal"] is True
    assert isinstance(body["terminal_reason"], str) and body["terminal_reason"].strip()
    # The console prints this string verbatim — it has to name the cause.
    assert "perplexity" in body["terminal_reason"]
    assert "401" in body["terminal_reason"]


def test_the_router_neither_renames_nor_drops_a_single_field(geo, monkeypatch):
    """`poll_step` returns the agent's dict untouched today. If a response model
    or a post-processing step is ever added, this says so before the console
    starts reading `undefined`."""
    from final_geo_agent import geo_poll

    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(1)
    wire = _step().json()

    reference = geo_poll._progress(
        done=0, total=0, used=0, cap=0, engines=[], day="20260814",
        aio_capped=False, aio_credits_month=0,
    )
    assert set(wire) == set(reference), (
        f"router-shaped response drifted from geo_poll._progress: "
        f"missing={sorted(set(reference) - set(wire))} "
        f"extra={sorted(set(wire) - set(reference))}"
    )


def test_json_null_is_what_the_console_receives_for_no_reason(geo, monkeypatch):
    """`terminal_reason: null` is a value the TS type declares (`string | null`).
    A backend that omitted the key instead would typecheck fine in TS and then
    read `undefined` — the same class of bug, one layer down."""
    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(1)
    raw = _step().text
    assert '"terminal_reason":null' in raw.replace(" ", "")


def test_the_cap_stop_is_terminal_too_not_just_capped(geo, monkeypatch):
    """`capped` and `terminal` are separate fields with separate tones in the
    console; the daily cap must set both, or the loop keeps calling a backend
    that will only ever refuse."""
    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(12)  # more work than the cap allows, so the cap is what stops us
    assert client.put(
        f"/api/geo/brands/{BRAND['id']}/config", json={"daily_cap": 10}
    ).status_code == 200

    first = _step(batch_size=50).json()
    assert (first["capped"], first["terminal"]) == (False, False)
    assert first["calls_used_today"] == 10  # the reservation stopped at the cap

    body = _step(batch_size=50).json()
    assert (body["capped"], body["terminal"]) == (True, True)
    assert "cap" in body["terminal_reason"]
    assert body["done"] < body["total"], "stopped short, and says so"


# --------------------------------------------------------------------------- #
# C-2, the far side: the field names the console's TypeScript declares.
# Read from source rather than duplicated here, so a rename in either language
# breaks this test instead of breaking the browser.
# --------------------------------------------------------------------------- #

def _ts_interface_fields(path: Path, name: str) -> dict[str, str]:
    """``{field: "optional"|"required"}`` for one `export interface`."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.S)
    assert match, f"interface {name} not found in {path.name}"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    return {
        m.group(1): ("optional" if m.group(2) else "required")
        for m in re.finditer(r"^\s*(\w+)(\??):", body, re.M)
    }


@pytest.fixture()
def ts_types():
    if not FRONTEND.is_dir():
        pytest.skip(f"newfrontend/ not present next to backend/ ({FRONTEND})")
    return (
        _ts_interface_fields(FRONTEND / "lib" / "api.ts", "GeoPollProgress"),
        _ts_interface_fields(
            FRONTEND / "components" / "console" / "geo" / "pollLoop.ts",
            "PollStepProgress",
        ),
    )


def test_the_console_type_declares_the_stop_signal_as_required(ts_types):
    api_type, _ = ts_types
    assert api_type.get("terminal") == "required"
    assert api_type.get("terminal_reason") == "required"


def test_every_field_the_console_reads_is_a_field_the_backend_sends(ts_types, geo, monkeypatch):
    """The join. Anything the loop reasons about must exist in the payload."""
    api_type, loop_type = ts_types
    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(1)
    wire = set(_step().json())

    assert set(api_type) <= wire, (
        f"lib/api.ts GeoPollProgress declares fields the backend never sends: "
        f"{sorted(set(api_type) - wire)}"
    )
    assert set(loop_type) <= wire, (
        f"pollLoop.ts PollStepProgress reads fields the backend never sends: "
        f"{sorted(set(loop_type) - wire)}"
    )
    assert set(C2_FIELDS) <= set(loop_type), (
        "the console's poll loop stopped reading part of the contract: "
        f"{sorted(set(C2_FIELDS) - set(loop_type))}"
    )


# The reverse direction, which the assertion above cannot see: fields the
# backend sends that the console has no type for, and therefore cannot read.
#
# RECORDED GAP. ``aio_capped`` is how poll_step says "Google AIO spent its
# monthly SerpAPI credit and dropped out of this poll" — geo_poll's own
# docstring calls that "honestly reported, never a surprise". It is reported all
# the way to the wire and then falls on the floor: GeoPollProgress declares
# neither field, so the panel just shows AIO's numbers quietly going stale.
# Owner: senior-frontend (see the matching block in
# newfrontend/components/console/geo/pollContract.test.ts).
#
# Equality ratchet: a THIRD undeclared field fails this, and declaring these two
# also fails it — which is the signal to empty the set.
KNOWN_UNDECLARED_BY_THE_CONSOLE = {"aio_capped", "aio_credits_month"}


def test_the_only_fields_the_console_cannot_read_are_the_known_two(ts_types, geo, monkeypatch):
    api_type, _ = ts_types
    monkeypatch.setattr(geo, "poll_engine", lambda e, p: _answer(e, p))
    _put_prompts(1)
    wire = set(_step().json())

    assert wire - set(api_type) == KNOWN_UNDECLARED_BY_THE_CONSOLE, (
        "the set of backend fields the console has no type for has changed — "
        f"now {sorted(wire - set(api_type))}"
    )


# =========================================================================== #
# C-4 — SheetsUnavailable, producer to consumer, as the real class
# =========================================================================== #

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _no_live_sheets(monkeypatch):
    """Nothing in this file may reach Google, and MR_OFFLINE does not stop it.

    ``fetch_all_trackers`` falls back to the anonymous xlsx export URL when the
    Sheets API path fails, and neither ``_default_xlsx_fetcher`` nor
    ``_default_fetcher`` consults ``MR_OFFLINE`` — so a test that stubs only the
    API seam still pulls the live workbook over the network. (Reported; the
    guard belongs in the product module, not here.) These two stubs are the
    local containment.
    """
    from marketing_research_agent.sources import sheets_source as ss

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "a test reached the live Google Sheets export endpoint — stub the "
            "fetcher instead of letting it fall through to the network"
        )

    monkeypatch.setattr(ss, "_default_xlsx_fetcher", _blocked)
    monkeypatch.setattr(ss, "_default_fetcher", _blocked)


@pytest.fixture()
def mr(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    from app.routers import marketing_research as mrr

    return mrr


def _seed_official_run():
    """A workspace holding yesterday's good headline figures."""
    from marketing_research_agent import runs as mr_runs

    run_id = mr_runs.new_run_id()
    mr_runs.save_run({
        "id": run_id, "kind": "official_spend", "user_id": USER["id"],
        "agent_id": "a6", "platform": "sheets-official",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "months": {"2026-07": 8632.0}, "totals": {"2026-07": {"spend": 8632.0}},
    })
    return run_id


def _one_tracker_tab():
    from datetime import date

    from marketing_research_agent.schemas import CampaignMetric

    metric = CampaignMetric(
        channel="Google", campaign="pi", utm_source="google", utm_medium="cpc",
        utm_campaign="pi", spend=1200.0, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 29),
    )
    return [{"tab": "Vendor A", "gid": 1, "metrics": [metric], "gaps": []}]


@pytest.fixture()
def sheets_failing(monkeypatch):
    """Break the workbook read at the transport seam and let the REAL
    ``fetch_official_totals`` decide what that means. Nothing is stubbed
    between the failure and the router, so the exception the router handles is
    the exception the producer actually raises."""
    from marketing_research_agent.sources import sheets_source as ss

    monkeypatch.setattr(ss, "_sheets_service", lambda: object())  # never a client
    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(
            RuntimeError("HttpError 429: Quota exceeded for reads")
        ),
    )
    # The xlsx export is a real fallback for "Sheets API disabled"; a 429 hits
    # it too, so break both or the pull quietly succeeds against the live sheet.
    monkeypatch.setattr(
        ss, "_default_xlsx_fetcher",
        lambda sid: (_ for _ in ()).throw(RuntimeError("HttpError 429: export throttled")),
    )
    return ss


def test_the_producer_raises_the_class_the_consumer_is_written_against(sheets_failing):
    """Both halves name ``SheetsUnavailable``; this is the one assertion that
    they are the same object and not two same-named classes."""
    from app.routers import marketing_research as mrr

    with pytest.raises(sheets_failing.SheetsUnavailable):
        mrr.fetch_official_totals("sheet-1", 2026)
    # The router imported the function by name at module import; it must still
    # be the module's current object, or monkeypatching the module is theatre.
    assert mrr.fetch_official_totals is sheets_failing.fetch_official_totals


def test_sheets_unavailable_survives_the_consumers_except_clause(mr, sheets_failing, monkeypatch):
    """End to end with the real exception type: a 429 on the roll-up read keeps
    the previous headline figures and reports 207, exactly as the prose said."""
    from marketing_research_agent import runs as mr_runs

    kept = _seed_official_run()
    monkeypatch.setattr(mr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    resp = client.post("/api/mr/ingest-sheet", json={})
    assert resp.status_code == 207, resp.text
    body = resp.json()
    assert body["status"] == "partial"
    assert any("official totals" in d for d in body["degraded"])
    assert any("429" in d for d in body["degraded"]), body["degraded"]

    survivor = mr_runs.get_run(kept)
    assert survivor is not None, "a Sheets 429 deleted the official figures"
    assert survivor["months"] == {"2026-07": 8632.0}


def test_the_same_failure_on_the_tracker_read_deletes_nothing_at_all(mr, sheets_failing):
    """``fetch_all_trackers`` raises the same class from the same seam. The
    corroborated-x3 finding in one assertion: a failed fetch costs zero rows."""
    from marketing_research_agent import runs as mr_runs

    before = {r["id"] for r in mr_runs.list_runs(USER["id"])}
    kept = _seed_official_run()

    resp = client.post("/api/mr/ingest-sheet", json={})
    assert resp.status_code == 502, resp.text
    assert "429" in resp.json()["detail"]

    after = {r["id"] for r in mr_runs.list_runs(USER["id"])}
    assert after == before | {kept}, "the failed pull removed runs"


def test_a_clean_empty_rollup_is_not_mistaken_for_the_failure(mr, monkeypatch):
    """The other half of C-4, driven through the real producer: a workbook that
    reads fine and simply has no roll-up tab returns ``{}``, and ``{}`` is the
    one input that legitimately retires the previous figures."""
    from marketing_research_agent import runs as mr_runs
    from marketing_research_agent.sources import sheets_source as ss

    kept = _seed_official_run()
    monkeypatch.setattr(ss, "_sheets_service", lambda: object())
    monkeypatch.setattr(ss, "list_tabs",
                        lambda sid, service=None: [{"gid": 1, "title": "Meta 360 RA"}])
    monkeypatch.setattr(ss, "fetch_tab_values",
                        lambda sid, title, service=None: [["Meta 360 RA"]])
    monkeypatch.setattr(mr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    assert ss.fetch_official_totals("sheet-1", 2026) == {}
    resp = client.post("/api/mr/ingest-sheet", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["degraded"] == []
    assert mr_runs.get_run(kept) is None  # correctly retired, not "kept on a blip"


def test_the_two_outcomes_are_still_distinguishable_at_the_router(mr, monkeypatch):
    """The bug, stated as one comparison: read-failure and genuine-emptiness
    must not produce the same status code."""
    from marketing_research_agent.sources import sheets_source as ss

    monkeypatch.setattr(mr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])
    monkeypatch.setattr(ss, "_sheets_service", lambda: object())

    monkeypatch.setattr(ss, "list_tabs",
                        lambda sid, service=None: [{"gid": 1, "title": "Vendor"}])
    monkeypatch.setattr(ss, "fetch_tab_values",
                        lambda sid, title, service=None: [["Vendor"]])
    empty = client.post("/api/mr/ingest-sheet", json={}).status_code

    monkeypatch.setattr(
        ss, "list_tabs",
        lambda sid, service=None: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    failed = client.post("/api/mr/ingest-sheet", json={}).status_code

    assert (empty, failed) == (200, 207)
