"""No hand-written template may ship as AI output.

MR's offline summaries are deliberately written in "the same shape the LLM is
asked for", so a degraded read renders identically to a real one — and the
report downloads as a client-facing PDF. That makes the ``ai`` / ``fallback_reason``
pair the *only* honest tell, and these tests pin it on all four paths that
substitute a template for the model:

* ``analysis.narrate`` → ``report["markdown"]`` / ``["html"]``
* ``reports._vendor_insights`` → ``structured["vendor_insights"]``
* ``insight.answer`` → the ``/mr/ask`` answer card
* ``profiles._llm_profile`` → "deep profile every tab with the LLM"

The contract (C-1): a degraded path returns ``ai: False`` AND a non-empty
``fallback_reason`` naming the actual cause. Never one without the other, and
never a canned payload carrying neither.
"""

from datetime import date

import pytest

from marketing_research_agent import analysis, insight, profiles, reports
from marketing_research_agent.schemas import CampaignMetric, Lead, MediaOpportunity
from marketing_research_agent.workbook import TabGrid


# --- helpers ---------------------------------------------------------------

class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, prompt):
        return _FakeResp(self._content)


@pytest.fixture
def online(monkeypatch):
    """Leave MR_OFFLINE behind so the real LLM branch is exercised. The repo-root
    conftest keeps the OpenRouter key empty, so with no further stubbing the
    branch fails the way a key-less deployment does."""
    monkeypatch.delenv("MR_OFFLINE", raising=False)


def _stub_llm(monkeypatch, content):
    from app.services import openrouter

    monkeypatch.setattr(openrouter, "get_llm", lambda **kw: _FakeLLM(content))


def _dataset():
    m = CampaignMetric(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="c", spend=1200.0, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 29),
    )
    l = Lead(id="1", channel="Google", utm_source="g", utm_medium="cpc",
             utm_campaign="c", practice_area="PI", stage="qualified",
             created_at=date(2026, 6, 29))
    o = MediaOpportunity(name="Pod", type="podcast", audience_size=50000,
                         engagement_rate=0.8, host_authority=0.9,
                         practice_area_fit=1.0)
    return {"metrics": [m], "leads": [l], "opportunities": [o],
            "today": date(2026, 6, 30)}


def _assert_honest(payload: dict, *, ai_key="ai", reason_key="fallback_reason"):
    """The invariant itself: ai False implies a populated reason, and vice versa."""
    ai, reason = payload[ai_key], payload[reason_key]
    assert isinstance(ai, bool)
    if ai:
        assert reason is None, f"claimed AI output but carried a reason: {reason!r}"
    else:
        assert reason and str(reason).strip(), "degraded output with no stated cause"


# --- analysis.narrate ------------------------------------------------------

def test_narrate_offline_is_flagged_not_passed_off_as_ai():
    out = analysis.narrate_result("daily_summary", {"totals": {"spend": 100}})
    assert out["text"]
    assert out["ai"] is False
    assert "MR_OFFLINE" in out["fallback_reason"]
    _assert_honest(out)


def test_narrate_missing_credential_is_named(online):
    """A key-less deployment previously got the template with no tell at all —
    the bare `except Exception` swallowed the cause."""
    out = analysis.narrate_result("daily_summary", {"totals": {"spend": 100}})
    assert out["text"], "the template read is still returned — it is legitimate"
    assert out["ai"] is False
    assert out["fallback_reason"] and "MR_OFFLINE" not in out["fallback_reason"]
    _assert_honest(out)


def test_narrate_provider_error_names_the_exception(online, monkeypatch):
    from app.services import openrouter

    def _boom(**kw):
        raise TimeoutError("upstream read timed out")

    monkeypatch.setattr(openrouter, "get_llm", _boom)
    out = analysis.narrate_result("daily_summary", {"totals": {"spend": 1}})
    assert out["ai"] is False
    assert "timed out" in out["fallback_reason"]
    assert "TimeoutError" in out["fallback_reason"]
    _assert_honest(out)


def test_narrate_empty_model_reply_is_a_fallback_not_a_success(online, monkeypatch):
    _stub_llm(monkeypatch, "   ")
    out = analysis.narrate_result("daily_summary", {"totals": {"spend": 1}})
    assert out["ai"] is False
    assert out["fallback_reason"]
    assert out["text"], "falls back to the template rather than returning blank"
    _assert_honest(out)


def test_narrate_real_model_output_is_claimed_as_ai(online, monkeypatch):
    _stub_llm(monkeypatch, "Spend is up 12% week over week.")
    out = analysis.narrate_result("daily_summary", {"totals": {"spend": 1}})
    assert out == {"text": "Spend is up 12% week over week.", "ai": True,
                   "fallback_reason": None}
    _assert_honest(out)


def test_narrate_str_wrapper_still_returns_plain_text():
    """competitor_intel and existing callers keep the old value-only contract."""
    assert isinstance(analysis.narrate("daily_summary", {"totals": {"spend": 1}}), str)


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("deadline"), "timed out"),
    (RuntimeError("OPENROUTER_API_KEY is not configured"), "credential"),
    (ImportError("no module named app"), "unavailable in this runtime"),
    (RuntimeError("429 rate limit exceeded"), "rate-limited"),
    (ValueError("something else"), "call failed"),
])
def test_failure_reason_names_the_actual_cause(exc, expected):
    assert expected in analysis.failure_reason(exc)


def test_failure_reason_is_bounded():
    assert len(analysis.failure_reason(RuntimeError("x" * 5000))) <= 300


# --- reports: the client-facing PDF ----------------------------------------

def test_offline_report_payload_admits_it_is_not_ai(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build("daily_summary", _dataset(), user_id="u1")
    assert r["markdown"] and r["html"]
    assert r["ai"] is False, "template narrative shipped as an AI report"
    assert r["fallback_reason"]
    _assert_honest(r)
    _assert_honest(r, ai_key="narrative_ai", reason_key="narrative_fallback_reason")


def test_every_report_kind_carries_the_pair(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    for kind in reports.KINDS:
        r = reports.build(kind, _dataset(), user_id="u1")
        assert "ai" in r and "fallback_reason" in r, f"{kind} lost the pair"
        _assert_honest(r)


def test_report_claims_ai_only_when_the_model_wrote_it(online, monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    _stub_llm(monkeypatch, "Google is the efficient channel this week.")
    r = reports.build("daily_summary", _dataset(), user_id="u1")
    assert r["ai"] is True
    assert r["fallback_reason"] is None
    assert "Google is the efficient channel" in r["markdown"]
    _assert_honest(r)


def test_vendor_insight_fallback_is_flagged_and_reported(monkeypatch, tmp_path):
    """The vendor rows are shape-identical whichever path produced them, so the
    structured block has to say which."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    vendors = [{"vendor": "V1", "spend": 100.0, "leads": 5, "qualified_leads": 2,
                "demos_booked": 2, "demos_completed": 1,
                "cost_per_qualified_lead": 50.0, "cost_per_demo_completed": 100.0}]
    rows, reason = reports._vendor_insights(vendors, [])
    assert len(rows) == 1 and len(rows[0]["insights"]) == 3
    assert reason, "canned vendor insights returned with no stated cause"


def test_vendor_insights_validation_rejection_says_so(online, monkeypatch):
    """A model reply that fails validation is a distinct cause from "no key" —
    the reason must not blur the two."""
    _stub_llm(monkeypatch, '[{"vendor": "V1", "insights": ["a"], "actions": ["b"]}]')
    vendors = [{"vendor": "V1", "spend": 100.0, "leads": 5, "qualified_leads": 2,
                "demos_booked": 2, "demos_completed": 1,
                "cost_per_qualified_lead": 50.0, "cost_per_demo_completed": 100.0}]
    rows, reason = reports._vendor_insights(vendors, [])
    assert rows and "validation" in reason


def test_vendor_insights_accepted_model_output_has_no_reason(online, monkeypatch):
    _stub_llm(monkeypatch, '[{"vendor": "V1", "insights": ["a", "b", "c"], '
                           '"actions": ["x", "y", "z"]}]')
    vendors = [{"vendor": "V1", "spend": 100.0, "leads": 5, "qualified_leads": 2,
                "demos_booked": 2, "demos_completed": 1,
                "cost_per_qualified_lead": 50.0, "cost_per_demo_completed": 100.0}]
    rows, reason = reports._vendor_insights(vendors, [])
    assert reason is None and rows[0]["insights"] == ["a", "b", "c"]


def test_degraded_vendor_insights_drag_the_whole_report_to_not_ai(online, monkeypatch, tmp_path):
    """Narrative model-written, vendor insights canned → the report as a whole is
    not AI output, and the reason survives to the payload."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(analysis, "narrate_result", lambda k, d: {
        "text": "real narrative", "ai": True, "fallback_reason": None})
    monkeypatch.setattr(reports, "_vendor_insights",
                        lambda v, r, t=None: ([], "the model provider call failed (X: y)"))
    ds = {**_dataset(), "vendor_metrics": {"V1": _dataset()["metrics"]}}
    r = reports.build("weekly_summary", ds, user_id="u1")
    assert r["ai"] is False
    assert "the model provider call failed" in r["fallback_reason"]
    assert r["narrative_ai"] is True
    _assert_honest(r)


# --- insight.answer (/mr/ask) ----------------------------------------------

_TRACKER = TabGrid(
    title="Marketing 2026 Overall Report", gid=1, hidden=False,
    rows=[["All", "Jan (Performance)", "Feb (Performance)"],
          ["Spend ", "$100", "$120"],
          ["Leads", "5", "8"]],
    n_rows=3, n_cols=3,
)


def _profiles_and_grids():
    profs = [profiles._heuristic_profile(_TRACKER, 2026)]
    return profs, {_TRACKER.title: _TRACKER.rows}


def test_offline_answer_is_flagged_not_silently_ai():
    profs, grids = _profiles_and_grids()
    out = insight.answer("How much did we spend?", profs, grids, year=2026)
    assert out["answer"]
    assert out["ai"] is False
    assert out["fallback_reason"]
    _assert_honest(out)


def test_model_written_answer_is_claimed_as_ai(online, monkeypatch):
    _stub_llm(monkeypatch, "Spend was $220.\n- Google led.\nRecommend: hold.")
    profs, grids = _profiles_and_grids()
    out = insight.answer("How much did we spend?", profs, grids, year=2026)
    assert out["ai"] is True and out["fallback_reason"] is None
    _assert_honest(out)


def test_answer_keeps_its_existing_keys():
    profs, grids = _profiles_and_grids()
    out = insight.answer("How much did we spend?", profs, grids, year=2026)
    assert {"question", "timeframe", "answer", "used_tabs"} <= set(out)


# --- profiles: "deep profile every tab with the LLM" -----------------------

def test_heuristic_profile_never_claims_to_be_ai():
    p = profiles._heuristic_profile(_TRACKER, 2026)
    assert p.ai is False and p.fallback_reason
    _assert_honest(p.__dict__)


def test_deep_profile_falling_back_is_flagged(online, monkeypatch):
    """`deep=True` promises an LLM profile; when that fails the caller gets
    keyword heuristics and must be told so."""
    from app.services import openrouter

    def _boom(**kw):
        raise TimeoutError("profile call timed out")

    monkeypatch.setattr(openrouter, "get_llm", _boom)
    p = profiles._llm_profile(_TRACKER, 2026)
    assert p.ai is False
    assert "timed out" in p.fallback_reason
    _assert_honest(p.__dict__)


def test_deep_profile_success_is_claimed_as_ai(online, monkeypatch):
    _stub_llm(monkeypatch, '{"kind": "performance_tracker", "granularity": "monthly", '
                           '"date_range": null, "platforms": [], "metrics": [], '
                           '"summary": "model summary", "useful": true}')
    p = profiles._llm_profile(_TRACKER, 2026)
    assert p.ai is True and p.fallback_reason is None
    assert p.summary == "model summary"
    _assert_honest(p.__dict__)


def test_profile_cache_roundtrip_preserves_provenance(monkeypatch, tmp_path):
    """Caches written before these fields existed must still load, and a fresh
    cache must not lose the flags."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    first = profiles.profile_workbook([_TRACKER], year=2026, deep=False)
    assert first[0].ai is False and first[0].fallback_reason
    profiles.save_cache("sig-1", first)
    loaded = profiles.load_cached("sig-1")
    assert loaded[0].ai is False and loaded[0].fallback_reason == first[0].fallback_reason


def test_legacy_cache_without_provenance_still_loads():
    legacy = {"title": "T", "gid": 1, "kind": "other", "granularity": "none",
              "date_range": None}
    p = profiles.TabProfile(**legacy)
    assert p.ai is False and p.fallback_reason is None
