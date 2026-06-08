"""LangGraph workflow assembly for AgentOS.

Flow:
    retrieve_context -> decide_intent --(analyze)--> analyze_brand -> END
                                       --(generate)-> categorize
                                                      -> build_master_prompt
                                                      -> generate_assets
                                                      -> persist -> END
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from langgraph.graph import END, StateGraph

from app.agent import nodes
from app.agent.state import AgentState


@lru_cache
def _build_app():
    builder = StateGraph(AgentState)

    builder.add_node("retrieve_context", nodes.retrieve_context)
    builder.add_node("decide_intent", nodes.decide_intent)
    builder.add_node("analyze_brand", nodes.analyze_brand)
    builder.add_node("categorize", nodes.categorize)
    builder.add_node("build_master_prompt", nodes.build_master_prompt)
    builder.add_node("generate_assets", nodes.generate_assets)
    builder.add_node("persist", nodes.persist)

    builder.set_entry_point("retrieve_context")
    builder.add_edge("retrieve_context", "decide_intent")
    builder.add_conditional_edges(
        "decide_intent",
        nodes.route_by_intent,
        {"analyze": "analyze_brand", "generate": "categorize"},
    )
    builder.add_edge("analyze_brand", END)
    builder.add_edge("categorize", "build_master_prompt")
    builder.add_edge("build_master_prompt", "generate_assets")
    builder.add_edge("generate_assets", "persist")
    builder.add_edge("persist", END)

    return builder.compile()


def run_agent(message: str, brand_id: Optional[str] = None) -> dict[str, Any]:
    """Invoke the compiled agent and return its structured result."""
    final_state = _build_app().invoke({"message": message, "brand_id": brand_id})
    return final_state["result"]
