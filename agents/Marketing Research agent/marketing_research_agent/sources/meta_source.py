"""Live META (Facebook/Instagram) Marketing API connector — same interface.

Credential-gated until ``META_ACCESS_TOKEN`` is provisioned.
"""

from __future__ import annotations

import os

from ..schemas import CampaignMetric, DataGap, DateRange, Lead
from .base import CredentialMissingError


class MetaSource:
    name = "meta_api"

    def __init__(self) -> None:
        if not os.environ.get("META_ACCESS_TOKEN"):
            raise CredentialMissingError(
                "META_ACCESS_TOKEN not configured — use CSV export ingestion "
                "until live META Marketing API access is provisioned."
            )

    def fetch_campaign_metrics(self, range: DateRange) -> tuple[list[CampaignMetric], list[DataGap]]:
        raise CredentialMissingError("META live connector not yet implemented.")

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]:
        raise CredentialMissingError("META live connector not yet implemented.")
