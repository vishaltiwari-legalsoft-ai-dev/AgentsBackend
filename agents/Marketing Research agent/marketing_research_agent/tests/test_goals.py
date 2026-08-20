"""2026 goals + red-flag thresholds, and the per-workspace targets store.

Targets were ONE document for the whole deployment until 2026-08-21 — any
desk's edit re-flagged every other desk's dashboard. The store half of this file
covers both sides of that fix: two workspaces are independent, and the workspaces
that had edits in the shared document still have them.
"""
import json
from datetime import date

import pytest

from marketing_research_agent import goals
from marketing_research_agent.schemas import CampaignMetric

#: Two workspaces. Nothing either of them saves may reach the other.
DESK = "mr-desk"
OTHER = "mr-other-desk"


@pytest.fixture()
def offline_targets(monkeypatch, tmp_path):
    """A throwaway targets store, isolated from the developer's real one."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    goals.invalidate_targets_cache()
    yield tmp_path / "targets.json"
    goals.invalidate_targets_cache()


def _m(**kw):
    base = dict(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="b", spend=4000.0, leads=10, qualified_leads=5,
        demos_booked=0, demos_completed=0, date=date(2026, 6, 30),
    )
    base.update(kw)
    return CampaignMetric(**base)


def _defaults() -> dict:
    """The verbatim 2026 targets, resolved without touching any store."""
    return {
        "thresholds": goals.default_thresholds(),
        "channel_goals": {name: {f: getattr(g, f) for f in goals._GOAL_FIELDS}
                          for name, g in goals.CHANNEL_GOALS.items()},
        "edited": False,
    }


def _write_legacy(path, **thresholds) -> None:
    """The pre-tenancy file: overrides at the TOP level, no ``users`` map."""
    path.write_text(json.dumps({"thresholds": thresholds}), encoding="utf-8")


def test_channel_goal_lookup_case_insensitive():
    g = goals.channel_goal("google", _defaults())
    assert g is not None and g.cpd_booked_low == 550 and g.cpd_booked_high == 750


def test_spend_with_no_demo_is_red():
    flags = goals.evaluate(_m(spend=3500.0, demos_booked=0), targets=_defaults())
    assert any(f.level == "red" and f.metric == "spend_no_demo" for f in flags)


def test_cost_per_booking_over_threshold_flags():
    # cost/booking = 300 > 150
    flags = goals.evaluate(_m(spend=900.0, demos_booked=3), targets=_defaults())
    assert any(f.metric == "cost_per_booking" for f in flags)


def test_cpql_red_flag():
    # cpql = 800 >= 600
    flags = goals.evaluate(_m(spend=4000.0, qualified_leads=5), targets=_defaults())
    assert any(f.level == "red" and f.metric == "cost_per_qualified_lead" for f in flags)


def test_conversion_drop_flag():
    m = _m(spend=100.0, leads=10, demos_booked=1)  # current conv = 0.1
    flags = goals.evaluate(m, prior_conversion=0.20, targets=_defaults())  # 50% drop
    assert any(f.metric == "conversion_drop" for f in flags)


def test_evaluate_cannot_reach_the_store_at_all():
    """``targets`` is required, so no flag can be judged against a workspace the
    caller never named — which is exactly how the shared-document bug worked."""
    with pytest.raises(TypeError):
        goals.evaluate(_m())  # type: ignore[call-arg]


def test_targets_are_editable_and_change_flags(offline_targets):
    assert goals.get_targets(DESK)["edited"] is False

    # CPQL 400 is under the default $600 red line…
    m = _m(spend=2000.0, qualified_leads=5, demos_booked=2)
    assert not any(f.metric == "cost_per_qualified_lead"
                   for f in goals.evaluate(m, targets=goals.get_targets(DESK)))

    # …but over an edited $300 red line.
    t = goals.set_targets(DESK, {"thresholds": {"cost_per_qualified_lead_red": 300}})
    assert t["edited"] is True and t["thresholds"]["cost_per_qualified_lead_red"] == 300
    assert any(f.metric == "cost_per_qualified_lead"
               for f in goals.evaluate(m, targets=goals.get_targets(DESK)))

    goals.reset_targets(DESK)
    assert goals.get_targets(DESK)["edited"] is False


def test_channel_goals_are_editable(offline_targets):
    goals.set_targets(DESK, {"channel_goals": {"Google": {"cpd_booked_high": 900}}})
    g = goals.channel_goal("google", goals.get_targets(DESK))
    assert g.cpd_booked_high == 900 and g.cpd_booked_low == 550  # untouched default


def test_set_targets_rejects_unknown_keys(offline_targets):
    with pytest.raises(ValueError):
        goals.set_targets(DESK, {"thresholds": {"nope": 1}})
    with pytest.raises(ValueError):
        goals.set_targets(DESK, {"thresholds": {"cac_red": "high"}})


# --- tenancy ----------------------------------------------------------------

def test_one_workspaces_edit_does_not_move_another_workspaces_red_line(offline_targets):
    """The leak, stated as an assertion: a $4,242 CAC line is one desk's call."""
    goals.set_targets(DESK, {"thresholds": {"cac_red": 4242}})

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == 4242
    assert goals.get_targets(OTHER)["thresholds"]["cac_red"] == goals.CAC_RED
    assert goals.get_targets(OTHER)["edited"] is False


def test_two_workspaces_can_both_hold_edits_at_once(offline_targets):
    goals.set_targets(DESK, {"thresholds": {"cac_red": 4242}})
    goals.set_targets(OTHER, {"thresholds": {"cac_red": 1111}})

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == 4242
    assert goals.get_targets(OTHER)["thresholds"]["cac_red"] == 1111


def test_a_reset_is_scoped_to_the_workspace_that_asked_for_it(offline_targets):
    goals.set_targets(DESK, {"thresholds": {"cac_red": 4242}})
    goals.set_targets(OTHER, {"thresholds": {"cac_red": 1111}})

    goals.reset_targets(DESK)

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == goals.CAC_RED
    assert goals.get_targets(OTHER)["thresholds"]["cac_red"] == 1111


# --- the one-time migration off the shared document -------------------------

def test_a_workspace_with_no_edits_and_no_legacy_doc_gets_the_verbatim_defaults(
    offline_targets
):
    t = goals.get_targets("brand-new-desk")
    assert t["edited"] is False
    assert t["thresholds"]["cac_red"] == goals.CAC_RED
    assert not offline_targets.exists(), "a read must not create a document"


def test_the_legacy_shared_document_seeds_a_workspaces_first_read(offline_targets):
    _write_legacy(offline_targets, cac_red=4242)

    t = goals.get_targets(DESK)
    assert t["thresholds"]["cac_red"] == 4242
    assert t["edited"] is True

    # …and it is now this workspace's OWN document.
    stored = json.loads(offline_targets.read_text(encoding="utf-8"))
    assert stored["users"][DESK]["thresholds"]["cac_red"] == 4242
    assert stored["users"][DESK]["seeded_from"] == goals.LEGACY_TARGETS_DOC_ID


def test_the_seed_lands_once_and_never_reopens_a_workspaces_own_edits(offline_targets):
    """The property that makes the migration safe to run on every read."""
    _write_legacy(offline_targets, cac_red=4242)

    goals.get_targets(DESK)                                     # seeds
    goals.set_targets(DESK, {"thresholds": {"cac_red": 2600}})   # own edit
    goals.invalidate_targets_cache()

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == 2600


def test_a_reset_does_not_hand_the_legacy_figures_back(offline_targets):
    """Reset means the 2026 defaults, not whatever the shared document said.

    This is why the reset writes an empty document instead of deleting one — an
    absent document is what the seed acts on.
    """
    _write_legacy(offline_targets, cac_red=4242)

    goals.get_targets(DESK)  # seeds
    goals.reset_targets(DESK)
    goals.invalidate_targets_cache()

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == goals.CAC_RED
    assert goals.get_targets(DESK)["edited"] is False


def test_the_legacy_document_is_copied_not_shared(offline_targets):
    """Two workspaces seeded from the same document must not re-converge."""
    _write_legacy(offline_targets, cac_red=4242)

    goals.set_targets(DESK, {"thresholds": {"cac_red": 2600}})
    goals.invalidate_targets_cache()

    assert goals.get_targets(OTHER)["thresholds"]["cac_red"] == 4242, (
        "the second desk inherited the shared figures, not the first desk's edit"
    )
    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == 2600
    # The legacy block itself is never rewritten.
    stored = json.loads(offline_targets.read_text(encoding="utf-8"))
    assert stored["thresholds"]["cac_red"] == 4242


def test_the_seed_window_can_be_closed(offline_targets, monkeypatch):
    """Once the desks that were using the shared document have signed in, a new
    workspace must start from the verbatim defaults."""
    _write_legacy(offline_targets, cac_red=4242)
    monkeypatch.setenv("MR_TARGETS_LEGACY_SEED", "0")

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == goals.CAC_RED
    assert "users" not in json.loads(offline_targets.read_text(encoding="utf-8"))


# --- durability: Cloud Run's disk is ephemeral, so edits must reach Firestore --

class _FakeStore:
    """Minimal stand-in for a Firestore collection, keyed by document id."""

    def __init__(self, fail=False):
        self.docs: dict[str, dict] = {}
        self._fail = fail

    def doc(self, doc_id: str):
        store, fail = self, self._fail

        class _Ref:
            @staticmethod
            def set(payload):
                if fail:
                    raise RuntimeError("firestore unavailable")
                store.docs[doc_id] = payload

            @staticmethod
            def get():
                class _Snap:
                    exists = doc_id in store.docs

                    @staticmethod
                    def to_dict():
                        return store.docs.get(doc_id)

                return _Snap()

            @staticmethod
            def delete():
                store.docs.pop(doc_id, None)

        return _Ref()


@pytest.fixture()
def cloud(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    store = _FakeStore()
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)
    monkeypatch.setattr(goals, "_doc", lambda doc_id: store.doc(doc_id))
    goals.invalidate_targets_cache()
    yield store
    goals.invalidate_targets_cache()


def test_saved_targets_survive_an_empty_disk(cloud, tmp_path):
    """The prod bug: targets.json lives on Cloud Run's ephemeral disk, so every
    redeploy silently reset the desk's edits. Reads must consult Firestore."""
    goals.set_targets(DESK, {"thresholds": {"cost_per_qualified_lead_red": 300}})

    # Redeploy: the container comes up with a blank disk.
    (tmp_path / "targets.json").unlink()
    goals.invalidate_targets_cache()
    assert goals.get_targets(DESK)["thresholds"]["cost_per_qualified_lead_red"] == 300


def test_each_workspace_gets_its_own_firestore_document(cloud):
    goals.set_targets(DESK, {"thresholds": {"cac_red": 4242}})
    goals.set_targets(OTHER, {"thresholds": {"cac_red": 1111}})

    assert set(cloud.docs) == {goals.targets_doc_id(DESK), goals.targets_doc_id(OTHER)}
    assert goals.LEGACY_TARGETS_DOC_ID not in cloud.docs, (
        "the legacy shared document must never be written to again"
    )


def test_the_legacy_cloud_document_seeds_and_is_left_alone(cloud):
    cloud.docs[goals.LEGACY_TARGETS_DOC_ID] = {"thresholds": {"cac_red": 4242}}

    assert goals.get_targets(DESK)["thresholds"]["cac_red"] == 4242
    assert cloud.docs[goals.targets_doc_id(DESK)]["thresholds"]["cac_red"] == 4242
    assert cloud.docs[goals.LEGACY_TARGETS_DOC_ID] == {"thresholds": {"cac_red": 4242}}


def test_an_unreadable_store_never_triggers_the_seed(monkeypatch, tmp_path):
    """The dangerous half of seed-on-read.

    A Firestore blip that looked like "this workspace has no document" would
    seed the pre-tenancy figures straight over the desk's real edits. The read
    failure is reported back instead, and the answer is the honest defaults.
    """
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)

    def _blows_up(_doc_id):
        raise RuntimeError("firestore unavailable")

    seeded: list[object] = []
    monkeypatch.setattr(goals, "_doc", _blows_up)
    monkeypatch.setattr(goals, "_seed_from_legacy",
                        lambda uid: (seeded.append(uid), {})[1])
    goals.invalidate_targets_cache()
    try:
        assert goals.get_targets(DESK)["thresholds"]["cac_red"] == goals.CAC_RED
        assert seeded == [], "a read failure was mistaken for an absent document"
    finally:
        goals.invalidate_targets_cache()


def test_failed_cloud_write_is_not_reported_as_saved(monkeypatch, tmp_path):
    """Saving to ephemeral disk only is not saving - say so rather than toast success."""
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    store = _FakeStore(fail=True)
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)
    monkeypatch.setattr(goals, "_doc", lambda doc_id: store.doc(doc_id))
    goals.invalidate_targets_cache()
    try:
        with pytest.raises(RuntimeError):
            goals.set_targets(DESK, {"thresholds": {"cost_per_qualified_lead_red": 300}})
    finally:
        goals.invalidate_targets_cache()


def test_reset_clears_the_cloud_copy(cloud):
    goals.set_targets(DESK, {"thresholds": {"cac_red": 4000}})
    goals.reset_targets(DESK)

    assert cloud.docs[goals.targets_doc_id(DESK)].get("thresholds") is None
    assert goals.get_targets(DESK)["edited"] is False
