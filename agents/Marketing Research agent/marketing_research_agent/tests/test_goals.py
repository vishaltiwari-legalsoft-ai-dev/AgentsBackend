from datetime import date

from marketing_research_agent import goals
from marketing_research_agent.schemas import CampaignMetric


def _m(**kw):
    base = dict(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="b", spend=4000.0, leads=10, qualified_leads=5,
        demos_booked=0, demos_completed=0, date=date(2026, 6, 30),
    )
    base.update(kw)
    return CampaignMetric(**base)


def test_channel_goal_lookup_case_insensitive():
    g = goals.channel_goal("google")
    assert g is not None and g.cpd_booked_low == 550 and g.cpd_booked_high == 750


def test_spend_with_no_demo_is_red():
    flags = goals.evaluate(_m(spend=3500.0, demos_booked=0))
    assert any(f.level == "red" and f.metric == "spend_no_demo" for f in flags)


def test_cost_per_booking_over_threshold_flags():
    flags = goals.evaluate(_m(spend=900.0, demos_booked=3))  # cost/booking = 300 > 150
    assert any(f.metric == "cost_per_booking" for f in flags)


def test_cpql_red_flag():
    flags = goals.evaluate(_m(spend=4000.0, qualified_leads=5))  # cpql = 800 >= 600
    assert any(f.level == "red" and f.metric == "cost_per_qualified_lead" for f in flags)


def test_conversion_drop_flag():
    m = _m(spend=100.0, leads=10, demos_booked=1)  # current conv = 0.1
    flags = goals.evaluate(m, prior_conversion=0.20)  # 50% drop
    assert any(f.metric == "conversion_drop" for f in flags)


def test_targets_are_editable_and_change_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    assert goals.get_targets()["edited"] is False

    # CPQL 400 is under the default $600 red line…
    m = _m(spend=2000.0, qualified_leads=5, demos_booked=2)
    assert not any(f.metric == "cost_per_qualified_lead" for f in goals.evaluate(m))

    # …but over an edited $300 red line.
    t = goals.set_targets({"thresholds": {"cost_per_qualified_lead_red": 300}})
    assert t["edited"] is True and t["thresholds"]["cost_per_qualified_lead_red"] == 300
    assert any(f.metric == "cost_per_qualified_lead" for f in goals.evaluate(m))

    goals.reset_targets()
    assert goals.get_targets()["edited"] is False


def test_channel_goals_are_editable(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    goals.set_targets({"channel_goals": {"Google": {"cpd_booked_high": 900}}})
    g = goals.channel_goal("google")
    assert g.cpd_booked_high == 900 and g.cpd_booked_low == 550  # untouched default


def test_set_targets_rejects_unknown_keys(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    with pytest.raises(ValueError):
        goals.set_targets({"thresholds": {"nope": 1}})
    with pytest.raises(ValueError):
        goals.set_targets({"thresholds": {"cac_red": "high"}})


# --- durability: Cloud Run's disk is ephemeral, so edits must reach Firestore --

class _FakeDoc:
    """Minimal stand-in for a Firestore document reference."""

    def __init__(self, store, fail=False):
        self._store = store
        self._fail = fail

    def set(self, payload):
        if self._fail:
            raise RuntimeError("firestore unavailable")
        self._store["doc"] = payload

    def get(self):
        doc = self

        class _Snap:
            exists = "doc" in doc._store

            @staticmethod
            def to_dict():
                return doc._store.get("doc")

        return _Snap()

    def delete(self):
        self._store.pop("doc", None)


def test_saved_targets_survive_an_empty_disk(monkeypatch, tmp_path):
    """The prod bug: targets.json lives on Cloud Run's ephemeral disk, so every
    redeploy silently reset the desk's edits. Reads must consult Firestore."""
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    cloud: dict = {}
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)
    monkeypatch.setattr(goals, "_doc", lambda: _FakeDoc(cloud))

    goals.set_targets({"thresholds": {"cost_per_qualified_lead_red": 300}})

    # Redeploy: the container comes up with a blank disk.
    (tmp_path / "targets.json").unlink()
    assert goals.get_targets()["thresholds"]["cost_per_qualified_lead_red"] == 300


def test_failed_cloud_write_is_not_reported_as_saved(monkeypatch, tmp_path):
    """Saving to ephemeral disk only is not saving - say so rather than toast success."""
    import pytest

    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)
    monkeypatch.setattr(goals, "_doc", lambda: _FakeDoc({}, fail=True))

    with pytest.raises(RuntimeError):
        goals.set_targets({"thresholds": {"cost_per_qualified_lead_red": 300}})


def test_reset_clears_the_cloud_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    cloud: dict = {}
    monkeypatch.setattr(goals, "_use_cloud", lambda: True)
    monkeypatch.setattr(goals, "_doc", lambda: _FakeDoc(cloud))

    goals.set_targets({"thresholds": {"cac_red": 4000}})
    goals.reset_targets()
    assert cloud == {}
    assert goals.get_targets()["edited"] is False
