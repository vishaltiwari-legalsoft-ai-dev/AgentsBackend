import { test } from "node:test";
import assert from "node:assert/strict";
import {
  anchorToXY, zone, headlineTokens, wrap, resolveColor, fontPx,
  layoutAutoText, layoutPinnedText, layoutCta,
} from "../src/textLayout.js";

// Fake typography: every char 10px wide, ascent 80, descent 20 (px-independent).
const txt = {
  measure: (_family, _px, s) => s.length * 10,
  metrics: () => ({ asc: 80, desc: 20 }),
};
const THEME = { dark: "#0F0F0F", white: "#FFFFFF",
                gradText: ["#86AFFE", "#2653AB"], ctaGrad: ["#FF8A3D", "#F26A1A"] };

test("anchorToXY mirrors layout.anchor_to_xy", () => {
  assert.deepEqual(anchorToXY(0.5, 0.5, 100, 50, "mc", 1000, 800), [450, 375]);
  assert.deepEqual(anchorToXY(0.06, 0.5, 100, 50, "ml", 1000, 800), [60, 375]);
  assert.deepEqual(anchorToXY(0.5, 0.5, 100, 50, "??", 1000, 800), [450, 375]); // bad → mc
});

test("zone mirrors the Pillow placement zones", () => {
  assert.deepEqual(zone("left", 1000, 800, 60), [420, "left", "center"]);
  assert.deepEqual(zone("center", 1000, 800, 60), [600, "center", "center"]);
  assert.deepEqual(zone("top", 1000, 800, 60), [880, "center", "top"]);
});

test("headlineTokens flags the highlight run", () => {
  assert.deepEqual(headlineTokens("Grow Your Firm", "Your"),
    [["Grow", false], ["Your", true], ["Firm", false]]);
});

test("wrap is greedy on width incl. joining spaces", () => {
  const tokens = [["aaaa", false], ["bbbb", false], ["cccc", false]]; // 40px each
  const lines = wrap(tokens, 95, (s) => s.length * 10, 10);
  assert.equal(lines.length, 2);                       // 40+10+40=90 fits; +50 doesn't
  assert.deepEqual(lines[1], [["cccc", false]]);
});

test("resolveColor handles tokens, hex, and bad input fallback", () => {
  assert.deepEqual(resolveColor("#AB12CD", THEME, "dark"), { kind: "solid", color: "#AB12CD" });
  assert.deepEqual(resolveColor("cta", THEME, "cta"), { kind: "grad", stops: THEME.ctaGrad });
  assert.deepEqual(resolveColor("nope", THEME, "white"), { kind: "solid", color: "#FFFFFF" });
});

test("layoutAutoText stacks two left-placed elements with the 2.5% gap", () => {
  const elems = [
    { id: "headline", tokens: [["Hi", false]], fontFamily: "F", size_pct: 8,
      placement: "left", offset: [0, 0], lineGap: 1.15 },
    { id: "subheading-0", tokens: [["there", false]], fontFamily: "F", size_pct: 3,
      placement: "left", offset: [0, 0], lineGap: 1.4 },
  ];
  const runs = layoutAutoText(elems, 1000, 800, 1, txt);
  // lh_head = trunc(100*1.15)=114, lh_sub = trunc(100*1.4)=140, gap = trunc(0.025*800)=20
  // total = 114+140+20 = 274 → y0 = floor((800-274)/2) = 263; x = mx = 60
  assert.deepEqual(runs.map(r => [r.text, r.x, r.y]),
    [["Hi", 60, 263], ["there", 60, 263 + 114 + 20]]);
});

test("layoutPinnedText honors \\n and anchors the wrapped box", () => {
  const layer = { id: "subheading-0", text: "one\ntwo", highlight: "", size_pct: 3,
                  fontFamily: "F", color: "dark", highlight_color: "gradient",
                  x: 0.5, y: 0.5, w: 0.42, anchor: "mc", offset: [0, 0] };
  const runs = layoutPinnedText(layer, 1000, 800, 1, txt);
  assert.equal(runs.length, 2);
  // box: w=max(30,30)=30, lh=trunc(100*1.4)=140, h=280 → top = round(400-140)=260
  assert.deepEqual([runs[0].y, runs[1].y], [260, 400]);
  assert.equal(runs[0].x, Math.round(0.5 * 1000 - 15));
});

test("layoutCta computes the pill box like _draw_cta", () => {
  const geo = layoutCta({ text: "Call now ", size_pct: 3.4, fontFamily: "F",
                          placement: "bottom", offset: [0, 0] }, 1000, 800, 1, txt, null);
  assert.equal(geo.label, "Call now  →");
  // th=100 → padX=90, padY=55; tw=11*10=110 → pw=290, ph=210, radius=105
  assert.deepEqual([geo.pw, geo.ph, geo.radius], [290, 210, 105]);
  assert.deepEqual([geo.x, geo.y], [Math.floor((1000 - 290) / 2), 800 - 48 - 210]);
});
