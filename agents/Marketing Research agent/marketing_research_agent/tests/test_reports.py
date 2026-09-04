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
    for kind in reports.KINDS:
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
    one of them as it widens."""
    assert br.ADDITIVE_FIELDS & br.RECOMPUTED_FIELDS == frozenset()
    assert set(reports._OFFICIAL_TOTAL_FIELDS) & br.RECOMPUTED_FIELDS == set()
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
    assert key != br.cache_key(periods, day, version="mr-board-report/2")
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
