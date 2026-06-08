"""Shared state for the LangGraph agent."""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # Inputs
    message: str
    brand_id: Optional[str]

    # Derived during the run
    intent: str  # "analyze" | "generate"
    category: str  # banner | flyer | brochure | social_post | advertisement | poster
    brand: Optional[dict[str, Any]]
    samples: list[dict[str, Any]]
    logo: Optional[dict[str, Any]]  # {"file_name":.., "view_url":..} for Canva export
    master_prompt: str
    images: dict[str, dict[str, Any]]  # {"with_logo": {"bytes":..,"mime":..}, ...}

    # Final output returned to the API layer
    result: dict[str, Any]
