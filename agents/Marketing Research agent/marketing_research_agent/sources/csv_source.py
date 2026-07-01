"""CSV/Excel-export ingestion — the connector that ships today.

Marketing exports a report from each platform; this maps the platform's columns
onto the canonical schema via ``config.COLUMN_MAPS``. Missing or renamed columns
produce a structured ``DataGap`` rather than crashing the run.
"""

from __future__ import annotations

import csv
from datetime import date, datetime

from .. import config
from ..schemas import CampaignMetric, DataGap, DateRange, Lead

_METRIC_INT_FIELDS = ("leads", "qualified_leads", "demos_booked", "demos_completed")


def _to_date(raw: str) -> date:
    return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()


class CsvSource:
    def __init__(self, path: str, platform: str):
        self.path = path
        self.platform = platform
        self.name = f"csv:{platform}"

    def _rows(self) -> tuple[list[dict], list[str]]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            return list(reader), headers

    def _mapped(self, headers: list[str]) -> tuple[dict, list[DataGap]]:
        """Resolve which canonical fields are present; report any that aren't."""
        cmap = config.COLUMN_MAPS.get(self.platform, {})
        present = {src: canon for src, canon in cmap.items() if src in headers}
        missing_canon = set(cmap.values()) - set(present.values())
        gaps = (
            [DataGap(self.name, f"missing column(s) for: {', '.join(sorted(missing_canon))}")]
            if missing_canon
            else []
        )
        return present, gaps

    def fetch_campaign_metrics(self, range: DateRange) -> tuple[list[CampaignMetric], list[DataGap]]:
        rows, headers = self._rows()
        present, gaps = self._mapped(headers)
        if gaps:
            return [], gaps
        channel = config.CHANNEL_BY_PLATFORM.get(self.platform, self.platform)
        out: list[CampaignMetric] = []
        for row in rows:
            rec = {present[src]: row[src] for src in present}
            out.append(
                CampaignMetric(
                    channel=channel,
                    campaign=rec.get("campaign", ""),
                    utm_source=rec.get("utm_source", ""),
                    utm_medium=rec.get("utm_medium", ""),
                    utm_campaign=rec.get("utm_campaign", ""),
                    spend=float(rec.get("spend", 0) or 0),
                    **{f: int(float(rec.get(f, 0) or 0)) for f in _METRIC_INT_FIELDS},
                    date=_to_date(rec["date"]),
                )
            )
        return out, []

    def fetch_leads(self, range: DateRange) -> tuple[list[Lead], list[DataGap]]:
        rows, headers = self._rows()
        present, gaps = self._mapped(headers)
        if gaps:
            return [], gaps
        out: list[Lead] = []
        for row in rows:
            rec = {present[src]: row[src] for src in present}
            out.append(
                Lead(
                    id=rec.get("id", ""),
                    channel=rec.get("channel", ""),
                    utm_source=rec.get("utm_source", ""),
                    utm_medium=rec.get("utm_medium", ""),
                    utm_campaign=rec.get("utm_campaign", ""),
                    practice_area=rec.get("practice_area", ""),
                    stage=rec.get("stage", "visit"),
                    created_at=_to_date(rec["created_at"]),
                )
            )
        return out, []
