import { buildApp } from "./app.js";

const app = buildApp();
app.listen({ port: Number(process.env.PORT ?? 8090), host: "0.0.0.0" })
  .then((addr) => console.log(`gd-renderer listening on ${addr}`));
