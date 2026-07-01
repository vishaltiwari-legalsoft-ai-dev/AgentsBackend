"""The DataSource contract every connector implements.

CSV export ingestion (``csv_source``) ships now. Live API connectors
(Google Ads / META / HubSpot) implement this same protocol and drop in with no
changes to the modules or reports that consume them.
"""

from __future__ import annotations

from typing import Protocol

from ..schemas import CampaignMetric, DataGap, DateRange, Lead


class CredentialMissingError(RuntimeError):
    """Raised by live API sources until their credentials are configured."""


class DataSource(Protocol):
    name: str

    def fetch_campaign_metrics(
        self, range: DateRange
    ) -> tuple[list[CampaignMetric], list[DataGap]]: ...

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]: ...
