from marketing_research_agent import snapshots


def _snap(slug, d="2026-07-09", budget=10000.0, spend=4000.0, leads=10, q=2,
          qdb=8, comp=3, sold=1, vendor=None):
    return {"vendor": vendor or slug, "vendor_slug": slug, "gid": 1, "date": d,
            "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {
                "budget": {"performance": budget, "investment": None},
                "spend": {"performance": spend, "investment": spend + 999},
                "leads": {"total": leads, "qualified": q},
                "demos": {"qualified_booked_all": qdb, "total_booked_all": qdb,
                          "completed_all": comp},
                "actualized_revenue": {"services_sold": sold},
            }, "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_renamed_tab_is_not_counted_twice(monkeypatch, tmp_path):
    """Regression: renaming a tab ("DrivGen LS Email" -> "… (Offboarded)") mints
    a second slug. The old one stops being captured but its final snapshot never
    expires, so the vendor was summed into every later bar for ever."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("drivgen-ls-email", d="2026-07-21", spend=500.0))
    snapshots.save_snapshot(_snap("drivgen-ls-email-offboarded", d="2026-07-27", spend=500.0))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-27", spend=1000.0))
    p = snapshots.portfolio()
    assert p["vendors"] == 2
    assert p["computed_spend"] == 1500.0            # not 2000 — DrivGen once
    assert p["vendors_excluded"] == ["drivgen-ls-email"]   # named, not silent


def test_a_genuine_zero_in_the_rollup_is_not_replaced_by_the_vendor_sum(monkeypatch, tmp_path):
    """Each official field used to fall back on any falsy value, so a month the
    sheet honestly reports as 0 came back as the vendor sum — and the bar then
    divided the roll-up's spend by the vendor sum's counts, printing a cost-per
    figure that exists on no cell of the sheet."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", leads=40, q=25, comp=9))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", spend=9000.0,
                                  budget=12000.0, leads=0, q=0, qdb=0, comp=0, sold=0))
    p = snapshots.portfolio()
    assert p["source"] == "sheet_overall"
    assert (p["leads"], p["qualified_leads"], p["demos_completed"]) == (0, 0, 0)
    assert p["cost_per_qualified_lead"] is None    # not 9000/25 across two bases


def test_portfolio_overall_rollup_is_the_official_bar(monkeypatch, tmp_path):
    """Decision 2026-07-27: the Overall tab aggregates sources with no vendor
    tab, so when its snapshot exists ITS figures ARE the summary bar; the
    vendor sum stays as the audit trail and the roll-up is never a vendor."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra"))
    snapshots.save_snapshot(_snap("hawksem-ls-google", spend=2000.0, q=0, qdb=2, comp=1, sold=0))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", spend=99999.0,
                                  budget=120000.0, leads=50, q=20, qdb=30, comp=15, sold=5))
    p = snapshots.portfolio()
    assert p["vendors"] == 2                       # the roll-up is not a vendor
    assert p["source"] == "sheet_overall"
    assert p["total_spend"] == 99999.0 and p["total_budget"] == 120000.0
    assert p["qualified_leads"] == 20
    assert p["qual_demos_booked"] == 30 and p["demos_completed"] == 15
    assert p["cost_per_qual_demo_booked"] == round(99999.0 / 30, 2)
    assert p["show_rate_pct"] == 50.0
    assert p["services_sold"] == 5
    assert p["computed_spend"] == 6000.0 and p["computed_budget"] == 20000.0
    assert p["pacing"]["day"] == 9 and p["pacing"]["days_in_month"] == 31
    assert p["benchmarks"]["cpqdb_max"] == 500


def test_portfolio_latest_snapshot_per_vendor(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-08", spend=1000.0))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-09", spend=4000.0))
    p = snapshots.portfolio()
    assert p["total_spend"] == 4000.0 and p["date"] == "2026-07-09"


def test_portfolio_null_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("ghost", budget=0.0, spend=0.0, leads=0, q=0, qdb=0, comp=0, sold=0))
    p = snapshots.portfolio()
    assert p["budget_utilized_pct"] is None
    assert p["cost_per_qualified_lead"] is None
    assert p["show_rate_pct"] is None


def test_portfolio_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    assert snapshots.portfolio() is None


# --- one day's sweep: the day's FINAL capture run, nothing else ---------------

def _at(snap, ts, sweep_id=None, hidden=None):
    snap["captured_at"] = ts
    if sweep_id is not None:
        snap["sweep_id"] = sweep_id
    if hidden is not None:
        snap["hidden"] = hidden
    return snap


def _golden_day(d="2026-09-02"):
    """The shape of 2026-09-02 in production: the 15-minute refresh captured a
    duplicated tab under "Copy of …" names in the afternoon, the tabs were then
    renamed (same gid) or deleted, and only the 23:46 run is the real sweep."""
    return [
        # earlier runs — slugs that no later run overwrote
        _at(_snap("copy-of-flytech-meta-ls", d=d, budget=5100.0, spend=70.36, leads=1),
            f"{d}T19:46:48.093157+00:00"),
        _at(_snap("axenic-ls-meta-new", d=d, budget=5000.0, spend=98.63, leads=0),
            f"{d}T20:16:55.573547+00:00"),
        # the final run, ~0.1 s apart
        _at(_snap("flytech-meta-ls", d=d, budget=5100.0, spend=70.36, leads=1),
            f"{d}T23:46:33.600715+00:00"),
        _at(_snap("axenic-ls-meta", d=d, budget=5000.0, spend=98.63, leads=0),
            f"{d}T23:46:34.469666+00:00"),
        _at(_snap("website", d=d, budget=8632.0, spend=8632.0, leads=1),
            f"{d}T23:46:34.812272+00:00"),
        _at(_snap("marketing-2026-overall-report", d=d, budget=105732.0,
                  spend=11368.93, leads=19), f"{d}T23:46:34.881121+00:00"),
    ]


def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))


def test_a_same_day_stale_capture_is_not_a_second_vendor(monkeypatch, tmp_path):
    """Golden case 2026-09-02: eight stale same-day docs lifted the vendor sum
    from the team's $95,600 / $2,737 / 18 to $117,200 / $2,906 / 20."""
    _offline(monkeypatch, tmp_path)
    for s in _golden_day():
        snapshots.save_snapshot(s)
    sw = snapshots.vendor_sweep("2026-09")
    assert sw["date"] == "2026-09-02"
    assert [s["vendor_slug"] for s in sw["docs"]] == ["axenic-ls-meta", "flytech-meta-ls", "website"]
    assert sw["rollup"]["vendor_slug"] == "marketing-2026-overall-report"
    assert sw["excluded"] == [
        {"tab": "axenic-ls-meta-new", "slug": "axenic-ls-meta-new", "reason": "stale_capture"},
        {"tab": "copy-of-flytech-meta-ls", "slug": "copy-of-flytech-meta-ls",
         "reason": "stale_capture"},
    ]
    p = snapshots.portfolio()
    assert p["vendors"] == 3
    assert p["computed_budget"] == 10100.0 and p["computed_spend"] == 168.99  # no copies
    assert p["vendors_excluded"] == ["axenic-ls-meta-new", "copy-of-flytech-meta-ls"]


def test_a_doc_flagged_hidden_is_excluded_and_named(monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    d = "2026-10-05"
    snapshots.save_snapshot(_at(_snap("meta-360-ra", d=d), f"{d}T10:00:00+00:00", "run1", False))
    snapshots.save_snapshot(_at(_snap("archive-tab", d=d), f"{d}T10:00:00.1+00:00", "run1", True))
    sw = snapshots.vendor_sweep()
    assert [s["vendor_slug"] for s in sw["docs"]] == ["meta-360-ra"]
    assert sw["excluded"] == [{"tab": "archive-tab", "slug": "archive-tab", "reason": "hidden"}]


def test_stamped_runs_are_split_by_sweep_id_not_by_clock(monkeypatch, tmp_path):
    """Two stamped runs seconds apart (two 'Snapshot now' clicks around a tab
    rename): the gap rule alone would merge them; the sweep_id does not."""
    _offline(monkeypatch, tmp_path)
    d = "2026-10-06"
    snapshots.save_snapshot(_at(_snap("old-name", d=d), f"{d}T10:00:00+00:00", "aaa"))
    snapshots.save_snapshot(_at(_snap("new-name", d=d), f"{d}T10:00:05+00:00", "bbb"))
    sw = snapshots.vendor_sweep("2026-10")
    assert [s["vendor_slug"] for s in sw["docs"]] == ["new-name"]
    assert [e["slug"] for e in sw["excluded"]] == ["old-name"]


def test_a_legacy_doc_beside_a_stamped_run_is_stale(monkeypatch, tmp_path):
    """The deploy day: an unstamped morning capture under a slug the stamped
    evening run no longer writes."""
    _offline(monkeypatch, tmp_path)
    d = "2026-10-07"
    snapshots.save_snapshot(_at(_snap("legacy-only", d=d), f"{d}T23:59:00+00:00"))
    snapshots.save_snapshot(_at(_snap("meta-360-ra", d=d), f"{d}T23:59:30+00:00", "run9"))
    sw = snapshots.vendor_sweep()
    assert [s["vendor_slug"] for s in sw["docs"]] == ["meta-360-ra"]


def test_vendor_sweep_picks_the_newest_date_in_the_month(monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    for d in ("2026-08-30", "2026-08-31", "2026-09-01"):
        snapshots.save_snapshot(_snap("meta-360-ra", d=d))
    assert snapshots.vendor_sweep("2026-08")["date"] == "2026-08-31"
    assert snapshots.vendor_sweep()["date"] == "2026-09-01"
    assert snapshots.previous_month_sweep("2026-09")["date"] == "2026-08-31"
    assert snapshots.vendor_sweep("2026-07") is None
    assert snapshots.previous_month_sweep("2026-08") is None


def test_previous_month_sweep_crosses_the_year(monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    snapshots.save_snapshot(_snap("meta-360-ra", d="2025-12-31"))
    assert snapshots.previous_month_sweep("2026-01")["date"] == "2025-12-31"


def test_vendor_sweep_rejects_a_malformed_month(monkeypatch, tmp_path):
    import pytest

    _offline(monkeypatch, tmp_path)
    for bad in ("2026-9", "2026-13", "2026-09-02", "", "09-2026"):
        with pytest.raises(ValueError):
            snapshots.vendor_sweep(bad)


def test_the_portfolio_rollup_comes_from_the_same_sweep(monkeypatch, tmp_path):
    """A run whose roll-up failed must not borrow an earlier day's roll-up
    under today's date — it falls back to the vendor sum and says so."""
    _offline(monkeypatch, tmp_path)
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", d="2026-07-08",
                                  spend=99999.0))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-09", spend=4000.0))
    p = snapshots.portfolio()
    assert p["source"] == "vendor_sum" and p["total_spend"] == 4000.0


# --- sweep_months: the month picker ------------------------------------------

def test_sweep_months_lists_months_with_a_sweep_newest_first(monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    for d in ("2026-07-15", "2026-08-31", "2026-10-02"):     # September is a gap
        snapshots.save_snapshot(_snap("meta-360-ra", d=d))
    assert snapshots.sweep_months() == ["2026-10", "2026-08", "2026-07"]
    assert snapshots.sweep_months(limit=2) == ["2026-10", "2026-08"]
    assert snapshots.sweep_months(limit=0) == []


def test_sweep_months_agrees_with_vendor_sweep_on_excluded_only_months(monkeypatch, tmp_path):
    """A month whose newest day holds nothing but excluded docs (hidden) or the
    roll-up is not a month: vendor_sweep returns None for it, so the picker
    must not offer it."""
    _offline(monkeypatch, tmp_path)
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-08-31"))
    snapshots.save_snapshot(_at(_snap("archive-tab", d="2026-09-30"),
                                "2026-09-30T10:00:00+00:00", "r1", True))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", d="2026-10-01"))
    assert snapshots.vendor_sweep("2026-09") is None
    assert snapshots.vendor_sweep("2026-10") is None
    assert snapshots.sweep_months() == ["2026-08"]
    for m in snapshots.sweep_months():
        assert snapshots.vendor_sweep(m) is not None


def test_sweep_months_empty_store(monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    assert snapshots.sweep_months() == []


def test_the_rollup_rides_alongside_the_vendors_never_among_them(monkeypatch, tmp_path):
    """``rollup`` is the Overall tab's doc from the SAME sweep: present when the
    final run captured it, out of ``docs`` always, ``None`` when that run did
    not capture it — including when only an EARLIER run that day did."""
    _offline(monkeypatch, tmp_path)
    d = "2026-09-02"
    for s in _golden_day(d):
        snapshots.save_snapshot(s)
    sw = snapshots.vendor_sweep("2026-09")
    assert sw["rollup"]["vendor_slug"] == "marketing-2026-overall-report"
    assert sw["rollup"]["canonical"]["team_overall"]["spend"]["performance"] == 11368.93
    assert all("overall" not in s["vendor_slug"] for s in sw["docs"])

    # Next month: the final run has no roll-up; an earlier run that day did.
    e = "2026-10-03"
    snapshots.save_snapshot(_at(_snap("marketing-2026-overall-report", d=e),
                                f"{e}T08:00:00+00:00"))
    snapshots.save_snapshot(_at(_snap("meta-360-ra", d=e), f"{e}T09:00:00+00:00"))
    later = snapshots.vendor_sweep("2026-10")
    assert later["rollup"] is None
    assert [s["vendor_slug"] for s in later["docs"]] == ["meta-360-ra"]
    assert snapshots.previous_month_sweep("2026-10")["rollup"]["date"] == d
