"""What a tenant key *is*, per router — the ``str()`` disagreement, pinned.

``get_current_user`` returns ``dict[str, object]``. The tenant lives at
``user["id"]`` and its type is annotated as ``object``, so every router decides
for itself what to compare. Three routers, three decisions:

===================  ==================================  ==================
router               stores                              compares
===================  ==================================  ==================
graphics_designer    ``str(user["id"])``                 ``str(user["id"])``
creative_agent       ``str(user["id"])``                 ``str(user["id"])``
marketing_research   ``user["id"]``  (raw)               ``user["id"]``  (raw)
===================  ==================================  ==================

Two stringify, one does not. As long as every id really is a string the two
behaviours are identical, which is why this has never bitten: ``user["id"]``
comes from the JWT ``sub`` claim, which carries a Firestore document id.

That is a property of today's *data*, not of the code. The type is not enforced
anywhere on the path — ``create_token(user_id: str, ...)`` is an annotation, and
``jwt.decode`` will hand back whatever was encoded. So this file answers the
question the refactor has to answer anyway: **if two tenants' ids differ only by
type, which routers treat them as the same tenant?**

The answer today, proven below: Graphics Designer and Creative Agent both
collapse ``7`` and ``"7"`` into one tenant — a cross-tenant read. Marketing
Research keeps them apart. Whatever the unified ``TenantId`` does, it
cannot do both, and this file is where that choice becomes visible.

Latent, not live: nothing in the sign-in path mints a non-string id today. These
tests exist so that stops being load-bearing and unexamined.
"""
from __future__ import annotations

import io
import os

os.environ["MR_OFFLINE"] = "1"

import pytest

from app.routers.tests.conftest import client

#: The same tenant identity written two ways. Whether these are one tenant or
#: two is the entire subject of this file.
NUMERIC = {"id": 7, "email": "seven@legalsoft.com", "is_admin": False,
           "is_creator": False, "session_id": "", "timezone": "UTC"}
STRINGY = {"id": "7", "email": "stringy@legalsoft.com", "is_admin": False,
           "is_creator": False, "session_id": "", "timezone": "UTC"}

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    """Throwaway stores for all three agents, numeric-id caller signed in."""
    from graphics_designer_agent import runs as gd_runs
    from graphics_designer_agent.creative import runs as cruns

    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path / "gd"))
    monkeypatch.setattr(gd_runs, "RUNS_ROOT", tmp_path / "gd")
    monkeypatch.setattr(cruns, "CREATIVE_RUNS_ROOT", tmp_path / "creative")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "mr"))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    as_caller(NUMERIC)


# --------------------------------------------------------------------------- #
# The two routers that stringify — a numeric id and its string collapse
# --------------------------------------------------------------------------- #

def test_graphics_designer_treats_7_and_seven_as_the_same_tenant(as_caller):
    """``_owned_run`` compares ``str(user["id"])`` against a ``user_id`` that was
    itself stored through ``str()``. Both sides normalise, so the two identities
    meet in the middle and the second caller reads the first one's run.

    Written as an assertion of the *current* behaviour, not of the desired one.
    If a unified tenant key makes this a 404, that is very likely an improvement
    — but it is a behaviour change, and it should be made on purpose.
    """
    run_id = client.post("/api/gd/runs", json={}).json()["id"]

    as_caller(STRINGY)
    read = client.get(f"/api/gd/runs/{run_id}")
    assert read.status_code == 200, (
        "if this now 404s, Graphics Designer stopped collapsing 7 and '7' — "
        "a deliberate change, so update this test rather than reverting it"
    )
    assert read.json()["user_id"] == "7"


def test_creative_agent_treats_7_and_seven_as_the_same_tenant(as_caller):
    """``_owned`` — same normalisation on both sides, same collapse."""
    created = client.post("/api/creative/runs",
                          json={"creative_type": "brochure", "brief": "x"})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]

    as_caller(STRINGY)
    read = client.get(f"/api/creative/runs/{run_id}")
    assert read.status_code == 200
    assert read.json()["user_id"] == "7"


# --------------------------------------------------------------------------- #
# The router that does not — and therefore isolates where the others do not
# --------------------------------------------------------------------------- #

def test_marketing_research_treats_7_and_seven_as_two_different_tenants(as_caller):
    """MR stores and compares the id raw, so ``7 != "7"`` and the two stay apart
    — at the named-resource comparison *and* in the aggregate list path.

    This is the opposite answer to the two tests above, on the same input.
    Both cannot survive a unified tenant key.
    """
    ingested = client.post("/api/mr/ingest",
                           files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                           data={"platform": "google_ads"})
    assert ingested.status_code == 200, ingested.text
    dataset_id = ingested.json()["dataset_id"]
    report_id = client.post("/api/mr/reports/daily_summary").json()["id"]

    as_caller(STRINGY)
    assert client.get("/api/mr/datasets").json() == []
    assert client.get("/api/mr/overview").json()["has_data"] is False
    assert client.get(f"/api/mr/runs/{report_id}").status_code == 404
    assert client.delete(f"/api/mr/datasets/{dataset_id}").status_code == 404


def test_the_routers_do_not_agree_and_that_is_the_finding(as_caller):
    """One test that states the disagreement in one place.

    Same two callers, same question — "may the second one read the first one's
    work?" — and the answer depends on which agent you ask. This is the summary
    a reviewer should see when the refactor lands: whatever the new tenant key
    does with a non-string id, exactly one of these columns keeps its current
    behaviour.
    """
    gd_run = client.post("/api/gd/runs", json={}).json()["id"]
    client.post("/api/mr/ingest",
                files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                data={"platform": "google_ads"})

    as_caller(STRINGY)
    verdicts = {
        "graphics_designer": client.get(f"/api/gd/runs/{gd_run}").status_code,
        "marketing_research": 200 if client.get("/api/mr/datasets").json() else 404,
    }
    assert verdicts == {
        "graphics_designer": 200,     # same tenant
        "marketing_research": 404,    # different tenant
    }, verdicts


# --------------------------------------------------------------------------- #
# The other half of the same disagreement: a caller with no id
# --------------------------------------------------------------------------- #
# ``user["id"]`` (GD, Creative, MR) subscripts, so an absent key is a crash
# rather than a refusal. Unreachable through real sign-in — the JWT always
# carries ``sub`` — so this is a contract test, not a vulnerability test. It is
# here because a refactor that swaps ``dict`` for a typed tenant object has to
# decide what an id-less caller is.

def test_an_id_less_caller_crashes_the_subscripting_routers(as_caller):
    """GD raises ``KeyError`` rather than answering. A 500, not a refusal.

    Asserted as a raise because the app re-raises after its handler runs, so
    this is what the caller's request actually does today.
    """
    as_caller({"email": "no-id@legalsoft.com", "is_admin": False, "is_creator": False,
               "session_id": "", "timezone": "UTC"})
    with pytest.raises(KeyError):
        client.post("/api/gd/runs", json={})
