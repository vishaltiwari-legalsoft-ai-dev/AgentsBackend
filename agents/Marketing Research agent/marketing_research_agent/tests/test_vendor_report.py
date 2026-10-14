"""Vendor performance report — compute (vendor_report.py) and render
(vendor_report_render.py), one module, the board report's precedent.

The golden fixture is the real stored 2026-09-02 day (every doc, including the
eight written by earlier capture runs under since-renamed titles) plus
2026-08-31, read back through ``snapshots.vendor_sweep`` from a temp disk
store — so the final-run selection under test is the production one, and no
test reaches Firestore (``MR_OFFLINE=1`` from the package conftest).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from marketing_research_agent import goals, runs, snapshots
from marketing_research_agent import vendor_report as vr
from marketing_research_agent import vendor_report_render as vrr

FIXTURE = Path(__file__).parent / "fixtures" / "vendor_sweep_2026-09-02.json"


# --- harness ------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """The fixture docs as a disk snapshot store, read through the real API."""
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    for doc in json.loads(FIXTURE.read_text(encoding="utf-8"))["docs"]:
        (snap_dir / f"{doc['vendor_slug']}_{doc['date']}.json").write_text(
            json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(snap_dir))
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "runs"))
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    goals.invalidate_targets_cache()
    return snap_dir


def _targets(**threshold_overrides) -> dict:
    t = {"thresholds": goals.default_thresholds(),
         "channel_goals": {k: {f: getattr(g, f) for f in goals._GOAL_FIELDS}
                           for k, g in goals.CHANNEL_GOALS.items()}}
    t["thresholds"].update(threshold_overrides)
    return t


@pytest.fixture()
def golden(store):
    sweep = snapshots.vendor_sweep("2026-09")
    previous = snapshots.previous_month_sweep("2026-09")
    return vr.compute(sweep, targets=_targets(), previous=previous)


def _by_name(report: dict) -> dict[str, dict]:
    return {v["name"]: v for v in report["vendors"]}


def _doc(name: str, *, budget=1000.0, spend=0.0, leads=0.0, ql=0.0, booked=0.0,
         qbooked=0.0, completed=0.0, dnc=0.0, proj=0.0, actual=0.0, clients=0.0,
         not_actual=0.0, fee=None, inv_budget=None, inv_spend=None, gid=None,
         captured="2026-09-10T10:00:00+00:00", date_iso="2026-09-10") -> dict:
    """One vendor doc in the stored shape (canonical.team_overall only). Each
    title gets its own gid unless one is given — tabs never share a gid. The
    Investment column equals Performance unless ``inv_*`` is given; ``fee=None``
    leaves the management-fee row off the tab (absent, as on a tab without it)."""
    team = {
        "budget": {"performance": budget,
                   "investment": budget if inv_budget is None else inv_budget},
        "spend": {"performance": spend, "investment": spend if inv_spend is None else inv_spend},
        "leads": {"total": leads, "qualified": ql, "lost_dnc_bad_lead": dnc},
        "demos": {"total_booked_all": booked, "qualified_booked_all": qbooked,
                  "completed_all": completed},
        "projected_revenue": {"total_amount_sold_actualized": proj},
        "actualized_revenue": {"amount_sold": actual, "revenue_clients": clients},
        "not_actualized_revenue": {"amount_sold": not_actual},
    }
    if fee is not None:
        team["management_fees_investment"] = fee
    return {
        "vendor": name, "vendor_slug": snapshots.slugify(name),
        "gid": gid if gid is not None else sum(map(ord, name)) * 1000 + len(name),
        "date": date_iso, "month": date_iso[:7], "captured_at": captured,
        "canonical": {"team_overall": team, "channels": {}},
    }


def _sweep(*docs, rollup=None, date_iso="2026-09-10") -> dict:
    return {"date": date_iso, "docs": list(docs), "excluded": [], "rollup": rollup}


# --- the golden test --------------------------------------------------------------

def test_the_2026_09_02_sweep_reproduces_the_published_portfolio(golden):
    p = golden["portfolio"]
    assert p["total_budget"] == 95_600
    assert round(p["total_spend"]) == 2_737
    assert p["total_leads"] == 18 and p["qualified_leads"] == 9
    assert round(p["ql_ratio_pct"], 1) == 50.0
    assert p["qual_demos_booked"] == 14 and p["demos_completed"] == 2
    assert round(p["show_rate_pct"], 1) == 14.3
    assert round(p["budget_utilized_pct"], 1) == 2.9
    assert p["dnc_bad_leads"] == 2
    # The template's own defect, not copied: its "Demos booked" column summed to
    # 16 beside a show rate dividing by 14. Both counts exist, under two names.
    assert p["demos_booked"] == 16


def test_the_2026_09_02_movers_are_the_published_bars_in_order(golden):
    bars = [(m["key"], round(m["gap_pct"], 1)) for m in golden["movers"]]
    assert bars == [
        ("cost_per_qual_demo_booked", 58.8),
        ("cost_per_lead", 30.9),
        ("cost_per_qualified_lead", 24.0),
        ("ql_ratio_pct", -33.3),
        ("show_rate_pct", -74.0),
        ("cost_per_demo_completed", -76.6),
    ]
    # Polarity-aware: a LOW cost beats its ceiling (right, green); a LOW show
    # rate misses its target (left, red).
    beating = {m["key"]: m["beating"] for m in golden["movers"]}
    assert beating["cost_per_lead"] is True and beating["show_rate_pct"] is False
    assert golden["movers_not_charted"] == []


def test_the_sweep_is_the_final_capture_run_with_website_named_not_counted(golden):
    names = [v["name"] for v in golden["vendors"]]
    assert len(names) == 18
    assert "Website" not in names
    assert not any("overall" in (v["slug"] or "") for v in golden["vendors"])
    # Workbook tab order (capture order), not slug order.
    assert names[:3] == ["Hawksem LS Google", "Hawksem LS Meta", "Meta 360 RA"]
    reasons = {e["tab"]: e["reason"] for e in golden["sweep"]["excluded"]}
    assert reasons["Website"] == "non_media"
    assert sum(1 for r in reasons.values() if r == "stale_capture") == 8
    assert reasons["Copy of Meta 360 LS"] == "stale_capture"


def test_a_roll_up_doc_inside_docs_is_never_counted_as_a_vendor(store):
    sweep = snapshots.vendor_sweep("2026-09")
    rollup = sweep["rollup"]
    sneaky = {**sweep, "docs": sweep["docs"] + [rollup], "rollup": None}
    r = vr.compute(sneaky, targets=_targets())
    assert r["portfolio"]["total_budget"] == 95_600
    assert r["sweep"]["vendor_count"] == 18


# --- targets ----------------------------------------------------------------------

def test_targets_used_are_the_owners_figures_with_their_basis(golden):
    t = golden["targets"]
    assert t["applied_set"] == "META" and t["dominant_share_pct"] == 100.0
    assert "100% Meta-channel spend" in t["reason"] and "80%" in t["reason"]
    by_key = {i["key"]: i for i in t["items"]}
    assert {k: i["target"] for k, i in by_key.items()} == {
        "cost_per_lead": 220.0, "cost_per_qualified_lead": 400.0,
        "cost_per_qual_demo_booked": 475.0, "ql_ratio_pct": 75.0,
        "show_rate_pct": 55.0, "cost_per_demo_completed": 775.0,
    }
    assert by_key["cost_per_qualified_lead"]["basis"] == (
        "Cost / Qual. Lead ceiling $400 (top of the $200–$400 target range)")
    assert by_key["cost_per_qual_demo_booked"]["basis"] == (
        "Cost / Qual. Demo Booked $475 (midpoint of Meta's $400–$550 range)")
    assert by_key["cost_per_demo_completed"]["basis"] == (
        "Cost / Demo Completed $775 (midpoint of Meta's $700–$850 range)")
    assert by_key["show_rate_pct"]["basis"] == "Show-up Rate target 55% (Meta)"
    assert "Targets used" in golden["notes"]["targets_used"]["text"]


def test_the_cpl_ceiling_is_new_and_lives_with_the_other_editable_thresholds():
    assert goals.default_thresholds()["cost_per_lead_ceiling"] == 220.0
    # The Meta ranges are REUSED from CHANNEL_GOALS, not copied.
    meta = goals.CHANNEL_GOALS["META"]
    assert (meta.cpd_booked_low + meta.cpd_booked_high) / 2 == 475
    assert (meta.cpd_completed_low + meta.cpd_completed_high) / 2 == 775


def test_mixed_spend_below_the_rule_uses_the_total_benchmarks():
    sweep = _sweep(_doc("Acme LS Meta", spend=600, leads=10, ql=8, qbooked=4, completed=2),
                   _doc("Acme LS Google", spend=400, leads=5, ql=4, qbooked=2, completed=1))
    r = vr.compute(sweep, targets=_targets())
    assert r["targets"]["applied_set"] == "Total"
    assert "no channel holds 80%" in r["targets"]["reason"]
    by_key = {i["key"]: i["target"] for i in r["targets"]["items"]}
    assert by_key["cost_per_qual_demo_booked"] == 575.0   # midpoint of Total 500–650
    assert by_key["show_rate_pct"] == 63.0
    # The threshold is editable: at 50% the same 60/40 split is a Meta portfolio.
    r2 = vr.compute(sweep, targets=_targets(vendor_channel_mix_pct=50.0))
    assert r2["targets"]["applied_set"] == "META"


def test_a_dominant_channel_with_no_benchmark_set_falls_back_to_total_and_says_why():
    sweep = _sweep(_doc("Dante Microsoft", spend=900, leads=5),
                   _doc("Acme LS Meta", spend=100, leads=1))
    r = vr.compute(sweep, targets=_targets())
    assert r["targets"]["applied_set"] == "Total"
    assert "Microsoft holds 90%" in r["targets"]["reason"]


def test_no_spend_at_all_uses_total_and_charts_only_what_is_measurable():
    r = vr.compute(_sweep(_doc("Acme LS Meta")), targets=_targets())
    assert r["targets"]["applied_set"] == "Total"
    assert r["movers"] == []          # every ratio has a zero denominator
    assert {n["key"] for n in r["movers_not_charted"]} == {
        "cost_per_lead", "cost_per_qualified_lead", "cost_per_qual_demo_booked",
        "ql_ratio_pct", "show_rate_pct", "cost_per_demo_completed"}


def test_a_metric_with_no_target_is_left_out_of_the_movers_and_named(store):
    targets = _targets()
    del targets["thresholds"]["cost_per_lead_ceiling"]
    r = vr.compute(snapshots.vendor_sweep("2026-09"), targets=targets)
    assert "cost_per_lead" not in {m["key"] for m in r["movers"]}
    assert {"key": "cost_per_lead", "label": "Cost / Lead",
            "reason": "no target set"} in r["movers_not_charted"]
    assert any(m["metric"] == "cost_per_lead" and m["reason"].startswith("no target set")
               for m in r["missing"])
    assert "Cost / Lead is not charted: no target set." in r["notes"]["movers_reading"]["text"]


# --- channels -----------------------------------------------------------------------

@pytest.mark.parametrize("title, channel", [
    ("Meta 360 RA", "Meta"), ("Hawksem LS Meta", "Meta"),
    ("DanteAgency LI Google", "Google"), ("Elevate MKT LS Email", "Email"),
    ("DanteAgency Microsoft", "Microsoft"), ("AB Twitter LS", "Twitter"),
    ("Acme LinkedIn", "LinkedIn"), ("DanteAgency ChatGPT", "Other"),
    ("Elevate MKT LS Emailo", "Other"),   # whole words only — a typo is not a channel
])
def test_channel_comes_from_whole_words_in_the_tab_title(title, channel):
    assert vr.channel_of(title)[0] == channel


def test_unrecognised_and_minor_channels_fold_into_other_listed_by_name(golden):
    buckets = {b["channel"]: b for b in golden["channels"]["buckets"]}
    assert list(buckets) == ["Meta", "Google", "Email", "Other"]
    assert round(buckets["Meta"]["spend"]) == 2_737 and buckets["Google"]["spend"] == 0
    assert buckets["Meta"]["projected_revenue"] == 3_671
    assert golden["channels"]["other_vendors"] == [
        {"vendor": "DanteAgency ChatGPT", "channel": "unrecognised"},
        {"vendor": "DanteAgency Microsoft", "channel": "Microsoft"},
        {"vendor": "AB Twitter LS", "channel": "Twitter"},
    ]


# --- status pills -------------------------------------------------------------------

def test_the_default_pill_rules_give_the_samples_pills(golden):
    s = {n: (v["status"], v["status_rule"]) for n, v in _by_name(golden).items()}
    assert s["Hawksem LS Meta"] == ("Strong Start", "completions")
    assert s["Meta 360 RA"] == ("Strong Start", "qualified_volume")
    assert s["DanteAgency LI Google"] == ("Check In", "dnc_zero_spend")
    assert s["Ariya LS Meta"] == ("Too Early", "booked_watch")
    others = {n for n, (st, _r) in s.items() if st == "Too Early"} - {"Ariya LS Meta"}
    assert len(others) == 14
    actions = [(a["vendor"], a["status"]) for a in golden["actions"]]
    assert actions == [
        ("DanteAgency LI Google", "Check In"),
        ("Hawksem LS Meta", "Strong Start"),
        ("Meta 360 RA", "Strong Start"),
        ("Ariya LS Meta", "Too Early"),
        ("All other vendors", "Too Early"),
    ]
    assert len(golden["actions"][-1]["vendors"]) == 14
    assert golden["actions"][0]["text"] == (
        "Confirm with the vendor why 1 demo was booked and 1 DNC bad lead was logged "
        "with $0 spend recorded.")


def test_pill_thresholds_are_editable(store):
    sweep = snapshots.vendor_sweep("2026-09")
    r = vr.compute(sweep, targets=_targets(vendor_strong_start_min_qualified_leads=5.0))
    assert _by_name(r)["Meta 360 RA"]["status"] == "Too Early"
    r = vr.compute(sweep, targets=_targets(vendor_check_in_min_dnc_zero_spend=2.0))
    assert _by_name(r)["DanteAgency LI Google"]["status"] == "Too Early"


def test_check_in_on_a_bad_lead_rate_and_on_spend_with_no_demo():
    sweep = _sweep(_doc("Acme LS Meta", spend=900, leads=20, ql=10, dnc=7, qbooked=2),
                   _doc("Burn LS Google", spend=3500, leads=4))
    r = _by_name(vr.compute(sweep, targets=_targets()))
    assert (r["Acme LS Meta"]["status"], r["Acme LS Meta"]["status_rule"]) == (
        "Check In", "bad_lead_rate")
    assert (r["Burn LS Google"]["status"], r["Burn LS Google"]["status_rule"]) == (
        "Check In", "spend_no_demo")
    assert "7 of 20 leads (35.0%)" in r["Acme LS Meta"]["action"]["text"]


# --- prose is fixed templates over computed facts --------------------------------------

def test_standouts_are_deduplicated_one_bullet_per_vendor_fact(golden):
    keys = [(s["vendor"], s["fact"]) for s in golden["standouts"]]
    assert keys == [("Hawksem LS Meta", "completions"), ("Meta 360 RA", "qualified_volume")]
    assert len(keys) == len(set(keys))
    first = golden["standouts"][0]
    assert first["text"] == (
        "Hawksem LS Meta has completed 2 of its 2 qualified demos booked, and already shows "
        "$3,671 in projected revenue — the only revenue signal in the portfolio so far.")
    # The revenue fact is IN that bullet, so it is not a third, repeated one.
    assert sum("3,671" in s["text"] for s in golden["standouts"]) == 1
    assert [seg["t"] for seg in first["segments"] if seg["em"]] == [
        "completed 2 of its 2 qualified demos booked", "$3,671"]


def test_completions_are_worded_as_completions_everywhere_they_are_said(golden):
    """The old wording ("2 for 2 on qualified demos booked") read as a booking
    stat. Thesis, standout and action now all say what happened: demos
    COMPLETED, out of the qualified bookings the show rate divides by."""
    phrase = "has completed 2 of its 2 qualified demos booked"
    hawk = next(a for a in golden["actions"] if a["vendor"] == "Hawksem LS Meta")
    said = [golden["notes"]["thesis"]["text"], golden["standouts"][0]["text"], hawk["text"]]
    for text in said:
        assert phrase in text, text
        assert "2 for 2" not in text
    assert hawk["text"] == ("No action needed — it has completed 2 of its 2 qualified demos "
                            "booked, and $3,671 in projected revenue is already on the books. "
                            "Keep tracking.")
    assert "2 for 2" not in vrr.render(golden)


def test_vendor_names_are_the_sheets_tab_titles_verbatim(store, golden):
    """No case changes, no slug-derived names: exactly the stored tab title."""
    titles = [d["vendor"] for d in snapshots.vendor_sweep("2026-09")["docs"]
              if d["vendor_slug"] != "website"]
    assert sorted(v["name"] for v in golden["vendors"]) == sorted(titles)
    assert {"Hawksem LS Meta", "SaffronEdge LS Meta", "DanteAgency ChatGPT"} <= set(titles)
    untitled = _doc("x")
    untitled["vendor"] = ""
    r = vr.compute(_sweep(untitled), targets=_targets())
    assert r["vendors"][0]["name"].startswith("Untitled tab (gid ")
    assert r["basis_notes"][0]["field"] == "name"


def test_watch_items_and_thesis(golden):
    texts = [w["text"] for w in golden["watch_items"]]
    assert texts[0].startswith("DanteAgency LI Google logged 1 DNC bad lead and 1 demo booked")
    assert "9 of 16 funded vendors show $0 spend so far" in texts[1]
    assert texts[2] == ("Portfolio show rate sits at 14.3% against a 55% target, but on only "
                        "14 qualified demos booked — not yet meaningful.")
    assert golden["notes"]["thesis"]["text"].startswith(
        "First read of the month — $2,737 spent against a $95,600 budget (2.9% utilized).")
    assert golden["small_sample"] is True


def test_new_this_period_diffs_budgets_against_the_previous_months_final_sweep(golden):
    n = golden["new_this_period"]
    assert n["compared_to"] == "2026-08-31"
    changes = {c["vendor"]: (c["previous"], c["budget"]) for c in n["budget_changes"]}
    assert changes["SaffronEdge LS Meta"] == (5000.0, 10000.0)
    assert [p["vendor"] for p in n["paused"]] == ["DanteAgency ChatGPT"]
    assert [z["vendor"] for z in n["still_zero"]] == ["AB Twitter LS"]
    # The EHackers tab was replaced (new gid) AND renamed (+ "LS"): one vendor,
    # said once — not "new" plus "gone".
    assert n["renamed"] == [{"vendor": "EHackers LS Email", "was": "EHackers Email"}]
    assert n["first_seen"] == [] and n["not_in_sweep"] == []
    texts = [x["text"] for x in n["sentences"]]
    assert "EHackers LS Email (was EHackers Email) now has a $1,000 budget." in texts
    assert ("DanteAgency ChatGPT now shows a $0 budget (was $2,000 on Aug 31), consistent "
            "with being paused.") in texts
    # "(New)" is housekeeping, not a rename worth announcing.
    assert not any("(was AB Twitter LS (New))" in t for t in texts)


def test_revenue_is_never_compared_across_months(golden):
    """Month-to-date figures restart on the 1st: last month's month-end revenue
    against this month's first days fires for every vendor that sold anything."""
    n = golden["new_this_period"]
    assert "revenue_rolled_off" not in n
    assert not any("revenue" in s["text"].lower() for s in n["sentences"])
    # Revenue IS read per vendor (month to date, 2026-10-15) — but only this
    # month's: no row of new_this_period carries a revenue figure of any kind.
    for key in ("budget_changes", "newly_funded", "paused", "still_zero", "first_seen"):
        for item in n[key]:
            assert not any("revenue" in k for k in item), (key, item)


def test_vendors_pair_across_months_by_gid_then_title_then_brand_marker():
    prev = _sweep(_doc("Old Name LS Meta", gid=11, budget=1000),
                  _doc("Acme LS Google (New)", gid=12, budget=2000),
                  _doc("Beta Email", gid=13, budget=500),
                  _doc("Gone LS Meta", gid=14, budget=700),
                  date_iso="2026-08-31")
    cur = _sweep(_doc("Renamed LS Meta", gid=11, budget=1000),     # same gid: a rename
                 _doc("Acme LS Google", gid=22, budget=2500),       # new gid, same title
                 _doc("Beta LS Email", gid=23, budget=500),         # adds "LS" only
                 _doc("Brand New LS Meta", gid=24, budget=900))     # genuinely new
    n = vr.compute(cur, targets=_targets(), previous=prev)["new_this_period"]
    # In tab order (same capture time here, so by title).
    assert n["renamed"] == [{"vendor": "Beta LS Email", "was": "Beta Email"},
                            {"vendor": "Renamed LS Meta", "was": "Old Name LS Meta"}]
    assert [(c["vendor"], c["was"]) for c in n["budget_changes"]] == [("Acme LS Google", None)]
    assert [f["vendor"] for f in n["first_seen"]] == ["Brand New LS Meta"]
    assert n["not_in_sweep"] == ["Gone LS Meta"]


def test_a_brand_marker_rename_is_only_paired_when_it_is_unambiguous():
    prev = _sweep(_doc("Acme Email", gid=1), _doc("Acme RA Email", gid=2),
                  date_iso="2026-08-31")
    cur = _sweep(_doc("Acme LS Email", gid=3))
    n = vr.compute(cur, targets=_targets(), previous=prev)["new_this_period"]
    assert n["renamed"] == [{"vendor": "Acme LS Email", "was": "Acme Email"}]
    # Two current tabs could each be "Acme Email" renamed: neither is guessed.
    prev2 = _sweep(_doc("Acme Email", gid=1), date_iso="2026-08-31")
    cur2 = _sweep(_doc("Acme LS Email", gid=3), _doc("Acme RA Email", gid=4))
    n2 = vr.compute(cur2, targets=_targets(), previous=prev2)["new_this_period"]
    assert n2["renamed"] == [] and len(n2["first_seen"]) == 2
    # One current tab that could be either of two old ones: not guessed either.
    prev3 = _sweep(_doc("Acme LS Email", gid=1), _doc("Acme RA Email", gid=2),
                   date_iso="2026-08-31")
    cur3 = _sweep(_doc("Acme Email", gid=3))
    n3 = vr.compute(cur3, targets=_targets(), previous=prev3)["new_this_period"]
    assert n3["renamed"] == [] and n3["first_seen"][0]["vendor"] == "Acme Email"
    assert sorted(n3["not_in_sweep"]) == ["Acme LS Email", "Acme RA Email"]


def test_with_no_previous_month_it_says_so():
    r = vr.compute(_sweep(_doc("Acme LS Meta")), targets=_targets(), previous=None)
    assert [s["text"] for s in r["new_this_period"]["sentences"]] == [
        "No sweep from the previous month to compare against."]


# --- reconciliation ------------------------------------------------------------------

def test_vendor_totals_are_compared_with_the_same_pulls_roll_up_and_the_gap_printed(golden):
    rec = golden["reconciliation"]
    assert rec["available"] and rec["removed"] == ["Website"]
    assert {d["key"]: d["difference"] for d in rec["differences"]} == {
        "total_budget": 1500.0, "qual_demos_booked": 1.0, "demos_booked": 1.0,
        "demos_completed": 1.0}
    assert rec["matches"] is False
    assert ("roll-up minus vendor rows — total budget +$1,500, qual. demos booked +1, "
            "demos booked (all) +1, demos completed +1; every other figure matches.") \
        in golden["notes"]["reconciliation"]
    # The sample claimed a reconciliation against a column this report never
    # parses. The footer states what was actually compared.
    assert "Vendor Total column" not in golden["notes"]["footer"]["text"]


def test_with_no_roll_up_the_report_says_so_rather_than_guessing(store):
    sweep = {**snapshots.vendor_sweep("2026-09"), "rollup": None}
    r = vr.compute(sweep, targets=_targets())
    assert r["reconciliation"] == {
        "available": False,
        "reason": "The roll-up tab wasn't captured in this pull, so vendor totals aren't "
                  "compared against it."}
    assert r["reconciliation"]["reason"] in r["notes"]["footer"]["text"]


# --- absent is not zero ------------------------------------------------------------

def test_a_row_the_tab_does_not_report_is_absent_named_and_never_zero():
    missing_row = _doc("Acme LS Meta", spend=100, leads=2)
    del missing_row["canonical"]["team_overall"]["projected_revenue"]
    r = vr.compute(_sweep(missing_row, _doc("Beta LS Meta", proj=500.0)), targets=_targets())
    assert _by_name(r)["Acme LS Meta"]["projected_revenue"] is None
    assert r["portfolio"]["projected_revenue"] is None   # never the partial 500
    assert {"metric": "projected_revenue", "label": "Projected Revenue",
            "vendors": ["Acme LS Meta"], "reason": "not reported on the vendor tab"} \
        in r["missing"]


def test_missing_figures_are_a_structured_list_with_reasons(golden):
    by_metric = {m["metric"]: m for m in golden["missing"]}
    show = by_metric["show_rate_pct"]
    assert show["label"] == "Show Rate" and show["reason"] == "no qualified demos booked yet"
    assert len(show["vendors"]) == 11
    assert len(by_metric["ql_ratio_pct"]["vendors"]) == 11
    assert by_metric["budget_utilized_pct"]["vendors"] == ["DanteAgency ChatGPT",
                                                          "AB Twitter LS"]
    # A $0 budget gives no utilization — an em-dash, not 0%.
    assert _by_name(golden)["AB Twitter LS"]["budget_utilized_pct"] is None


def test_the_report_is_deterministic_and_json_safe(store):
    sweep = snapshots.vendor_sweep("2026-09")
    prev = snapshots.previous_month_sweep("2026-09")
    a = vr.compute(copy.deepcopy(sweep), targets=_targets(), previous=prev)
    b = vr.compute(copy.deepcopy(sweep), targets=_targets(), previous=prev)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert vrr.render(a) == vrr.render(b)


# --- render -------------------------------------------------------------------------

def test_the_document_is_self_contained_and_print_ready(golden):
    html = vrr.render(golden)
    lowered = html.lower()
    for banned in ("<script", "<link", "@import", "http://", "https://", "<img", "<iframe"):
        assert banned not in lowered, banned
    assert not re.search(r"url\((?!data:)", html), "a url() that is not an embedded data: URI"
    assert "print-color-adjust:exact" in html and "@page" in html
    assert "tr{break-inside:avoid" in html
    assert html.count("<svg") == 5   # movers, budget/spend, demos, 2 x channel
    assert "September <em>2</em>" in html
    # A chart card or a standouts/watch card never splits from its title in print.
    assert html.count('class="panel full chartcard"') == 3
    assert html.count('class="panel chartcard"') == 2
    assert ".chartcard,.ins .col{break-inside:avoid;page-break-inside:avoid}" in html


def test_a_missing_figure_is_a_plain_muted_dash_not_a_decorated_marker(golden):
    html = vrr.render(golden)
    assert 'class="absent"' not in html          # the gold dotted-underline marker
    assert html.count('<span class="dash" title="No figure:') > 10
    assert ".dash{color:var(--muted);font-weight:400;text-decoration:none;border:0" in html


def test_left_out_tabs_collapse_to_one_sentence_per_reason(golden):
    assert golden["notes"]["left_out"] == [
        "8 captures from earlier in the day were left out because those tabs were later "
        "renamed or removed: AB Twitter LS (New), Axenic LS Meta (New), Copy of Flytech Meta "
        "LS, Copy of Meta 360 LS, DanteAgency LI Googlex, EHackers Email, Elevate MKT LS "
        "Emailo, Rendition LS Email (New).",
        "Website: non-media tab (organic/website) — kept out of paid vendor figures.",
    ]
    assert "written by an earlier capture run" not in vrr.render(golden)


def test_the_default_layout_is_the_samples_eight_numbered_sections_in_order(golden):
    html = vrr.render(golden)
    titles = re.findall(r'<span class="num">(\d\d)</span><h2>([^<]+)</h2>', html)
    assert [t[1] for t in titles] == [
        "Portfolio at a glance", "Biggest movers vs. benchmark",
        "Budget allocation vs. spend so far", "Demos booked vs. completed",
        "Spend &amp; projected revenue by channel — September only", "Vendor scorecard",
        "What&#x27;s working / what needs attention", "Action summary"]
    assert [t[0] for t in titles] == [f"{i:02d}" for i in range(1, 9)]
    for pill in ('pill strong">Strong Start', 'pill early">Too Early', 'pill check">Check In'):
        assert pill in html


def test_every_dynamic_string_is_escaped():
    r = vr.compute(_sweep(_doc("<script>alert(1)</script> LS Meta", spend=50, leads=1)),
                   targets=_targets())
    html = vrr.render(r)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; LS Meta" in html


def test_a_report_from_another_generator_is_refused_not_half_rendered(golden):
    with pytest.raises(ValueError, match="mr-vendor-report/1"):
        vrr.render({**golden, "generator": "mr-vendor-report/0"})


# --- the template seam -------------------------------------------------------------

def test_the_registry_is_the_placeholder_vocabulary(golden):
    vocab = vrr.placeholder_vocabulary()
    for block in ("{{chart:benchmark_movers}}", "{{table:vendor_scorecard}}",
                  "{{list:watch_items}}", "{{table:action_summary}}",
                  "{{list:standouts}}", "{{tiles:portfolio_glance}}"):
        assert block in vocab["blocks"], block
    assert vocab["scalars"]["total_spend"] == {"label": "Total Spend", "format": "money"}
    assert vrr.render_scalar(golden, "total_spend") == "$2,737"
    assert vrr.render_scalar(golden, "show_rate_pct") == "14.3%"
    # Every registered section renders on its own — a placeholder is a lookup.
    for entry in vrr.SECTION_REGISTRY.values():
        assert vrr.render_block(golden, vrr.SectionSpec(entry.type)), entry.type
    with pytest.raises(KeyError):
        vrr.render_scalar(golden, "nope")


@pytest.mark.parametrize("spec, fragment", [
    ({"sections": [{"type": "pie_chart"}]}, "unknown section type"),
    ({"sections": [{"type": "header", "options": {"colour": "red"}}]}, "does not take"),
    ({"sections": [{"type": "benchmark_movers", "options": {"metrics": ["total_leads"]}}]},
     "does not accept"),
    ({"sections": [{"type": "vendor_scorecard", "options": {"columns": ["roas"]}}]},
     "unknown scorecard column"),
    ({"theme": {"colors": {"chartreuse": "#0f0"}}, "sections": [{"type": "header"}]},
     "unknown theme colour"),
    ({"sections": []}, "at least one section"),
    # A layout can arrive from a client (the template save route), and theme
    # values are written verbatim into <style> and SVG fill="…": refused, not
    # escaped somewhere downstream.
    ({"theme": {"colors": {"ink": "#000}</style><script>alert(1)</script>"}},
      "sections": [{"type": "header"}]}, "must be a colour"),
    ({"theme": {"colors": {"gold": "url(https://evil.example/x)"}},
      "sections": [{"type": "header"}]}, "must be a colour"),
    ({"theme": {"colors": {"pos": 7}}, "sections": [{"type": "header"}]}, "must be a colour"),
    ({"theme": {"serif": "x;}</style><script>"}, "sections": [{"type": "header"}]},
     "plain font list"),
    ({"sections": [{"type": "demos_by_vendor", "options": {"min_booked": "lots"}}]},
     "wrong type"),
    ({"sections": [{"type": "action_summary", "options": {"include_catch_all": "yes"}}]},
     "wrong type"),
    ({"sections": [{"type": "header", "title": {"x": 1}}]}, "title must be text"),
    ({"sections": [{"type": "header"}] * 41}, "at most 40"),
    ({"sections": [{"title": "no type"}]}, "must be an object with a 'type'"),
    ({"sections": "header"}, "must be a list"),
    (["header"], "must be an object"),
])
def test_a_layout_the_registry_cannot_honour_is_refused_before_rendering(spec, fragment):
    with pytest.raises(ValueError, match=fragment):
        vrr.layout_from_dict(spec)


def test_a_layout_round_trips_and_reorders_and_retheme(golden):
    assert vrr.render(golden, vrr.layout_from_dict(vrr.layout_to_dict(vrr.DEFAULT_LAYOUT))) \
        == vrr.render(golden)
    custom = vrr.layout_from_dict({
        "theme": {"colors": {"ink": "#000000"}},
        "sections": [{"type": "benchmark_movers", "title": "Against target"},
                     {"type": "action_summary", "options": {"include_catch_all": False}}],
    })
    html = vrr.render(golden, custom)
    assert re.findall(r'<span class="num">(\d\d)</span><h2>([^<]+)</h2>', html) == [
        ("01", "Against target"), ("02", "Action summary")]
    assert "--ink:#000000" in html
    assert "All other vendors" not in html


def test_the_builtin_template_is_the_default_layout_by_definition(golden):
    from marketing_research_agent import report_templates as rt

    assert runs.builtin_template()["spec"] is None
    assert rt.render_with_template(golden, runs.builtin_template()) == vrr.render(golden)
    assert rt.render_with_template(golden, {"kind": "builtin"}) == vrr.render(golden)


@pytest.mark.parametrize("template, label", [
    (None, "built-in"),
    ({"kind": "builtin", "number": None, "id": None}, "built-in"),
    ({"kind": "layout", "number": 5, "id": "t5"}, "team template, version 5"),
    ({"kind": "html", "number": 2, "id": "t2"}, "team template, version 2"),
    ({"kind": "builtin", "fallback": {"number": 3, "id": "t3", "reason": "x"}},
     "built-in, because the team template (version 3) failed"),
    ({"kind": "builtin", "override_of": {"number": 3, "id": "t3"}},
     "built-in, chosen instead of the team template (version 3)"),
    ({"kind": "preview"}, "a preview of an unsaved template"),
])
def test_the_template_label_is_a_real_name_never_version_none(template, label):
    assert vrr.template_label(template) == label


# --- build: persistence, idempotency, template, empty month ----------------------------

def test_build_persists_a_run_with_its_metadata_and_is_idempotent(store):
    first = vr.build(workspace_id="ws-1", year_month="2026-09")
    assert first["reused"] is False
    assert first["kind"] == "vendor_report" and first["user_id"] == "ws-1"
    assert first["sweep_date"] == "2026-09-02" and first["built_at"]
    assert first["template"] == {"kind": "builtin", "number": None, "id": None}
    assert first["structured"]["build"]["template"] == first["template"]
    assert first["ai"] is False and "no model" in first["fallback_reason"]
    again = vr.build(workspace_id="ws-1", year_month="2026-09")
    assert again["reused"] is True and again["id"] == first["id"]
    assert [r["id"] for r in runs.list_runs("ws-1", kind="vendor_report")] == [first["id"]]
    # Another workspace never gets this workspace's run served.
    other = vr.build(workspace_id="ws-2", year_month="2026-09")
    assert other["id"] != first["id"] and other["reused"] is False


def test_a_target_edit_re_derives_instead_of_serving_the_stale_run(store):
    first = vr.build(workspace_id="ws-1", year_month="2026-09")
    goals.set_targets("ws-1", {"thresholds": {"cost_per_lead_ceiling": 250}})
    second = vr.build(workspace_id="ws-1", year_month="2026-09")
    assert second["id"] != first["id"]
    cpl = next(m for m in second["structured"]["movers"] if m["key"] == "cost_per_lead")
    assert cpl["target"] == 250.0


def test_an_empty_month_is_refused_with_the_specs_message(store):
    with pytest.raises(vr.EmptyMonth) as exc:
        vr.build(workspace_id="ws-1", year_month="2026-05")
    assert str(exc.value) == ("The last pull has no vendor figures for May 2026. Pull the "
                              "workbook, then build again.")


def _save_layout(ws, sections, who="a@x.com"):
    return runs.save_template_version(ws, uploaded_by=who, source_kind="builder",
                                      spec={"sections": sections})


def test_a_template_name_other_than_builtin_is_refused(store):
    with pytest.raises(vr.TemplateRefused) as exc:
        vr.build(workspace_id="ws-1", year_month="2026-09", template="v7")
    assert exc.value.code == "invalid_template"
    with pytest.raises(vr.EmptyMonth) as exc:
        vr.build(workspace_id="ws-1", year_month="2026-05")
    assert exc.value.code == "empty_month"


def test_the_active_team_template_is_used_only_while_templates_are_on(store):
    v = _save_layout("ws-1", [{"type": "header"}, {"type": "vendor_scorecard"}])
    off = vr.build(workspace_id="ws-1", year_month="2026-09")
    assert off["template"] == {"kind": "builtin", "number": None, "id": None}
    on = vr.build(workspace_id="ws-1", year_month="2026-09", use_templates=True)
    assert on["template"] == {"kind": "layout", "number": 1, "id": v["id"]}
    assert on["id"] != off["id"]                                # a different document
    again = vr.build(workspace_id="ws-1", year_month="2026-09", use_templates=True)
    assert again["reused"] is True and again["id"] == on["id"]
    # Another workspace's template is never this workspace's.
    other = vr.build(workspace_id="ws-2", year_month="2026-09", use_templates=True)
    assert other["template"]["kind"] == "builtin"


def test_a_team_template_that_cannot_render_saves_nothing_and_says_why(store, monkeypatch):
    from marketing_research_agent import report_templates as rt

    v = _save_layout("ws-1", [{"type": "header"}])

    def broken(report, version):
        raise rt.TemplateRenderError("The team template broke on this month.")

    monkeypatch.setattr(rt, "render_with_template", broken)
    with pytest.raises(vr.TemplateFailed) as exc:
        vr.build(workspace_id="ws-1", year_month="2026-09", use_templates=True)
    assert exc.value.code == "template_failed"
    assert exc.value.reason == "The team template broke on this month."
    assert exc.value.template == {"kind": "layout", "number": 1, "id": v["id"]}
    assert runs.list_runs("ws-1", kind="vendor_report") == []    # nothing saved


def test_the_builtin_after_a_team_failure_says_so_in_the_report(store, monkeypatch):
    from marketing_research_agent import report_templates as rt

    v = _save_layout("ws-1", [{"type": "header"}])
    real = rt.render_with_template

    def team_breaks(report, version):
        if version.get("id") == v["id"]:
            raise rt.TemplateRenderError("The team template broke on this month.")
        return real(report, version)

    monkeypatch.setattr(rt, "render_with_template", team_breaks)
    run = vr.build(workspace_id="ws-1", year_month="2026-09", template="builtin",
                   use_templates=True)
    assert run["template"] == {"kind": "builtin", "number": None, "id": None,
                               "fallback": {"number": 1, "id": v["id"],
                                            "reason": "The team template broke on this month."}}
    html = vrr.render(run["structured"])
    assert html.count(vrr.FALLBACK_SENTENCE) == 2                # header band and footer
    assert "Template: built-in, because the team template (version 1) failed." in html
    assert "The team template broke on this month." in html


def test_choosing_the_builtin_over_a_working_team_template_is_not_called_a_failure(store):
    v = _save_layout("ws-1", [{"type": "header"}])
    run = vr.build(workspace_id="ws-1", year_month="2026-09", template="builtin",
                   use_templates=True)
    assert run["template"]["override_of"] == {"number": 1, "id": v["id"]}
    assert "fallback" not in run["template"]
    html = vrr.render(run["structured"])
    assert vrr.FALLBACK_SENTENCE not in html
    assert "Template: built-in, chosen instead of the team template (version 1)." in html


def test_a_preview_report_is_computed_never_stored(store):
    report = vr.preview_report(workspace_id="ws-1")
    assert report["year_month"] == "2026-09"
    assert report["build"]["template"] == {"kind": "preview"}
    assert runs.list_runs("ws-1", kind="vendor_report") == []
    assert vr.preview_report(workspace_id="ws-1", year_month="2026-05") is None


def test_periods_lists_the_months_with_a_sweep(store):
    assert vr.periods() == [{"year_month": "2026-09", "label": "September 2026"},
                            {"year_month": "2026-08", "label": "August 2026"}]


# --- template coverage (2026-10-15): investment basis, pace, revenue, lead status ------
# The owner's rule: if the tracker has the figure, fill it; if not, flag it.
# Every new figure below is pinned on the golden 2026-09-02 sweep AND on the
# absent-is-not-zero path.

NEW_SCALARS = ("total_management_fees", "total_spend_investment", "cost_per_lead_investment",
               "cost_per_qualified_lead_investment", "cost_per_demo_completed_investment",
               "pace_pct", "pace_gap_pct", "projected_runout_date", "actualized_revenue",
               "actualized_clients", "not_actualized_revenue", "roas_pct")
NEW_BLOCKS = ("lead_status_by_vendor", "channel_benchmarks", "vendor_investment", "vendor_detail")


def _lead_vendor(campaign: str, **counts) -> dict:
    """One lead-sheet vendor bucket as ``lead_analysis.summarize`` writes it."""
    base = {"booked": 0, "completed": 0, "no_show": 0, "canceled": 0, "bad_lead": 0,
            "pending": 0, "completed_dnc": 0, "deal_stages": {}}
    base.update(counts)
    return {"campaign": campaign, "slug": snapshots.slugify(campaign), **base}


LEAD_SUMMARY = {"months": {"2026-09": {"vendors": [
    # "ElevateMKT" (lead sheet) vs the tab "Elevate MKT LS Email": joins only
    # with separators removed.
    _lead_vendor("ElevateMKT LS Email", booked=5, completed=2, no_show=1, canceled=1,
                 pending=1, completed_dnc=1,
                 deal_stages={"contract_sent": 1, "hot_leads": 1, "lost_dnc": 1, "none": 2}),
    _lead_vendor("Hawksem LS Meta", booked=2, completed=2,
                 deal_stages={"contract_sent": 2}),
    _lead_vendor("Mystery Co", booked=1, no_show=1),          # no such tab
]}}}


def test_the_golden_management_fees_are_the_seven_vendors_figures_summed(golden):
    fees = {v["name"]: v["management_fee"] for v in golden["vendors"]}
    assert {n: f for n, f in fees.items() if f} == {
        "Hawksem LS Google": 2200.0, "Hawksem LS Meta": 2200.0,
        "DanteAgency LI Google": 1700.0, "Advantech RA Google": 1200.0,
        "SaffronEdge LS Meta": 1000.0, "EHackers LS Email": 1000.0,
        "DanteAgency ChatGPT": 300.0}
    assert all(f is not None for f in fees.values())    # every final tab carries the row
    p = golden["portfolio"]
    assert p["total_management_fees"] == 9600.0
    assert p["total_spend_investment"] == 12336.93
    assert p["cost_per_lead_investment"] == round(12336.93 / p["total_leads"], 2)
    assert p["cost_per_qualified_lead_investment"] == round(12336.93 / p["qualified_leads"], 2)
    assert p["cost_per_demo_completed_investment"] == round(12336.93 / p["demos_completed"], 2)
    assert not any(m["metric"] == "total_management_fees" for m in golden["missing"])
    hawk = _by_name(golden)["Hawksem LS Meta"]
    assert (hawk["budget_investment"], hawk["spend_investment"]) == (7200.0, 2543.72)
    assert hawk["cost_per_lead_investment"] == round(2543.72 / hawk["leads"], 2)
    # The Performance figures the published report rests on did not move.
    assert (p["total_budget"], p["total_spend"], p["total_leads"]) == (95600.0, 2736.93, 18.0)


def test_a_fee_total_nobody_reports_is_none_and_a_partial_sum_names_who_is_left_out():
    r = vr.compute(_sweep(_doc("A LS Meta"), _doc("B LS Meta")), targets=_targets())
    assert r["portfolio"]["total_management_fees"] is None
    assert {"metric": "total_management_fees", "label": "Total Management Fees",
            "vendors": "portfolio", "reason": "no vendor tab reports a management fee"} \
        in r["missing"]
    assert "&#8212;" in vrr.render_scalar(r, "total_management_fees")
    r = vr.compute(_sweep(_doc("A LS Meta", fee=500.0), _doc("B LS Meta")), targets=_targets())
    assert r["portfolio"]["total_management_fees"] == 500.0     # those who report it
    m = next(m for m in r["missing"] if m["metric"] == "total_management_fees")
    assert m["vendors"] == ["B LS Meta"] and "sum of the vendors that report it" in m["reason"]
    # A tab that reports a $0 fee reports a figure: counted, not treated as absent.
    r = vr.compute(_sweep(_doc("A LS Meta", fee=0.0), _doc("B LS Meta", fee=250.0)),
                   targets=_targets())
    assert r["portfolio"]["total_management_fees"] == 250.0
    assert not any(m["metric"] == "total_management_fees" for m in r["missing"])


def test_the_investment_column_is_read_as_is_with_no_fallback_and_its_ratios_recomputed():
    doc = _doc("A LS Meta", budget=5000, spend=400, leads=4, ql=2, completed=1, fee=1000,
               inv_budget=6000, inv_spend=1400)
    r = vr.compute(_sweep(doc), targets=_targets())
    v = _by_name(r)["A LS Meta"]
    assert (v["budget"], v["spend"]) == (5000.0, 400.0)                 # Performance, unchanged
    assert (v["budget_investment"], v["spend_investment"], v["management_fee"]) == (
        6000.0, 1400.0, 1000.0)
    assert (v["cost_per_lead_investment"], v["cost_per_qualified_lead_investment"],
            v["cost_per_demo_completed_investment"]) == (350.0, 700.0, 1400.0)
    assert (v["cost_per_lead"], v["cost_per_qualified_lead"],
            v["cost_per_demo_completed"]) == (100.0, 200.0, 400.0)
    assert (r["portfolio"]["total_spend_investment"],
            r["portfolio"]["cost_per_lead_investment"]) == (1400.0, 350.0)
    # A blank Investment cell is absent, never back-filled from Performance.
    blank = _doc("B LS Meta", spend=100, leads=1)
    blank["canonical"]["team_overall"]["spend"]["investment"] = None
    r = vr.compute(_sweep(blank), targets=_targets())
    v = _by_name(r)["B LS Meta"]
    assert v["spend"] == 100.0 and v["spend_investment"] is None
    assert v["cost_per_lead_investment"] is None
    assert r["portfolio"]["total_spend_investment"] is None
    assert {"metric": "total_spend_investment", "label": "Total Spend incl. Fees",
            "vendors": ["B LS Meta"], "reason": "not reported on the vendor tab"} in r["missing"]


def test_pace_is_the_day_over_the_month_and_run_out_is_a_date_only_inside_the_month(golden):
    p = golden["portfolio"]
    assert (golden["day_of_month"], golden["days_in_month"]) == (2, 30)
    assert p["pace_pct"] == 6.67
    assert p["pace_gap_pct"] == round(p["budget_utilized_pct"] - 6.67, 2) == -3.81
    assert p["projected_runout_date"] is None     # $2,737 in 2 days vs $95,600 lasts past Sep 30
    assert any(m["metric"] == "projected_runout_date" and "lasts past month end" in m["reason"]
               for m in golden["missing"])
    by = _by_name(golden)
    assert all(v["pace_pct"] == 6.67 for v in golden["vendors"])
    assert by["Hawksem LS Meta"]["projected_runout_date"] == "2026-09-29"  # $343.72 / 2 days
    assert by["Anushka LS Meta"]["projected_runout_date"] == "2026-09-19"
    assert by["Hawksem LS Google"]["projected_runout_date"] is None       # $0 spent: no rate
    assert by["AB Twitter LS"]["pace_gap_pct"] is None                    # $0 budget: no util.
    # Synthetic: day 10, $900 of $1,000 at $90/day runs out on the 11th; already
    # over budget gives the (past) day it ran out; no spend gives nothing.
    r = vr.compute(_sweep(_doc("A LS Meta", budget=1000, spend=900),
                          _doc("B LS Meta", budget=1000, spend=1200),
                          _doc("C LS Meta", budget=1000, spend=0)), targets=_targets())
    by = _by_name(r)
    assert by["A LS Meta"]["projected_runout_date"] == "2026-09-11"
    assert by["A LS Meta"]["pace_gap_pct"] == round(90.0 - round(10 / 30 * 100, 2), 2)
    assert by["B LS Meta"]["projected_runout_date"] == "2026-09-08"
    assert by["C LS Meta"]["projected_runout_date"] is None
    # Portfolio: $2,100 of $3,000 at $210/day leaves $900, 4 more days: the 14th.
    assert vrr.render_scalar(r, "projected_runout_date") == "Sep 14"
    assert vr.fmt("2026-09-29", vr.DATE) == "Sep 29" and vr.fmt(None, vr.DATE) == "—"


def test_revenue_is_the_trackers_actualized_blocks_and_roas_is_recomputed_as_the_tab_does(
        store, golden):
    p = golden["portfolio"]
    assert (p["actualized_revenue"], p["actualized_clients"],
            p["not_actualized_revenue"]) == (0.0, 0.0, 3671.0)
    assert p["roas_pct"] == 0.0      # $0 actualized over $2,737 spent: a reported zero
    assert _by_name(golden)["Hawksem LS Meta"]["not_actualized_revenue"] == 3671.0
    # The formula is the tracker's own: on every 2026-08-31 tab that prints a
    # ROAS, the recomputed figure IS that figure (actualized revenue / spend).
    checked = 0
    for doc in snapshots.previous_month_sweep("2026-09")["docs"]:
        sheet = (((doc.get("canonical") or {}).get("team_overall") or {}).get("kpis") or {}
                 ).get("roas_pct")
        if sheet:
            row = vr._vendor_row(doc, [])
            assert row["roas_pct"] == sheet == round(
                row["actualized_revenue"] / row["spend"] * 100, 2), doc["vendor"]
            checked += 1
    assert checked >= 4
    # No spend means no ROAS (never 0%); revenue with a clients row is read too.
    r = vr.compute(_sweep(_doc("A LS Meta", actual=500.0, clients=1, not_actual=900.0)),
                   targets=_targets())
    v = _by_name(r)["A LS Meta"]
    assert (v["actualized_revenue"], v["actualized_clients"], v["not_actualized_revenue"]) == (
        500.0, 1.0, 900.0)
    assert v["roas_pct"] is None and r["portfolio"]["roas_pct"] is None
    assert any(m["metric"] == "roas_pct" and m["reason"] == "no spend yet" for m in r["missing"])


def test_lead_status_joins_by_slug_exact_first_then_with_separators_removed(store):
    sweep = snapshots.vendor_sweep("2026-09")
    r = vr.compute(sweep, targets=_targets(), lead_summary=LEAD_SUMMARY,
                   lead_as_of="2026-09-02T10:00:00+00:00")
    ls = r["lead_status"]
    assert ls["available"] is True and ls["reason"] is None
    assert ls["as_of"] == "2026-09-02T10:00:00+00:00"
    assert [v["vendor"] for v in ls["vendors"]] == ["Hawksem LS Meta", "Elevate MKT LS Email"]
    elevate = _by_name(r)["Elevate MKT LS Email"]["lead_status"]
    assert elevate == {"campaign": "ElevateMKT LS Email", "contract_sent": 1,
                       "hot_lead_follow_up": 1, "completed_dnc": 1, "scheduled": 1,
                       "no_show": 1, "canceled": 1, "bad_lead": 0, "completed": 2, "total": 5}
    assert _by_name(r)["Hawksem LS Meta"]["lead_status"]["contract_sent"] == 2
    assert _by_name(r)["Meta 360 RA"]["lead_status"] is None       # no campaign for it
    assert ls["unmatched_campaigns"] == ["Mystery Co"]
    assert ls["totals"]["total"] == 7 and ls["totals"]["completed_dnc"] == 1
    # Exact beats compact: with both spellings on the sheet the exact one joins
    # and the other is listed, not guessed onto the same tab.
    both = {"months": {"2026-09": {"vendors": [
        _lead_vendor("ElevateMKT LS Email", booked=9),
        _lead_vendor("Elevate MKT LS Email", booked=1)]}}}
    r = vr.compute(sweep, targets=_targets(), lead_summary=both)
    assert _by_name(r)["Elevate MKT LS Email"]["lead_status"]["total"] == 1
    assert r["lead_status"]["unmatched_campaigns"] == ["ElevateMKT LS Email"]
    # The month's vendor list on its own is accepted too.
    r = vr.compute(sweep, targets=_targets(),
                   lead_summary=LEAD_SUMMARY["months"]["2026-09"]["vendors"])
    assert r["lead_status"]["available"] is True


def test_without_lead_data_the_report_says_so_and_never_zero_fills(store):
    sweep = snapshots.vendor_sweep("2026-09")
    none = vr.compute(sweep, targets=_targets())
    assert none["lead_status"] == {
        "available": False, "as_of": None, "vendors": [], "unmatched_campaigns": [],
        "totals": None,
        "reason": "No lead-status data: no lead-analysis sheet has been pulled yet."}
    assert all(v["lead_status"] is None for v in none["vendors"])
    other_month = vr.compute(sweep, targets=_targets(),
                             lead_summary={"months": {"2026-08": {"vendors": []}}})
    assert other_month["lead_status"]["available"] is False
    assert "September 2026" in other_month["lead_status"]["reason"]
    no_match = vr.compute(sweep, targets=_targets(),
                          lead_summary={"months": {"2026-09": {"vendors": [
                              _lead_vendor("Mystery Co", booked=3)]}}})
    assert no_match["lead_status"]["available"] is False
    assert no_match["lead_status"]["unmatched_campaigns"] == ["Mystery Co"]
    assert "matched a vendor tab" in no_match["lead_status"]["reason"]


def test_the_new_blocks_render_with_data_and_say_so_without(store):
    sweep = snapshots.vendor_sweep("2026-09")
    without = vr.compute(sweep, targets=_targets())
    with_ = vr.compute(sweep, targets=_targets(), lead_summary=LEAD_SUMMARY,
                       lead_as_of="2026-09-02T10:00:00+00:00")

    html = vrr.render_block(without, vrr.SectionSpec("lead_status_by_vendor"))
    assert "no lead-analysis sheet has been pulled yet" in html and "<table" not in html
    html = vrr.render_block(with_, vrr.SectionSpec("lead_status_by_vendor"))
    assert "<th>Completed → DNC</th>" in html and "<th>Scheduled</th>" in html
    assert "<td>Elevate MKT LS Email</td>" in html
    assert "pulled 2026-09-02" in html
    assert "Lead-sheet campaigns with no vendor tab this month: Mystery Co." in html
    assert "Matched vendors" in html and "$0" not in html

    html = vrr.render_block(with_, vrr.SectionSpec("channel_benchmarks"))
    assert "<td>Meta</td><td>$400&#8211;$550</td><td>$700&#8211;$850</td><td>55.0%</td>" in html
    assert "<td>Total</td>" in html and "Cost per lead ceiling $220" in html
    assert "bad-lead rate 30.0%" in html

    html = vrr.render_block(with_, vrr.SectionSpec("vendor_investment"))
    assert "<th>Mgmt fee</th>" in html and "<th>Spend incl. fees</th>" in html
    assert ("<td>Hawksem LS Meta</td><td>Meta</td><td>$5,000</td><td>$344</td><td>$2,200</td>"
            "<td>$2,544</td>") in html
    assert '<td class="down">$1,272</td>' in html          # CPL incl. fees vs the $220 ceiling
    assert ("<td>Portfolio total</td><td></td><td>$95,600</td><td>$2,737</td><td>$9,600</td>"
            in html)
    assert html.count('<span class="dash"') >= 10              # unfunded vendors: em-dash
    sorted_ = vrr.render_block(with_, vrr.SectionSpec("vendor_investment",
                                                      options={"sort": "spend"}))
    assert sorted_.index("Hawksem LS Meta") < sorted_.index("Meta 360 RA")

    cards = vrr.render_block(with_, vrr.SectionSpec("vendor_detail"))
    assert cards.count('class="vcard"') == 18
    for pill in ('pill strong">Strong Start', 'pill early">Too Early', 'pill check">Check In'):
        assert pill in cards
    assert 'class="tick" style="left:6.67%"' in cards           # the pace tick
    assert ("Lead status (5 demos): 1 contract sent · 1 hot lead follow-up · "
            "1 completed → dnc") in cards
    assert cards.count("no lead-sheet campaign matched this tab") == 16
    assert "runs out Sep 29 at this pace" in cards
    assert "Keep tracking." in cards                            # the rule-based action, verbatim
    assert "<small>ceiling $850</small>" in cards and "<small>target 55.0%</small>" in cards
    assert vrr.render_block(without, vrr.SectionSpec("vendor_detail")).count(
        "Lead status: No lead-status data") == 18

    empty = vr.compute(_sweep(), targets=_targets())
    assert "No paid vendor tabs in this pull." in vrr.render_block(
        empty, vrr.SectionSpec("vendor_detail"))

    full = vrr.render(with_, vrr.layout_from_dict({"sections": [{"type": t} for t in NEW_BLOCKS]}))
    lowered = full.lower()
    for banned in ("<script", "<link", "@import", "http://", "https://", "<img", "<iframe"):
        assert banned not in lowered, banned
    hostile = vr.compute(_sweep(_doc("<script>x</script> LS Meta", spend=1, leads=1)),
                         targets=_targets(),
                         lead_summary=[_lead_vendor("<script>x</script> LS Meta", booked=1)])
    assert "<script>" not in vrr.render_block(hostile, vrr.SectionSpec("vendor_detail"))
    assert "<script>" not in vrr.render_block(hostile, vrr.SectionSpec("lead_status_by_vendor"))


def test_a_stored_v1_run_still_renders_with_em_dashes_where_the_new_fields_would_be(golden):
    old = copy.deepcopy(golden)
    old["generator"] = "mr-vendor-report/1"
    for key in ("benchmarks", "lead_status"):
        del old[key]
    for key in NEW_SCALARS:
        del old["portfolio"][key]
    for v in old["vendors"]:
        for key in ("management_fee", "budget_investment", "spend_investment", "benchmarks",
                    "lead_status", "pace_pct", "pace_gap_pct", "projected_runout_date",
                    "actualized_revenue", "actualized_clients", "not_actualized_revenue",
                    *vr._VENDOR_COST_RATIOS):
            v.pop(key, None)
    assert vrr.render(old)                                     # the built-in layout renders
    html = vrr.render(old, vrr.layout_from_dict({"sections": [{"type": t} for t in NEW_BLOCKS]}))
    assert "Benchmarks were not recorded in this report" in html
    assert "No lead-status data for this month." in html
    assert html.count('class="vcard"') == 18
    assert "&#8212;" in vrr.render_scalar(old, "total_management_fees")
    assert "&#8212;" in vrr.render_scalar(old, "projected_runout_date")
    with pytest.raises(ValueError, match="mr-vendor-report/1"):
        vrr.render({**golden, "generator": "mr-vendor-report/0"})


def test_the_new_figures_and_blocks_are_catalog_entries_so_templates_get_them_for_free():
    kinds = {"total_management_fees": vr.MONEY, "total_spend_investment": vr.MONEY,
             "cost_per_lead_investment": vr.MONEY, "pace_pct": vr.PCT, "pace_gap_pct": vr.PCT,
             "projected_runout_date": vr.DATE, "actualized_revenue": vr.MONEY,
             "actualized_clients": vr.INT, "not_actualized_revenue": vr.MONEY,
             "roas_pct": vr.PCT}
    for key, kind in kinds.items():
        assert vr.METRICS[key][1] == kind, key
    vocab = vrr.placeholder_vocabulary()
    assert set(NEW_SCALARS) <= set(vocab["scalars"])
    assert {"{{table:lead_status_by_vendor}}", "{{table:channel_benchmarks}}",
            "{{table:vendor_investment}}", "{{cards:vendor_detail}}"} <= set(vocab["blocks"])
    assert vocab["blocks"]["{{cards:vendor_detail}}"]["kind"] == "cards"
    # The pills are the three the report already had; nothing relabelled.
    assert vr.STATUSES == ("Strong Start", "Too Early", "Check In")


def test_a_fresh_lead_sheet_pull_re_derives_instead_of_serving_the_stale_join(store):
    first = vr.build(workspace_id="ws-1", year_month="2026-09")
    assert first["structured"]["lead_status"]["available"] is False
    lead_run = {"id": "lr1", "kind": "lead_analysis", "user_id": "ws-1",
                "generated_at": "2026-09-02T10:00:00+00:00", "summary": LEAD_SUMMARY}
    second = vr.build(workspace_id="ws-1", year_month="2026-09", lead_run=lead_run)
    assert second["id"] != first["id"] and second["reused"] is False
    assert second["structured"]["lead_status"]["available"] is True
    assert second["structured"]["lead_status"]["as_of"] == "2026-09-02T10:00:00+00:00"
    again = vr.build(workspace_id="ws-1", year_month="2026-09", lead_run=lead_run)
    assert again["reused"] is True and again["id"] == second["id"]
    newer = {**lead_run, "id": "lr2", "generated_at": "2026-09-03T10:00:00+00:00"}
    third = vr.build(workspace_id="ws-1", year_month="2026-09", lead_run=newer)
    assert third["id"] != second["id"]
    preview = vr.preview_report(workspace_id="ws-1", lead_run=lead_run)
    assert preview["lead_status"]["available"] is True
    assert len(runs.list_runs("ws-1", kind="vendor_report")) == 3
