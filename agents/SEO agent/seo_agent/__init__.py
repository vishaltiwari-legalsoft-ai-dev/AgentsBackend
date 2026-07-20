"""SEO + GEO agent (marketing department) — Legal Soft.

Frontend card ``a2`` ("SEO Analyst"). Two capabilities:
- P1 content optimizer: analyze a keyword's live SERP into a Benchmark, then
  score drafts against it live (pure function, no I/O in the hot path).
- P2 GEO skeleton: weekly AI-answer visibility runs → GEO score /10 per brand.

Importable package root: ``seo_agent`` (outer folder has spaces and goes on
``sys.path`` — same pattern as ``marketing_research_agent``).
"""

from __future__ import annotations
