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


def test_offline_narrate_is_structured_like_the_prompts_ask(monkeypatch):
    """The offline read is rendered by the same <Prose> as the LLM's, so it must
    come back as a lead line + '- ' bullets + Recommend, not one joined blob."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    out = analysis.narrate("daily_summary", {
        "totals": {"spend": 18624, "demos_completed": 4, "cost_per_demo_completed": 4656},
        "channels": {
            "Google": {"cost_per_demo_booked": 900},
            "Email": {"cost_per_demo_booked": 300},
        },
        "red_flag_vendors": [{"vendor": "Advantech RA", "reasons": ["spent with zero leads"]}],
        "issues": [{"text": "15 campaigns over the $600 cost-per-qualified-lead ceiling", "count": 15}],
    })
    lines = [ln for ln in out.splitlines() if ln.strip()]

    assert len(lines) > 1, "offline read collapsed into a single blob"
    assert not lines[0].startswith("- "), "first line must be the standalone verdict"
    assert any(ln.startswith("- ") for ln in lines), "no bullet findings"
    assert lines[-1].startswith("Recommend:"), "Recommend must be the trailing line"
    assert any("Advantech RA" in ln and ln.startswith("- ") for ln in lines), \
        "each flagged vendor gets its own bullet"
