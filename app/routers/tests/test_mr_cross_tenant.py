"""Cross-tenant coverage for the Marketing Research router (``/api/mr``).

MR is the router with **no ownership helper at all**. Where the other three have
one function each, MR has three hand-written copies of the comparison, spread
across 1,200 lines::

    # delete_dataset
    if not run or run.get("user_id") != user["id"] or run.get("kind") != "dataset":
    # get_report_run
    if not run or run.get("user_id") != user["id"]:
    # report_run_pdf
    if not run or run.get("user_id") != user["id"] or run.get("kind") not in reports.KINDS:

Note what is missing: ``str()``. Graphics Designer, Creative and Browser all
stringify both sides; MR compares raw. Today every id is a Firestore document id
so nothing turns on it, but the four routers do not agree on what a tenant key
*is*, and ``test_tenant_id_type_contract.py`` pins what each one does when they
diverge.

Everything else in MR is scoped a fourth way — by threading ``user["id"]``
through ``runs.list_runs(user_id, ...)``, which filters in Python. That is the
read path behind ``/overview``, ``/trends``, ``/report-periods``,
``/lead-analysis`` and every report build, and it too was untested.

Also here: ``/mr/targets``, which was one document for the whole deployment
until 2026-08-21 — a second tenant read and overwrote the first tenant's
thresholds, and every report built from them. It is keyed on ``user["id"]`` now
and the last section pins that, at the store and at the two places the figures
are *applied* (report flags, overview traffic lights).

And the blind spot this file had of its own (fixed 2026-09-05). The harness
below has set ``MR_SOURCES_FILE`` since the day it was written, and not one
assertion ever touched ``/mr/sources``. Meanwhile ``sources_registry`` kept one
global document with no caller anywhere in it, so any signed-in user could
enumerate and permanently disconnect every other user's connected Google
Sheet — demonstrated in production, one desk deleting another's "Confidential
P&L" — while this suite stayed green and the route ledger called all three
routes TENANT_SCOPED.

The resolution is NOT per-user sources; see ``sources_registry``'s docstring
for why (one shared primary tracker, one shared service account, a cron that
scans every workbook). Reads stay shared and the ledger now says
WORKSPACE_SHARED. What closed is the destructive path, and the last section
here is what holds it shut.
"""
from __future__ import annotations

import io
import os

os.environ["MR_OFFLINE"] = "1"

import pytest

from app.routers.tests.conftest import client

OWNER = {"id": "mr-tenant-a", "email": "a@legalsoft.com", "is_admin": False,
         "is_creator": False, "session_id": "", "timezone": "UTC"}
STRANGER = {"id": "mr-tenant-b", "email": "b@legalsoft.com", "is_admin": False,
            "is_creator": False, "session_id": "", "timezone": "UTC"}

#: MR's own 404 wording. Lower-case and different again from the other three
#: routers ("Run not found", "Unknown run") — pinned so a unification notices.
RUN_NOT_FOUND = "run not found"
DATASET_NOT_FOUND = "dataset not found"

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    """A throwaway run store and targets file, with the owner signed in.

    ``MR_RUNS_DIR`` is re-read on every call (``runs._root()``), so the env var
    is enough here — unlike the two Graphics Designer stores, which bind their
    root at import.
    """
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "mr"))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    as_caller(OWNER)


@pytest.fixture()
def owner_workspace(_harness) -> dict:
    """One tenant's whole MR workspace: an ingested dataset, a built report, and
    a lead-analysis run.

    The lead run is written straight into the store rather than produced by a
    sheet pull — pulling needs a live Google workbook, and what these tests care
    about is only which ``user_id`` the row carries. Same shape the MR router
    suite seeds with.
    """
    from marketing_research_agent import runs as mr_runs

    ingested = client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    assert ingested.status_code == 200, ingested.text

    report = client.post("/api/mr/reports/daily_summary")
    assert report.status_code == 200, report.text

    lead_id = mr_runs.new_run_id()
    mr_runs.save_run({
        "id": lead_id, "kind": "lead_analysis", "user_id": OWNER["id"],
        "agent_id": "a6", "platform": "sheets-leads",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "source_label": "Primary", "tab": "Lead Analysis", "gaps": [],
        "summary": {"latest_month": "2026-07",
                    "months": {"2026-07": {"vendors": [], "flag_count": 0}}},
    })
    return {"dataset_id": ingested.json()["dataset_id"],
            "report_id": report.json()["id"], "lead_id": lead_id}


# --------------------------------------------------------------------------- #
# The three hand-written ownership comparisons
# --------------------------------------------------------------------------- #

def test_another_tenant_cannot_delete_a_dataset(owner_workspace, as_caller):
    """``delete_dataset`` — a destructive endpoint with a raw, unstringified
    comparison and no helper behind it.

    Both halves are asserted: the 404 (with MR's wording), and that the dataset
    is still there afterwards. ``runs.delete_run`` is unscoped, so if the
    comparison ever stops firing this endpoint deletes another tenant's ingested
    data outright.
    """
    as_caller(STRANGER)
    refused = client.delete(f"/api/mr/datasets/{owner_workspace['dataset_id']}")
    assert refused.status_code == 404
    assert refused.json()["detail"] == DATASET_NOT_FOUND

    as_caller(OWNER)
    still_there = client.get("/api/mr/datasets").json()
    assert [d["id"] for d in still_there] == [owner_workspace["dataset_id"]]


def test_another_tenant_cannot_read_a_report_run(owner_workspace, as_caller):
    """``get_report_run`` — the endpoint that returns the whole report document,
    figures and all."""
    as_caller(STRANGER)
    refused = client.get(f"/api/mr/runs/{owner_workspace['report_id']}")
    assert refused.status_code == 404
    assert refused.json()["detail"] == RUN_NOT_FOUND


def test_another_tenant_cannot_export_a_report_as_pdf(owner_workspace, as_caller):
    """``report_run_pdf`` — the export path.

    A separate test from the read above because it is a separately written
    comparison at a separate call site: fixing one and forgetting the other is
    exactly the failure mode four independent predicates invite. The owner's
    side is asserted too, so this proves the export works and is closed, not
    that it is broken for everyone.
    """
    mine = client.get(f"/api/mr/runs/{owner_workspace['report_id']}/pdf")
    assert mine.status_code == 200
    assert mine.headers["content-type"] == "application/pdf"

    as_caller(STRANGER)
    refused = client.get(f"/api/mr/runs/{owner_workspace['report_id']}/pdf")
    assert refused.status_code == 404
    assert refused.json()["detail"] == RUN_NOT_FOUND


def test_a_real_run_id_and_an_invented_one_look_the_same(owner_workspace, as_caller):
    """No ownership oracle on the MR run endpoints either."""
    as_caller(STRANGER)
    real = client.get(f"/api/mr/runs/{owner_workspace['report_id']}")
    invented = client.get("/api/mr/runs/000000000000")
    assert (real.status_code, real.json()) == (invented.status_code, invented.json())


# --------------------------------------------------------------------------- #
# The list / aggregate reads, scoped by threading user["id"] into list_runs
# --------------------------------------------------------------------------- #

def test_the_owners_workspace_is_populated(owner_workspace):
    """The control. Every assertion below is "the stranger sees nothing", which
    is trivially true of a broken fixture — this is what makes those meaningful.
    """
    assert client.get("/api/mr/datasets").json()
    assert client.get("/api/mr/runs").json()
    assert client.get("/api/mr/overview").json()["has_data"] is True
    assert client.get("/api/mr/trends").json()["has_data"] is True
    assert client.get("/api/mr/report-periods").json()["months"]
    assert client.get("/api/mr/lead-analysis").json()["has_data"] is True


def test_a_second_tenant_sees_an_empty_workspace(owner_workspace, as_caller):
    """Every aggregate read is empty for the other tenant — no counts, no
    vendors, no months, no lead summary.

    This is the assertion the delete/read/export tests cannot make: those prove
    a *named* resource is refused, this proves nothing leaks through the
    unnamed, aggregate path where the tenant key is a filter argument rather
    than a comparison.
    """
    as_caller(STRANGER)
    assert client.get("/api/mr/datasets").json() == []
    assert client.get("/api/mr/runs").json() == []

    overview = client.get("/api/mr/overview").json()
    assert overview["has_data"] is False
    assert overview["totals"] is None and overview["sources"] == []

    trends = client.get("/api/mr/trends").json()
    assert trends["has_data"] is False and trends["monthly"] == []

    periods = client.get("/api/mr/report-periods").json()
    assert periods["months"] == [] and periods["quarters"] == []

    assert client.get("/api/mr/lead-analysis").json()["has_data"] is False


def test_a_second_tenants_report_is_built_from_their_own_empty_data(
    owner_workspace, as_caller
):
    """Building a report is a read of the tenant's dataset, so it is a leak
    surface too — and one that answers 200 rather than 404.

    The owner's daily summary carries the ingested $1,200; the stranger's must
    not, whatever else it says.
    """
    mine = client.post("/api/mr/reports/daily_summary")
    assert mine.status_code == 200
    assert "1200" in str(mine.json()["structured"])

    as_caller(STRANGER)
    theirs = client.post("/api/mr/reports/daily_summary")
    assert theirs.status_code == 200, theirs.text
    assert "1200" not in str(theirs.json()["structured"])


def test_a_second_tenant_cannot_export_the_lead_analysis_pdf(owner_workspace, as_caller):
    """``/mr/lead-analysis/pdf`` resolves its run through ``_latest_lead_run``,
    which is scoped by argument rather than by comparison.

    The stranger's 404 is "no lead-analysis data yet" — the *empty workspace*
    answer, not an ownership refusal. That is the correct outcome here and worth
    naming: the scoping happens before there is any document to refuse.
    """
    mine = client.get("/api/mr/lead-analysis/pdf")
    assert mine.status_code == 200
    assert mine.headers["content-type"] == "application/pdf"

    as_caller(STRANGER)
    theirs = client.get("/api/mr/lead-analysis/pdf")
    assert theirs.status_code == 404
    assert theirs.json()["detail"] == "no lead-analysis data yet"


def test_two_tenants_ingesting_the_same_file_keep_separate_datasets(as_caller):
    """The store is shared; the tenancy is not.

    ``mr_runs`` is one collection (one directory offline) for every workspace,
    so isolation here is entirely a matter of the filter argument being passed.
    Two tenants ingest byte-identical CSVs and each must see exactly one
    dataset: their own.
    """
    def _ingest():
        r = client.post("/api/mr/ingest",
                        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                        data={"platform": "google_ads"})
        assert r.status_code == 200, r.text
        return r.json()["dataset_id"]

    mine = _ingest()
    as_caller(STRANGER)
    theirs = _ingest()
    assert mine != theirs
    assert [d["id"] for d in client.get("/api/mr/datasets").json()] == [theirs]

    as_caller(OWNER)
    assert [d["id"] for d in client.get("/api/mr/datasets").json()] == [mine]


# --------------------------------------------------------------------------- #
# Targets — keyed per workspace since 2026-08-21
# --------------------------------------------------------------------------- #
# Until then ``GET``/``POST /mr/targets`` took the caller only to authenticate
# them: ``get_targets()`` / ``set_targets(body)`` accepted no user id and
# read/wrote a single document (``mr_config/targets``, or ``MR_TARGETS_FILE``
# locally), so one desk's threshold edit re-flagged every other desk's dashboard
# and ``GET /mr/config`` mirrored the changed figure back to everyone. Every
# entry point now takes ``user["id"]``. The migration off the shared document is
# covered where it lives, in ``marketing_research_agent/tests/test_goals.py``.


def test_a_tenants_threshold_edit_does_not_move_another_tenants_red_line(
    owner_workspace, as_caller
):
    """The read half. Both sides are asserted so this cannot pass by the
    endpoint simply being broken for everyone."""
    baseline = client.get("/api/mr/targets").json()["thresholds"]["cac_red"]

    as_caller(STRANGER)
    edited = client.post("/api/mr/targets", json={"thresholds": {"cac_red": 4242}})
    assert edited.status_code == 200
    assert edited.json()["thresholds"]["cac_red"] == 4242

    as_caller(OWNER)
    after = client.get("/api/mr/targets").json()["thresholds"]["cac_red"]
    assert after == baseline != 4242, "another tenant's edit moved this tenant's CAC line"
    assert client.get("/api/mr/config").json()["thresholds"]["cac_red"] == baseline


def test_each_tenants_own_edit_sticks(owner_workspace, as_caller):
    """Two tenants edit the same threshold to different figures and both keep
    theirs — the store is shared, the tenancy is not."""
    assert client.post(
        "/api/mr/targets", json={"thresholds": {"cac_red": 2600}}
    ).status_code == 200

    as_caller(STRANGER)
    assert client.post(
        "/api/mr/targets", json={"thresholds": {"cac_red": 4242}}
    ).status_code == 200
    assert client.get("/api/mr/targets").json()["thresholds"]["cac_red"] == 4242

    as_caller(OWNER)
    assert client.get("/api/mr/targets").json()["thresholds"]["cac_red"] == 2600
    assert client.get("/api/mr/config").json()["thresholds"]["cac_red"] == 2600


def test_a_tenants_reset_does_not_reset_another_tenants_targets(
    owner_workspace, as_caller
):
    """``{"reset": true}`` is the destructive one: on the shared document it
    wiped the whole deployment's edits."""
    assert client.post(
        "/api/mr/targets", json={"thresholds": {"cac_red": 2600}}
    ).status_code == 200

    as_caller(STRANGER)
    assert client.post("/api/mr/targets", json={"reset": True}).json()["edited"] is False

    as_caller(OWNER)
    kept = client.get("/api/mr/targets").json()
    assert kept["thresholds"]["cac_red"] == 2600 and kept["edited"] is True


def test_a_report_is_flagged_against_its_own_tenants_thresholds(
    owner_workspace, as_caller
):
    """Targets are not just stored per tenant — they are *applied* per tenant.

    The fixture CSV spends $1,200 for 4 booked demos (cost/booking $300). The
    owner drops their cost-per-booking line to $200 so their own report flags
    it; the stranger, on the untouched $150 default… also flags it. So the
    assertion runs the other way: the owner raises their line to $500, and the
    stranger's report must still carry the flag the default produces.
    """
    assert client.post(
        "/api/mr/targets", json={"thresholds": {"cost_per_booking_flag": 500}}
    ).status_code == 200

    mine = client.post("/api/mr/reports/daily_summary").json()["structured"]
    assert not [f for f in mine["flags"] if f["metric"] == "cost_per_booking"], (
        "the owner raised their own ceiling above $300 and is still flagged"
    )

    as_caller(STRANGER)
    client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                data={"platform": "google_ads"})
    theirs = client.post("/api/mr/reports/daily_summary").json()["structured"]
    assert [f for f in theirs["flags"] if f["metric"] == "cost_per_booking"], (
        "the stranger's report was judged against the owner's raised ceiling"
    )


def test_the_overview_traffic_lights_use_the_readers_own_targets(
    owner_workspace, as_caller
):
    """``/mr/overview`` resolves targets separately from the report path, so it
    is a separate scoping site and gets its own test."""
    assert client.post(
        "/api/mr/targets", json={"thresholds": {"cost_per_booking_flag": 500}}
    ).status_code == 200

    mine = client.get("/api/mr/overview").json()
    assert not [f for f in mine["flag_summary"] if f["metric"] == "cost_per_booking"]

    as_caller(STRANGER)
    client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                data={"platform": "google_ads"})
    theirs = client.get("/api/mr/overview").json()
    assert [f for f in theirs["flag_summary"] if f["metric"] == "cost_per_booking"]


# --------------------------------------------------------------------------- #
# The board report
# --------------------------------------------------------------------------- #
# Its idempotency key is a hash of (periods, capture date, generator version)
# and deliberately does NOT carry the workspace — the LOOKUP is scoped instead.
# That makes "two tenants ask for the same quarter of the same capture" the
# interesting case rather than an unlikely one: they hash identically by
# construction, so a lookup that ever stops filtering on user_id serves one
# tenant's revenue figures to the other with a cache hit and no error anywhere.

_BOARD_MONTHS = {
    "2026-01": {"spend": 80000.0, "leads": 430, "revenue_clients": 16,
                "revenue_amount_sold": 88000.0},
    "2026-02": {"spend": 79000.0, "leads": 425, "revenue_clients": 15,
                "revenue_amount_sold": 86000.0},
    "2026-03": {"spend": 80581.57, "leads": 426, "revenue_clients": 17,
                "revenue_amount_sold": 88947.7},
}


def _seed_official_for(user_id: str) -> None:
    from marketing_research_agent import runs as mr_runs

    mr_runs.save_run({
        "id": mr_runs.new_run_id(), "kind": "official_spend", "user_id": user_id,
        "agent_id": "a6", "platform": "sheets-official",
        "generated_at": "2026-04-01T00:00:00+00:00",
        "months": {k: v["spend"] for k, v in _BOARD_MONTHS.items()},
        "totals": _BOARD_MONTHS,
    })


@pytest.fixture()
def board_on(monkeypatch):
    monkeypatch.setenv("MR_BOARD_REPORT", "1")


def test_an_identical_board_request_from_another_tenant_is_not_a_cache_hit(
        board_on, as_caller):
    """Same period, same capture date, same generator — so the same key. The
    second tenant must still derive their OWN run from their OWN figures."""
    _seed_official_for(OWNER["id"])
    _seed_official_for(STRANGER["id"])

    as_caller(OWNER)
    mine = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    as_caller(STRANGER)
    theirs = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()

    assert mine["structured"]["cache_key"] == theirs["structured"]["cache_key"], (
        "the keys diverged, so this test no longer exercises the collision it exists for")
    assert theirs["reused"] is False, "another tenant's run was served as a cache hit"
    assert theirs["id"] != mine["id"]
    assert theirs["user_id"] == STRANGER["id"]


def test_a_second_tenant_with_no_capture_gets_an_error_not_the_first_tenants_figures(
        board_on, as_caller):
    _seed_official_for(OWNER["id"])
    as_caller(OWNER)
    assert client.post("/api/mr/board-report",
                       json={"period": "2026-Q1"}).status_code == 200

    as_caller(STRANGER)
    refused = client.post("/api/mr/board-report", json={"period": "2026-Q1"})
    assert refused.status_code == 422
    assert "sheet pull" in refused.json()["detail"]


def test_another_tenant_cannot_read_a_board_run(board_on, as_caller):
    _seed_official_for(OWNER["id"])
    as_caller(OWNER)
    run_id = client.post("/api/mr/board-report",
                         json={"period": "2026-Q1"}).json()["id"]

    as_caller(STRANGER)
    assert client.get(f"/api/mr/runs/{run_id}").status_code == 404
    assert run_id not in [r["id"] for r in client.get("/api/mr/runs").json()]


def test_another_tenant_cannot_read_a_board_report_as_a_document(board_on, as_caller):
    """The JSON is scoped; the document must be scoped by the same check.

    A board report is the most sensitive thing this agent produces — one page
    carrying a quarter's spend, revenue and client counts — and it now has two
    more routes that hand it over. Both take a run id straight off the URL, so
    both are IDOR surfaces until this asserts they are not. 404, not 403: a 403
    would confirm to a stranger that the id exists.
    """
    _seed_official_for(OWNER["id"])
    as_caller(OWNER)
    run_id = client.post("/api/mr/board-report",
                         json={"period": "2026-Q1"}).json()["id"]
    assert client.get(f"/api/mr/board-report/{run_id}/html").status_code == 200

    as_caller(STRANGER)
    for suffix in ("html", "pdf"):
        resp = client.get(f"/api/mr/board-report/{run_id}/{suffix}")
        assert resp.status_code == 404, suffix
        assert resp.json()["detail"] == "board report not found", suffix


def test_another_tenants_board_report_never_reaches_the_renderer(
        board_on, as_caller, monkeypatch):
    """Ownership is checked BEFORE the document is built, so a stranger's request
    costs nothing and — more to the point — never puts another workspace's
    figures into an outbound HTTP body. A configured renderer is installed here
    precisely so the test would notice if the order were the other way round.
    """
    from app.routers import marketing_research as mr_router

    monkeypatch.setenv("RENDERER_URL", "http://renderer.invalid")
    monkeypatch.setenv("RENDERER_TOKEN", "test-token-not-a-real-secret")
    sent: list[dict] = []
    monkeypatch.setattr(mr_router.httpx, "post",
                        lambda url, **kw: sent.append(kw) or _never_called())

    _seed_official_for(OWNER["id"])
    as_caller(OWNER)
    run_id = client.post("/api/mr/board-report",
                         json={"period": "2026-Q1"}).json()["id"]

    as_caller(STRANGER)
    assert client.get(f"/api/mr/board-report/{run_id}/pdf").status_code == 404
    assert sent == [], "another tenant's ledger was sent to the renderer"


def _never_called():
    raise AssertionError("the renderer was called for a run the caller does not own")


# --------------------------------------------------------------------------- #
# The sheet-sources registry — shared reads, owned deletes
# --------------------------------------------------------------------------- #
# The registry is ONE document for the whole workspace and stays that way: the
# primary tracker is a deployment-wide constant every caller already reads, the
# secondaries are reached through a shared service account rather than anyone's
# own Google identity, and the nightly lead-analysis cron scans all of them.
# Shared reads are therefore the intended behaviour, and the ledger says so.
#
# The DELETE was the defect. It took no caller at all until 2026-09-05, so a
# signed-in user could disconnect a sheet somebody else had connected and
# nothing recorded who that had been. Every assertion below fails if that
# argument is dropped again.

ADMIN = {"id": "mr-tenant-admin", "email": "admin@legalsoft.com", "is_admin": True,
         "is_creator": False, "session_id": "", "timezone": "UTC"}

SHEET_A = "1AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"
SHEET_LEGACY = "2AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"

#: ``sources_registry.removal_refusal`` — pinned here so the wording the console
#: shows and the wording the registry raises cannot drift apart.
REFUSED_NOT_YOURS = ("Only the person who connected this sheet, or an admin, "
                     "can disconnect it.")


@pytest.fixture()
def sheets_reachable(monkeypatch):
    """``POST /mr/sources`` without Google.

    The endpoint validates access by opening the workbook, which needs a live
    Sheets API. Both reads are stubbed at the router's own namespace: the meta
    call succeeds (so the add proceeds) and the first-pass profile raises (which
    the handler already treats as "no tabs yet"). Nothing here reaches the
    network, which is the point.
    """
    from app.routers import marketing_research as mr_router

    def _meta(spreadsheet_id, **_kw):
        return {"title": f"Workbook {spreadsheet_id[:4]}", "tabs": ["Sheet1"]}

    def _no_fetch(*_a, **_kw):
        raise RuntimeError("no Sheets API in tests")

    monkeypatch.setattr(mr_router, "workbook_meta", _meta)
    monkeypatch.setattr(mr_router.mr_workbook, "fetch_workbook", _no_fetch)


def _connect(sheet_id: str) -> dict:
    resp = client.post("/api/mr/sources", json={"url": sheet_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["source"]


def _listed_ids() -> list[str]:
    return [s["id"] for s in client.get("/api/mr/sources").json()["sources"]]


def test_the_source_tests_run_against_a_throwaway_store(tmp_path):
    """The guard, verified from inside pytest rather than assumed.

    ``backend/conftest.py`` has been proven leaky twice, and this suite writes
    to the same collection (``mr_config``) that once took a test fixture into
    production. Three things are checked: the offline flag is on, the registry
    agrees it is offline, and its file is this test's tmp path — plus that the
    cloud handle raises rather than connecting, so a future edit that ignores
    ``_use_cloud`` still cannot reach the real database.
    """
    from marketing_research_agent import sources_registry as reg
    from marketing_research_agent import goals as mr_goals

    assert os.environ.get("MR_OFFLINE") == "1"
    assert mr_goals._use_cloud() is False
    assert reg._sources_path() == tmp_path / "sources.json"
    with pytest.raises(RuntimeError, match="Firestore access blocked"):
        reg._doc()


def test_a_connected_sheet_records_who_connected_it(sheets_reachable):
    """``added_by`` is the whole basis of the delete gate. Nothing recorded it
    before, which is why the gate could not exist."""
    src = _connect(SHEET_A)
    assert src["added_by"] == OWNER["id"]

    row = next(s for s in client.get("/api/mr/sources").json()["sources"]
               if s["id"] == SHEET_A)
    assert row["added_by"] == OWNER["id"]
    assert row["can_remove"] is True


def test_the_registry_is_shared_and_that_is_deliberate(sheets_reachable, as_caller):
    """The control for the refusal tests below, and the statement of intent.

    Every "the stranger is refused" assertion would also pass if the stranger
    simply could not see the row. They can: the listing is workspace-wide on
    purpose, so a refusal below is a real refusal and not an accident of
    visibility. If this ever flips to per-user sources, this test is the one
    that has to be rewritten first — deliberately.
    """
    _connect(SHEET_A)

    as_caller(STRANGER)
    listing = client.get("/api/mr/sources").json()
    assert SHEET_A in [s["id"] for s in listing["sources"]]
    theirs = next(s for s in listing["sources"] if s["id"] == SHEET_A)
    assert theirs["added_by"] == OWNER["id"], "the listing hides who connected it"
    assert theirs["can_remove"] is False, "the console would show a button that 403s"


def test_another_tenant_cannot_disconnect_a_sheet_they_did_not_connect(
        sheets_reachable, as_caller):
    """THE defect, closed.

    Reproduces what the review did against production: a second signed-in user,
    with nothing but their own token, disconnecting somebody else's workbook.
    Both halves are asserted — the 403, and that the sheet is still connected
    afterwards — because a refusal that still mutated the store is not a
    refusal. Delete ``requested_by`` from ``remove_source`` and this goes red on
    the status code; weaken ``may_remove`` and it goes red on the listing.
    """
    _connect(SHEET_A)

    as_caller(STRANGER)
    refused = client.delete(f"/api/mr/sources/{SHEET_A}")
    assert refused.status_code == 403, (
        "another user disconnected a sheet they did not connect: "
        f"{refused.status_code} {refused.text}"
    )
    assert refused.json()["detail"] == REFUSED_NOT_YOURS
    assert SHEET_A in _listed_ids(), "the sheet was disconnected despite the refusal"

    as_caller(OWNER)
    assert SHEET_A in _listed_ids()


def test_the_person_who_connected_a_sheet_can_still_disconnect_it(sheets_reachable):
    """The other half: the gate must not close the feature.

    Without this, deleting the whole endpoint would pass every test above.
    """
    _connect(SHEET_A)
    removed = client.delete(f"/api/mr/sources/{SHEET_A}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] == SHEET_A
    assert SHEET_A not in _listed_ids()


def test_an_admin_can_disconnect_a_sheet_somebody_else_connected(
        sheets_reachable, as_caller):
    """Somebody has to be able to clean up after a colleague who has left."""
    _connect(SHEET_A)

    as_caller(ADMIN)
    removed = client.delete(f"/api/mr/sources/{SHEET_A}")
    assert removed.status_code == 200, removed.text
    assert SHEET_A not in _listed_ids()


def test_a_sheet_connected_before_attribution_stays_visible_to_everyone(as_caller):
    """The migration case, at the HTTP surface.

    Rows written before ``added_by`` existed have no owner and none can be
    invented — no store ever recorded one. They are NOT hidden and NOT deleted:
    that would be data loss dressed as a security fix. They stay listed for the
    whole workspace exactly as before, and only an admin can retire one.
    """
    from marketing_research_agent import sources_registry as reg

    reg.add_source(SHEET_LEGACY, label="Confidential P&L")  # pre-attribution shape
    assert reg.owner_of(reg.find_source(SHEET_LEGACY)) is None

    for who in (OWNER, STRANGER, ADMIN):
        as_caller(who)
        assert SHEET_LEGACY in _listed_ids(), (
            f"a legacy source vanished from {who['id']}'s list")

    as_caller(STRANGER)
    refused = client.delete(f"/api/mr/sources/{SHEET_LEGACY}")
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"]
    assert SHEET_LEGACY in _listed_ids()

    as_caller(ADMIN)
    assert client.delete(f"/api/mr/sources/{SHEET_LEGACY}").status_code == 200
    assert SHEET_LEGACY not in _listed_ids()


def test_the_kill_switch_still_refuses_both_write_paths(sheets_reachable, monkeypatch):
    """``MR_MULTI_SHEET=0`` turns the feature off. It defaults to ON — see
    ``.env.example`` — so this is the behaviour a deployment opts into, and the
    ownership gate must not have quietly replaced it."""
    _connect(SHEET_A)
    monkeypatch.setenv("MR_MULTI_SHEET", "0")

    assert client.post("/api/mr/sources", json={"url": SHEET_LEGACY}).status_code == 403
    assert client.delete(f"/api/mr/sources/{SHEET_A}").status_code == 403
    assert client.get("/api/mr/sources").json()["enabled"] is False
