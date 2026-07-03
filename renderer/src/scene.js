// Konva scene builder: base image + Python-prerastered shape/element layers +
// text/CTA drawn natively. Text runs use a custom Shape with alphabetic
// baseline at y+ascent so vertical placement matches Pillow's top-of-ascent
// convention (Konva.Text centers within its own line box — do not use it here).
import Konva from "konva";
import { createCanvas, loadImage, registerFont } from "canvas";
import { readdirSync } from "node:fs";
import path from "node:path";
import {
  layoutAutoText, layoutPinnedText, layoutCta, resolveColor, headlineTokens,
} from "./textLayout.js";

let mctx = null; // created lazily AFTER registerFonts (node-canvas requirement)
const ctx2d = () => (mctx ??= createCanvas(1, 1).getContext("2d"));

export function registerFonts(dir) {
  for (const f of readdirSync(dir)) {
    if (/\.(otf|ttf)$/i.test(f)) {
      registerFont(path.join(dir, f), { family: path.parse(f).name });
    }
  }
}

export const fontFamilyFor = (fontFile) => path.parse(fontFile).name;

export const txt = {
  measure(family, px, s) {
    const c = ctx2d();
    c.font = `${px}px "${family}"`;
    return c.measureText(s).width;
  },
  metrics(family, px) {
    const c = ctx2d();
    c.font = `${px}px "${family}"`;
    const m = c.measureText("Mg");
    const asc = m.fontBoundingBoxAscent ?? m.emHeightAscent ?? px * 0.8;
    const desc = m.fontBoundingBoxDescent ?? m.emHeightDescent ?? px * 0.25;
    return { asc: Math.round(asc), desc: Math.round(desc) };
  },
};

const wordsOf = (s) => (s || "").split(/\s+/).filter(Boolean).map((w) => [w, false]);

function textRunNode(run, fill) {
  const w = Math.max(1, Math.ceil(txt.measure(run.fontFamily, run.px, run.text)));
  const { asc, desc } = txt.metrics(run.fontFamily, run.px);
  return new Konva.Shape({
    x: run.x, y: run.y, width: w, height: asc + desc, listening: false,
    sceneFunc(context, shape) {
      const c = context._context; // native 2d context
      c.font = `${run.px}px "${run.fontFamily}"`;
      c.textBaseline = "alphabetic";
      if (fill.kind === "grad") {
        const g = c.createLinearGradient(0, 0, w, 0); // per-run gradient, like Pillow
        g.addColorStop(0, fill.stops[0]);
        g.addColorStop(1, fill.stops[1]);
        c.fillStyle = g;
      } else {
        c.fillStyle = fill.color;
      }
      c.fillText(run.text, 0, asc);
      context.fillStrokeShape(shape);
    },
  });
}

function hexToRgba(hex, alpha) {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

function drawCta(layer, l, geo, theme, S) {
  const fill = resolveColor(l.color ?? "cta", theme, "cta");
  const shadowHex = fill.kind === "grad" ? fill.stops[1] : fill.color;
  const pad = Math.max(1, Math.round(40 * S));
  const blur = Math.max(1, Math.round(14 * S));
  const drop = Math.max(1, Math.round(10 * S));
  const shadow = new Konva.Rect({
    x: geo.x, y: geo.y + drop, width: geo.pw, height: geo.ph,
    cornerRadius: geo.radius, fill: hexToRgba(shadowHex, 90 / 255), listening: false,
  });
  shadow.cache({ x: -pad, y: -pad, width: geo.pw + 2 * pad, height: geo.ph + 2 * pad });
  shadow.filters([Konva.Filters.Blur]);
  shadow.blurRadius(blur);
  layer.add(shadow);
  const grad = fill.kind === "grad"
    ? { fillLinearGradientStartPoint: { x: 0, y: 0 },
        fillLinearGradientEndPoint: { x: geo.pw, y: 0 },
        fillLinearGradientColorStops: [0, fill.stops[0], 1, fill.stops[1]] }
    : { fill: fill.color };
  layer.add(new Konva.Rect({ x: geo.x, y: geo.y, width: geo.pw, height: geo.ph,
                             cornerRadius: geo.radius, listening: false, ...grad }));
  layer.add(textRunNode(
    { x: geo.x + geo.padX, y: geo.y + geo.padY, text: geo.label,
      px: geo.px, fontFamily: l.fontFamily },
    { kind: "solid", color: "#FFFFFF" }));
}

export async function renderRequest(req) {
  const { base_w: W, base_h: H, px_scale: S = 1, theme, layers } = req;
  const stage = new Konva.Stage({ width: W, height: H });
  const layer = new Konva.Layer({ listening: false });
  stage.add(layer);

  const base = await loadImage(Buffer.from(req.base_png_b64, "base64"));
  layer.add(new Konva.Image({ image: base, x: 0, y: 0, width: W, height: H }));

  const rasters = layers.filter((l) => l.type === "raster");
  for (const group of ["shape", "element"]) { // shapes behind elements, like Pillow
    const inGroup = rasters.filter((l) => l.group === group)
                           .sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
    for (const r of inGroup) {
      const img = await loadImage(Buffer.from(r.png_b64, "base64"));
      layer.add(new Konva.Image({ image: img, x: 0, y: 0, width: W, height: H }));
    }
  }

  const texts = layers
    .filter((l) => l.type === "text" || l.type === "cta")
    .map((l) => ({ ...l, fontFamily: fontFamilyFor(l.font_file) }));
  const auto = texts.filter((l) => !l.pinned);
  const pinned = texts.filter((l) => l.pinned).sort((a, b) => (a.z ?? 0) - (b.z ?? 0));

  // Auto path mirrors _parts_from_layers: ONLY headline / subheading-* / cta
  // participate (auto venue/website layers are dropped — Pillow does the same).
  const byId = new Map();
  const autoElems = [];
  for (const l of auto) {
    if (l.type !== "text") continue;
    if (l.id !== "headline" && !l.id.startsWith("subheading-")) continue;
    const isHead = l.id === "headline";
    byId.set(l.id, {
      main: resolveColor(l.color ?? "dark", theme, "dark"),
      hl: resolveColor(l.highlight_color ?? "gradient", theme, "gradient"),
    });
    autoElems.push({
      id: l.id,
      tokens: isHead ? headlineTokens(l.text || "", l.highlight || "") : wordsOf(l.text),
      fontFamily: l.fontFamily, size_pct: l.size_pct, placement: l.placement,
      offset: l.offset, lineGap: isHead ? 1.15 : 1.4,
    });
  }
  for (const run of layoutAutoText(autoElems, W, H, S, txt)) {
    const colors = byId.get(run.elemId);
    layer.add(textRunNode(run, run.highlight ? colors.hl : colors.main));
  }
  const autoCta = auto.find((l) => l.type === "cta");
  if (autoCta && (autoCta.text || "").trim()) {
    drawCta(layer, autoCta, layoutCta(autoCta, W, H, S, txt, null), theme, S);
  }

  for (const l of pinned) {
    if (l.type === "cta") {
      drawCta(layer, l,
        layoutCta(l, W, H, S, txt, { x: l.x, y: l.y, anchor: l.anchor }), theme, S);
    } else {
      const main = resolveColor(l.color ?? "dark", theme, "dark");
      const hl = resolveColor(l.highlight_color ?? "gradient", theme, "gradient");
      for (const run of layoutPinnedText(l, W, H, S, txt)) {
        layer.add(textRunNode(run, run.highlight ? hl : main));
      }
    }
  }

  const dataUrl = stage.toDataURL({ pixelRatio: 1, mimeType: "image/png" });
  stage.destroy();
  return Buffer.from(dataUrl.split(",")[1], "base64");
}
