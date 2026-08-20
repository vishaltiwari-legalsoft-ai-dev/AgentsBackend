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

Also here, deliberately: the MR endpoints that are **not** scoped by anything.
``/mr/targets`` is one document for the whole deployment, so a second tenant
reads and overwrites the first tenant's thresholds. That is pinned as a known
leak, not endorsed — see the test's docstring.
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
# The MR endpoints with no tenant key at all
# --------------------------------------------------------------------------- #

def test_KNOWN_LEAK_targets_are_one_document_shared_by_every_tenant(
    owner_workspace, as_caller
):
    """DOCUMENTS A LIVE CROSS-TENANT DEFECT. Do not read this as a spec.

    ``GET``/``POST /mr/targets`` take the caller only to authenticate them:
    ``mr_goals.get_targets()`` and ``set_targets(body)`` accept no user id and
    read/write a single document (``mr_config/targets``, or ``MR_TARGETS_FILE``
    locally). So one tenant's threshold edit re-flags every other tenant's
    dashboard, and ``GET /mr/config`` mirrors the same values back to everyone.

    Like the Browser Agent's watch rules, this is not a forgotten scoping line —
    there is no per-tenant concept in this path at all. Pinned so the refactor
    that introduces one has to come here and delete this test on purpose. Fix
    belongs to ``senior-python-backend``; this suite only refuses to let it
    change by accident.
    """
    baseline = client.get("/api/mr/targets").json()["thresholds"]["cac_red"]

    as_caller(STRANGER)
    edited = client.post("/api/mr/targets", json={"thresholds": {"cac_red": 4242}})
    assert edited.status_code == 200
    assert edited.json()["thresholds"]["cac_red"] == 4242

    as_caller(OWNER)
    after = client.get("/api/mr/targets").json()["thresholds"]["cac_red"]
    assert after == 4242 and after != baseline, (
        "if this now fails, MR targets became per-tenant — good; delete this test"
    )
    assert client.get("/api/mr/config").json()["thresholds"]["cac_red"] == 4242
