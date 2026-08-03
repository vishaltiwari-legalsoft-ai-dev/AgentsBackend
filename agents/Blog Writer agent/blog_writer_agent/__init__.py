"""Blog Writer agent (a9) — deep-research blog drafting per brand.

Spec: docs/superpowers/specs/2026-08-03-blog-writer-rebuild-design.md

- ``state``     — Firestore persistence (collection ``blog_writer``) with a
                  local-JSON fallback when ``BLOG_OFFLINE=1``.
- ``llm``       — reasoning-model adapter stamping ``agent_id="a9"``.
- ``inventory`` — sitemap scan → the brand's published blog list.
- ``research``  — gap-driven multi-round research building an evidence ledger.
- ``drafting``  — ledger-grounded draft blocks + per-block revision.
- ``visuals``   — the agent's own visual plan (count/type/theme/prompt).
- ``export``    — md / html / txt renderers + the visual-prompt document.

Deliberate seams: ``seo_geo_agent.sources`` (Serper search, page fetch,
sitemap, CredentialMissing) and ``seo_geo_agent.insights.list_brands()`` —
brands stay managed in one place.
"""
