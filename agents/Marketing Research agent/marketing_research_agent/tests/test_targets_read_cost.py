"""The targets document must be read once per report, not once per metric row.

``mr_config/targets__{user_id}`` is ONE small document. ``goals.evaluate`` called
``thresholds()`` and ``channel_goal()``, each of which resolved the whole
targets object, so ``campaign_reporting.flag_all`` cost 2 store reads per metric
row — ~120 reads of the same document to build one report. These tests count the
reads directly, so a regression shows up as a number, not as latency.

Since targets became per workspace (2026-08-21) the same numbers pin a second
property: a helper that cannot reach the store cannot resolve a workspace, so it
cannot judge one desk's campaigns against another desk's red lines. The
threshold helpers now take the resolved dict, so most of these counts are zero.
"""
from datetime import date

import pytest

from marketing_research_agent import goals, reports
from marketing_research_agent.modules import campaign_reporting as cr
from marketing_research_agent.schemas import CampaignMetric


@pytest.fixture(autouse=True)
def _isolated_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    # reports.build persists a run — without this it lands in the developer's
    # real MR runs directory and shows up as a fake report in the local console.
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "runs"))
    goals.invalidate_targets_cache()
    yield
    goals.invalidate_targets_cache()


def _count_store_reads(monkeypatch) -> list[int]:
    """Count trips to the STORE (``_load_overrides`` is the only door to it).
    Pins the process cache."""
    calls = [0]
    real = goals._load_overrides

    def _counted(user_id):
        calls[0] += 1
        return real(user_id)

    monkeypatch.setattr(goals, "_load_overrides", _counted)
    return calls


def _count_resolves(monkeypatch) -> list[int]:
    """Count ``get_targets`` RESOLUTIONS — cached or not.

    Counted separately from store reads on purpose: with the cache in place a
    per-row resolve is nearly free to measure but still wrong, and would hide
    the day the cache is removed or its TTL expires mid-request. This is the
    number that pins targets being passed *down* instead of refetched.
    """
    calls = [0]
    real = goals.get_targets

    def _counted(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(goals, "get_targets", _counted)
    return calls


def _metrics(n: int) -> list[CampaignMetric]:
    return [
        CampaignMetric(channel="Google", campaign=f"c{i}", utm_source="g",
                       utm_medium="cpc", utm_campaign=f"c{i}", spend=4000.0,
                       leads=10, qualified_leads=5, demos_booked=0,
                       demos_completed=0, date=date(2026, 6, 30))
        for i in range(n)
    ]


def test_flag_all_resolves_nothing_because_its_caller_hands_the_targets_in(
    monkeypatch
):
    """Was 2 resolves per metric row (``thresholds()`` + ``channel_goal()``
    inside ``evaluate``); 40 rows cost 80. It is now zero: ``targets`` is a
    required argument, so this loop cannot reach the store — which is both the
    read-cost bar and the reason it cannot pick the wrong workspace."""
    targets = goals.get_targets("u1")
    reads = _count_store_reads(monkeypatch)
    resolves = _count_resolves(monkeypatch)
    flags = cr.flag_all(_metrics(40), None, targets)
    assert flags, "the fixture must actually trip flags or this proves nothing"
    assert resolves[0] == 0, (
        f"{resolves[0]} targets resolves for 40 metric rows that were handed "
        "their targets")
    assert reads[0] == 0, f"{reads[0]} store reads for one dataset"


def test_evaluate_accepts_resolved_targets_and_resolves_nothing(monkeypatch):
    targets = goals.get_targets("u1")
    reads = _count_store_reads(monkeypatch)
    resolves = _count_resolves(monkeypatch)
    for m in _metrics(25):
        goals.evaluate(m, targets=targets)
    assert resolves[0] == 0, (
        f"{resolves[0]} resolves while the targets were handed in — evaluate is "
        "refetching what its caller already paid for")
    assert reads[0] == 0, f"{reads[0]} store reads while targets were passed in"


def test_a_whole_report_resolves_the_targets_once(monkeypatch):
    ds = {"metrics": _metrics(30), "leads": [], "today": date(2026, 6, 30),
          "vendor_metrics": {"Vendor A": _metrics(30)}}
    reads = _count_store_reads(monkeypatch)
    resolves = _count_resolves(monkeypatch)
    out = reports.build("weekly_summary", ds, user_id="u1")
    assert out["structured"]["flags"], "fixture must produce flags"
    assert resolves[0] == 1, (
        f"{resolves[0]} targets resolves to build one report — it should resolve once")
    assert reads[0] <= 1, f"{reads[0]} store reads to build one report"


def test_repeat_calls_are_served_from_the_process_cache(monkeypatch):
    goals.get_targets("u1")  # prime
    calls = _count_store_reads(monkeypatch)
    for _ in range(50):
        goals.thresholds(goals.get_targets("u1"))
        goals.channel_goal("Google", goals.get_targets("u1"))
    assert calls[0] == 0, f"{calls[0]} store reads for 100 cached lookups"


def test_the_cache_is_keyed_per_workspace(monkeypatch):
    """One warm cache entry must not answer for a second workspace.

    A process-wide cache over a now-per-workspace document is the exact shape
    that would re-open the leak behind the API instead of in the store.
    """
    goals.set_targets("desk-a", {"thresholds": {"cac_red": 4242}})
    assert goals.get_targets("desk-a")["thresholds"]["cac_red"] == 4242
    assert goals.get_targets("desk-b")["thresholds"]["cac_red"] == goals.CAC_RED


def test_an_edit_invalidates_only_the_editing_workspace(monkeypatch):
    """…and the other way round: B's warm entry survives A's save."""
    assert goals.get_targets("desk-b")["thresholds"]["cac_red"] == goals.CAC_RED
    goals.set_targets("desk-a", {"thresholds": {"cac_red": 4242}})

    calls = _count_store_reads(monkeypatch)
    assert goals.get_targets("desk-b")["thresholds"]["cac_red"] == goals.CAC_RED
    assert calls[0] == 0, "A's save blew away B's cache entry"


def test_an_edit_is_visible_immediately_not_after_the_ttl(monkeypatch):
    """The cache must never serve a stale figure back to the desk that just
    edited it — writes invalidate."""
    assert goals.thresholds(goals.get_targets("u1"))["cac_red"] == 3000.0
    goals.set_targets("u1", {"thresholds": {"cac_red": 4321.0}})
    assert goals.thresholds(goals.get_targets("u1"))["cac_red"] == 4321.0
    goals.reset_targets("u1")
    assert goals.thresholds(goals.get_targets("u1"))["cac_red"] == 3000.0


def test_the_cache_never_serves_another_store_s_values(monkeypatch, tmp_path):
    """Keyed on the store identity: moving MR_TARGETS_FILE must not hand back
    the previous file's edits."""
    goals.set_targets("u1", {"thresholds": {"cac_red": 4321.0}})
    assert goals.thresholds(goals.get_targets("u1"))["cac_red"] == 4321.0
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "other.json"))
    assert goals.thresholds(goals.get_targets("u1"))["cac_red"] == 3000.0


def test_mr_config_endpoint_resolves_the_thresholds_once(monkeypatch):
    """/mr/config built a five-field dict with five separate resolves."""
    import os

    os.environ["MR_OFFLINE"] = "1"
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app
    from app.security import get_current_user

    fastapi_app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    try:
        reads = _count_store_reads(monkeypatch)
        resolves = _count_resolves(monkeypatch)
        r = TestClient(fastapi_app).get("/api/mr/config")
        assert r.status_code == 200, r.text
        assert len(r.json()["thresholds"]) == 5
        assert resolves[0] == 1, (
            f"{resolves[0]} resolves to render five fields of one dict")
        assert reads[0] <= 1, f"{reads[0]} store reads to render five fields"
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
