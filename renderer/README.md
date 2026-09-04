# gd-renderer — Node render service

Two unrelated jobs behind one Fastify process, because they share a base image
and neither justifies its own service:

- `POST /render` — graphics-designer layout-JSON (v1) → **PNG**, via Konva +
  node-canvas. The FastAPI backend calls it when `GD_RENDERER=konva` and
  `GD_RENDERER_URL` are set; any failure falls back to the Pillow engine.
- `POST /pdf` — self-contained HTML → **PDF**, via headless Chromium. Serves the
  Marketing Research board report.

`/render` and `/pdf` do not share an engine. node-canvas (cairo/pango/rsvg) is a
2D drawing surface and cannot lay out HTML; the board report is CSS grid,
`@media print`, `break-inside` and `thead{display:table-header-group}`, so it
needs a real browser engine. That is the whole reason Chromium is in the image.

## Local dev
    npm install
    npm start          # :8090, fonts auto-resolved from ../agents/.../Causten Font Family
    npm test           # /pdf render tests self-skip when no Chromium is present

Environment variables:
- `PORT` (default `8090`): server listen port.
- `FONTS_DIR` (default: the repo's `agents/Graphics designer agent/Causten Font
  Family`): TrueType directory for **node-canvas only**. Chromium does not read
  it — it resolves fonts through fontconfig.
- `CHROME_PATH` (default `/usr/bin/chromium`, or installed Chrome on Windows):
  the browser `/pdf` drives.
- `RENDERER_TOKEN`: shared secret for `/pdf`. **Unset ⇒ `/pdf` returns 503.**
- `PDF_CONCURRENCY` (default `2`), `PDF_TIMEOUT_MS` (default `60000`).

## `POST /pdf`

    POST /pdf
    X-Renderer-Token: <RENDERER_TOKEN>
    {"v": 1, "html": "<!DOCTYPE html>…"}

    200 application/pdf
    X-Blocked-Subresources: 0

Rendering options are not negotiable by the caller and are fixed in `src/pdf.js`:

- `printBackground: true` — **mandatory**. The board report's entire visual
  identity is background colour (navy cover, gold cells, navy group bands).
  Without it the PDF prints as white paper.
- `preferCSSPageSize: true` — honours the document's own `@page{size:A4;
  margin:13mm 11mm}` instead of imposing a page size.

The document must be **self-contained**: no `<script>`, `<link>`, `<img>`,
`url()` or any external reference; charts inline SVG. This is enforced, not
assumed — JavaScript is disabled in the page and every subresource request is
aborted. `X-Blocked-Subresources` reports how many were aborted; a non-zero
count means the document grew an external dependency and is rendering wrong.

Note that A4 content width is **711 CSS px** (188mm at 96dpi). A document with a
`@media(max-width:900px)` breakpoint will render in its *mobile* layout.

### Fonts
`node:20-bookworm-slim` ships almost no fonts, so the image installs
`fonts-liberation2` (Arial/Times/Courier metric equivalents) and
`fonts-dejavu-core` (DejaVu Sans Mono). Without them Chromium falls back to a
last-resort face and the sans/serif/mono distinction the report relies on
collapses. To match a designer's browser exactly, add the real faces the report
asks for (Inter, Fraunces, IBM Plex Mono) to the fonts directory.

## Rollout (Cloud Run)
1. `docker build -f renderer/Dockerfile -t gd-renderer .` (from `backend/`)
2. Deploy as a SEPARATE Cloud Run service, **private**, same region.
3. `/pdf` callers need both `roles/run.invoker` and `RENDERER_TOKEN`.
4. For the Konva path, set on the main backend: `GD_RENDERER=konva`,
   `GD_RENDERER_URL=https://<gd-renderer-url>`.

   **WARNING:** After setting `GD_RENDERER=konva`, watch the main backend's logs
   for `"falling back to Pillow"`. The fallback is silent by design — recurring
   warnings mean Konva is NOT serving (403 IAM, network timeout).

5. Rollback of the Konva path = unset `GD_RENDERER` (instant, no deploy here).

**Never patch this service with a minimal v2 `UpdateService` body.** v2
UpdateService is full-replace: any service-level field absent from the request
resets to default. Read-modify-write the full service (v1 `ReplaceService`, or
real `gcloud run deploy`). See `scripts/repair_invoker_flag.py`.

**Security:** `/render` has no authentication and must not be publicly
reachable — keep ingress internal. `/pdf` requires `RENDERER_TOKEN` and fails
closed without it, but that secret is a second lock, not the first: IAM and
ingress are.

## Parity
`tests/test_konva_parity.py` in the agent test suite (opt-in via
`GD_RENDERER_URL`) bounds the Pillow↔Konva mean pixel difference.
