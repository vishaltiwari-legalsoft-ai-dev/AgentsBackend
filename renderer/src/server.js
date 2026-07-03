import { fileURLToPath } from "node:url";
import { buildApp } from "./app.js";

const fontsDir = process.env.FONTS_DIR ?? fileURLToPath(
  new URL("../../agents/Graphics designer agent/Causten Font Family", import.meta.url));
const app = buildApp({ fontsDir });
app.listen({ port: Number(process.env.PORT ?? 8090), host: "0.0.0.0" })
  .then((addr) => console.log(`gd-renderer listening on ${addr}`));
