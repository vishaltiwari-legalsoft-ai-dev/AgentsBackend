"""Live Google Ads connector — same DataSource interface as ``CsvSource``.

Credential-gated: until ``GOOGLE_ADS_DEVELOPER_TOKEN`` (and the OAuth client
config) are provisioned, this raises a clear, actionable error. Implement the
fetch methods against the Google Ads API when access is granted; nothing
upstream changes.
"""

from __future__ import annotations

import os

from ..schemas import CampaignMetric, DataGap, DateRange, Lead
from .base import CredentialMissingError


class GoogleAdsSource:
    name = "google_ads_api"

    def __init__(self) -> None:
        if not os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
            raise CredentialMissingError(
                "GOOGLE_ADS_DEVELOPER_TOKEN not configured — use CSV export "
                "ingestion until live Google Ads API access is provisioned."
            )

    def fetch_campaign_metrics(self, range: DateRange) -> tuple[list[CampaignMetric], list[DataGap]]:
        raise CredentialMissingError("Google Ads live connector not yet implemented.")

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]:
        raise CredentialMissingError("Google Ads live connector not yet implemented.")
