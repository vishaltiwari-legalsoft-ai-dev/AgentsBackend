import { createHash, timingSafeEqual } from "node:crypto";
import Fastify from "fastify";
import { registerFonts, renderRequest } from "./scene.js";
import { renderPdf } from "./pdf.js";

// /pdf executes caller-supplied HTML in a browser, so unlike /render it is
// never left open. Transport auth is Cloud Run IAM (private ingress + a single
// run.invoker service account); this shared secret is the second lock, because
// this project has already lost an ingress annotation once (2026-08-31) and a
// service that is briefly public must still refuse the request.
//
// Fail CLOSED: with RENDERER_TOKEN unset the route answers 503, never 200.
const digest = (s) => createHash("sha256").update(String(s)).digest();

export function pdfAuth(req, reply) {
  const expected = process.env.RENDERER_TOKEN ?? "";
  if (!expected) {
    reply.code(503);
    return { error: "pdf rendering unconfigured" };
  }
  const presented = req.headers["x-renderer-token"] ?? "";
  if (!timingSafeEqual(digest(expected), digest(presented))) {
    reply.code(401);
    return { error: "unauthorized" };
  }
  return null;
}

export function buildApp({ fontsDir } = {}) {
  if (fontsDir) registerFonts(fontsDir);
  const app = Fastify({ bodyLimit: 64 * 1024 * 1024 });
  app.get("/health", async () => ({ ok: true }));
  app.post("/render", async (req, reply) => {
    if (req.body?.v !== 1) {
      reply.code(400);
      return { error: "unsupported contract version" };
    }
    const png = await renderRequest(req.body);
    reply.type("image/png");
    return png;
  });
  app.post("/pdf", async (req, reply) => {
    const denied = pdfAuth(req, reply);
    if (denied) return denied;
    if (req.body?.v !== 1) {
      reply.code(400);
      return { error: "unsupported contract version" };
    }
    const html = req.body?.html;
    if (typeof html !== "string" || !html.trim()) {
      reply.code(400);
      return { error: "html required" };
    }
    const { pdf, blocked } = await renderPdf(html);
    reply.type("application/pdf");
    // A self-contained document blocks nothing. A non-zero count means the
    // report grew an external dependency and is silently rendering wrong.
    reply.header("x-blocked-subresources", String(blocked));
    return pdf;
  });
  return app;
}
