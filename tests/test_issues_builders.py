"""Golden tests for the pure Issue builders (``app.services.issues``).

Every rule is a function of documents, so every test here is a document in,
an issue out — no state adapter, no network, no clock except the one passed in.
"""
from __future__ import annotations

import datetime as dt
import hashlib

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from app.services import issues as svc

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True}
NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)

# The exact shape a2 writes into ``run-{brand}.degraded`` when the service
# account is not a user on the property (captured from production).
GSC_403 = (
    "Search Console rejected sc-domain:legalsoft.com: <HttpError 403 when requesting "
    "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain%3Alegalsoft.com/"
    "searchAnalytics/query?alt=json returned \"User does not have sufficient permission "
    "for site 'sc-domain:legalsoft.com'. See also: https://support.google.com/webmasters/"
    "answer/9999\". Details: \"User does not have sufficient permission for site "
    "'sc-domain:legalsoft.com'. See also: https://support.google.com/webmasters/answer/9999\">"
)
GA_403 = (
    "Google Analytics rejected properties/123456: <HttpError 403 when requesting "
    "https://analyticsdata.googleapis.com/v1beta/properties/123456:runReport?alt=json "
    "returned \"User does not have sufficient permissions for this property.\">"
)

ALL_ON = {
    "perplexity": {"connected": True, "mode": "native", "model": "sonar", "means": ""},
    "gemini": {"connected": True, "mode": "proxy", "model": "g", "means": ""},
    "chatgpt": {"connected": True, "mode": "native", "model": "o", "means": ""},
    "aio": {"connected": True, "mode": "dataforseo", "model": "aio", "means": ""},
    "ai_mode": {"connected": True, "mode": "dataforseo", "model": "aim", "means": ""},
}


def _shape_ok(issue: dict) -> None:
    assert set(issue) == {"id", "severity", "area", "brand_id", "brand", "code",
                          "title", "detail", "fix", "since"}
    assert issue["severity"] in svc.SEVERITIES
    assert issue["area"] in svc.AREAS
    assert len(issue["title"]) <= svc.TITLE_MAX
    assert len(issue["detail"]) <= svc.DETAIL_MAX
    assert "<HttpError" not in issue["detail"] and "http" not in issue["detail"].lower()
    assert issue["id"] == hashlib.sha1(
        f"{issue['area']}|{issue['brand_id']}|{issue['code']}".encode()
    ).hexdigest()[:12]
    if issue["fix"] is not None:
        assert set(issue["fix"]) == {"label", "workspace", "subject", "section"}
        assert issue["fix"]["workspace"] in ("seo", "geo")
        assert issue["fix"]["subject"] == issue["brand_id"]


# ------------------------------------------------- humanize_google_error ----

def test_gsc_403_becomes_a_grant_access_instruction():
    code, title, detail = svc.humanize_google_error("Search Console", GSC_403)
    assert code == "gsc_no_access"
    assert title == "Search Console has not granted access to sc-domain:legalsoft.com"
    assert detail == ("Add the service account as a user on the property in Search "
                      "Console, then reconnect.")


def test_ga_403_names_analytics_and_its_property():
    code, title, detail = svc.humanize_google_error("Google Analytics", GA_403)
    assert code == "ga_no_access"
    assert title == "Google Analytics has not granted access to properties/123456"
    assert "Viewer" in detail and "Google Analytics" in detail


def test_404_is_property_not_found():
    msg = ("Search Console rejected sc-domain:nope.com: <HttpError 404 when requesting "
           "https://x returned \"Not Found\">")
    code, title, detail = svc.humanize_google_error("Search Console", msg)
    assert code == "gsc_property_missing"
    assert title == "Search Console cannot find sc-domain:nope.com"
    assert "property name" in detail


@pytest.mark.parametrize("msg", [
    "Search Console auth unavailable: invalid_grant: Bad Request",
    "Search Console rejected sc-domain:x.com: Token has been expired or revoked.",
])
def test_expired_grant_says_reconnect(msg):
    code, title, detail = svc.humanize_google_error("Search Console", msg)
    assert code == "gsc_token_expired"
    assert title.startswith("Search Console access has expired")
    assert "Reconnect" in detail


def test_missing_key_is_its_own_message():
    code, title, detail = svc.humanize_google_error("Rank tracking", "SEO_SERPER_API_KEY not set")
    assert code == "rank_no_key"
    assert title == "Rank tracking has no API key"
    assert "Settings" in detail and "SEO_SERPER_API_KEY" not in detail


def test_generic_error_keeps_only_the_human_part():
    msg = ("Search Console rejected sc-domain:legalsoft.com: <HttpError 500 when requesting "
           "https://searchconsole.googleapis.com/x?alt=json returned \"Backend Error. "
           "See also: https://status.cloud.google.com/\">")
    code, title, detail = svc.humanize_google_error("Search Console", msg)
    assert code == "gsc_unreadable"
    assert title == "Search Console could not be read for sc-domain:legalsoft.com"
    assert detail == "Backend Error"


def test_generic_error_without_httperror_is_clipped_to_120_chars():
    msg = "Search Console auth unavailable: DefaultCredentialsError: " + "x" * 300
    code, title, detail = svc.humanize_google_error("Search Console", msg)
    assert code == "gsc_unreadable"
    assert title == "Search Console could not be read"
    assert len(detail) == 120 and detail.startswith("Search Console auth unavailable")


def test_unknown_source_gets_a_slug_prefix_and_its_own_name():
    code, title, _ = svc.humanize_google_error("New-topic ideation skipped", "offline mode")
    assert code == "new_topic_ideation_skipped_unreadable"
    assert title == "New-topic ideation skipped could not be read"


# ------------------------------------------------------- issues_from_seo ----

def test_never_measured_brand_is_a_medium_issue_with_a_run_fix():
    (issue,) = svc.issues_from_seo(BRAND, None)
    _shape_ok(issue)
    assert issue["code"] == "seo_never_measured" and issue["severity"] == "medium"
    assert issue["title"] == "Legal Soft has never been measured for search"
    assert issue["fix"] == {"label": "Run the first analysis", "workspace": "seo",
                            "subject": "legalsoft", "section": "fixes"}
    assert issue["since"] is None


def test_degraded_notes_become_ranked_issues_stamped_with_the_run_date():
    run = {"at": "2026-08-29", "degraded": [
        f"Search Console: {GSC_403}",
        f"Google Analytics: {GA_403}",
        "Page analytics: Google Analytics rejected properties/1: <HttpError 403 x returned \"no permission\">",
        "Serper key missing — topics built from Search Console data only",
    ]}
    found = svc.issues_from_seo(BRAND, run)
    for issue in found:
        _shape_ok(issue)
        assert issue["since"] == "2026-08-29" and issue["area"] == "seo"
    by_code = {i["code"]: i for i in found}
    assert by_code["gsc_no_access"]["severity"] == "high"
    assert by_code["ga_no_access"]["severity"] == "medium"
    assert by_code["pages_no_access"]["severity"] == "low"
    note = by_code["seo_note"]
    assert note["severity"] == "low" and note["detail"].startswith("Serper key missing")


def test_same_source_failing_twice_is_one_issue():
    run = {"at": "2026-08-29", "degraded": [f"Search Console: {GSC_403}", f"Search Console: {GSC_403}"]}
    assert len(svc.issues_from_seo(BRAND, run)) == 1


def test_clean_run_has_no_issues():
    assert svc.issues_from_seo(BRAND, {"at": "2026-08-29", "degraded": []}) == []


def test_a_very_long_property_is_clipped_not_overflowed():
    prop = "sc-domain:" + "a" * 200 + ".com"
    run = {"at": "2026-08-29", "degraded": [f"Search Console: Search Console rejected {prop}: <HttpError 403 x returned \"no permission\">"]}
    (issue,) = svc.issues_from_seo(BRAND, run)
    _shape_ok(issue)
    assert issue["title"].endswith("…")


# ------------------------------------------------------- issues_from_geo ----

def _cfg(**over):
    base = {"brand_id": "legalsoft", "poll_interval_days": 2, "auto_poll": True,
            "last_poll_completed_at": "2026-08-29T02:00:00+00:00", "counters": {}}
    return base | over


def test_every_off_engine_is_a_medium_not_connected_issue():
    status = dict(ALL_ON) | {
        "aio": {"connected": False, "mode": "off", "model": "", "means": ""},
        "ai_mode": {"connected": False, "mode": "off", "model": "", "means": ""},
    }
    found = svc.issues_from_geo(BRAND, _cfg(engine_last_seen={"aio": "2026-08-11T02:00:00+00:00"}),
                                status, None, now=NOW)
    for issue in found:
        _shape_ok(issue)
    by_code = {i["code"]: i for i in found}
    assert set(by_code) == {"engine_off_aio", "engine_off_ai_mode"}
    aio = by_code["engine_off_aio"]
    assert aio["severity"] == "medium"
    assert aio["title"] == "Google AI Overview is not connected"
    assert aio["fix"] == {"label": "Connect", "workspace": "geo", "subject": "legalsoft", "section": "settings"}
    assert aio["since"] == "2026-08-11T02:00:00+00:00"
    assert by_code["engine_off_ai_mode"]["title"] == "Google AI Mode is not connected"
    assert by_code["engine_off_ai_mode"]["since"] is None


def test_engine_that_failed_every_call_in_the_last_check_is_high():
    last_run = {"finished_at": "2026-08-30T02:10:00+00:00", "completed": False,
                "engines": ["perplexity", "chatgpt"],
                "calls": {"perplexity": 40, "chatgpt": 40},
                "errors": {"chatgpt": 40, "perplexity": 3}}
    found = svc.issues_from_geo(BRAND, _cfg(), ALL_ON, last_run, now=NOW)
    (issue,) = found
    _shape_ok(issue)
    assert issue["code"] == "engine_failed_chatgpt" and issue["severity"] == "high"
    assert issue["title"] == "ChatGPT failed on every call in the last check"
    assert "All 40 calls" in issue["detail"] and "2026-08-30" in issue["detail"]
    assert issue["fix"]["section"] == "overview"
    assert issue["since"] == "2026-08-30T02:10:00+00:00"


def test_a_total_call_count_only_counts_when_one_engine_ran():
    one = {"finished_at": "2026-08-30T02:10:00+00:00", "engines": ["gemini"], "calls": 12,
           "errors": {"gemini": 12}}
    assert [i["code"] for i in svc.issues_from_geo(BRAND, _cfg(), ALL_ON, one, now=NOW)] == ["engine_failed_gemini"]
    two = {**one, "engines": ["gemini", "chatgpt"]}
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, two, now=NOW) == []


def test_zero_errors_and_zero_calls_is_not_a_failure():
    quiet = {"finished_at": "2026-08-30T02:10:00+00:00", "engines": ["aio"],
             "calls": {"aio": 0}, "errors": {"aio": 0}}
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, quiet, now=NOW) == []


def test_a_fail_streak_on_the_config_stands_in_for_a_missing_run_log():
    cfg = _cfg(poll_health={"day": "20260830", "streaks": {"gemini": 3, "perplexity": 1}})
    (issue,) = svc.issues_from_geo(BRAND, cfg, ALL_ON, None, now=NOW)
    assert issue["code"] == "engine_failed_gemini" and issue["severity"] == "high"
    assert "3 consecutive batches on 2026-08-30" in issue["detail"]
    assert issue["since"] == "2026-08-30"


def test_streak_and_run_log_for_one_engine_collapse_to_one_issue():
    cfg = _cfg(poll_health={"day": "20260830", "streaks": {"gemini": 5}})
    run = {"finished_at": "2026-08-30T02:10:00+00:00", "engines": ["gemini"],
           "calls": {"gemini": 9}, "errors": {"gemini": 9}}
    found = svc.issues_from_geo(BRAND, cfg, ALL_ON, run, now=NOW)
    assert [i["code"] for i in found] == ["engine_failed_gemini"]
    assert "All 9 calls" in found[0]["detail"]


def test_never_swept_brand_is_medium():
    (issue,) = svc.issues_from_geo(BRAND, _cfg(last_poll_completed_at=None), ALL_ON, None, now=NOW)
    _shape_ok(issue)
    assert issue["code"] == "never_swept" and issue["severity"] == "medium"
    assert issue["title"] == "Legal Soft has never completed an AI answer sweep"
    assert issue["since"] is None


def test_sweep_older_than_twice_the_interval_is_stale():
    fresh = _cfg(last_poll_completed_at="2026-08-26T02:00:00+00:00")   # 4 days = 2×2, not stale
    assert svc.issues_from_geo(BRAND, fresh, ALL_ON, None, now=NOW) == []
    stale = _cfg(last_poll_completed_at="2026-08-25T02:00:00+00:00")   # 5 days
    (issue,) = svc.issues_from_geo(BRAND, stale, ALL_ON, None, now=NOW)
    _shape_ok(issue)
    assert issue["code"] == "sweep_stale" and issue["severity"] == "medium"
    assert issue["title"] == "AI answers for Legal Soft are 5 days old"
    assert "2026-08-25" in issue["detail"] and "every 2 days" in issue["detail"]
    assert "scheduled poll" in issue["detail"]
    assert issue["since"] == "2026-08-25T02:00:00+00:00"


def test_stale_sweep_names_auto_poll_when_it_is_off():
    cfg = _cfg(last_poll_completed_at="2026-08-01T02:00:00+00:00", auto_poll=False)
    (issue,) = svc.issues_from_geo(BRAND, cfg, ALL_ON, None, now=NOW)
    assert "Auto-poll is switched off" in issue["detail"]


def test_interval_default_and_clamp_follow_the_poll_module():
    # no interval on the doc → the poll's default (2) → 5 days is stale
    (issue,) = svc.issues_from_geo(BRAND, _cfg(poll_interval_days=None, last_poll_completed_at="2026-08-25T02:00:00+00:00"), ALL_ON, None, now=NOW)
    assert issue["code"] == "sweep_stale"
    # an out-of-range interval clamps to 30 → 5 days is fine
    assert svc.issues_from_geo(BRAND, _cfg(poll_interval_days=999, last_poll_completed_at="2026-08-25T02:00:00+00:00"), ALL_ON, None, now=NOW) == []


def _plan(generated_at: str, statuses: list[str]) -> dict:
    return {"brand_id": "legalsoft", "current": {
        "generated_at": generated_at,
        "waves": [{"weeks": "1-2", "actions": [{"id": f"a{i}", "status": s} for i, s in enumerate(statuses)]}],
    }}


def test_plan_with_nothing_done_after_14_days_is_low():
    plan = _plan("2026-08-10T00:00:00+00:00", ["todo", "in_progress", "skipped"])
    (issue,) = svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan=plan, now=NOW)
    _shape_ok(issue)
    assert issue["code"] == "plan_untouched" and issue["severity"] == "low"
    assert issue["title"] == "The GEO plan for Legal Soft has had nothing completed in 20 days"
    assert "3 actions" in issue["detail"] and "2026-08-10" in issue["detail"]
    assert issue["fix"]["section"] == "plan"
    assert issue["since"] == "2026-08-10T00:00:00+00:00"


def test_plan_is_not_an_issue_when_young_or_worked_or_absent():
    young = _plan("2026-08-20T00:00:00+00:00", ["todo", "todo"])
    worked = _plan("2026-07-01T00:00:00+00:00", ["done", "todo"])
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan=young, now=NOW) == []
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan=worked, now=NOW) == []
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan=None, now=NOW) == []
    assert svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan={"brand_id": "legalsoft", "current": None}, now=NOW) == []


def test_old_pillar_shaped_plan_is_read_too():
    plan = {"current": {"generated_at": "2026-07-01T00:00:00+00:00",
                        "pillars": [{"actions": [{"id": "a", "status": "todo"}]}]}}
    (issue,) = svc.issues_from_geo(BRAND, _cfg(), ALL_ON, None, plan=plan, now=NOW)
    assert issue["code"] == "plan_untouched"


def test_unreadable_config_skips_only_the_rules_that_need_it():
    status = dict(ALL_ON) | {"aio": {"connected": False, "mode": "off", "model": "", "means": ""}}
    found = svc.issues_from_geo(BRAND, None, status, None, now=NOW)
    assert [i["code"] for i in found] == ["engine_off_aio"]


# -------------------------------------------------- unreadable + assembly ----

def test_unreadable_issue_is_low_and_says_it_is_not_health():
    issue = svc.unreadable_issue("geo", BRAND, "GEO configuration")
    _shape_ok(issue)
    assert issue["code"] == "unreadable_geo_configuration" and issue["severity"] == "low"
    assert issue["title"] == "GEO configuration could not be read"
    assert "not the same as it being healthy" in issue["detail"]
    assert issue["fix"] is None and issue["since"] is None


def test_build_issues_sorts_by_severity_then_brand_and_counts():
    acme = {"id": "acme", "name": "Acme"}
    zeta = {"id": "zeta", "name": "Zeta"}
    issues = (
        svc.issues_from_geo(zeta, _cfg(last_poll_completed_at=None), ALL_ON, None, now=NOW)   # medium
        + svc.issues_from_seo(acme, {"at": "2026-08-29", "degraded": [f"Search Console: {GSC_403}"]})  # high
        + [svc.unreadable_issue("geo", acme, "GEO plan")]                                     # low
        + svc.issues_from_geo(acme, _cfg(last_poll_completed_at=None), ALL_ON, None, now=NOW)  # medium
    )
    body = svc.build_issues(issues)
    assert [(i["severity"], i["brand"]) for i in body["issues"]] == [
        ("high", "Acme"), ("medium", "Acme"), ("medium", "Zeta"), ("low", "Acme"),
    ]
    assert body["counts"] == {"high": 1, "medium": 2, "low": 1}


def test_build_issues_keeps_one_entry_per_id_and_ids_are_stable():
    a = svc.issues_from_seo(BRAND, None)
    b = svc.issues_from_seo(BRAND, None)
    assert a[0]["id"] == b[0]["id"] == svc.issue_id("seo", "legalsoft", "seo_never_measured")
    assert len(svc.build_issues(a + b)["issues"]) == 1
    assert svc.build_issues([])["counts"] == {"high": 0, "medium": 0, "low": 0}
