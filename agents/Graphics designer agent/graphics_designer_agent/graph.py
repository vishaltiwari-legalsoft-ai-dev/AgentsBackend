"""Legacy entry point. The Graphics Designer agent is a human-in-the-loop,
multi-stage pipeline (spec §4), so there is no single-shot ``run_agent``. Use the
``pipeline`` module (create_run → generate → approve per stage) via the
``/api/gd/*`` router instead.
"""

from __future__ import annotations

from .runs import create_run


def run_agent(message: str, brand_id: str | None = None, **kwargs) -> dict:
    """Compatibility shim: start a new pipeline run and return its initial state.

    Real interaction happens stage-by-stage through ``graphics_designer_agent.pipeline``
    and the ``/api/gd`` endpoints, not through one blocking call.
    """
    user_id = str(kwargs.get("user_id") or "anonymous")
    return create_run(user_id=user_id, brand_id=brand_id)
