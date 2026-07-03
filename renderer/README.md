# gd-renderer — Node/Konva Stage-3 render service

Renders the graphics-designer layout-JSON contract (v1) to PNG. The FastAPI
backend calls it when `GD_RENDERER=konva` and `GD_RENDERER_URL` are set; any
failure falls back to the Pillow engine automatically.

## Local dev
    npm install
    npm start          # :8090, fonts auto-resolved from ../agents/.../Causten Font Family
    npm test

## Rollout (Cloud Run)
1. `docker build -f renderer/Dockerfile -t gd-renderer .` (from backend/)
2. Deploy as a SEPARATE Cloud Run service (private, same region).
3. On the main backend service set: `GD_RENDERER=konva`,
   `GD_RENDERER_URL=https://<gd-renderer-url>`.
4. Rollback = unset `GD_RENDERER` (instant, no deploy of this service needed).

## Parity
`tests/test_konva_parity.py` in the agent test suite (opt-in via
GD_RENDERER_URL) bounds the Pillow↔Konva mean pixel difference.
