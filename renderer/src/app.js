import Fastify from "fastify";

export function buildApp() {
  const app = Fastify({ bodyLimit: 64 * 1024 * 1024 });
  app.get("/health", async () => ({ ok: true }));
  app.post("/render", async (req, reply) => {
    reply.code(501);
    return { error: "renderer not implemented yet" };
  });
  return app;
}
