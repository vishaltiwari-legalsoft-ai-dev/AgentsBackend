"""GEO agent (a10) — AI answer-engine visibility + content optimization.

- ``geo_engines`` / ``geo_prompts`` / ``geo_poll`` / ``geo_metrics`` — Layer 7:
  poll AI engines for a brand's prompt set; honest visibility / share-of-voice /
  source-gap math (every rate carries its n, variance never hidden).
- ``geo_window`` — the ONE way to read stored answers: a brand's measured window
  over N clamped days, fetched in a single batch, with the config, alias map and
  report assembled on it. Every read path goes through it, so a new one inherits
  the batched fetch and the right engine constant by construction.
- ``opt_config`` / ``opt_terms`` / ``opt_structure`` / ``opt_score`` — the
  content-optimizer core (Layers 3/5/6: term importance, robust structural
  bands, 0-100 pattern-match score). Extraction, semantics and the live
  pipeline land here in later phases.

Brand registry and persistence are shared services owned by a2
(``seo_geo_agent.state`` / ``sources`` / ``insights``) — imported, not duplicated.
"""
