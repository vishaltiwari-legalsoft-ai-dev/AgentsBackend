"""Legal Soft Marketing Research agent (marketing department).

An AI research agent scoped to the Marketing Coordinator functions: campaign
performance reporting, competitor intelligence, lead-channel/funnel analysis,
and media-opportunity research — emitting scheduled reports and triggered
alerts.

Importable package root: ``marketing_research_agent`` (placed on ``sys.path`` by
``app/__init__.py`` because the parent folder contains spaces). See ``agent.md``
for the full build spec and ``README.md`` for the layout.

Data enters through the ``sources`` adapter layer: CSV/Excel export ingestion
ships today; live Google Ads / META / HubSpot connectors implement the same
``DataSource`` interface and drop in when credentials are provisioned.
"""

from __future__ import annotations

from . import analysis, config, goals, notify, reports, runs, schedule, schemas
from .reports import KINDS, build
from .runs import get_run, list_runs, new_run_id, save_run

__all__ = [
    "analysis",
    "config",
    "goals",
    "notify",
    "reports",
    "runs",
    "schedule",
    "schemas",
    "KINDS",
    "build",
    "get_run",
    "list_runs",
    "new_run_id",
    "save_run",
]
