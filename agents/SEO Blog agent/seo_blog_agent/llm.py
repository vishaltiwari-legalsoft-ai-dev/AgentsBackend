"""SEO Blog agent LLM adapters.

The blog pipeline reuses the shared ``seo_geo_agent.sources`` helpers; these
partials bind them to agent ``a9`` so the creator's per-agent model override
(Agent Configuration panel) applies to every blog LLM call. Modules default to
these instead of the raw helpers.
"""

from __future__ import annotations

from functools import partial

from seo_geo_agent import sources

AGENT_ID = "a9"

llm_text = partial(sources.llm_text, agent_id=AGENT_ID)
llm_json = partial(sources.llm_json, agent_id=AGENT_ID)
