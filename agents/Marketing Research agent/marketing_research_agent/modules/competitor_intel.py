"""Competitor Intelligence (requirements §3.2).

Snapshots each tracked competitor's page, diffs against the previous snapshot
(by content hash), and summarizes material changes via the analysis brain.
Network failures degrade to a note — the digest never crashes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .. import analysis, config
from ..schemas import CompetitorSnapshot
from ..sources import web_source


def snapshot(
    competitor: str, url: str, fetcher: Callable[[str], str] | None = None
) -> CompetitorSnapshot:
    digest, text = web_source.fetch(url, fetcher=fetcher)
    return CompetitorSnapshot(
        competitor=competitor,
        url=url,
        captured_at=datetime.now(timezone.utc).isoformat(),
        content_hash=digest,
        text=text,
    )


def diff(previous: CompetitorSnapshot | None, current: CompetitorSnapshot) -> dict:
    if previous is None:
        return {"changed": True, "summary": f"First snapshot captured for {current.competitor}."}
    if previous.content_hash == current.content_hash:
        return {"changed": False, "summary": f"No changes detected for {current.competitor}."}
    summary = analysis.narrate(
        "competitor_digest",
        {
            "competitor": current.competitor,
            "before": previous.text[:1500],
            "after": current.text[:1500],
        },
    )
    return {"changed": True, "summary": summary}


def refresh_all(
    previous_by_name: dict[str, CompetitorSnapshot],
    fetcher: Callable[[str], str] | None = None,
) -> list[dict]:
    """Snapshot + diff all tracked competitors. Returns one result per competitor."""
    results = []
    for comp in config.COMPETITORS:
        try:
            cur = snapshot(comp["name"], comp["url"], fetcher=fetcher)
            d = diff(previous_by_name.get(comp["name"]), cur)
            results.append({"competitor": comp["name"], "snapshot": cur, **d})
        except Exception as exc:  # network failure -> degrade, don't crash
            results.append(
                {
                    "competitor": comp["name"],
                    "changed": False,
                    "summary": f"Could not refresh {comp['name']}: {exc}",
                    "snapshot": None,
                }
            )
    return results
