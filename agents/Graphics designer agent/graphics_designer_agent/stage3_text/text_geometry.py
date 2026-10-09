"""Text geometry check — the polished Stage-3 copy must sit where the
deterministic layout put it.

The Stage-3 polish pass hands the deterministic composite to an image model,
which RE-RENDERS the text. It sometimes indents a line, nudges a block or
re-wraps a sentence; the vision QA reads words, not pixels, so it waved such
drift through (2026-10-08 E2E: on 1:1 "Build your team…" started right of the
headline's left edge while "Choose from…" started on it). The subject guard and
the Stage-4 logo guard vouch for the DETERMINISTIC geometry, so a drifted
polish also voids their guarantee.

How it measures — no model call: the exact per-line ink boxes come from the
renderer's own line layout (``text_overlay.layer_line_boxes``). In both the
composite and the polished image, "ink" is local contrast (each pixel against a
blurred copy of the image), minus structure the Stage-2 photo already had. Each
spec line is then found in both images and its left edge, right edge and
vertical position compared. Measuring both images the same way cancels the
detector's own bias, so the numbers are the drift the model introduced.

Checks:
* alignment — every line's alignment edge (left edge for left-aligned copy,
  centre for centred and for the CTA pill, right edge for right-aligned) stays within ``ALIGN_TOL`` of the deterministic spec, after removing a
  small uniform shift of the whole copy (``SHIFT_TOL``);
* line order / no reflow — every line is still there (ink coverage), at its own
  height and in order (``ROW_TOL`` of a line height), with the same width
  (``WIDTH_TOL``) — a word that jumped lines changes two widths.
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageFilter

from . import text_overlay

WORK_W = 1080            # measurement resolution (px across)
BLUR_R = 6.0             # high-pass radius at WORK_W
INK_T = 38.0             # |pixel - blurred| (max over RGB) that counts as ink
STRUCT_DILATE = 5        # px (odd) of clearance around the photo's own structure

ALIGN_TOL = 0.006        # x canvas W — per-line alignment drift allowed (~6.5 px @1080)
SHIFT_TOL = 0.015        # x canvas W — uniform shift of the whole copy allowed
ROW_TOL = 0.35           # x line height — vertical drift of one line vs the block
WIDTH_TOL = 0.12         # relative line-width change allowed (re-rendered glyphs)
MIN_COVERAGE = 0.45      # polished ink / spec ink in a line window

_SEARCH_X = 0.06         # x W — how far past the spec box an edge is looked for
_SEARCH_Y = 0.03         # x H — block-level vertical search


def spec_lines(layers: list[dict], width: int, height: int, *, pack=None,
               px_scale: float = 1.0) -> list[dict]:
    """The deterministic geometry: one entry per rendered line of every pinned
    text/CTA layer, ``{"layer","line","align","box"}`` (canvas px)."""
    from .render import _normalize_text_sizes  # the normalisation the render applies

    out: list[dict] = []
    for layer in _normalize_text_sizes(layers, width, height):
        if layer.get("type") not in ("text", "cta") or not layer.get("pinned"):
            continue
        if not (layer.get("text") or "").strip():
            continue
        align = "pill" if layer["type"] == "cta" else (layer.get("align") or "left")
        boxes = text_overlay.layer_line_boxes(layer, width, height, pack=pack,
                                              px_scale=px_scale)
        for i, box in enumerate(boxes):
            out.append({"layer": layer["id"], "line": i, "align": align, "box": box})
    return out


def _load(png: bytes, w: int, h: int) -> np.ndarray:
    img = Image.open(BytesIO(png)).convert("RGB")
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    return np.asarray(img, dtype=np.float32)


def _ink(arr: np.ndarray) -> np.ndarray:
    img = Image.fromarray(arr.astype("uint8"))
    blur = np.asarray(img.filter(ImageFilter.GaussianBlur(BLUR_R)), dtype=np.float32)
    return np.abs(arr - blur).max(axis=2) > INK_T


def _edges(cols: np.ndarray) -> tuple[int, int] | None:
    """(left, right) ink columns of a column-count profile, ignoring specks."""
    total = cols.sum()
    if total <= 0:
        return None
    c = np.cumsum(cols)
    lo = int(np.searchsorted(c, 0.004 * total))
    hi = int(np.searchsorted(c, 0.996 * total))
    return lo, hi + 1


def measure(composite_png: bytes, polished_png: bytes, lines: list[dict],
            width: int, height: int, base_png: bytes | None = None) -> dict | None:
    """Per-line drift of ``polished_png`` against ``composite_png`` for the
    ``lines`` spec. ``None`` when there is nothing measurable."""
    if not lines:
        return None
    k = WORK_W / width
    ww, wh = WORK_W, max(1, round(height * k))
    comp = _ink(_load(composite_png, ww, wh))
    pol = _ink(_load(polished_png, ww, wh))
    if base_png is not None:
        struct = Image.fromarray((_ink(_load(base_png, ww, wh)) * 255).astype("uint8"))
        struct = np.asarray(struct.filter(ImageFilter.MaxFilter(STRUCT_DILATE))) > 0
        comp &= ~struct
        pol &= ~struct
    sx = int(_SEARCH_X * ww)

    boxes = [tuple(int(round(v * k)) for v in ln["box"]) for ln in lines]
    # Block-level vertical shift: best row-profile match over the union.
    x0 = max(0, min(b[0] for b in boxes) - sx)
    x1 = min(ww, max(b[2] for b in boxes) + sx)
    y0 = min(b[1] for b in boxes)
    y1 = max(b[3] for b in boxes)
    sy = int(_SEARCH_Y * wh)
    cp = comp[max(0, y0):min(wh, y1), x0:x1].sum(axis=1).astype(np.float64)
    best, dy_block = -1.0, 0
    for dy in range(-sy, sy + 1):
        a, b = y0 + dy, y1 + dy
        if a < 0 or b > wh:
            continue
        pp = pol[a:b, x0:x1].sum(axis=1).astype(np.float64)
        score = float(np.minimum(cp[:len(pp)], pp).sum())
        if score > best:
            best, dy_block = score, dy

    out = []
    for ln, (bx0, by0, bx1, by1) in zip(lines, boxes):
        h = max(1, by1 - by0)
        core = max(1, int(0.2 * h))
        cx0, cx1 = max(0, bx0 - sx), min(ww, bx1 + sx)
        ref = comp[by0 + core:by1 - core, cx0:cx1]
        # Local vertical refine (±25% of the line) around the block shift.
        rbest, rdy = -1.0, dy_block
        rp_ref = ref.sum(axis=1).astype(np.float64)
        for dy in range(dy_block - h // 4, dy_block + h // 4 + 1):
            a, b = by0 + core + dy, by1 - core + dy
            if a < 0 or b > wh:
                continue
            pp = pol[a:b, cx0:cx1].sum(axis=1).astype(np.float64)
            sc = float(np.minimum(rp_ref[:len(pp)], pp).sum())
            if sc > rbest:
                rbest, rdy = sc, dy
        a, b = max(0, by0 + core + rdy), min(wh, by1 - core + rdy)
        got = pol[a:b, cx0:cx1]
        e_ref = _edges(ref.sum(axis=0))
        e_got = _edges(got.sum(axis=0)) if got.size else None
        cov = float(got.sum()) / max(1.0, float(ref.sum()))
        rec = {"layer": ln["layer"], "line": ln["line"], "align": ln["align"],
               "coverage": round(cov, 2), "dy": rdy - dy_block, "h": h}
        if e_ref and e_got:
            rec.update({
                "dl": (e_got[0] - e_ref[0]) / ww, "dr": (e_got[1] - e_ref[1]) / ww,
                "width_ratio": (e_got[1] - e_got[0]) / max(1, e_ref[1] - e_ref[0]),
            })
        out.append(rec)
    return {"lines": out, "dy_block": dy_block / wh}


def _axis(rec: dict) -> float | None:
    if "dl" not in rec:
        return None
    if rec["align"] in ("center", "pill"):
        # A pill is measured at its centre: a permitted glow around the button
        # widens its ink on both sides without the button moving.
        return (rec["dl"] + rec["dr"]) / 2
    if rec["align"] == "right":
        return rec["dr"]
    return rec["dl"]


def verdict(m: dict | None) -> dict | None:
    """``{"passed","violations","max_align_dev","shift","lines"}`` from
    :func:`measure`; ``None`` when nothing was measurable."""
    if not m:
        return None
    recs = m["lines"]
    axes = [a for a in (_axis(r) for r in recs) if a is not None]
    shift = float(np.median(axes)) if axes else 0.0
    violations: list[str] = []

    def name(r):
        return f"{r['layer']} line {r['line'] + 1}"

    max_dev = 0.0
    for r in recs:
        if r["coverage"] < MIN_COVERAGE or "dl" not in r:
            violations.append(f"{name(r)} is missing or was re-wrapped")
            continue
        dev = _axis(r) - shift
        r["align_dev"] = round(dev, 4)
        max_dev = max(max_dev, abs(dev))
        if abs(dev) > ALIGN_TOL:
            violations.append(f"{name(r)} drifted {abs(dev) * 100:.1f}% of the width "
                              f"off its alignment edge")
        if abs(r["width_ratio"] - 1) > WIDTH_TOL:
            violations.append(f"{name(r)} changed width ({r['width_ratio']:.2f}x) — "
                              "text was re-flowed")
        if abs(r["dy"]) > ROW_TOL * r["h"]:
            violations.append(f"{name(r)} moved vertically off its line")
    if abs(shift) > SHIFT_TOL:
        violations.append(f"the copy shifted {abs(shift) * 100:.1f}% of the width")
    if abs(m.get("dy_block", 0.0)) > SHIFT_TOL:
        violations.append(f"the copy shifted {abs(m['dy_block']) * 100:.1f}% vertically")
    for r in recs:
        for key in ("dl", "dr", "width_ratio"):
            if key in r:
                r[key] = round(r[key], 4)
    return {"passed": not violations, "violations": violations[:6],
            "max_align_dev": round(max_dev, 4), "shift": round(shift, 4),
            "lines": recs}


def check(composite_png: bytes, polished_png: bytes, lines: list[dict],
          width: int, height: int, base_png: bytes | None = None) -> dict | None:
    """Measure + judge. Never raises: an unmeasurable pair returns ``None``."""
    try:
        return verdict(measure(composite_png, polished_png, lines, width, height, base_png))
    except Exception:  # noqa: BLE001 - a measurement bug must not block generation
        import logging

        logging.getLogger("graphics_designer.stage3.text_geometry").warning(
            "text geometry check failed", exc_info=True)
        return None
