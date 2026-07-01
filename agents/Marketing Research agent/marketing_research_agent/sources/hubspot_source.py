"""Live HubSpot connector — same interface as ``CsvSource``.

Credential-gated until ``HUBSPOT_ACCESS_TOKEN`` is provisioned. HubSpot is the
lead/funnel system of record, so the live connector primarily implements
``fetch_leads``.
"""

from __future__ import annotations

import os

from ..schemas import CampaignMetric, DataGap, DateRange, Lead
from .base import CredentialMissingError


class HubSpotSource:
    name = "hubspot_api"

    def __init__(self) -> None:
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            raise CredentialMissingError(
                "HUBSPOT_ACCESS_TOKEN not configured — use CSV export ingestion "
                "until live HubSpot API access is provisioned."
            )

    def fetch_campaign_metrics(self, range: DateRange) -> tuple[list[CampaignMetric], list[DataGap]]:
        raise CredentialMissingError("HubSpot live connector not yet implemented.")

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]:
        raise CredentialMissingError("HubSpot live connector not yet implemented.")
