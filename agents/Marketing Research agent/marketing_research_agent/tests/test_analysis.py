from marketing_research_agent import analysis


def test_offline_narrate_is_deterministic_and_nonempty(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    out = analysis.narrate(
        "daily_summary", {"channels": {"Google": {"cost_per_demo_booked": 300}}}
    )
    assert isinstance(out, str) and "Google" in out


def test_narrate_never_raises_on_bad_kind(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    out = analysis.narrate("does_not_exist", {"x": 1})
    assert isinstance(out, str)
