"""Shared agent state for the Graphics Designer agent."""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    """LangGraph state passed between nodes. Add fields as the pipeline grows."""

    message: str
    brand_id: str | None
