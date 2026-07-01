"""Delivery hook for finished reports.

Pluggable by design: the default ``log`` channel records that a report is ready
and where to find it. Email/Slack/inbox connectors implement the same
``deliver`` signature and register a new channel later.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agentos.mr.notify")

_CHANNELS: dict[str, "Channel"] = {}


class Channel:
    def send(self, report: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def register(name: str, channel: "Channel") -> None:
    _CHANNELS[name] = channel


def deliver(report: dict, channel: str = "log") -> None:
    impl = _CHANNELS.get(channel)
    if impl is not None:
        impl.send(report)
        return
    logger.info(
        "MR report ready: kind=%s id=%s (channel=%s)",
        report.get("kind"),
        report.get("id"),
        channel,
    )
