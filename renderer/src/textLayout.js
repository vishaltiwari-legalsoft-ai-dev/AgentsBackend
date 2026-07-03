// Line-for-line port of text_overlay.py / layout.py geometry. Python int() →
// Math.trunc, // → Math.floor, round() → Math.round. Do not "improve" quirks —
// the Pillow engine is the parity reference.
export const ANCHORS = ["tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br"];
const FRAC = { l: 0, c: 0.5, r: 1, t: 0, m: 0.5, b: 1 };
const HEX_RE = /^#[0-9a-fA-F]{6}$/;
export const MARGIN = 0.06;

export function fontPx(sizePct, baseW) {
  return Math.max(6, Math.round((sizePct / 100) * baseW));
}

export function anchorToXY(x, y, w, h, anchor, cw, ch) {
  const a = ANCHORS.includes(anchor) ? anchor : "mc";
  return [Math.round(x * cw - FRAC[a[1]] * w), Math.round(y * ch - FRAC[a[0]] * h)];
}

export function zone(placement, baseW, baseH, mx) {
  if (placement === "right") return [Math.trunc(0.42 * baseW), "right", "center"];
  if (placement === "center") return [Math.trunc(0.60 * baseW), "center", "center"];
  if (placement === "top") return [baseW - 2 * mx, "center", "top"];
  if (placement === "bottom") return [baseW - 2 * mx, "center", "bottom"];
  return [Math.trunc(0.42 * baseW), "left", "center"];
}

const words = (s) => s.split(/\s+/).filter(Boolean);

export function headlineTokens(text, highlight) {
  if (highlight && text.includes(highlight)) {
    const i = text.indexOf(highlight);
    return [
      ...words(text.slice(0, i)).map((w) => [w, false]),
      ...words(highlight).map((w) => [w, true]),
      ...words(text.slice(i + highlight.length)).map((w) => [w, false]),
    ];
  }
  return words(text).map((w) => [w, false]);
}

export function wrap(tokens, maxW, measure, space) {
  const lines = [];
  let cur = [], curW = 0;
  for (const tok of tokens) {
    const ww = measure(tok[0]);
    const add = ww + (cur.length ? space : 0);
    if (cur.length && curW + add > maxW) { lines.push(cur); cur = [tok]; curW = ww; }
    else { cur.push(tok); curW += add; }
  }
  if (cur.length) lines.push(cur);
  return lines.length ? lines : [[["", false]]];
}

export function resolveColor(spec, theme, dflt) {
  if (typeof spec === "string" && HEX_RE.test(spec)) return { kind: "solid", color: spec };
  if (spec === "white") return { kind: "solid", color: theme.white };
  if (spec === "dark") return { kind: "solid", color: theme.dark };
  if (spec === "gradient") return { kind: "grad", stops: theme.gradText };
  if (spec === "cta") return { kind: "grad", stops: theme.ctaGrad };
  if (spec !== dflt) return resolveColor(dflt, theme, "dark");
  return { kind: "solid", color: theme.dark };
}

const lineWidths = (lines, measure, space) =>
  lines.map((ln) => ln.reduce((s, t) => s + measure(t[0]), 0) + space * Math.max(0, ln.length - 1));

export function layoutAutoText(elems, baseW, baseH, pxScale, txt) {
  const mx = Math.trunc(MARGIN * baseW), my = Math.trunc(MARGIN * baseH);
  const gap = Math.trunc(0.025 * baseH);
  const groups = new Map();
  for (const el of elems) {
    const px = fontPx(el.size_pct, baseW);
    const measure = (s) => txt.measure(el.fontFamily, px, s);
    const space = measure(" ");
    const [maxW, ha, va] = zone(el.placement, baseW, baseH, mx);
    const lines = wrap(el.tokens, maxW, measure, space);
    const { asc, desc } = txt.metrics(el.fontFamily, px);
    const lh = Math.trunc((asc + desc) * el.lineGap);
    const widths = lineWidths(lines, measure, space);
    const lay = { px, lines, lh, space, ha, va, widths,
                  height: lh * lines.length, measure };
    if (!groups.has(el.placement)) groups.set(el.placement, []);
    groups.get(el.placement).push([el, lay]);
  }
  const runs = [];
  for (const items of groups.values()) {
    const va = items[0][1].va;
    const total = items.reduce((s, [, l]) => s + l.height, 0) + gap * Math.max(0, items.length - 1);
    let y = va === "top" ? my
          : va === "bottom" ? baseH - my - total
          : Math.floor((baseH - total) / 2);
    for (const [el, lay] of items) {
      const ey = y + Math.round(el.offset[1] * pxScale);
      lay.lines.forEach((ln, i) => {
        const lw = lay.widths[i];
        let x = lay.ha === "right" ? baseW - mx - lw
              : lay.ha === "center" ? (baseW - lw) / 2
              : mx;
        x += Math.round(el.offset[0] * pxScale);
        const yy = ey + i * lay.lh;
        let cx = x;
        for (const tok of ln) {
          runs.push({ x: Math.trunc(cx), y: Math.trunc(yy), text: tok[0], px: lay.px,
                      fontFamily: el.fontFamily, highlight: !!tok[1], elemId: el.id });
          cx += lay.measure(tok[0]) + lay.space;
        }
      });
      y += lay.height + gap;
    }
  }
  return runs;
}

export function layoutPinnedText(l, baseW, baseH, pxScale, txt) {
  const px = fontPx(l.size_pct, baseW);
  const measure = (s) => txt.measure(l.fontFamily, px, s);
  const space = measure(" ");
  const maxW = Math.max(1, Math.trunc(l.w * baseW));
  const isHead = l.id === "headline";
  const { asc, desc } = txt.metrics(l.fontFamily, px);
  const lh = Math.trunc((asc + desc) * (isHead ? 1.15 : 1.4));
  const lines = [];
  for (const seg of (l.text || "").split("\n")) {
    const toks = isHead && l.highlight
      ? headlineTokens(seg, l.highlight)
      : words(seg).map((w) => [w, false]);
    if (toks.length) lines.push(...wrap(toks, maxW, measure, space));
    else lines.push([["", false]]);
  }
  const widths = lineWidths(lines, measure, space);
  const boxW = widths.length ? Math.max(...widths) : 1;
  const boxH = lh * lines.length;
  let [left, top] = anchorToXY(l.x, l.y, boxW, boxH, l.anchor, baseW, baseH);
  left += Math.round(l.offset[0] * pxScale);
  top += Math.round(l.offset[1] * pxScale);
  const runs = [];
  lines.forEach((ln, i) => {
    let cx = left;
    const yy = top + i * lh;
    for (const tok of ln) {
      runs.push({ x: Math.trunc(cx), y: Math.trunc(yy), text: tok[0], px,
                  fontFamily: l.fontFamily, highlight: isHead && !!tok[1], elemId: l.id });
      cx += measure(tok[0]) + space;
    }
  });
  return runs;
}

export function layoutCta(l, baseW, baseH, pxScale, txt, coords) {
  const label = (l.text || "").replace(/\s+$/u, "") + "  →";
  const px = fontPx(l.size_pct, baseW);
  const { asc, desc } = txt.metrics(l.fontFamily, px);
  const th = asc + desc;
  const tw = Math.round(txt.measure(l.fontFamily, px, label));
  const padX = Math.trunc(th * 0.9), padY = Math.trunc(th * 0.55);
  const pw = tw + 2 * padX, ph = th + 2 * padY;
  const radius = Math.floor(ph / 2);
  const mx = Math.trunc(MARGIN * baseW), my = Math.trunc(MARGIN * baseH);
  let x, y;
  if (coords) {
    [x, y] = anchorToXY(coords.x, coords.y, pw, ph, coords.anchor, baseW, baseH);
  } else {
    const p = l.placement;
    x = p === "left" ? mx : p === "right" ? baseW - mx - pw : Math.floor((baseW - pw) / 2);
    y = p === "top" ? my : baseH - my - ph;
  }
  x += Math.round(l.offset[0] * pxScale);
  y += Math.round(l.offset[1] * pxScale);
  return { label, px, x, y, pw, ph, radius, padX, padY };
}
