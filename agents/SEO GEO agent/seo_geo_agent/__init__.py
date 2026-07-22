"""SEO agent (a2) — per-brand search insights, traffic-estimated to-dos, blog topic lab.

Business surface (what the dashboard shows), not an audit engine:
- ``insights``  — per-brand GSC pull -> plain-language findings + a to-do list where
  every item carries an estimated monthly-clicks gain.
- ``topics``    — blog topic lab: seed keywords -> Serper expansion -> volume/trend
  proxies from GSC -> ranked, explained topic list.
- ``state``     — Firestore persistence (local JSON when SEO_OFFLINE=1; tests force this).
- ``sources``   — Google Search Console + Serper.dev adapters, graceful degradation.

GEO (AI-answer citations) intentionally deferred; SEO part first per product owner.
"""
