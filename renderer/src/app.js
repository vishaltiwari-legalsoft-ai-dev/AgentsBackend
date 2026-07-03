import Fastify from "fastify";
import { registerFonts, renderRequest } from "./scene.js";

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
  return app;
}
