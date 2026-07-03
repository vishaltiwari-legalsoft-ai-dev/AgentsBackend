import { test } from "node:test";
import assert from "node:assert/strict";
import { buildApp } from "../src/app.js";

test("health responds ok", async () => {
  const app = buildApp();
  const res = await app.inject({ method: "GET", url: "/health" });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), { ok: true });
});

test("render is wired (501 until the scene builder lands)", async () => {
  const app = buildApp();
  const res = await app.inject({ method: "POST", url: "/render", payload: { v: 1 } });
  assert.equal(res.statusCode, 501);
});
