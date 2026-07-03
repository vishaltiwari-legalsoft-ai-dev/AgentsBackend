import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createCanvas, loadImage } from "canvas";
import { buildApp } from "../src/app.js";

const FONTS = fileURLToPath(
  new URL("../../agents/Graphics designer agent/Causten Font Family", import.meta.url));

function basePng(w, h) {
  const c = createCanvas(w, h);
  const x = c.getContext("2d");
  x.fillStyle = "#88AACC";
  x.fillRect(0, 0, w, h);
  return c.toBuffer("image/png");
}

const THEME = { dark: "#0F0F0F", white: "#FFFFFF",
                gradText: ["#86AFFE", "#2653AB"], ctaGrad: ["#FF8A3D", "#F26A1A"] };

export function makeReq(layers = [], w = 200, h = 250) {
  return { v: 1, base_w: w, base_h: h, px_scale: 1, theme: THEME,
           base_png_b64: basePng(w, h).toString("base64"), layers };
}

export const HEADLINE = {
  type: "text", id: "headline", text: "Hello World", highlight: "World",
  font: "Causten Bold", font_file: "Causten-Bold.otf", size_pct: 8, color: "dark",
  highlight_color: "gradient", placement: "left", offset: [0, 0], z: 10,
  pinned: false, x: 0.06, y: 0.5, w: 0.42, anchor: "ml",
};

const pngW = (buf) => buf.readUInt32BE(16);
const pngH = (buf) => buf.readUInt32BE(20);

test("render returns a PNG at base size", async () => {
  const app = buildApp({ fontsDir: FONTS });
  const res = await app.inject({ method: "POST", url: "/render", payload: makeReq() });
  assert.equal(res.statusCode, 200);
  assert.equal(res.headers["content-type"], "image/png");
  assert.equal(pngW(res.rawPayload), 200);
  assert.equal(pngH(res.rawPayload), 250);
});

test("a text layer changes the output pixels", async () => {
  const app = buildApp({ fontsDir: FONTS });
  const plain = await app.inject({ method: "POST", url: "/render", payload: makeReq() });
  const withText = await app.inject({ method: "POST", url: "/render",
                                      payload: makeReq([HEADLINE]) });
  assert.notDeepEqual(withText.rawPayload, plain.rawPayload);
});

test("a pinned multi-line layer renders without error", async () => {
  const layer = { ...HEADLINE, id: "subheading-0", text: "one\ntwo", highlight: "",
                  pinned: true, x: 0.5, y: 0.5, anchor: "mc", z: 11 };
  const app = buildApp({ fontsDir: FONTS });
  const res = await app.inject({ method: "POST", url: "/render", payload: makeReq([layer]) });
  assert.equal(res.statusCode, 200);
});

test("raster layers composite in z order without error", async () => {
  const spot = createCanvas(200, 250);
  const sx = spot.getContext("2d");
  sx.fillStyle = "rgba(255,0,0,1)";
  sx.fillRect(90, 115, 20, 20);
  const raster = { type: "raster", group: "shape", z: 5,
                   png_b64: spot.toBuffer("image/png").toString("base64") };
  const app = buildApp({ fontsDir: FONTS });
  const res = await app.inject({ method: "POST", url: "/render", payload: makeReq([raster]) });
  assert.equal(res.statusCode, 200);
});

async function pixelAt(buf, x, y, w, h) {
  const img = await loadImage(buf);
  const c = createCanvas(w, h);
  const cx = c.getContext("2d");
  cx.drawImage(img, 0, 0);
  return cx.getImageData(x, y, 1, 1).data;
}

test("a default CTA paints an orange pill at bottom-center", async () => {
  const cta = { type: "cta", id: "cta", text: "Book a call", font: "Causten Bold",
                font_file: "Causten-Bold.otf", size_pct: 3.4, color: "cta",
                placement: "bottom", offset: [0, 0], z: 20, pinned: false,
                x: 0.5, y: 0.94, w: 0.88, anchor: "bc" };
  const app = buildApp({ fontsDir: FONTS });
  const res = await app.inject({ method: "POST", url: "/render",
                                 payload: makeReq([cta], 400, 500) });
  assert.equal(res.statusCode, 200);
  // Pill sits at bottom center: sample a pixel just inside its left half.
  // my = trunc(0.06*500)=30 → pill bottom at 470; probe ~20px above bottom, center x.
  const [r, g, b] = await pixelAt(res.rawPayload, 200, 450, 400, 500);
  assert.ok(r > 180 && r > b, `expected orange-ish pill pixel, got rgb(${r},${g},${b})`);
});

test("a hex CTA color changes the pill pixels", async () => {
  const base = { type: "cta", id: "cta", text: "Book a call", font: "Causten Bold",
                 font_file: "Causten-Bold.otf", size_pct: 3.4, placement: "bottom",
                 offset: [0, 0], z: 20, pinned: false,
                 x: 0.5, y: 0.94, w: 0.88, anchor: "bc" };
  const app = buildApp({ fontsDir: FONTS });
  const orange = await app.inject({ method: "POST", url: "/render",
    payload: makeReq([{ ...base, color: "cta" }], 400, 500) });
  const green = await app.inject({ method: "POST", url: "/render",
    payload: makeReq([{ ...base, color: "#00AA00" }], 400, 500) });
  assert.notDeepEqual(green.rawPayload, orange.rawPayload);
});
