"""The download-report PDFs must read like the console panels they mirror —
same sections, same figures (2026-07-27)."""

import fitz

from marketing_research_agent import pdf_export


def _text(data: bytes) -> str:
    assert data[:5] == b"%PDF-"
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def _report():
    return {
        "id": "run_test1",
        "kind": "monthly_summary",
        "generated_at": "2026-07-27T10:00:00+00:00",
        "sources": [{"platform": "sheets:Meta 360 RA",
                     "generated_at": "2026-07-27T09:00:00+00:00", "metrics": 7, "leads": 0}],
        "markdown": "# Monthly Summary\n\nSpend ran hot this month.\nRecommend: rebalance the Meta budget.",
        "structured": {
            "period": {"start": "2026-07-01", "end": "2026-07-26", "label": "Jul 1–26, 2026"},
            "totals": {"spend": 45461.20, "spend_computed": 32329.20, "spend_delta": 13132.0,
                       "spend_source": "sheet_overall", "demos_completed": 73,
                       "cost_per_demo_completed": 622.76, "qualified_leads": 124,
                       "demos_booked": 129, "leads": 305},
            "channels": {"Google": {"spend": 11882.36, "leads": 100, "qualified_leads": 40,
                                    "demos_booked": 30, "demos_completed": 20,
                                    "cost_per_lead": 118.82, "cost_per_qualified_lead": 297.06,
                                    "cost_per_demo_booked": 396.08, "cost_per_demo_completed": 594.12,
                                    "cac": 594.12}},
            "flag_summary": [{"metric": "cac", "level": "red", "count": 2,
                              "text": "2 campaigns over the $2,500 CAC ceiling (worst $4,100)"}],
            "red_flag_vendors": [{"vendor": "SaffronEdge LS Meta",
                                  "reasons": ["$45 spent with zero leads"]}],
            "vendors": [{"vendor": "Meta 360 RA", "spend": 7364.17, "leads": 60,
                         "qualified_leads": 25, "demos_booked": 20, "demos_completed": 12,
                         "cost_per_qualified_lead": 294.57, "cost_per_demo_booked": 368.21,
                         "cost_per_demo_completed": 613.68}],
            "vendor_insights": [{"vendor": "Meta 360 RA",
                                 "insights": ["a", "b", "c"], "actions": ["x", "y", "z"]}],
        },
    }


def test_report_pdf_mirrors_console_sections_and_figures():
    text = _text(pdf_export.report_pdf(_report()))
    # header, verdict, period — as on screen
    assert "Monthly Performance Summary" in text
    assert "2 red flags" in text
    assert "Jul 1–26, 2026" in text
    # section titles in the console's order
    for sec in ("Executive summary", "What needs attention", "Performance detail",
                "Vendor detail", "Vendor insights & actions", "Appendix — data & provenance"):
        assert sec in text, f"missing section: {sec}"
    # the official headline + reconciliation trail
    assert "$45,461" in text and "$32,329" in text
    # narrative + recommend callout + red-flag vendor
    assert "Spend ran hot this month." in text
    assert "rebalance the Meta budget" in text
    assert "SaffronEdge LS Meta" in text
    # channel + vendor figures
    assert "$7,364" in text and "Meta 360 RA" in text
    assert "run_test1" in text


def test_report_pdf_handles_non_campaign_kinds():
    report = {
        "id": "run_test2", "kind": "daily_movement",
        "generated_at": "2026-07-27T10:00:00+00:00", "sources": [],
        "markdown": "# Daily Movement\n\nQuiet day.",
        "structured": {"vendors": [{
            "vendor": "Meta 360 RA", "date": "2026-07-27", "since": None, "days": 1,
            "month_start": False, "corrected": False,
            "blocks": {"team_overall": {"additive": {
                "spend.performance": {"delta": 250.0, "mtd": 7364.17, "corrected": False},
                "leads.total": {"delta": 3, "mtd": 60, "corrected": False},
            }}},
        }]},
    }
    text = _text(pdf_export.report_pdf(report))
    assert "Daily Movement Report" in text
    assert "+$250" in text and "+3" in text


def _vendor_detail():
    return {
        "vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "gid": 42,
        "dates": ["2026-07-25", "2026-07-26"],
        "snapshot": {
            "vendor": "Meta 360 RA", "date": "2026-07-26", "month": "2026-07",
            "captured_at": "2026-07-26T18:00:00+00:00",
            "canonical": {
                "team_overall": {
                    "budget": {"performance": 10000.0, "investment": None},
                    "spend": {"performance": 7364.17, "investment": 7364.17},
                    "leads": {"total": 60, "qualified": 25},
                    "demos": {"qualified_booked_all": 20, "total_booked_all": 22, "completed_all": 12},
                    "actualized_revenue": {"services_sold": 4},
                    "cost_metrics": {"cost_per_lead_performance": 122.74},
                },
                "channels": {"google": {"spend": {"performance": 1200.0, "investment": None},
                                        "leads": {"total": 10, "qualified": 4}}},
            },
        },
        "delta": {"month_start": False, "since": "2026-07-25", "days": 1, "corrected": False,
                  "blocks": {"team_overall": {"additive": {
                      "spend.performance": {"delta": 300.0, "mtd": 7364.17, "corrected": False},
                      "leads.total": {"delta": 2, "mtd": 60, "corrected": False},
                  }}, "channels": {}}},
    }


def test_vendor_pdf_mirrors_the_dossier():
    benchmarks = {"cpqdb_max": 500, "show_rate_min": 60, "cac_target": 2500, "cpql_red": 600,
                  "ql_ratio_min": 30}
    text = _text(pdf_export.vendor_pdf(_vendor_detail(), benchmarks))
    assert "Meta 360 RA" in text
    assert "VENDOR DOSSIER" in text and "GID 42" in text
    # official summary grid figures (same math as VendorsView.vendorStats)
    assert "$10,000" in text          # budget
    assert "$7,364" in text           # spend
    assert "$368" in text             # cost / qual. demo booked = 7364.17/20
    assert "60.0%" in text            # show rate = 12/20
    # day movement + dossier sections + channel block
    assert "Day movement" in text and "+$300" in text
    assert "Budget & spend".upper() in text.upper()
    assert "GOOGLE channel".upper() in text.upper()
    assert "Cost per lead performance" in text


def test_vendor_pdf_survives_missing_benchmarks_and_delta():
    d = _vendor_detail()
    d["delta"] = {}
    text = _text(pdf_export.vendor_pdf(d, None))
    assert "Meta 360 RA" in text and "$7,364" in text
