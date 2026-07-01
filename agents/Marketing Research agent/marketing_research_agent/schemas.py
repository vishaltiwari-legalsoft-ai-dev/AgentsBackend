"""Canonical data schemas — the lingua franca every module and report speaks.

Raw platform/CSV formats are normalized into these dataclasses at the source
boundary (``sources/``); nothing downstream sees a platform-specific shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


def _safe_div(num: float, den: float) -> float | None:
    """Cost metrics divide by counts that can legitimately be zero. Return
    ``None`` rather than raising so reports degrade gracefully."""
    return round(num / den, 2) if den else None


@dataclass
class DateRange:
    start: date
    end: date


@dataclass
class CampaignMetric:
    """One channel/campaign's spend + funnel counts for a period."""

    channel: str
    campaign: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    spend: float
    leads: int
    qualified_leads: int
    demos_booked: int
    demos_completed: int
    date: date

    @property
    def cpl(self) -> float | None:
        return _safe_div(self.spend, self.leads)

    @property
    def cost_per_qualified_lead(self) -> float | None:
        return _safe_div(self.spend, self.qualified_leads)

    @property
    def cost_per_demo_booked(self) -> float | None:
        return _safe_div(self.spend, self.demos_booked)

    @property
    def cost_per_demo_completed(self) -> float | None:
        return _safe_div(self.spend, self.demos_completed)

    @property
    def cac(self) -> float | None:
        # CAC == cost per completed demo (closed/won proxy) at this granularity.
        return _safe_div(self.spend, self.demos_completed)


@dataclass
class Lead:
    """A single lead and the furthest funnel stage it reached."""

    id: str
    channel: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    practice_area: str
    stage: str  # visit | form_fill | booked | qualified | completed
    created_at: date


@dataclass
class CompetitorSnapshot:
    competitor: str
    url: str
    captured_at: str
    content_hash: str
    text: str


@dataclass
class MediaOpportunity:
    name: str
    type: str  # meta_vendor | google_vendor | podcast | youtube | affiliate
    audience_size: int
    engagement_rate: float
    host_authority: float
    practice_area_fit: float
    icp_score: float = 0.0
    outreach_status: str = "new"  # new | contacted | active | declined
    last_contact: str | None = None
    promo_code_live: bool = False
    utm_firing: bool = False


@dataclass
class DataGap:
    """A structured note about missing/malformed input — surfaced in reports
    instead of crashing the pipeline."""

    source: str
    message: str


@dataclass
class Flag:
    level: str  # warn | red
    message: str
    metric: str | None = None
