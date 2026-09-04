from datetime import date

import pytest

from marketing_research_agent import reports
from marketing_research_agent.schemas import CampaignMetric, Lead, MediaOpportunity


def _dataset():
    m = CampaignMetric(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="c", spend=1200.0, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 29),
    )
    l = Lead(
        id="1", channel="Google", utm_source="g", utm_medium="cpc", utm_campaign="c",
        practice_area="PI", stage="qualified", created_at=date(2026, 6, 29),
    )
    o = MediaOpportunity(
        name="Pod", type="podcast", audience_size=50000, engagement_rate=0.8,
        host_authority=0.9, practice_area_fit=1.0,
    )
    return {"metrics": [m], "leads": [l], "opportunities": [o], "today": date(2026, 6, 30)}


def test_daily_summary_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build("daily_summary", _dataset(), user_id="u1")
    assert r["kind"] == "daily_summary"
    assert r["markdown"] and r["html"] and r["structured"]["channels"]["Google"]


def test_all_kinds_build_without_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    # NARRATED_KINDS, not KINDS: the two board kinds are real report kinds that
    # this builder deliberately refuses (they take two periods and have no
    # narrative). The refusal is asserted in the board section below.
    for kind in reports.NARRATED_KINDS:
        r = reports.build(kind, _dataset(), user_id="u1")
        assert r["markdown"]


def test_daily_movement_report_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    deltas = [{"vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "date": "2026-02-07",
               "since": "2026-02-06", "days": 1, "month_start": False, "corrected": False,
               "blocks": {"team_overall": {"additive": {"spend.performance": {"delta": 100.0, "mtd": 300.0, "corrected": False},
                                                        "leads.total": {"delta": 2, "mtd": 5, "corrected": False}},
                                           "rates": {}},
                          "channels": {}}}]
    r = reports.build("daily_movement", {"snapshot_deltas": deltas}, user_id="u1")
    assert r["kind"] == "daily_movement"
    assert r["structured"]["vendors"][0]["vendor"] == "Meta 360 RA"
    assert r["markdown"]


def test_report_stamps_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = _dataset()
    ds["sources"] = [{"platform": "sheets:123", "generated_at": "2026-07-07T00:00:00+00:00",
                      "metrics": 1, "leads": 1}]
    r = reports.build("daily_summary", ds, user_id="u1")
    assert r["sources"] == ds["sources"]


def test_daily_report_covers_month_to_date_through_yesterday(monkeypatch, tmp_path):
    """On July 9 the daily report must read July 1–8: July data only, no other
    months, and the period stamped on the report."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(month, day, spend):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=5,
            demos_booked=2, demos_completed=1, date=date(2026, month, day),
        )

    ds = {"metrics": [m(6, 15, 999.0), m(7, 1, 500.0), m(9, 1, 2200.0)],
          "leads": [], "today": date(2026, 7, 9)}
    r = reports.build("daily_summary", ds, user_id="u1")
    p = r["structured"]["period"]
    assert p["start"] == "2026-07-01" and p["end"] == "2026-07-08"
    assert r["structured"]["totals"]["spend"] == 500.0  # June + future September excluded


def test_quarterly_report_spans_the_quarter(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(month, spend):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=5,
            demos_booked=2, demos_completed=1, date=date(2026, month, 1),
        )

    ds = {"metrics": [m(6, 100.0), m(7, 200.0)], "leads": [], "today": date(2026, 7, 9)}
    r = reports.build("quarterly_summary", ds, user_id="u1")
    assert r["structured"]["period"]["start"] == "2026-07-01"
    assert r["structured"]["totals"]["spend"] == 200.0  # Q3 only; June is Q2


def test_report_names_red_flag_vendors_and_insights(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(spend, ql):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=ql,
            demos_booked=2, demos_completed=1, date=date(2026, 7, 1),
        )

    good, bad = m(600.0, 3), m(4000.0, 5)  # bad: CPQL $800 >= $600 red line
    ds = {"metrics": [good, bad], "leads": [], "today": date(2026, 7, 9),
          "vendor_metrics": {"Good Vendor": [good], "Bad Vendor": [bad]}}
    r = reports.build("daily_summary", ds, user_id="u1")
    s = r["structured"]
    assert [v["vendor"] for v in s["red_flag_vendors"]] == ["Bad Vendor"]
    assert "qualified lead" in s["red_flag_vendors"][0]["reasons"][0]
    assert len(s["vendors"]) == 2
    rows = {i["vendor"]: i for i in s["vendor_insights"]}
    assert set(rows) == {"Good Vendor", "Bad Vendor"}
    assert all(len(i["insights"]) == 3 and len(i["actions"]) == 3 for i in rows.values())
    assert "Bad Vendor" in r["markdown"]  # exec summary names the flagged vendor


def test_daily_movement_has_a_real_prompt():
    """Without a prompt file the LLM free-styles a 3000-word report; the daily
    brief must ship with explicit brevity instructions."""
    from marketing_research_agent import analysis

    prompt = analysis.load_prompt("daily_movement")
    assert prompt != "{data}"
    assert "Recommend:" in prompt and "{data}" in prompt


# Every kind whose narrative is rendered by the report doc's <Prose>, which turns
# a lead line + "- " bullets into readable blocks. The desk called these reports
# unreadable while the prompts were ordering "NO bullet points" — guard the
# instruction against drifting away from the renderer again.
NARRATIVE_KINDS = [
    "daily_summary", "weekly_summary", "monthly_summary",
    "quarterly_summary", "threshold_alert", "daily_movement",
]


@pytest.mark.parametrize("kind", NARRATIVE_KINDS)
def test_narrative_prompt_asks_for_the_shape_the_doc_renders(kind):
    from marketing_research_agent import analysis

    prompt = analysis.load_prompt(kind)
    low = prompt.lower()
    assert "{data}" in prompt, f"{kind}: prompt lost its data slot"
    assert "recommend:" in low, f"{kind}: no Recommend line"
    assert "bullet" in low, f"{kind}: does not ask for bullets"
    assert "\n- " in prompt, f"{kind}: does not show the '- ' marker"
    assert "no markdown" not in low, f"{kind}: blanket markdown ban contradicts bullets"
    assert "no bullet" not in low, f"{kind}: still forbids the bullets the doc renders"


# --- explicit period (month/quarter picker) ----------------------------------

def _pm(year, month, day, spend, channel="Google", campaign="c"):
    return CampaignMetric(
        channel=channel, campaign=campaign, utm_source="g", utm_medium="cpc",
        utm_campaign=campaign, spend=spend, leads=10, qualified_leads=5,
        demos_booked=2, demos_completed=1, date=date(year, month, day),
    )


def test_monthly_report_builds_explicit_past_month(monkeypatch, tmp_path):
    """Aug 4: picking July must read July only — not June, not the pre-filled
    September retainer month, and not month-to-date August."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = {"metrics": [_pm(2026, 6, 29, 999.0), _pm(2026, 7, 31, 500.0),
                      _pm(2026, 8, 1, 100.0), _pm(2026, 9, 1, 2200.0)],
          "leads": [], "today": date(2026, 8, 4)}
    r = reports.build("monthly_summary", ds, user_id="u1", period="2026-07")
    p = r["structured"]["period"]
    assert p["start"] == "2026-07-01" and p["end"] == "2026-07-31"
    assert r["structured"]["totals"]["spend"] == 500.0


def test_monthly_report_current_month_clamps_to_yesterday(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = {"metrics": [_pm(2026, 7, 1, 500.0)], "leads": [], "today": date(2026, 7, 9)}
    r = reports.build("monthly_summary", ds, user_id="u1", period="2026-07")
    p = r["structured"]["period"]
    assert p["start"] == "2026-07-01" and p["end"] == "2026-07-08"


def test_quarterly_report_builds_explicit_quarter(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = {"metrics": [_pm(2026, 4, 1, 100.0), _pm(2026, 6, 1, 200.0), _pm(2026, 7, 1, 400.0)],
          "leads": [], "today": date(2026, 8, 4)}
    r = reports.build("quarterly_summary", ds, user_id="u1", period="2026-Q2")
    p = r["structured"]["period"]
    assert p["start"] == "2026-04-01" and p["end"] == "2026-06-30"
    assert r["structured"]["totals"]["spend"] == 300.0


def test_explicit_period_without_data_raises_not_falls_back(monkeypatch, tmp_path):
    """The silent latest-month fallback is for the default path only. An explicit
    'May 2026' with no May data must error, never show another month's numbers."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = {"metrics": [_pm(2026, 6, 29, 999.0)], "leads": [], "today": date(2026, 8, 4)}
    with pytest.raises(reports.PeriodError) as exc:
        reports.build("monthly_summary", ds, user_id="u1", period="2026-05")
    assert "May 2026" in str(exc.value)


def test_bad_period_values_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = {"metrics": [_pm(2026, 7, 1, 1.0)], "leads": [], "today": date(2026, 8, 4)}
    for kind, period in [("monthly_summary", "2026-13"),   # not a month
                         ("monthly_summary", "julio"),      # not a format
                         ("quarterly_summary", "2026-Q5"),  # not a quarter
                         ("daily_summary", "2026-07"),      # kind takes no period
                         ("icp_signal", "2026-07"),         # non-campaign kind
                         ("monthly_summary", "2027-01"),    # future month
                         ("monthly_summary", "0000-01"),    # degenerate year
                         ("quarterly_summary", "9999-Q4")]:  # year overflow in _month_end
        with pytest.raises(reports.PeriodError):
            reports.build(kind, ds, user_id="u1", period=period)


def test_default_window_also_drops_vendors_without_data(monkeypatch, tmp_path):
    """Regression, 2026-08-15: the explicit-period path was honest, the DEFAULT
    one was not — a vendor with nothing in the window was backfilled with its
    latest earlier month and printed under the window's heading. Two live cards
    showed June figures beneath "Aug 1-13, 2026"."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    aug, june = _pm(2026, 8, 1, 500.0), _pm(2026, 6, 1, 9999.0)
    ds = {"metrics": [june, aug], "leads": [], "today": date(2026, 8, 15),
          "vendor_metrics": {"Active Vendor": [aug], "Dormant Vendor": [june]}}
    r = reports.build("daily_summary", ds, user_id="u1")
    assert [v["vendor"] for v in r["structured"]["vendors"]] == ["Active Vendor"]


def test_explicit_month_drops_vendors_without_data_in_month(monkeypatch, tmp_path):
    """A vendor with no July rows disappears from the July report — it must not
    fall back to its June numbers (per-vendor honesty)."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    july, june = _pm(2026, 7, 1, 500.0), _pm(2026, 6, 1, 300.0)
    ds = {"metrics": [june, july], "leads": [], "today": date(2026, 8, 4),
          "vendor_metrics": {"Meta 360 RA": [july], "Old Vendor": [june]}}
    r = reports.build("monthly_summary", ds, user_id="u1", period="2026-07")
    assert [v["vendor"] for v in r["structured"]["vendors"]] == ["Meta 360 RA"]


# --- available periods (picker data) ----------------------------------------

def test_available_periods_lists_data_months_newest_first(monkeypatch, tmp_path):
    """June/July/September data on Aug 4 → July, June (Sept is a pre-filled
    future retainer month; no August data so no August entry)."""
    ds = {"metrics": [_pm(2026, 6, 29, 999.0), _pm(2026, 7, 31, 500.0),
                      _pm(2026, 9, 1, 2200.0)],
          "leads": [], "today": date(2026, 8, 4)}
    p = reports.available_periods(ds)
    assert [m["period"] for m in p["months"]] == ["2026-07", "2026-06"]
    assert [m["label"] for m in p["months"]] == ["July 2026", "June 2026"]
    assert all(m["current"] is False for m in p["months"])
    assert [q["period"] for q in p["quarters"]] == ["2026-Q3", "2026-Q2"]
    assert p["quarters"][0]["current"] is True  # Q3 contains yesterday (Aug 3)


def test_available_periods_flags_current_month(monkeypatch, tmp_path):
    ds = {"metrics": [_pm(2026, 7, 31, 500.0), _pm(2026, 8, 1, 100.0)],
          "leads": [], "today": date(2026, 8, 4)}
    p = reports.available_periods(ds)
    assert p["months"][0] == {"period": "2026-08", "label": "August 2026", "current": True}
    assert p["months"][1]["current"] is False


def test_available_periods_on_month_first_day(monkeypatch, tmp_path):
    """On Aug 1 'yesterday' is July 31 — July is the current/topmost month and a
    zero-day August window is never offered."""
    ds = {"metrics": [_pm(2026, 7, 31, 500.0), _pm(2026, 8, 1, 100.0)],
          "leads": [], "today": date(2026, 8, 1)}
    p = reports.available_periods(ds)
    assert [m["period"] for m in p["months"]] == ["2026-07"]
    assert p["months"][0]["current"] is True


def test_available_periods_empty_dataset(monkeypatch, tmp_path):
    assert reports.available_periods({"metrics": [], "today": date(2026, 8, 4)}) == \
        {"months": [], "quarters": []}


# =============================================================================
# Board report — metric catalog + period roll-up  (board_report.py)
# =============================================================================
# The contract is the marketing team's own Q1-vs-Q2 comparison template: a
# ledger of ``[label, valueA, valueB, format, polarity]`` rows under seven group
# bands. ``_PUBLISHED`` below carries that template's own figures for all 38
# rows and is the single source of truth these tests measure against — the month
# fixtures are DERIVED from it, so a fixture can never drift from the numbers
# the report claims to reproduce.
#
# Three defects are pinned here specifically, because each publishes a number
# that looks entirely plausible on a board slide:
#
#   1. CAC is a name collision. ``CampaignMetric.cac`` is spend/demos_completed
#      ($880.81 on Q1; its own comment calls it a closed/won proxy). The
#      report's CAC is spend / revenue clients ($4,991.28). 5.7x apart.
#   2. A ratio averaged across months instead of recomputed from the summed
#      components is wrong by percentage POINTS, in the direction that flatters.
#   3. A field the sheet does not report must arrive absent. Three of them are
#      absent in production right now, and a 0 in any of those cells is a lie.
#
# These fixtures are hand-built dicts on purpose: they state the sheet's output
# shape rather than calling the parser, so the parser's own mapping work can
# land without touching this file, and vice versa.

import ast
import json
import os
import pathlib

from marketing_research_agent import board_report as br

# The template's own Q1 / Q2 figures, keyed by catalog key. Every published row,
# including the twelve the report derives rather than reads.
_PUBLISHED: dict[str, tuple[float, float]] = {
    # --- Budget & Efficiency
    "budget": (248446.0, 199796.0),
    "spend": (239581.57, 187299.79),
    "leads": (1281, 1220),
    "qualified_leads": (636, 515),
    "qualified_lead_ratio_pct": (49.65, 42.21),
    "cost_per_lead": (187.03, 153.52),
    "cost_per_qualified_lead": (376.70, 363.69),
    "qual_demos_booked": (431, 343),
    "demos_completed": (272, 238),
    # The Conversion Rate denominator — not the all-in count above it.
    "demos_completed_direct": (248, 212),
    "show_up_rate_pct": (63.11, 69.39),
    "lost_dnc_bad_lead": (124, 149),
    "cost_per_qual_demo_booked": (555.87, 546.06),
    "cost_per_demo_completed": (880.81, 786.97),
    # --- Projected — Actualized
    "projected_new_clients": (118, 133),
    "projected_services_sold": (156, 224),
    "projected_amount_sold": (479608.30, 649575.09),
    "projected_mrr_without_setup_fee": (422607.70, 594269.84),
    # --- Revenue — Actualized
    "revenue_clients": (48, 57),
    "services_sold": (81, 135),
    "revenue_amount_sold": (262947.70, 401614.09),
    "revenue_amount_sold_without_setup_fee": (231098.70, 372182.84),
    # --- Projected — Not Actualized
    "projected_new_clients_not_actualized": (114, 136),
    "services_sold_not_actualized": (149, 227),
    "revenue_amount_sold_not_actualized": (452607.70, 660703.09),
    "revenue_amount_sold_without_setup_fee_not_actualized": (397087.70, 603397.84),
    # --- Paying — Not Actualized
    "paying_new_clients": (52, 58),
    "paying_services_sold": (90, 137),
    "paying_revenue_amount_sold": (290691.70, 409704.59),
    "paying_revenue_amount_sold_without_setup_fee": (256943.70, 378958.84),
    # --- Inbound Sales Pipeline
    "inbound_pipeline_revenue_amount_sold": (448996.54, 588791.09),
    # --- Goal & Blended Financials
    "revenue_target_pct": (94.29, 123.50),
    "revenue_sold_goal": (480000.0, 535000.0),
    "average_deal_amount": (4064.48, 4884.02),
    "conversion_rate_pct": (19.35, 26.89),
    "roas_pct": (109.75, 214.42),
    "roas_not_actualized_pct": (188.92, 352.75),
    br.CAC_KEY: (4991.28, 3285.96),
}

# Fields whose monthly figures lean to the END of the period rather than the
# start. A ratio's numerator and denominator must move on DIFFERENT month
# profiles — otherwise averaging the monthly ratios happens to land on the
# recomputed one and the recompute-vs-average tests below prove nothing.
_BACK_LOADED = frozenset({
    "revenue_amount_sold_not_actualized", "revenue_amount_sold", "revenue_clients",
    "projected_amount_sold", "qualified_leads",
})

_INT_SOURCES = frozenset(m.source for m in br.CATALOG
                         if m.format == br.INT and m.source)

_Q1_MONTHS = ("2026-01", "2026-02", "2026-03")
_Q2_MONTHS = ("2026-04", "2026-05", "2026-06")

#: How far a month leans away from an even share of its period. Deliberately
#: MILD — roughly a +/-3pp tilt, the kind a real quarter has. The failure being
#: demonstrated is a ratio wrong by a couple of points that survives review, not
#: one that is obviously broken.
_SKEW = 0.028


def _weights(n: int, back: bool) -> list[float]:
    """An even split tilted by ``_SKEW``, front-loaded or back-loaded."""
    if n == 1:
        return [1.0]
    step = 2 * _SKEW / (n - 1)
    raw = [1.0 / n + _SKEW - i * step for i in range(n)]
    if back:
        raw.reverse()
    return raw


def _split(total: float, weights: list[float], *, integer: bool) -> list:
    """Spread a period total across its months, exactly. The last month absorbs
    the rounding residue so the parts always re-sum to the published total."""
    parts: list = []
    for w in weights[:-1]:
        parts.append(int(round(total * w)) if integer else round(total * w, 2))
    tail = (int(total) if integer else round(total, 2)) - sum(parts)
    parts.append(tail if integer else round(tail, 2))
    return parts


def _monthly(col: int, months: tuple[str, ...], *,
             drop: frozenset[str] = frozenset()) -> dict[str, dict[str, float]]:
    """Per-month official figures whose sums are the template's own column."""
    out: dict[str, dict[str, float]] = {mk: {} for mk in months}
    for field in sorted(br.ADDITIVE_FIELDS - drop):
        parts = _split(_PUBLISHED[field][col],
                       _weights(len(months), field in _BACK_LOADED),
                       integer=field in _INT_SOURCES)
        for mk, v in zip(months, parts):
            out[mk][field] = v
    return out


# The template's own channel table (its ``CH`` array). Client counts are not
# printed there but are exact in it — spend / CAC gives 23, 10, 6, 1 for Q1 and
# 24, 6, 14, 1 for Q2, which is what leaves 8 and 12 clients unattributed.
_CHANNEL_FIGURES = {
    #           spend Q1,  spend Q2,  revenue Q1, revenue Q2, clients Q1, Q2
    "Website": (25896.00, 25896.00, 140759.00, 203891.25, 23, 24),
    "Meta": (134258.75, 69767.61, 54319.00, 66540.84, 10, 6),
    "Google": (60401.94, 64834.46, 24017.00, 61437.50, 6, 14),
    "Email": (2144.00, 4290.00, 3882.00, 5785.00, 1, 1),
}


def _channels(col: int, months: tuple[str, ...]) -> dict:
    out: dict = {}
    for name, figures in _CHANNEL_FIGURES.items():
        spend, revenue, clients = figures[col], figures[2 + col], figures[4 + col]
        per_month: dict[str, dict[str, float]] = {mk: {} for mk in months}
        for field, total, integer in (("spend", spend, False),
                                      ("revenue_amount_sold", revenue, False),
                                      ("revenue_clients", clients, True)):
            for mk, v in zip(months, _split(total, _weights(len(months), False),
                                            integer=integer)):
                per_month[mk][field] = v
        out[name] = per_month
    return out


def _q1(**kw) -> br.PeriodRollup:
    return br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS, **kw))


def _q2(**kw) -> br.PeriodRollup:
    return br.roll_up(br.quarter_period(2026, 2), _monthly(1, _Q2_MONTHS, **kw))


def _averaged(cells: dict, months: tuple[str, ...], num: str, den: str,
              scale: float = 100.0) -> float:
    """What the WRONG implementation publishes: the mean of the monthly ratios.
    Every month reports both components here, so this is the plausible wrong
    answer, not a degenerate one."""
    per_month = [cells[mk][num] / cells[mk][den] * scale for mk in months]
    return round(sum(per_month) / len(per_month), 2)


# --- the catalog itself ------------------------------------------------------

def test_board_report_catalog_is_data_the_renderer_can_read_alone():
    """Every row carries its own label, group, format and polarity, and either a
    sheet field or a formula — never both, never neither. A renderer that has to
    know a sheet label is a renderer that breaks when the team renames one."""
    assert len(br.CATALOG) == 38
    assert br.GROUPS == (
        "Budget & Efficiency", "Projected — Actualized", "Revenue — Actualized",
        "Projected — Not Actualized", "Paying — Not Actualized",
        "Inbound Sales Pipeline", "Goal & Blended Financials")
    keys = [m.key for m in br.CATALOG]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(_PUBLISHED)
    for m in br.CATALOG:
        assert m.label and m.group in br.GROUPS
        assert m.format in br.FORMATS and m.polarity in br.POLARITIES
        assert (m.source is None) != (m.formula is None), m.key
    # Rows arrive grouped, in band order, so the renderer emits bands by walking
    # the list rather than carrying its own copy of the group order.
    order = [m.group for m in br.CATALOG]
    assert [g for i, g in enumerate(order) if i == 0 or order[i - 1] != g] == list(br.GROUPS)


def test_board_report_r_array_matches_the_template_row_shape():
    """``[label, valueA, valueB, format, polarity]`` plus ``['group', name]``
    bands — the exact shape the template's ``R[]`` renderer consumes."""
    rows = br.compare(_q1(), _q2()).to_r_array()
    assert [r[1] for r in rows if r[0] == br.GROUP] == list(br.GROUPS)
    assert len(rows) == 38 + 7
    for row in rows:
        if row[0] == br.GROUP:
            assert len(row) == 2
            continue
        label, _a, _b, fmt, pol = row
        assert isinstance(label, str) and label
        assert fmt in br.FORMATS and pol in br.POLARITIES
    assert rows[0] == [br.GROUP, "Budget & Efficiency"]
    assert rows[1] == ["Budget", 248446.0, 199796.0, "money", "neutral"]
    assert rows[-1] == ["CAC (Spend / Revenue Client)", 4991.28, 3285.96, "money", "down"]


def test_board_report_reproduces_every_published_template_figure():
    """The whole ledger, rolled up from monthly cells, against the template's own
    Q1/Q2 columns: 38 rows, 26 summed and 12 recomputed from those sums."""
    wrong = []
    for row in br.compare(_q1(), _q2()).rows:
        want = _PUBLISHED[row.key]
        if (row.a, row.b) != want:
            wrong.append(f"{row.key}: got {(row.a, row.b)}, want {want}")
    assert not wrong, "rows that do not reproduce the template:\n" + "\n".join(wrong)
    assert len([m for m in br.CATALOG if m.derived]) == 12


def test_board_report_polarity_decouples_direction_from_goodness():
    """Spend down is good, Qualified Leads down is bad, Budget is never coloured
    — the reason a row carries polarity instead of a sign rule."""
    rows = {r.key: r for r in br.compare(_q1(), _q2()).rows}
    assert rows["spend"].delta < 0 and rows["spend"].improved is True
    assert rows[br.CAC_KEY].delta < 0 and rows[br.CAC_KEY].improved is True
    assert rows["qualified_leads"].delta < 0 and rows["qualified_leads"].improved is False
    assert rows["revenue_amount_sold"].delta > 0 and rows["revenue_amount_sold"].improved is True
    # Budget and the revenue goal are inputs the team chose, not outcomes it earned.
    assert rows["budget"].delta != 0 and rows["budget"].improved is None
    assert rows["revenue_sold_goal"].improved is None


# --- landmine 1: the CAC name collision --------------------------------------

def test_board_report_cac_is_never_the_cost_per_completed_demo_proxy():
    """``CampaignMetric.cac`` is spend/demos_completed — $880.81 on Q1, and its
    own comment calls it a closed/won *proxy*. The report's CAC is spend per
    actualized revenue client: $4,991.28. Same word, 5.7x apart, and wiring the
    report's CAC card to ``totals.cac`` ships the smaller number under the bigger
    number's name. That is the 2026-08-15 wrong-numbers defect exactly."""
    q1 = _q1()
    report_cac = q1.value(br.CAC_KEY)
    proxy = q1.value("cost_per_demo_completed")
    assert report_cac == 4991.28
    assert proxy == 880.81
    assert round(report_cac / proxy, 1) == 5.7

    # The proxy is literally CampaignMetric.cac, computed on the same quarter.
    same = CampaignMetric(
        channel="Total", campaign="q1", utm_source="t", utm_medium="paid",
        utm_campaign="q1", spend=q1.components["spend"], leads=0, qualified_leads=0,
        demos_booked=0, demos_completed=int(q1.components["demos_completed"]),
        date=date(2026, 1, 1),
    )
    assert round(same.cac, 2) == proxy != report_cac

    # Structural, not conventional: the bare name is not a catalog key, no row
    # reads the sheet's own ``cac`` row, and the published CAC divides by revenue
    # clients. Any one of those three going wrong IS this defect.
    assert "cac" not in {m.key for m in br.CATALOG}
    assert br.CAC_KEY == "cac_per_revenue_client"
    assert "cac" not in br.ADDITIVE_FIELDS and "cac" in br.SHEET_RATIO_FIELDS
    cac = br.BY_KEY[br.CAC_KEY]
    assert cac.formula.numerator == "spend"
    assert cac.formula.denominator == "revenue_clients"
    assert "demos_completed" not in cac.formula.components()
    # And the label says which CAC it is, because the proxy sits nine rows above.
    assert cac.label == "CAC (Spend / Revenue Client)" and cac.basis


def test_board_report_catalog_refuses_a_cac_wired_to_demos_completed():
    """The guard is executable, not a comment: hand the validator a catalog whose
    CAC uses the proxy denominator and it refuses, so the crossing cannot ship."""
    import dataclasses

    bad = tuple(
        dataclasses.replace(m, formula=br.Ratio("spend", "demos_completed"))
        if m.key == br.CAC_KEY else m
        for m in br.CATALOG
    )
    with pytest.raises(ValueError, match="must divide by revenue_clients"):
        br._validate_catalog(bad)
    # Renaming the report's CAC back to the ambiguous bare name is refused too.
    ambiguous = tuple(
        dataclasses.replace(m, key="cac") if m.key == br.CAC_KEY else m
        for m in br.CATALOG
    )
    with pytest.raises(ValueError, match="'cac' is ambiguous"):
        br._validate_catalog(ambiguous)


def test_board_report_catalog_refuses_a_row_that_reads_a_sheet_ratio_straight():
    """A ratio read straight through would be summed by the roll-up. Refused."""
    import dataclasses

    bad = tuple(
        dataclasses.replace(m, formula=None, source="roas_pct")
        if m.key == "roas_pct" else m
        for m in br.CATALOG
    )
    with pytest.raises(ValueError, match="cannot be summed or averaged"):
        br._validate_catalog(bad)


# --- landmine 2: recompute ratios, never average them ------------------------

def test_board_report_recomputes_roas_not_actualized_instead_of_averaging():
    """Averaging the monthly ROAS figures instead of recomputing from summed
    revenue and spend is wrong in percentage POINTS, not in rounding. On the real
    months that error measured +1.74pp; on this fixture's mild month tilt it is
    +1.79pp — small enough to pass a review, which is the whole problem."""
    cells = _monthly(0, _Q1_MONTHS)
    averaged = _averaged(cells, _Q1_MONTHS, "revenue_amount_sold_not_actualized", "spend")
    recomputed = br.roll_up(br.quarter_period(2026, 1), cells).value("roas_not_actualized_pct")
    assert recomputed == 188.92            # the template's published Q1 figure
    assert averaged == 190.71              # what averaging would have published
    assert round(averaged - recomputed, 2) == 1.79
    assert br.BY_KEY["roas_not_actualized_pct"].derived


def test_board_report_recomputes_revenue_target_pct_instead_of_averaging():
    """% of goal is revenue / goal on the period's sums, and the goal MOVES
    between periods (480,000 -> 535,000), read per period and never pinned."""
    cells = _monthly(1, _Q2_MONTHS)
    averaged = _averaged(cells, _Q2_MONTHS, "revenue_amount_sold_not_actualized",
                         "revenue_sold_goal")
    roll = br.roll_up(br.quarter_period(2026, 2), cells)
    assert roll.value("revenue_target_pct") == 123.50    # published Q2
    assert averaged == 124.67                            # averaging's answer
    assert round(averaged - roll.value("revenue_target_pct"), 2) == 1.17
    assert roll.value("revenue_sold_goal") == 535000.0
    assert _q1().value("revenue_sold_goal") == 480000.0
    assert _q1().value("revenue_target_pct") == 94.29


def test_board_report_recomputes_conversion_rate_instead_of_averaging():
    """Revenue clients / demos completed (Direct), on the period's sums. The
    averaging error here is only +0.19pp — the least visible of the three and so
    the most likely to ship."""
    cells = _monthly(0, _Q1_MONTHS)
    averaged = _averaged(cells, _Q1_MONTHS, "revenue_clients", "demos_completed_direct")
    recomputed = br.roll_up(br.quarter_period(2026, 1), cells).value("conversion_rate_pct")
    assert recomputed == 19.35             # the template's published Q1 figure
    assert averaged == 19.54               # what averaging would have published
    assert round(averaged - recomputed, 2) == 0.19


def test_board_report_never_sums_or_averages_a_sheet_ratio_row():
    """The structural half of the rule: the fields the roll-up sums and the
    fields that must be recomputed are disjoint, and ``_OFFICIAL_TOTAL_FIELDS``
    in reports.py — which sums each of its fields across months — may never gain
    one of them as it widens.

    ``MONTHLY_FIELDS`` is the same surface one level down: it names the fields
    the roll-up carries per month for the by-month chart, and a ratio in it
    would be a per-month recompute that cannot reconcile with the period ratio
    printed beside it.
    """
    assert br.ADDITIVE_FIELDS & br.RECOMPUTED_FIELDS == frozenset()
    assert set(reports._OFFICIAL_TOTAL_FIELDS) & br.RECOMPUTED_FIELDS == set()
    assert set(br.MONTHLY_FIELDS) & br.RECOMPUTED_FIELDS == set()
    assert set(br.MONTHLY_FIELDS) <= br.ADDITIVE_FIELDS
    for m in br.CATALOG:
        if m.formula:
            assert not set(m.formula.components()) & br.SHEET_RATIO_FIELDS, m.key
        else:
            assert m.source not in br.SHEET_RATIO_FIELDS, m.key


# --- landmine 3: the Direct denominator --------------------------------------

def test_board_report_carries_the_direct_demo_denominator_as_its_own_row():
    """Conversion Rate is 48/248 = 19.35% and 57/212 = 26.89%, both exact to the
    template. The all-in count (272 / 238) gives 17.65% / 23.95% and is NOT the
    denominator — but it is the number printed above it, so the report publishes
    the Direct count too. Two adjacent cards a reader cannot reconcile do not
    ship."""
    q1, q2 = _q1(), _q2()
    assert (q1.value("demos_completed_direct"), q2.value("demos_completed_direct")) == (248, 212)
    assert (q1.value("demos_completed"), q2.value("demos_completed")) == (272, 238)
    assert q1.value("demos_completed_direct") != q1.value("demos_completed")

    rate = br.BY_KEY["conversion_rate_pct"]
    assert rate.formula.numerator == "revenue_clients"
    assert rate.formula.denominator == "demos_completed_direct"
    assert rate.basis == "revenue clients / demos completed (Direct)"
    for roll, want, all_in in ((q1, 19.35, 17.65), (q2, 26.89, 23.95)):
        assert roll.value("conversion_rate_pct") == want
        assert round(roll.components["revenue_clients"]
                     / roll.components["demos_completed"] * 100, 2) == all_in != want
    # The denominator is a published row of its own, labelled as such.
    direct = br.BY_KEY["demos_completed_direct"]
    assert direct.label == "Total Demos Completed (Direct)" and direct.basis


# --- missing means missing ---------------------------------------------------

# Absent in production today: the live roll-up tab spells the revenue-amount row
# differently in the Projected and Paying blocks, so occurrence 2 resolves to
# nothing and the parser withholds both rather than publish one block's figures
# under the other block's name. A parser fix is in flight; these assertions hold
# either way because the fixture states the absence itself.
_ABSENT_IN_PROD = frozenset({"revenue_amount_sold_not_actualized",
                             "paying_revenue_amount_sold"})


def test_board_report_absent_fields_stay_absent_through_rollup_and_compare():
    """Absent must survive the sum, the derivation, the comparison and the JSON
    the renderer reads. A 0 in these cells is a board slide claiming no revenue
    was sold."""
    a, b = _q1(drop=_ABSENT_IN_PROD), _q2(drop=_ABSENT_IN_PROD)
    # ...and everything derived from them, which is how roas_not_actualized_pct
    # went missing in production without anyone touching its own row.
    gone = _ABSENT_IN_PROD | {"roas_not_actualized_pct", "revenue_target_pct"}
    for roll in (a, b):
        for key in gone:
            assert key not in roll.values
            assert roll.value(key) is None
        assert "revenue_amount_sold_not_actualized" not in roll.components

    ledger = br.compare(a, b)
    rows = {r.key: r for r in ledger.rows}
    assert set(rows) == set(_PUBLISHED)        # the row still exists, unfilled
    for key in gone:
        row = rows[key]
        assert row.a is None and row.b is None
        assert row.a != 0 and row.b != 0
        assert row.delta is None and row.change_pct is None and row.improved is None
        assert row.to_ledger() == [row.label, None, None, row.format, row.polarity]
    # Absent reaches the renderer as null, never a substituted default.
    payload = json.loads(json.dumps(ledger.as_dict()))
    by_key = {r["key"]: r for r in payload["rows"]}
    for key in gone:
        assert by_key[key]["a"] is None and by_key[key]["b"] is None
    # Nothing else is collateral damage.
    assert rows["spend"].a == 239581.57 and rows["roas_pct"].a == 109.75
    assert rows["paying_services_sold"].a == 90


def test_board_report_withholds_a_total_a_month_did_not_report():
    """Two thirds of a quarter is not two thirds of a number, it is an unknown
    quarter. The total is withheld and the missing month is named — a partial sum
    published as the period is the wrong number nobody can see."""
    cells = _monthly(0, _Q1_MONTHS)
    del cells["2026-02"]["spend"]
    roll = br.roll_up(br.quarter_period(2026, 1), cells)
    assert roll.value("spend") is None
    assert "spend" not in roll.components
    # Everything computed on spend goes with it rather than using two months.
    for key in ("cost_per_lead", "cost_per_qualified_lead", "roas_pct",
                "roas_not_actualized_pct", br.CAC_KEY):
        assert roll.value(key) is None, key
    assert roll.value("leads") == 1281        # untouched fields still resolve
    assert any("'spend' is missing for 2026-02" in g for g in roll.gaps), roll.gaps


def test_board_report_a_permanently_blank_field_is_a_normal_absent_state():
    """``management_fees`` is label-correct on the sheet and has been blank since
    June 2026. It is not one of the template's rows, so the report does not
    publish it — and what is pinned here is that its absence is inert: the
    roll-up asks only for the fields the catalog uses, and an extra field in the
    sheet data changes nothing about the ledger."""
    assert "management_fees" not in br.ADDITIVE_FIELDS
    assert "management_fees" not in {m.key for m in br.CATALOG}
    cells = _monthly(0, _Q1_MONTHS)
    for mk in _Q1_MONTHS:                     # present in the sheet, unused here
        cells[mk]["management_fees"] = 12000.0
    roll = br.roll_up(br.quarter_period(2026, 1), cells)
    assert "management_fees" not in roll.components
    assert roll.values == _q1().values and not roll.gaps


# --- the by-month cells the chart draws --------------------------------------

def test_board_report_carries_three_series_per_month_and_not_the_whole_catalog():
    """The by-month chart needs figures the period totals cannot reconstruct, so
    the roll-up carries the months through uncollapsed — but only the three
    fields the chart draws.

    The bound is the point. These cells ride in every ``ReportLedger`` and
    therefore in every persisted run; a cell per month per metric would be 38 x N
    numbers stored to draw three bars.
    """
    assert br.MONTHLY_FIELDS == ("spend", "revenue_amount_sold",
                                 "revenue_amount_sold_not_actualized")
    roll = _q1()
    assert [c.month for c in roll.monthly] == list(_Q1_MONTHS)
    for cell in roll.monthly:
        assert set(cell.values) == set(br.MONTHLY_FIELDS), cell.month
        # Not a slice of the whole roll-up: the other 30-odd additive fields are
        # summed into the period and never carried per month.
        assert "leads" not in cell.values and "revenue_clients" not in cell.values

    cells = _monthly(0, _Q1_MONTHS)
    for field in br.MONTHLY_FIELDS:
        drawn = [c.value(field) for c in roll.monthly]
        assert drawn == [cells[mk][field] for mk in _Q1_MONTHS], field
        # The bars re-sum to the column the ledger prints, or the chart and the
        # table beside it are telling a reader two different stories.
        assert round(sum(drawn), 2) == pytest.approx(roll.components[field], abs=0.02)

    ledger = br.compare(_q1(), _q2())
    assert [c.month for c in ledger.monthly[0]] == list(_Q1_MONTHS)
    assert [c.month for c in ledger.monthly[1]] == list(_Q2_MONTHS)
    # Two tuples, not one merged list: a single-period ledger is the same period
    # twice, and merging here would draw every month of it twice.
    same = br.compare(_q1(), _q1())
    assert same.monthly[0] == same.monthly[1]

    payload = json.loads(json.dumps(ledger.as_dict()))
    assert payload["monthly"][0][0] == {
        "month": "2026-01", "values": {f: cells["2026-01"][f] for f in br.MONTHLY_FIELDS}}


def test_board_report_a_month_that_did_not_report_is_absent_not_zero():
    """The absent-vs-zero rule, one level down from the period totals.

    A field a month did not report is simply not a key in that month's cell, and
    the month still gets a cell. Both halves matter: a 0 would draw a bar
    claiming nothing was spent, and dropping the month would shorten the chart's
    axis so a two-thirds-covered quarter reads as a complete two-month period.
    """
    cells = _monthly(0, _Q1_MONTHS)
    del cells["2026-02"]["revenue_amount_sold"]
    roll = br.roll_up(br.quarter_period(2026, 1), cells)

    assert [c.month for c in roll.monthly] == list(_Q1_MONTHS)   # no month dropped
    feb = roll.monthly[1]
    assert "revenue_amount_sold" not in feb.values
    assert feb.value("revenue_amount_sold") is None
    assert feb.value("revenue_amount_sold") != 0
    assert feb.value("spend") == cells["2026-02"]["spend"]       # not collateral

    # The period total is withheld while the two months that DID report still
    # carry their cells — that pair is what keeps a partial period visibly
    # partial instead of publishing a two-month sum as the quarter.
    assert roll.value("revenue_amount_sold") is None
    assert roll.value("roas_pct") is None
    assert roll.monthly[0].value("revenue_amount_sold") is not None
    assert roll.monthly[2].value("revenue_amount_sold") is not None
    assert any("\'revenue_amount_sold\' is missing for 2026-02" in g for g in roll.gaps)

    # A month the sheet has nothing for at all still holds its slot, empty.
    blank = br.roll_up(br.quarter_period(2026, 1), {mk: cells[mk] for mk in _Q1_MONTHS[:2]})
    assert [c.month for c in blank.monthly] == list(_Q1_MONTHS)
    assert blank.monthly[2].values == {}


def test_board_report_refuses_a_monthly_field_that_cannot_be_summed():
    """The guard that keeps the list three summable fields wide.

    Proven by handing the validator a broken list rather than by editing module
    globals — the same shape as the catalog guards above.
    """
    br._validate_monthly()          # the shipping list, at import and here

    with pytest.raises(ValueError, match="recomputed from summed components"):
        br._validate_monthly(("roas_pct",), br.ADDITIVE_FIELDS, br.RECOMPUTED_FIELDS)
    with pytest.raises(ValueError, match="recomputed from summed components"):
        br._validate_monthly(("cost_per_lead",), br.ADDITIVE_FIELDS, br.RECOMPUTED_FIELDS)
    with pytest.raises(ValueError, match="not a field the roll-up sums"):
        br._validate_monthly(("management_fees",), br.ADDITIVE_FIELDS, br.RECOMPUTED_FIELDS)
    with pytest.raises(ValueError, match="duplicate field"):
        br._validate_monthly(("spend", "spend"), br.ADDITIVE_FIELDS, br.RECOMPUTED_FIELDS)


# --- the untracked / Other reconciliation row --------------------------------

def test_board_report_untracked_row_reconciles_channels_to_all_sources():
    """Tracked channels do not sum to the All-Sources totals, and the gap is
    growing: Q1 leaves $16,881 (7.0%) and 8 clients unattributed, Q2 $22,512
    (12.0%) and 12. The user's decision is that this is a row, not a footnote —
    a reader who cannot see it infers the channel table IS the total."""
    q1 = br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS),
                    channels=_channels(0, _Q1_MONTHS))
    q2 = br.roll_up(br.quarter_period(2026, 2), _monthly(1, _Q2_MONTHS),
                    channels=_channels(1, _Q2_MONTHS))

    assert q1.untracked.spend == 16880.88
    assert round(q1.untracked.spend) == 16881
    assert round(q1.untracked.spend_pct, 1) == 7.0
    assert q1.untracked.clients == 8
    assert q1.untracked.revenue == 39970.70

    assert q2.untracked.spend == 22511.72
    assert round(q2.untracked.spend) == 22512
    assert round(q2.untracked.spend_pct, 1) == 12.0
    assert q2.untracked.clients == 12
    assert q2.untracked.revenue == 63959.50

    # 7.0% -> 12.0% is the finding. The row is what makes it legible.
    assert q2.untracked.spend_pct > q1.untracked.spend_pct
    assert not q1.gaps and not q2.gaps

    ledger = br.compare(q1, q2)
    assert [c.channel for c in ledger.channels] == \
        ["Email", "Google", "Meta", "Website", "Other / untracked"]
    untracked = ledger.channels[-1]
    assert untracked.channel == br.UNTRACKED_LABEL
    assert untracked.as_dict()["spend_a"] == 16880.88
    assert untracked.as_dict()["spend_b"] == 22511.72


def test_board_report_channel_cac_uses_the_report_definition_not_the_proxy():
    """The channel table's CAC column is spend per revenue client — the same
    definition as the ledger's CAC row, so the two tables reconcile."""
    q1 = br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS),
                    channels=_channels(0, _Q1_MONTHS))
    by_channel = {c.channel: c for c in q1.channels}
    assert by_channel["Website"].cac == 1125.91      # the template's own figures
    assert by_channel["Meta"].cac == 13425.88
    assert by_channel["Website"].roas_pct == 543.55
    blended = q1.value(br.CAC_KEY)
    assert blended == round(q1.components["spend"] / q1.components["revenue_clients"], 2)
    assert blended == 4991.28


def test_board_report_untracked_names_a_double_count_instead_of_hiding_it():
    """Channels summing ABOVE the All-Sources total is not an untracked surplus,
    it is a block counted twice — the 2026-08-15 shape, where one block pasted
    into seven vendor tabs was counted eight times. The figure is still published
    (a silent drop is how that ran unseen) and the cause is named."""
    channels = _channels(0, _Q1_MONTHS)
    channels["Meta-duplicate"] = {mk: dict(v) for mk, v in channels["Meta"].items()}
    roll = br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS),
                      channels=channels)
    assert roll.untracked.spend < 0
    assert any("counted twice" in g and "spend" in g for g in roll.gaps), roll.gaps


def test_board_report_untracked_is_absent_when_a_channel_month_is_absent():
    """No channel data, or incomplete channel data, means no reconciliation for
    that measure — never a reconciliation against a partial channel sum."""
    assert br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS)).untracked is None
    channels = _channels(0, _Q1_MONTHS)
    del channels["Meta"]["2026-02"]["spend"]
    roll = br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS), channels=channels)
    assert roll.untracked.spend is None and roll.untracked.spend_pct is None
    assert roll.untracked.clients == 8       # the measures that ARE complete stand


# --- two periods, and nothing quarter-shaped ---------------------------------

def test_board_report_compares_any_two_periods_not_just_quarters():
    """``q1c`` / ``q2c`` in the template are only "column A" and "column B", so
    month-vs-month, year-vs-year and a fiscal window that straddles a year all
    fall out of the same structure."""
    jan = br.roll_up(br.month_period(2026, 1), _monthly(0, ("2026-01",)))
    feb = br.roll_up(br.month_period(2026, 2), _monthly(1, ("2026-02",)))
    m = br.compare(jan, feb)
    assert m.columns == ("2026-01", "2026-02")
    assert len(m.rows) == 38
    assert {r.key: (r.a, r.b) for r in m.rows}["spend"] == (239581.57, 187299.79)

    y2025, y2026 = br.year_period(2025), br.year_period(2026)
    assert len(y2025.months) == 12 and y2025.months[0] == "2025-01"
    yy = br.compare(br.roll_up(y2025, _monthly(0, y2025.months)),
                    br.roll_up(y2026, _monthly(1, y2026.months)))
    assert yy.columns == ("2025", "2026")
    assert {r.key: r.a for r in yy.rows}["conversion_rate_pct"] == 19.35

    # A period is a list of months; nothing in the roll-up counts to three.
    fiscal = br.PeriodSpec("FY-Q4", "FY Q4", ("2025-11", "2025-12", "2026-01", "2026-02"))
    assert br.roll_up(fiscal, _monthly(0, fiscal.months)).value("spend") == 239581.57
    assert br.quarter_period(2026, 1).months == ("2026-01", "2026-02", "2026-03")
    assert br.months_of(2025, 11, 4) == ("2025-11", "2025-12", "2026-01", "2026-02")


def test_board_report_period_spec_refuses_a_nonsense_window():
    with pytest.raises(ValueError, match="no months"):
        br.PeriodSpec("empty", "Empty", ())
    with pytest.raises(ValueError, match="repeats a month"):
        br.PeriodSpec("dup", "Dup", ("2026-01", "2026-01"))
    with pytest.raises(ValueError, match="quarter must be 1-4"):
        br.quarter_period(2026, 5)


# --- idempotency -------------------------------------------------------------

def test_board_report_cache_key_covers_periods_capture_date_and_version():
    """Keyed by period spec + capture date + generator version, so a repeated
    request does not re-derive and a generator bump is a cache invalidation
    rather than a stale read. Persistence itself lands in step 3."""
    periods = [br.quarter_period(2026, 1), br.quarter_period(2026, 2)]
    day = date(2026, 9, 4)
    key = br.cache_key(periods, day)
    assert len(key) == 64 and key == br.cache_key(periods, day)
    assert key != br.cache_key(periods, date(2026, 9, 5))
    # A sentinel that can never BE the shipping version. Writing the next
    # version number here instead makes this line pass for the wrong reason the
    # day someone bumps to it, and the by-month change did exactly that bump.
    assert br.GENERATOR_VERSION != "mr-board-report/never"
    assert key != br.cache_key(periods, day, version="mr-board-report/never")
    assert key != br.cache_key(list(reversed(periods)), day)
    assert key != br.cache_key([br.quarter_period(2026, 1), br.quarter_period(2026, 3)], day)
    ledger = br.compare(_q1(), _q2(), captured_on=day)
    assert ledger.cache_key == key
    assert br.compare(_q1(), _q2()).cache_key == ""   # opt-in, never invented
    assert ledger.as_dict()["generator"] == br.GENERATOR_VERSION


# --- the guards this module is allowed to rely on ----------------------------

def test_board_report_runs_offline_and_reaches_no_store():
    """Verified from inside pytest rather than assumed — the offline guard has
    been proven leaky twice. board_report reaches no network and no datastore
    because it imports none, checked structurally so an import added later fails
    here instead of in a cron fire.

    The snapshots exclusion is a scoping rule, not a style one: its routes are
    WORKSPACE_SHARED with an equality-asserted baseline, and importing it would
    drag that into the TENANT_SCOPED report path."""
    assert os.environ.get("MR_OFFLINE") == "1"
    from app.services import firestore_repo

    with pytest.raises(RuntimeError, match="Firestore access blocked in tests"):
        firestore_repo._db()

    tree = ast.parse(pathlib.Path(br.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add("." * node.level + node.module.split(".")[0])
    assert imported == {"__future__", "hashlib", "json", "dataclasses", "datetime", "typing"}


# =============================================================================
# Board report — wired to the parser, the run store and the kill switch
# =============================================================================
# The section above proves the catalog and the roll-up in isolation, on
# hand-built dicts. This one proves the JOIN, which is where step 3's risk
# actually lives:
#
#   sheets_source.parse_official_totals -> _load_dataset()["official_totals"]
#     -> board_report.roll_up -> compare -> a run in mr_runs, keyed on
#        cache_key() so the second identical request re-reads instead of
#        re-deriving.
#
# Both halves were green and unconnected: board_report.py had no importer at
# all, and the field NAMES the two sides use had never been compared. A rename
# on either side would surface as a metric that is quietly, permanently absent —
# which on a board slide is indistinguishable from a month the sheet did not
# report.

_BOARD_CAPTURE = "2026-07-01T09:00:00+00:00"


def _board_dataset(*, months=None, **kw) -> dict:
    """A dataset in the exact shape ``_load_dataset`` hands the builder.

    ``official_totals`` is the parser's own output shape and is built from
    ``_monthly`` above, so these figures are still the template's published
    column totals and cannot drift from them.
    """
    cells = {**_monthly(0, _Q1_MONTHS, **kw), **_monthly(1, _Q2_MONTHS, **kw)}
    if months is not None:
        cells = {k: v for k, v in cells.items() if k in months}
    return {
        "official_totals": cells,
        "official_captured_at": _BOARD_CAPTURE,
        "sources": [{"platform": "sheets:Marketing 2026 Overall Report"}],
    }


def _rollup_grid(cells: dict) -> list[list[str]]:
    """A miniature roll-up tab carrying ``cells``, laid out the way the live one
    is: a title/month header, the plain rows, then the two repeated blocks each
    behind its own anchor header.

    Rows are labelled from ``sheets_source._OFFICIAL_FIELD_LABELS`` rather than
    hand-typed, so this stays a test of the JOIN — catalog key to sheet field to
    sheet row to period total — and never becomes a second copy of the parser's
    label fixtures, which live in test_sheets_source.py and are that module's to
    maintain.
    """
    from marketing_research_agent.sources import sheets_source as ss

    months = sorted({int(mk.split("-")[1]) for mk in cells})
    header = ["All"]
    col_of: dict[int, int] = {}
    for mo in months:
        # The three-letter form, because that is what ``sheets_source._MONTHS``
        # knows — a header reading "January (Performance)" scans as no month at
        # all and the whole grid parses empty.
        name = ss._MONTH_NAMES[mo][:3]
        col_of[mo] = len(header)
        header += [f"{name} (Performance)", f"{name} (Investment)"]

    def row(label: str, field: str | None) -> list[str]:
        out = [label] + [""] * (len(header) - 1)
        if field is not None:
            for mk, fields in cells.items():
                if field in fields:
                    out[col_of[int(mk.split("-")[1])]] = str(fields[field])
        return out

    fields = sorted(br.ADDITIVE_FIELDS)
    anchors = (ss._ANCHOR_PROJECTED_NA, ss._ANCHOR_PAYING_NA)
    plain, anchored = [], {a: [] for a in anchors}
    for f in fields:
        labels, where = ss._OFFICIAL_FIELD_LABELS[f]
        (anchored[where] if isinstance(where, str) else plain).append((labels[0], f))

    rows = [header]
    rows += [row(lab, f) for lab, f in plain if lab not in anchors]
    for anchor in anchors:
        # The anchor row is itself a published field, and it opens its window.
        own = next((f for lab, f in plain if lab == anchor), None)
        rows.append(row(anchor, own))
        rows += [row(lab, f) for lab, f in anchored[anchor]]
    rows.append(row(ss._ANCHOR_INBOUND, None))  # boundary only, no field
    return rows


# --- the join between the two halves -----------------------------------------

def test_board_report_reads_only_fields_the_roll_up_parser_can_produce():
    """Every field the catalog names is a field the parser knows how to fill.

    The cheapest possible guard on the seam, and the one nothing had: the two
    modules were written against the same sheet but never against each other. A
    catalog row pointing at a field the parser does not emit is not an error
    anywhere — it is a permanently blank row that looks like missing data.
    """
    from marketing_research_agent.sources import sheets_source as ss

    unreachable = sorted(br.ADDITIVE_FIELDS - set(ss._OFFICIAL_FIELD_LABELS))
    assert not unreachable, (
        "these board-report fields have no row in the roll-up parser, so the "
        f"metrics reading them can never fill: {unreachable}")


def test_board_report_fills_the_whole_catalog_from_a_parsed_roll_up_grid():
    """End to end on the real parser: grid -> parse_official_totals -> roll_up.

    Not a re-run of the fixture tests above. Those hand ``roll_up`` a dict; this
    hands the PARSER a sheet and checks the catalog comes out the other side
    with the template's own Q1 figures, which is the only way a label the parser
    resolves differently than the catalog expects shows up as a failure.
    """
    from marketing_research_agent.sources import sheets_source as ss

    cells = _monthly(0, _Q1_MONTHS)
    official = ss.parse_official_totals(_rollup_grid(cells), 2026)
    assert set(official) == set(_Q1_MONTHS), "the grid did not parse as three months"

    rolled = br.roll_up(br.quarter_period(2026, 1), official)
    absent = [m.key for m in br.CATALOG if rolled.value(m.key) is None]
    assert not absent, f"parsed from a real grid, these rows still came back absent: {absent}"
    for key, (q1, _q2) in _PUBLISHED.items():
        assert rolled.value(key) == pytest.approx(q1, abs=0.02), key


def test_board_report_absent_stays_absent_through_the_parser_too():
    """A row the sheet does not carry arrives as absent, not as 0.00.

    The same rule the roll-up already keeps, asserted one layer down: the row is
    removed from the GRID, so the parser never emits the field at all. This is
    the production case — the capture in the store on 2026-09-05 carries 8 of
    the 43 fields, because it was pulled before the parser learned the rest.
    """
    from marketing_research_agent.sources import sheets_source as ss

    cells = _monthly(0, _Q1_MONTHS)
    grid = [r for r in _rollup_grid(cells)
            if r[0] != ss._OFFICIAL_FIELD_LABELS["revenue_clients"][0][0]]
    rolled = br.roll_up(br.quarter_period(2026, 1),
                        ss.parse_official_totals(grid, 2026))
    assert rolled.value("revenue_clients") is None
    assert br.CAC_KEY not in rolled.values, "CAC published without its denominator"
    assert rolled.value("conversion_rate_pct") is None
    assert rolled.value("spend") == pytest.approx(_PUBLISHED["spend"][0], abs=0.02)


# --- the rail: kinds, refusal, persistence -----------------------------------

def test_board_kinds_are_report_kinds_but_are_not_narrated():
    for kind in reports.BOARD_KINDS:
        assert kind in reports.KINDS, f"{kind} must list and read back like any run"
        assert kind not in reports.NARRATED_KINDS
    assert set(reports.KINDS) == set(reports.NARRATED_KINDS) | set(reports.BOARD_KINDS)


def test_build_refuses_a_board_kind_and_names_the_builder_that_takes_it(
        monkeypatch, tmp_path):
    """The wrong door is locked, and it says where the right one is.

    ``build`` would otherwise fall through to its non-campaign branch and emit a
    report with an empty structured block and an LLM paragraph about it.
    """
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="build_board_report"):
        reports.build("board_report", _dataset(), user_id="u1")


def test_board_report_is_persisted_as_a_run_the_same_way_every_kind_is(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build_board_report(_board_dataset(), user_id="u1", period="2026-Q1")

    assert r["kind"] == "board_report"
    assert r["user_id"] == "u1" and r["agent_id"] == "a6"
    assert r["reused"] is False
    # The provenance pair, and it is a design statement rather than a failure.
    assert r["ai"] is False and r["fallback_reason"]
    stored = reports.runs.get_run(r["id"])
    assert stored and stored["user_id"] == "u1"
    assert stored["structured"]["cache_key"] == r["structured"]["cache_key"]

    values = {row["key"]: row["value"] for row in r["structured"]["rows"]}
    assert len(values) == len(br.CATALOG)
    for key, (q1, _q2) in _PUBLISHED.items():
        assert values[key] == pytest.approx(q1, abs=0.02), key


def test_board_comparison_carries_the_ledger_and_the_renderer_array(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build_board_report(_board_dataset(), user_id="u1",
                                   period="2026-Q1", compare_to="2026-Q2")
    s = r["structured"]
    assert r["kind"] == "board_report_comparison"
    assert s["columns"] == ["Q1", "Q2"]
    rows = {row["key"]: (row["a"], row["b"]) for row in s["rows"]}
    for key, published in _PUBLISHED.items():
        assert rows[key] == pytest.approx(published, abs=0.02), key
    # The template's own R[] shape: a band row per group, then its metric rows.
    bands = [row for row in s["r_array"] if row[0] == br.GROUP]
    assert [b[1] for b in bands] == list(br.GROUPS)
    assert len(s["r_array"]) == len(br.CATALOG) + len(br.GROUPS)


def test_two_periods_of_different_shapes_compare(monkeypatch, tmp_path):
    """Nothing on this path is quarter-shaped — a month against a month is the
    same code, which is what keeps the columns "A" and "B" rather than Q1/Q2."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build_board_report(_board_dataset(), user_id="u1",
                                   period="2026-01", compare_to="2026-04")
    assert r["structured"]["columns"] == ["2026-01", "2026-04"]


# --- idempotency --------------------------------------------------------------

def test_the_same_request_is_served_from_the_store_not_re_derived(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = _board_dataset()
    first = reports.build_board_report(ds, user_id="u1", period="2026-Q1",
                                       compare_to="2026-Q2")
    second = reports.build_board_report(ds, user_id="u1", period="2026-Q1",
                                        compare_to="2026-Q2")
    assert first["reused"] is False and second["reused"] is True
    assert second["id"] == first["id"], "a second run was written for the same key"
    assert len(reports.runs.list_runs("u1", kind="board_report_comparison")) == 1


def test_a_fresh_capture_re_derives_instead_of_serving_the_old_roll_up(
        monkeypatch, tmp_path):
    """The failure this key exists to prevent: the sheet is re-pulled, the
    numbers move, and the report keeps answering with yesterday's."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    first = reports.build_board_report(_board_dataset(), user_id="u1", period="2026-Q1")
    later = reports.build_board_report(
        {**_board_dataset(), "official_captured_at": "2026-07-02T09:00:00+00:00"},
        user_id="u1", period="2026-Q1")
    assert later["reused"] is False
    assert later["id"] != first["id"]
    assert later["structured"]["cache_key"] != first["structured"]["cache_key"]


def test_a_generator_bump_is_a_cache_invalidation_not_a_stale_read(
        monkeypatch, tmp_path):
    """A bump has to change the stored key, or the next request serves numbers
    the new generator would not produce.

    Asserted through ``cache_key`` rather than by monkeypatching
    ``GENERATOR_VERSION``: the constant is a DEFAULT ARGUMENT of that function,
    bound once at import, so patching the module attribute moves nothing. In
    production the bump is a source edit and the default rebinds — but the test
    that patches the attribute would pass for the wrong reason, and then keep
    passing if the version ever dropped out of the key entirely.
    """
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build_board_report(_board_dataset(), user_id="u1", period="2026-Q1")
    s = r["structured"]
    assert s["generator"] == br.GENERATOR_VERSION, "the store does not record the generator"

    spec = reports.board_period("2026-Q1")
    captured = date.fromisoformat(s["captured_on"])
    assert br.cache_key([spec], captured) == s["cache_key"], "the key is not reproducible"
    assert br.cache_key([spec], captured, version="mr-board-report/next") != s["cache_key"]


def test_one_workspaces_board_run_is_never_served_to_another(monkeypatch, tmp_path):
    """The cache key deliberately does not carry the workspace — the LOOKUP is
    scoped instead. Two tenants asking for the same quarter of the same capture
    therefore hash identically, which is exactly the case worth pinning."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = _board_dataset()
    mine = reports.build_board_report(ds, user_id="tenant-a", period="2026-Q1")
    theirs = reports.build_board_report(ds, user_id="tenant-b", period="2026-Q1")
    assert theirs["reused"] is False, "another tenant's run was served as a cache hit"
    assert theirs["id"] != mine["id"]
    assert mine["structured"]["cache_key"] == theirs["structured"]["cache_key"]
    assert [r["id"] for r in reports.runs.list_runs("tenant-a", kind="board_report")] \
        == [mine["id"]]


# --- honest degradation -------------------------------------------------------

def test_coverage_names_every_absent_metric_and_why(monkeypatch, tmp_path):
    """The block that keeps a thin CAPTURE from reading as a thin quarter.

    Production's stored capture on 2026-09-05 carries 8 fields, so 25 of the 38
    rows are absent; the run has to say which and why, or the report looks like
    a quarter where nothing sold.
    """
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    thin = _board_dataset(drop=frozenset({"revenue_clients", "revenue_amount_sold"}))
    r = reports.build_board_report(thin, user_id="u1", period="2026-Q1")
    col = r["structured"]["coverage"]["columns"][0]

    assert col["metric_count"] == len(br.CATALOG)
    assert col["filled_count"] == len(col["filled"])
    assert col["filled_count"] + len(col["absent"]) == len(br.CATALOG)
    # Both the rows that read the dropped fields and the ratios built on them.
    for key in ("revenue_clients", "revenue_amount_sold", "roas_pct",
                "conversion_rate_pct", br.CAC_KEY):
        assert key in col["absent"], key
        assert col["absent_reasons"][key], f"{key} absent with no stated cause"
    assert "revenue_clients" in col["absent_reasons"][br.CAC_KEY]
    values = {row["key"]: row["value"] for row in r["structured"]["rows"]}
    assert values["revenue_clients"] is None, "an absent metric came back as a number"
    assert values["spend"] == pytest.approx(_PUBLISHED["spend"][0], abs=0.02)


def test_the_untracked_row_is_absent_rather_than_zero_with_no_channel_feed(
        monkeypatch, tmp_path):
    """a6 has no per-channel revenue/client source this path can read, so the
    channel table and the "Other / untracked" row are ABSENT.

    Zeroing them would publish "0% untracked" — a reconciliation nobody
    computed — and the one channel block that does exist would publish "100%
    untracked" against a documented 7%. Both are the plausible wrong number.
    """
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build_board_report(_board_dataset(), user_id="u1",
                                   period="2026-Q1", compare_to="2026-Q2")
    s = r["structured"]
    assert s["channels"] == []
    assert s["untracked"] == [None, None]
    status = s["coverage"]["channel_reconciliation"]
    assert status and status.startswith("absent"), status
    assert "0" != status


def test_a_period_a_month_is_missing_withholds_the_total_and_names_the_month(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    partial = _board_dataset(months={"2026-01", "2026-02"})
    r = reports.build_board_report(partial, user_id="u1", period="2026-Q1")
    values = {row["key"]: row["value"] for row in r["structured"]["rows"]}
    assert values["spend"] is None, "two thirds of a quarter published as the quarter"
    assert any("2026-03" in g for g in r["structured"]["gaps"]), r["structured"]["gaps"]


def test_no_capture_at_all_is_an_error_not_a_report_full_of_zeros(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    with pytest.raises(reports.PeriodError, match="sheet pull"):
        reports.build_board_report({"official_totals": {}}, user_id="u1",
                                   period="2026-Q1")


# --- period vocabulary --------------------------------------------------------

@pytest.mark.parametrize(("text", "key", "n_months"), [
    ("2026-07", "2026-07", 1),
    ("2026-Q2", "2026-Q2", 3),
    ("2026-q2", "2026-Q2", 3),
    ("2026", "2026", 12),
])
def test_board_period_accepts_month_quarter_and_year(text, key, n_months):
    spec = reports.board_period(text)
    assert spec.key == key and len(spec.months) == n_months


@pytest.mark.parametrize("text", ["", "2026-13", "2026-Q5", "Q2", "last quarter",
                                  "2026-7", "26-07", "2026-00"])
def test_board_period_refuses_anything_it_cannot_read(text):
    with pytest.raises(reports.PeriodError):
        reports.board_period(text)


def test_a_comparison_needs_two_different_periods(monkeypatch, tmp_path):
    """A period against itself renders a delta column of zeros, which is a
    claim — "nothing moved" — that nobody made."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    with pytest.raises(reports.PeriodError, match="two different periods"):
        reports.build_board_report(_board_dataset(), user_id="u1",
                                   period="2026-Q1", compare_to="2026-Q1")


# --- the kill switch ----------------------------------------------------------

def test_the_board_report_is_off_unless_a_deployment_turns_it_on(monkeypatch):
    """Default OFF. The inverse of MR_MULTI_SHEET, which defaults on."""
    monkeypatch.delenv("MR_BOARD_REPORT", raising=False)
    assert reports.board_report_enabled() is False
    for off in ("0", "false", "off", "", "no", "maybe"):
        monkeypatch.setenv("MR_BOARD_REPORT", off)
        assert reports.board_report_enabled() is False, off
    for on in ("1", "true", "TRUE", "on"):
        monkeypatch.setenv("MR_BOARD_REPORT", on)
        assert reports.board_report_enabled() is True, on


# =============================================================================
# Board report renderer — the ledger as one self-contained file
# (board_report_render.py)
# =============================================================================
# The contract is the same three marketing templates, minus their defects. Each
# test below pins one, because every one of them shipped a page that looked
# entirely finished:
#
#   1. Chart.js from a CDN and three families from Google Fonts, so behind a
#      corporate proxy the emailed file rendered as unstyled text with four
#      blank canvases.
#   2. "-21.8%" on Spend carrying a green UP arrow, because one CSS class
#      encoded both "favourable" and "rising".
#   3. Website's 543%/787% ROAS flattening Meta and Google to slivers on a
#      linear axis — the three channels the reallocation decision is about.
#   4. print-color-adjust absent, so a PDF of a design made of background
#      colours prints blank; and break-inside:avoid on the table WRAPPER, so a
#      45-row table gets clipped at one page instead of paginating.
#   5. A single-period insight card in green carrying "thin 110% ROAS" and
#      "CAC high at $4,991".
#
# The renderer is a pure function, so every test here is: build a ledger, render
# it, read the bytes. No fixture files and no golden HTML — a golden file makes
# every layout change a diff review instead of an assertion.

import re
from html.parser import HTMLParser

from marketing_research_agent import board_report_render as brr

#: Tags that never close. SVG shapes are emitted self-closing, so the balance
#: check must not expect an end tag for them either.
_VOID_TAGS = frozenset({
    "meta", "br", "hr", "img", "input", "link", "source", "area", "base", "col",
    "embed", "param", "track", "wbr",
    "rect", "line", "path", "circle", "use", "stop", "polyline", "polygon",
})


class _TagBalance(HTMLParser):
    """A PDF pass gets one shot at the markup; an unclosed ``<td>`` silently
    swallows a section."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.bad: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.bad.append((tag, list(self.stack[-3:])))
            return
        self.stack.pop()


def _q1_ch() -> br.PeriodRollup:
    return br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS),
                      channels=_channels(0, _Q1_MONTHS))


def _q2_ch() -> br.PeriodRollup:
    return br.roll_up(br.quarter_period(2026, 2), _monthly(1, _Q2_MONTHS),
                      channels=_channels(1, _Q2_MONTHS))


def _comparison(**kw) -> str:
    return brr.render(br.compare(_q1_ch(), _q2_ch()), brand="Legal Soft", **kw)


def _standalone(**kw) -> str:
    return brr.render(brr.single_period(_q1_ch()), brand="Legal Soft", **kw)


def _between(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def _rows_of(table: str) -> list[str]:
    """The metric rows of a rendered table, in print order, group bands out."""
    return [f for f in table.split("<tr")
            if "<td" in f and 'class="group"' not in f]


def _tr_with(page: str, label: str) -> str:
    for frag in page.split("<tr"):
        if ">" + label + "<" in frag:
            return frag[:frag.index("</tr>") + 5] if "</tr>" in frag else frag
    raise AssertionError(f"no row labelled {label!r}")


def _svg_named(page: str, title: str) -> str:
    i = page.index("<title>" + title + "</title>")
    return page[page.rindex("<svg", 0, i):page.index("</svg>", i) + 6]


def _kickers(page: str) -> list[str]:
    return re.findall(r'<span class="num">(\d+)</span>', page)


# --- self-contained, which is the whole point --------------------------------

def test_board_report_render_reaches_the_network_nowhere_at_all():
    """The marketing team emails these files to clients. The templates pulled
    Chart.js from cdnjs and three families from Google Fonts, so behind a
    corporate proxy the whole visual identity collapsed. Proven mechanically
    rather than by reading the source: one ``<link>`` added later is invisible
    in review and total in effect."""
    for page in (_comparison(), _standalone()):
        for token in ("http://", "https://", "<script", "<link", "@import",
                      "<iframe", "<img", "<object", "<embed", "srcset", "url("):
            assert token not in page, token
        assert page.startswith('<!DOCTYPE html><html lang="en">')
        assert page.rstrip().endswith("</html>")
        # Charts are markup, not a library.
        assert page.count('<svg class="chart"') >= 3
        assert "<canvas" not in page

        parser = _TagBalance()
        parser.feed(page)
        assert not parser.bad and not parser.stack, (parser.bad, parser.stack)


def test_board_report_render_is_pure_and_imports_nothing_that_could_do_io():
    """No clock, no store, no network — the same ledger renders the same bytes,
    which is what lets step 3 cache the document instead of re-deriving it.
    Checked structurally so an import added later fails here, not in a cron
    fire."""
    assert os.environ.get("MR_OFFLINE") == "1"
    from app.services import firestore_repo

    with pytest.raises(RuntimeError, match="Firestore access blocked in tests"):
        firestore_repo._db()

    source = pathlib.Path(brr.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add("." * node.level + node.module.split(".")[0])
    assert imported == {"__future__", "html", "math", "dataclasses", "datetime",
                        "typing", ".board_report"}
    assert "today()" not in source        # a date is passed in, never read

    assert _comparison() == _comparison()
    assert _standalone() == _standalone()


# --- one shell, two compositions ---------------------------------------------

def test_board_report_render_section_list_is_chosen_by_period_count():
    """Q1 and Q2 standalone are structurally identical and differ only in
    numbers; the comparison is a different section list off the same shell. The
    switch is the period count and nothing else."""
    assert brr.is_single_period(brr.single_period(_q1_ch())) is True
    assert brr.is_single_period(br.compare(_q1_ch(), _q2_ch())) is False

    two = _comparison()
    assert _kickers(two) == ["00", "01", "02", "03", "04"]
    assert "At a glance" in two and "Biggest movers" in two
    assert "Full comparison ledger" in two and "Channel shift" in two

    one = _standalone()
    assert _kickers(one) == ["01", "02", "03"]
    # The cover names the window a client can read, and the year once.
    assert "<b>Jan – Mar 2026</b>" in one
    fiscal = br.PeriodSpec("f", "F", ("2025-11", "2026-02"))
    assert brr._months_label(fiscal) == "Nov 2025 \u2013 Feb 2026"
    assert brr._months_label(br.PeriodSpec("w", "W", ("wk-01",))) == "wk-01"
    assert "At a glance" not in one and "Biggest movers" not in one

    # Exactly one highlighted card, in the report that has cards.
    assert one.count('class="card hl"') == 1
    assert one.count('<div class="card') == 20
    assert two.count('<div class="card') == 0

    # Same head, same palette, same component classes across compositions.
    assert two[two.index("<style>"):two.index("</style>")] == \
        one[one.index("<style>"):one.index("</style>")]


def test_board_report_render_prints_every_catalog_row_in_catalog_order():
    """38 rows under seven bands, labelled and ordered by the catalog and never
    by a copy kept in the renderer — including ``Cost / Qualified Demo
    Completed``, which the template mislabels and the catalog names against its
    real denominator (239,581.57 / 880.81 = 272, the all-in count)."""
    page = _comparison()
    table = _between(page, "Full comparison ledger", "</section>")
    for group in br.GROUPS:
        band = group.replace("&", "&amp;")
        assert '<tr class="group"><td colspan="5">' + band + "</td></tr>" in table
    assert table.count('<tr class="group">') == 7

    rows = _rows_of(table)
    assert len(rows) == 38
    for metric, frag in zip(br.CATALOG, rows):
        assert ">" + metric.label + "<" in frag, metric.key
        a, b = _PUBLISHED[metric.key]
        for value in (a, b):
            printed = ("$" + format(round(value), ",") if metric.format == br.MONEY
                       else format(value, ".2f") + "%" if metric.format == br.PCT
                       else format(round(value), ","))
            assert printed in frag, (metric.key, printed)
    assert "Cost / Qualified Demo Completed (SDR+VAPI+Direct)" in table
    # The printf escape the templates never unescaped: 544%%, 40%%, 95%%.
    assert "%%" not in page


# --- absent is not zero ------------------------------------------------------

def test_board_report_render_absent_is_an_em_dash_and_a_named_gap_never_zero():
    """Two metrics are absent in production right now, and two more derive from
    them. A blank cell reads as zero and a ``$0`` states one, so absence gets
    its own mark AND its own entry in the basis panel — while a genuine zero
    still prints as a zero, or the distinction is decorative."""
    zeroed = "lost_dnc_bad_lead"          # a real 0 in column B, uniquely labelled
    cells_a = _monthly(0, _Q1_MONTHS, drop=_ABSENT_IN_PROD)
    cells_b = _monthly(1, _Q2_MONTHS, drop=_ABSENT_IN_PROD)
    for mk in _Q2_MONTHS:
        cells_b[mk][zeroed] = 0
    ledger = br.compare(
        br.roll_up(br.quarter_period(2026, 1), cells_a, channels=_channels(0, _Q1_MONTHS)),
        br.roll_up(br.quarter_period(2026, 2), cells_b, channels=_channels(1, _Q2_MONTHS)))
    page = brr.render(ledger)
    table = _between(page, "Full comparison ledger", "</section>")

    gone = _ABSENT_IN_PROD | {"roas_not_actualized_pct", "revenue_target_pct"}
    for key in sorted(gone):
        label = br.BY_KEY[key].label
        frag = _tr_with(table, label)
        assert frag.count('class="absent"') == 4, (key, frag)   # A, B, delta, %
        assert "$0" not in frag and ">0<" not in frag and "0.00%" not in frag
        assert label + " — not reported" in page, key

    zero_row = _tr_with(table, br.BY_KEY[zeroed].label)
    assert 'class="absent"' not in zero_row
    assert ">0<" in zero_row and ">124<" in zero_row

    assert "Absent figures in this report" in page
    assert "not reported</b> for that column. It is not zero" in page


def test_board_report_render_says_so_when_nothing_is_absent():
    """The honesty panel is not conditional decoration — a complete report says
    it is complete, so a reader never has to wonder whether the panel was
    dropped or the data was clean."""
    page = _comparison()
    assert "None — every published row reported a figure in every column." in page
    assert 'class="absent"' not in _between(page, "Full comparison ledger", "</section>")


# --- colour and glyph are two different questions ----------------------------

def test_board_report_render_colour_is_goodness_and_the_arrow_is_direction():
    """At a glance gave ``-21.8%`` and ``-34.2%`` the ``up`` class and printed a
    green triangle pointing UP beside a falling number. One class encoded two
    orthogonal things; here colour comes from ``LedgerRow.improved`` and the
    arrow from the sign of the delta, chosen by two different functions."""
    page = _comparison()
    glance = _between(page, "At a glance", "</table>")

    def cls_of(label):
        return re.search(r'class="(dcell[^"]*)"', _tr_with(glance, label)).group(1)

    # Spend and CAC fell, which is favourable: green, arrow DOWN.
    assert cls_of("Spend") == "dcell up fall"
    assert cls_of("CAC (Spend / Revenue Client)") == "dcell up fall"
    # Qualified leads fell, which is not: red, arrow DOWN.
    assert cls_of("Qualified Leads") == "dcell down fall"
    # Revenue rose and that is favourable: green, arrow UP.
    assert cls_of("Revenue Amount Sold (Actualized)") == "dcell up rise"

    # No cell anywhere pairs a rising glyph with a falling number.
    for cls, text in re.findall(r'class="(dcell[^"]*)">([^<]*)<', glance):
        if text.startswith("-") or text.startswith("−"):
            assert "rise" not in cls, (cls, text)
        elif text.startswith("+"):
            assert "fall" not in cls, (cls, text)

    # Budget moved, but it is an input the team chose: never coloured, still
    # given its direction.
    budget = _tr_with(_between(page, "Full comparison ledger", "</section>"), "Budget")
    assert 'class="dc neu"' in budget and "neu" in budget
    assert brr._tone(None) == "neu" and brr._glyph(-1.0) == "fall"
    assert brr._tone(True) == "up" and brr._glyph(1.0) == "rise"


def test_board_report_render_win_column_can_never_carry_a_deterioration():
    """The single-period card was ``.win`` — green ``+`` bullets — and carried
    "thin 110% ROAS" and "CAC high at $4,991". With no prose supplied the two
    columns are filled from the ledger, and membership is decided by
    ``improved``, so the defect is structurally unreachable."""
    ledger = br.compare(_q1_ch(), _q2_ch())
    page = brr.render(ledger)
    section = _between(page, "What improved, what dropped", "</section>")
    wins = _between(section, 'class="col win"', "</div>")
    misses = _between(section, 'class="col miss"', "</div>")

    def labels(block):
        return [m.split(":")[0] for m in re.findall(r"<li>(.*?)</li>", block)]

    # Bullet labels are group-qualified for the same reason the charts are.
    names = brr._disambiguated(ledger)
    improved = {names[r.key] for r in ledger.rows if r.improved is True}
    dropped = {names[r.key] for r in ledger.rows if r.improved is False}
    assert labels(wins) and labels(misses)
    for label in labels(wins):
        assert label in improved, label
    for label in labels(misses):
        assert label in dropped, label
    assert "derived from the ledger" in section


def test_board_report_render_is_complete_and_honest_with_no_prose_at_all():
    """The thesis sentence and the win/miss bullets are step 8's problem. With
    the slot empty the report still has every section, every number and the
    basis panel, and it invents no narrative to fill the hole."""
    page = _comparison()
    assert 'class="sub"' not in page              # no thesis, and no empty line
    assert _kickers(page) == ["00", "01", "02", "03", "04"]
    assert "Basis &amp; data gaps" in page

    prose = brr.ReportProse(
        thesis="Q2 traded volume for productivity.",
        wins=("Revenue efficiency flipped.",), misses=("Top of funnel shrank.",))
    with_prose = _comparison(prose=prose)
    # The thesis appears once. The templates printed the cover .sub again,
    # verbatim, as the section-01 lead.
    assert with_prose.count("Q2 traded volume for productivity.") == 1
    assert "Revenue efficiency flipped." in with_prose
    assert "derived from the ledger" not in with_prose

    # One column of bullets is one column of layout, not half a dead panel.
    one_sided = _comparison(prose=brr.ReportProse(wins=("Only a win.",)))
    assert '<div class="ins one">' in one_sided
    assert '<div class="ins">' in with_prose


def test_board_report_render_treats_prose_and_labels_as_text_not_markup():
    """Prose will be LLM-written and this page goes to clients. Everything that
    reaches the document is escaped, so injected markup is shown, not run."""
    prose = brr.ReportProse(
        thesis='<script>alert(1)</script> & "quoted"',
        wins=("<b>bold</b> attempt",), misses=("<img src=x onerror=y>",))
    page = brr.render(br.compare(_q1_ch(), _q2_ch()), prose=prose,
                      brand="<em>Acme</em> & Co", title="</title><script>x</script>")
    assert "<script" not in page and "<img" not in page and "<b>bold" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;em&gt;Acme&lt;/em&gt; &amp; Co" in page
    # The catalog's own ampersands survive as entities, not as raw markup.
    assert "Goal &amp; Blended Financials" in page


# --- the two reconciliations a reader is entitled to -------------------------

def test_board_report_render_conversion_rate_shows_the_denominator_it_uses():
    """Conversion Rate divides by Total Demos Completed (Direct) — 248/212 —
    not by the all-in 272/238 printed one card away. Two adjacent cards a reader
    cannot reconcile will not ship, so the card and the ledger row both print
    the basis the catalog carries."""
    basis = br.BY_KEY["conversion_rate_pct"].basis
    assert basis and br.BY_KEY[br.CAC_KEY].basis      # the catalog is where it lives
    for page in (_comparison(), _standalone()):
        assert basis in page
        assert br.BY_KEY[br.CAC_KEY].basis in page
        assert "Denominators worth stating" in page

    core = _between(_standalone(), "Actualized &amp; core", "Not actualized")
    assert ">272<" in core and ">248<" in core and ">19.4%<" in core
    assert core.index("Total Demos Completed (Direct)") < core.index("Conversion Rate (%)")
    assert basis in core


def test_board_report_render_untracked_is_a_row_in_the_table_not_a_footnote():
    """Q1 leaves $16,881 (7.0%) and 8 clients unattributed, Q2 $22,512 (12.0%)
    and 12, and the gap is growing. A reader who cannot see it infers the
    channel table IS the total."""
    for page in (_comparison(), _standalone()):
        assert page.count('<tr class="untracked">') == 1
        row = _tr_with(page, br.UNTRACKED_LABEL)
        assert br.UNTRACKED_LABEL in row and "$16,881" in row
        assert "not a footnote" in page

    two = _comparison()
    untracked = _tr_with(two, br.UNTRACKED_LABEL)
    assert "$16,881" in untracked and "$22,512" in untracked
    assert "7.0% of spend" in two and "12.0% of spend" in two
    assert "widening" in two and "7.0% → 12.0%" in two
    # Its revenue is charted, so the money is visible rather than footnoted.
    assert br.UNTRACKED_LABEL in _svg_named(two, "Revenue sold by channel")


def test_board_report_render_untracked_columns_may_be_absent_and_render_absent():
    """The reconciliation is per measure: a channel month missing its spend
    leaves that one measure unknown while the others still stand. Unknown prints
    as an em-dash in the row, not as a zero share."""
    channels = _channels(0, _Q1_MONTHS)
    del channels["Meta"]["2026-02"]["spend"]
    rollup = br.roll_up(br.quarter_period(2026, 1), _monthly(0, _Q1_MONTHS),
                        channels=channels)
    page = brr.render(brr.single_period(rollup))
    row = _tr_with(page, br.UNTRACKED_LABEL)
    assert 'class="absent"' in row       # spend, and the two ratios built on it
    assert "$0" not in row
    assert ">8<" in row                  # the measures that ARE complete stand
    assert "Roll-up warnings" in page


# --- the headline chart is by month, off the ledger\'s own cells --------------

def _month_bars(svg: str) -> list[tuple[float, float]]:
    """``(x, height)`` per drawn bar, in emit order, legend swatches dropped.

    The legend is three 11px squares; every bar is far wider than that at any
    axis length this chart reaches, so width is the discriminator.
    """
    found = re.findall(
        r'<rect x="([-\d.]+)" y="[-\d.]+" width="([\d.]+)" height="([\d.]+)"', svg)
    return [(float(x), float(h)) for x, w, h in found if float(w) > 12]


def test_board_report_render_draws_the_period_by_month_not_by_column():
    """The marketing team\'s headline chart is "By month — spend vs revenue":
    three series across Jan/Feb/Mar in the Q1 file and Apr/May/Jun in the Q2
    one. It was drawn per COLUMN while the ledger carried only period totals;
    the roll-up now carries the months, so the real series is drawn.

    The assertion that matters is the one a divided total would fail: spend is
    front-loaded across this quarter and actualized revenue is back-loaded, so
    the two series slope opposite ways. Three equal bars would be a period total
    cut in three and presented as a trend.
    """
    one = _standalone()
    svg = _svg_named(one, "Spend against revenue sold, by month")

    labels = re.findall(r"<text[^>]*>([^<]*)</text>", svg)
    assert ["Jan", "Feb", "Mar"] == [l for l in labels if l in ("Jan", "Feb", "Mar")]
    for series in ("Spend", "Revenue Sold (Actualized)", "Revenue Sold (Not Actualized)"):
        assert series in labels, series
    assert "By month — spend vs revenue" in one
    assert "Q1 covers Jan – Mar 2026." in one

    bars = _month_bars(svg)
    assert len(bars) == 9, bars                      # 3 months x 3 series
    assert [x for x, _ in bars] == sorted(x for x, _ in bars)   # left to right
    spend = [h for i, (_, h) in enumerate(bars) if i % 3 == 0]
    revenue = [h for i, (_, h) in enumerate(bars) if i % 3 == 1]
    assert spend[0] > spend[1] > spend[2], spend
    assert revenue[0] < revenue[1] < revenue[2], revenue

    # Bar heights are the months\' own figures, to the axis scale.
    cells = brr._chart_months(brr.single_period(_q1_ch()))
    values = [c.value("spend") for c in cells]
    assert spend[0] / spend[2] == pytest.approx(values[0] / values[2], abs=0.01)

    # Two periods draw both windows on one axis, in order, each month once —
    # and a single-period ledger is the same period twice, so the dedup is what
    # keeps the standalone chart at three months rather than six.
    two = _comparison()
    two_svg = _svg_named(two, "Spend against revenue sold, by month")
    drawn = [l for l in re.findall(r"<text[^>]*>([^<]*)</text>", two_svg)
             if len(l) == 3 and l.isalpha()]
    assert drawn == ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    assert len(_month_bars(two_svg)) == 18
    assert "Q1 covers Jan – Mar 2026. Q2 covers Apr – Jun 2026." in two
    assert "never a period total divided across its months" in two


def test_board_report_render_a_month_missing_a_series_keeps_its_slot_as_an_em_dash():
    """The absent-vs-zero rule, on the chart. A month that did not report a
    series keeps its place on the axis with an em-dash where the bar would be:
    a zero bar states no spend and a dropped month shortens the quarter.

    The same month withholds that column\'s period TOTAL, so the ledger row
    beside the chart is an em-dash while two of its bars still stand. That
    disagreement is the design — a partly covered period has to look partly
    covered.
    """
    cells = _monthly(0, _Q1_MONTHS)
    del cells["2026-02"]["revenue_amount_sold"]
    page = brr.render(brr.single_period(br.roll_up(
        br.quarter_period(2026, 1), cells, channels=_channels(0, _Q1_MONTHS))))
    svg = _svg_named(page, "Spend against revenue sold, by month")

    labels = re.findall(r"<text[^>]*>([^<]*)</text>", svg)
    assert [l for l in labels if l in ("Jan", "Feb", "Mar")] == ["Jan", "Feb", "Mar"]
    assert labels.count("—") == 1                    # exactly the one hole
    assert len(_month_bars(svg)) == 8                 # 9 slots, 1 unreported

    assert "Revenue Sold (Actualized) — Feb 2026" in page
    assert "Not reported, and drawn as an em-dash in the bar" in page
    assert "a partly covered period stays visibly partial" in page

    # ...and the standalone card for the same metric is absent, not a two-month
    # sum: two bars stand in the chart while the headline figure is withheld.
    card = _between(page, '<div class="card hl">', "</div></div>")
    assert br.BY_KEY["revenue_amount_sold"].label in card
    assert 'class="absent"' in card and "$0" not in card
    assert "not reported for this period — this is absent, not zero" in card
    assert br.BY_KEY["revenue_amount_sold"].label + " — not reported" in page
    assert "Roll-up warnings" in page


def test_board_report_render_drops_the_by_month_chart_rather_than_inventing_months():
    """A ledger with no monthly cells — a hand-built one, or a run persisted
    before they existed. The chart is not reconstructed by cutting the period
    total into equal months, which would draw a flat trend nobody measured."""
    full = br.compare(_q1_ch(), _q2_ch())
    bare = br.ReportLedger(columns=full.columns, periods=full.periods, rows=full.rows,
                           channels=full.channels, untracked=full.untracked,
                           gaps=full.gaps)
    assert bare.monthly == ((), ())
    page = brr.render(bare)
    assert "carries no monthly cells, so the by-month chart is not drawn" in page
    assert "<title>Spend against revenue sold, by month</title>" not in page
    assert "Q1 covers Jan – Mar 2026" in page       # still says what it covers
    assert _kickers(page) == ["00", "01", "02", "03", "04"]      # nothing else lost


def test_board_report_render_series_styles_cannot_drift_from_the_carried_fields():
    """The chart\'s three series and the ledger\'s three carried fields are one
    decision. Adding a field in ``board_report`` with no style here would drop
    it from the chart silently, so the pairing is refused at import."""
    assert tuple(brr._MONTH_SERIES_STYLE) == br.MONTHLY_FIELDS
    assert [c for _, c in brr._MONTH_SERIES_STYLE.values()] == [brr.SLATE, brr.GOLD,
                                                                brr.INK]


# --- no invisible bars -------------------------------------------------------

def test_board_report_render_roas_chart_keeps_the_small_channels_visible():
    """Website returns 543%/787% against Meta and Google near 40%/95%. On a
    linear axis scaled to Website, Meta's bar is 5% of the plot, so the three
    channels the reallocation decision is about are invisible. The axis is
    capped off the median instead, the outlier is drawn torn with its true value
    printed, and break-even lands where a reader can use it."""
    page = _comparison()
    svg = _svg_named(page, "ROAS by channel against break-even")
    heights = sorted(float(h) for h in re.findall(r'height="([\d.]+)"', svg)
                     if float(h) > 12)
    assert heights, svg[:200]
    # Uncapped, the ratio would be 40.46 / 787.35 = 0.051.
    assert heights[0] / heights[-1] > 0.15, heights

    assert "Axis capped at 250%" in page
    assert "Website Q1 543.5%" in page and "Website Q2 787.4%" in page
    assert "torn top" in page
    # Break-even is drawn dark at 100%, not as one more grey gridline.
    assert 'stroke="' + brr.INK + '" stroke-width="1.6"' in svg
    assert "break-even 100%" in svg
    # Bars carry the benchmark colours, so above/below reads without the axis.
    assert brr.POS in svg and brr.NEG in svg


def test_board_report_render_draws_three_bar_charts_and_nothing_else():
    """Grouped vertical (spend vs revenue by month), horizontal (revenue by
    channel, and the sorted movers), and vertical against a benchmark (ROAS).
    No line, no pie, no doughnut, and still no library."""
    page = _comparison()
    for title in ("Spend against revenue sold, by month", "Percent change Q1 to Q2",
                  "Revenue sold by channel", "ROAS by channel against break-even"):
        svg = _svg_named(page, title)
        assert "<rect" in svg and "<circle" not in svg and "<polyline" not in svg

    movers = _svg_named(page, "Percent change Q1 to Q2")
    assert 'stroke="' + brr.INK + '" stroke-width="1.5"' in movers      # zero line
    # Bar direction is the sign; bar colour is whether the move helped — so CAC
    # falling is a left-pointing GREEN bar.
    assert brr.POS in movers and brr.NEG in movers
    assert "8 largest improvements and 7 largest deteriorations of 36 comparable" in page
    # Ranked by size alone this chart is sixteen green bars, because on this data
    # every large move is a revenue increase. Both directions are drawn, so the
    # seven rows that got worse cannot fall off the bottom.
    assert movers.count(brr.NEG) == 7 and movers.count(brr.POS) == 8


def test_board_report_render_chart_labels_stay_distinct_without_group_bands():
    """Two catalog labels appear twice — the Projected and Paying not-actualized
    blocks publish identically-named rows, and the ledger tells them apart by
    the band above. A chart has no bands, so the group qualifies the label
    there; otherwise different rows share one bar name."""
    joined = " ".join(re.findall(r"<text[^>]*>(.*?)</text>",
                                 _svg_named(_comparison(), "Percent change Q1 to Q2")))
    assert "· Paying" in joined and "· Projected" in joined
    labels = brr._disambiguated(br.compare(_q1_ch(), _q2_ch()))
    assert len(set(labels.values())) == len(br.CATALOG)


# --- print / PDF correctness, all of it absent from the templates ------------

def test_board_report_render_carries_the_print_rules_the_templates_lacked():
    """A headless-Chromium PDF pass follows in step 5. Each rule below is zero
    occurrences in the supplied files and each has a specific failure: a blank
    PDF, a 45-row table with no headers after page 1, and a 9-column table
    clipped at A4."""
    page = _comparison()
    css = _between(page, "<style>", "</style>")

    # The whole identity is background colour — navy cover, gold cells, navy
    # group bands. Without this the PDF prints them white.
    assert "print-color-adjust:exact" in css
    assert "-webkit-print-color-adjust:exact" in css
    assert "@page{" in css

    # 45 rows cannot fit one page, so the header has to repeat.
    assert "thead{display:table-header-group}" in css

    print_block = css[css.index("@media print"):]
    # break-inside on the WRAPPER clips a table that cannot fit a page; it
    # belongs on the row.
    assert "tr{break-inside:avoid" in print_block
    assert not re.search(r"\.twrap[^{]*\{[^}]*break-inside:avoid", css), \
        "break-inside:avoid on .twrap clips the ledger instead of paginating it"
    assert re.search(r"\.twrap\{[^}]*overflow:visible", print_block)

    # The 9-column channel table relies on overflow-x + min-width, neither of
    # which paginates, so print gets its own variant.
    assert 'class="twrap wide"' in page
    assert "table.cmp{min-width:0" in print_block
    assert ".twrap.wide table.cmp{font-size" in print_block
    assert re.search(r"table\.cmp th:first-child,table\.cmp td:first-child\{position:static",
                     print_block)


def test_board_report_render_keeps_the_typography_decision_the_user_made():
    """Fraunces / Inter / IBM Plex Mono with tabular figures — a client-facing
    document, deliberately not the console's Archivo. The faces are named first
    and then fall back through what a Windows, macOS or headless-Chromium box
    actually has, because embedding them would add roughly a megabyte of base64
    to every file the team emails."""
    css = _between(_comparison(), "<style>", "</style>")
    assert css.index("Fraunces") < css.index("Georgia")
    assert "Inter," in css and "'IBM Plex Mono'" in css
    assert "font-variant-numeric:tabular-nums" in css
    assert 'font-feature-settings:"tnum" 1' in css
    for fallback in ("Georgia", "'Segoe UI'", "Consolas", "'DejaVu Sans Mono'"):
        assert fallback in css, fallback
    # Fallbacks, not @font-face blobs, and not a remote stylesheet.
    assert "@font-face" not in css and "data:font" not in css
    for name, value in brr.PALETTE.items():
        assert "--" + name + ":" + value in css


def test_board_report_render_says_the_brand_is_unset_rather_than_printing_a_hole():
    """``brand`` is optional and nothing calls :func:`render` yet, so the first
    call site is the one that can get it wrong. An unnamed brand reads as
    unnamed — on the cover, in the tab title and in the footer — instead of
    collapsing to a page that looks finished and does not say whose it is.

    Whitespace is the case that bites: a name read out of a config row can
    arrive as ``" "``, which is truthy, and would print an eyebrow with a gap in
    front of it that nobody could account for.
    """
    ledger = br.compare(_q1_ch(), _q2_ch())
    assert brr._brand_of(None) == brr.UNNAMED_BRAND
    assert brr._brand_of("") == brr.UNNAMED_BRAND
    assert brr._brand_of("   ") == brr.UNNAMED_BRAND
    assert brr._brand_of("  Legal Soft  ") == "Legal Soft"

    for missing in (None, "", "   ", "\t"):
        page = brr.render(ledger) if missing is None else brr.render(ledger, brand=missing)
        assert f"{brr.UNNAMED_BRAND} · Growth marketing" in page
        assert f"<title>{brr.UNNAMED_BRAND} — Q1 vs Q2 board report</title>" in page
        assert f"<b>{brr.UNNAMED_BRAND} — Q1 vs Q2 comparison.</b>" in page
        assert "> · Growth marketing<" not in page       # never the blank eyebrow

    named = brr.render(ledger, brand="Legal Soft")
    assert "Legal Soft · Growth marketing" in named
    assert "<title>Legal Soft — Q1 vs Q2 board report</title>" in named
    assert "<b>Legal Soft — Q1 vs Q2 comparison.</b>" in named
    assert brr.UNNAMED_BRAND not in named
    # An explicit title still wins; the brand is not welded into it.
    assert "<title>Board pack</title>" in brr.render(ledger, title="Board pack")


def test_board_report_render_refuses_a_ledger_that_is_not_two_columns():
    with pytest.raises(ValueError, match="two columns"):
        brr.render(br.ReportLedger(columns=("only",), periods=(), rows=()))
