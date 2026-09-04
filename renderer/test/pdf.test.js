import { test, after } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { inflateSync } from "node:zlib";
import { buildApp } from "../src/app.js";
import { chromePath, closeBrowser } from "../src/pdf.js";

/** Concatenated, inflated content streams — where the paint operators live. */
function contentStreams(buf) {
  const s = buf.toString("latin1");
  const out = [];
  for (const m of s.matchAll(/stream\r?\n/g)) {
    const start = m.index + m[0].length;
    const end = s.indexOf("endstream", start);
    if (end < 0) continue;
    try { out.push(inflateSync(buf.subarray(start, end)).toString("latin1")); }
    catch { /* font programs and other non-Flate streams */ }
  }
  return out.join("\n");
}

const TOKEN = "test-token-not-a-real-secret";
const body = (html) => ({ v: 1, html });
const DOC = `<!DOCTYPE html><html><head><style>
@page{size:A4;margin:13mm 11mm}
html,body,*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;background:#FBFAF7}
.cover{background:#14213A;color:#FBFAF7;padding:40px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:10px}
</style></head><body><div class="cover"><h1>Board report</h1></div>
<div class="g"><div>a</div><div>b</div></div>
<svg width="80" height="40"><rect width="80" height="40" fill="#C9A227"/></svg>
</body></html>`;

// Chromium is a runtime dependency of /pdf, not of the test host. Where it is
// absent (a bare CI box) the auth and contract tests below still run.
const HAVE_CHROME = existsSync(chromePath());
after(() => closeBrowser());

test("pdf requires a token — and fails closed when unconfigured", async (t) => {
  delete process.env.RENDERER_TOKEN;
  const app = buildApp();
  const res = await app.inject({ method: "POST", url: "/pdf", payload: body(DOC) });
  assert.equal(res.statusCode, 503, "no RENDERER_TOKEN must not render");
});

test("pdf rejects a wrong or missing token", async () => {
  process.env.RENDERER_TOKEN = TOKEN;
  const app = buildApp();
  const missing = await app.inject({ method: "POST", url: "/pdf", payload: body(DOC) });
  assert.equal(missing.statusCode, 401);
  const wrong = await app.inject({
    method: "POST", url: "/pdf", payload: body(DOC),
    headers: { "x-renderer-token": "wrong" },
  });
  assert.equal(wrong.statusCode, 401);
  delete process.env.RENDERER_TOKEN;
});

test("pdf rejects an unknown contract version and empty html", async () => {
  process.env.RENDERER_TOKEN = TOKEN;
  const app = buildApp();
  const h = { "x-renderer-token": TOKEN };
  const bad = await app.inject({ method: "POST", url: "/pdf", payload: { v: 99 }, headers: h });
  assert.equal(bad.statusCode, 400);
  const empty = await app.inject({ method: "POST", url: "/pdf", payload: body("  "), headers: h });
  assert.equal(empty.statusCode, 400);
  delete process.env.RENDERER_TOKEN;
});

test("render and health stay open — /pdf's auth must not leak onto them", async () => {
  const app = buildApp();
  const health = await app.inject({ method: "GET", url: "/health" });
  assert.equal(health.statusCode, 200);
  const render = await app.inject({ method: "POST", url: "/render", payload: { v: 99 } });
  assert.equal(render.statusCode, 400, "expected /render's own 400, not 401/503");
});

test("pdf returns an A4 PDF with backgrounds painted", { skip: !HAVE_CHROME }, async () => {
  process.env.RENDERER_TOKEN = TOKEN;
  const app = buildApp();
  const res = await app.inject({
    method: "POST", url: "/pdf", payload: body(DOC),
    headers: { "x-renderer-token": TOKEN },
  });
  assert.equal(res.statusCode, 200);
  assert.equal(res.headers["content-type"], "application/pdf");
  assert.equal(res.headers["x-blocked-subresources"], "0",
    "the report contract is self-contained — a blocked request means it is not");
  const buf = res.rawPayload;
  assert.equal(buf.subarray(0, 5).toString("latin1"), "%PDF-");

  const text = buf.toString("latin1");
  // preferCSSPageSize honoured the document's @page{size:A4}. Chrome emits
  // 594.96 x 841.92pt (209.9 x 297.0mm) rather than the nominal 595.28 x
  // 841.89 — A4 to within a rounding step, so assert a tolerance, not equality.
  const box = text.match(/\/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]/);
  assert.ok(box, "no /MediaBox in the PDF");
  const [w, h] = [Number(box[1]), Number(box[2])];
  assert.ok(Math.abs(w - 595.28) < 1 && Math.abs(h - 841.89) < 1,
    `expected A4 from the document's own @page rule, got ${w}x${h}pt`);
  // printBackground:true — the navy cover must be a painted rectangle, not
  // white paper. Content streams are Flate-compressed, so inflate before
  // looking for the fill: #14213A == ".0784 .1294 .2275 rg" followed by "re f".
  assert.match(contentStreams(buf), /\.0784 \.1294 \.2275 rg[\s\S]{0,40}re\s*\nf/,
    "navy cover background missing — printBackground did not take effect");
  delete process.env.RENDERER_TOKEN;
});
