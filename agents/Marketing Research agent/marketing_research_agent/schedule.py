"""Scheduler entrypoints (requirements §4 cadences).

Pure functions a cron/Cloud Scheduler job calls — no timing logic lives here.
Each builds the report(s) for its period, delivers them, and returns the
primary report. ``check_alerts`` only fires when a threshold is breached.
"""

from __future__ import annotations

from . import notify, reports
from .modules import campaign_reporting as cr


def _run(kind: str, dataset: dict, user_id: str) -> dict:
    report = reports.build(kind, dataset, user_id)
    notify.deliver(report)
    return report


def run_daily(dataset: dict, user_id: str) -> dict:
    """Daily 3pm PST — performance summary + alert check."""
    check_alerts(dataset, user_id)
    return _run("daily_summary", dataset, user_id)


def run_weekly(dataset: dict, user_id: str) -> dict:
    """Monday 12pm PST — weekly summary + competitor digest + UTM attribution."""
    _run("competitor_digest", dataset, user_id)
    _run("utm_attribution", dataset, user_id)
    return _run("weekly_summary", dataset, user_id)


def run_biweekly(dataset: dict, user_id: str) -> dict:
    """Bi-weekly — new media opportunity report."""
    return _run("opportunity_report", dataset, user_id)


def run_monthly(dataset: dict, user_id: str) -> dict:
    """Monthly — ICP audience signal report."""
    return _run("icp_signal", dataset, user_id)


def check_alerts(dataset: dict, user_id: str) -> dict | None:
    """Triggered threshold alert — only emits a report if a flag is tripped."""
    flags = cr.flag_all(dataset.get("metrics", []), dataset.get("prior"))
    if not flags:
        return None
    return _run("threshold_alert", dataset, user_id)
