"""Subject guard — Stage-3 copy must never sit on the Stage-2 subject.

The headline, sub-headings and CTA are drawn deterministically over the approved
Stage-2 image. Nothing used to check WHERE the subject landed: the default text
column (left, vertically centred) and the default subject framing ("lower
portion, upper area open") overlap whenever the image model seats the person
near the middle — the 2026-10-08 E2E put the headline across her face on 1:1,
4:5 and 9:16.

How the subject is found — no ML: Stage 2 is the approved Stage-1 background
PLUS a subject, so the pixels that differ between the two images ARE the
subject (person, monitor, props). A ground band at the bottom (a desk or floor
spanning the frame) is not protected; everything above it is.

What happens on a collision — never silent:
* the current arrangement is kept when its ink is clear of the subject;
* otherwise the copy (headline, sub-headings, CTA) is re-flowed as one
  left-aligned stack into the largest clean column — beside the subject, or the
  band above it — stepping the size down when the column is narrow;
* the move is returned as a record the attempt stores (``placement_guard``);
* when no clean arrangement exists (e.g. Stage 2 repainted the whole
  background, so the subject cannot be told apart from it) :class:`SubjectGuardError`
  says so — a creative with text over the face is never produced.
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageFilter

from . import text_overlay

# Working resolution of the subject mask (px across). Coarse on purpose: the
# question is "is this text box on the person", not pixel-exact matting.
WORK_W = 270
# Per-pixel RGB distance (after a light blur) that counts as "changed by Stage
# 2". Background drift from a preserving edit model stays well below it (GPT
# Image 2.5 kept the Stage-1 gradient within CIEDE2000 ~1-5); skin, hair and
# clothing against the brand gradient sit far above it.
DIFF_THRESHOLD = 40.0
# A bottom band where at least this share of each row changed is the ground
# plane (desk / floor) — copy may sit on it, so it is not protected.
GROUND_ROW_COVERAGE = 0.92
GROUND_MAX_SHARE = 0.2
# Changed regions smaller than this share of the frame are speckle (gradient
# drift, compression), not subject — one stray pixel must not close a column.
MIN_COMPONENT_SHARE = 0.004
# More than this share of the frame (above the ground) "changed" means Stage 2
# did not keep the background — the subject cannot be located honestly.
MAX_SUBJECT_SHARE = 0.7

MARGIN = 0.06          # canvas safe margin (matches the renderer's 6%)
PAD = 0.015            # clearance kept between any text and the subject (x canvas W)
MIN_COLUMN = 0.24      # narrowest column worth setting copy in (x canvas W)
# Readability floor: (headline, body) size steps, tried in order, every plan at
# each step. Every arrangement (column, column following the subject's contour,
# band above, headline-above-rest-beside) is tried at full, 90% and 80% before
# anything smaller; below 80% the HEADLINE gives way first so the sub-headings
# and CTA — the copy that turns illegible first — keep 80%.
SCALE_STEPS = ((1.0, 1.0), (0.9, 0.9), (0.8, 0.8), (0.7, 0.8), (0.6, 0.8),
               (0.7, 0.7), (0.6, 0.6))
GAP_AFTER_HEADLINE = 0.03
GAP_BETWEEN = 0.02
GAP_BEFORE_CTA = 0.04

_PLAN_SLACK = 0.004
_MOVABLE = ("text", "cta")
_FOOTERS = ("venue", "website")


class SubjectGuardError(Exception):
    """No arrangement keeps the copy off the subject — the reason is user-facing."""


def subject_mask(stage1_png: bytes, stage2_png: bytes, work_w: int = WORK_W) -> np.ndarray:
    """Boolean mask (rows x cols, ``work_w`` wide) of the protected subject:
    pixels Stage 2 changed relative to Stage 1, minus a ground band."""
    s2 = Image.open(BytesIO(stage2_png)).convert("RGB")
    s1 = Image.open(BytesIO(stage1_png)).convert("RGB")
    work_h = max(1, round(work_w * s2.height / s2.width))

    def prep(img: Image.Image) -> np.ndarray:
        return np.asarray(img.resize((work_w, work_h), Image.BILINEAR)
                          .filter(ImageFilter.GaussianBlur(1.5)), dtype=np.float32)

    diff = np.linalg.norm(prep(s1) - prep(s2), axis=2) > DIFF_THRESHOLD
    m = Image.fromarray((diff * 255).astype("uint8"))
    # Open (drop speckle) then dilate (a little clearance around the subject).
    m = m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(5))
    mask = _drop_specks(np.asarray(m) > 0, MIN_COMPONENT_SHARE)

    coverage = mask.mean(axis=1)
    ground = work_h
    while ground > 0 and coverage[ground - 1] >= GROUND_ROW_COVERAGE:
        ground -= 1
    if work_h - ground <= GROUND_MAX_SHARE * work_h:
        mask = mask.copy()
        mask[ground:] = False
    return mask


def _drop_specks(mask: np.ndarray, min_share: float) -> np.ndarray:
    """Keep only 4-connected regions of at least ``min_share`` of the frame."""
    h, w = mask.shape
    keep = np.zeros_like(mask)
    seen = np.zeros_like(mask)
    min_area = max(1, int(min_share * h * w))
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        stack, comp = [(r0, c0)], []
        seen[r0, c0] = True
        while stack:
            r, c = stack.pop()
            comp.append((r, c))
            for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= rr < h and 0 <= cc < w and mask[rr, cc] and not seen[rr, cc]:
                    seen[rr, cc] = True
                    stack.append((rr, cc))
        if len(comp) >= min_area:
            rows, cols = zip(*comp)
            keep[list(rows), list(cols)] = True
    return keep


def _box_hits(mask: np.ndarray, box, canvas_w: int, canvas_h: int,
              pad_frac: float = PAD) -> int:
    """Protected mask pixels inside ``box`` (canvas px), padded by ``pad_frac``."""
    mh, mw = mask.shape
    pad = pad_frac * canvas_w
    x0, y0, x1, y1 = box
    sx, sy = mw / canvas_w, mh / canvas_h
    c0 = max(0, int((x0 - pad) * sx))
    c1 = min(mw, int(np.ceil((x1 + pad) * sx)))
    r0 = max(0, int((y0 - pad) * sy))
    r1 = min(mh, int(np.ceil((y1 + pad) * sy)))
    if c1 <= c0 or r1 <= r0:
        return 0
    return int(mask[r0:r1, c0:c1].sum())


def _text_layers(layers: list[dict]) -> list[dict]:
    return [l for l in layers
            if l.get("type") in _MOVABLE and (l.get("text") or "").strip()]


def _normalized(layers: list[dict], canvas_w: int, canvas_h: int) -> list[dict]:
    from .render import _normalize_text_sizes  # the same normalisation the render applies

    return _normalize_text_sizes(layers, canvas_w, canvas_h)


def ink_boxes(layers: list[dict], canvas_w: int, canvas_h: int, pack=None) -> dict[str, tuple]:
    """Exact ink bbox per pinned text/CTA layer, as the renderer will draw it."""
    out: dict[str, tuple] = {}
    for layer in _normalized(_text_layers(layers), canvas_w, canvas_h):
        if not layer.get("pinned"):
            continue
        box = text_overlay.layer_ink_bbox(layer, canvas_w, canvas_h, pack=pack)
        if box:
            out[layer["id"]] = box
    return out


def overlap_report(layers: list[dict], mask: np.ndarray, canvas_w: int, canvas_h: int,
                   pack=None) -> dict[str, int]:
    """Protected-subject pixels under each text element's box (0 = clean).
    Un-pinned (legacy stacked) layers are measured from a full render."""
    report: dict[str, int] = {}
    boxes = ink_boxes(layers, canvas_w, canvas_h, pack)
    for lid, box in boxes.items():
        report[lid] = _box_hits(mask, box, canvas_w, canvas_h)
    unpinned = [l for l in _text_layers(layers) if not l.get("pinned")]
    if unpinned:
        report["_stacked"] = _stacked_hits(layers, mask, canvas_w, canvas_h, pack)
    return report


def _stacked_hits(layers, mask, canvas_w, canvas_h, pack) -> int:
    """Hits for the legacy auto-stacked text: render on a flat key colour and
    take the ink as everything that changed."""
    key = (255, 0, 255)
    buf = BytesIO()
    Image.new("RGB", (canvas_w, canvas_h), key).save(buf, "PNG")
    only_text = [l for l in _normalized(_text_layers(layers), canvas_w, canvas_h)
                 if not l.get("pinned")]
    png = text_overlay.render_layers(buf.getvalue(), only_text, canvas_w, canvas_h, pack=pack)
    ink = np.any(np.asarray(Image.open(BytesIO(png)).convert("RGB")) != key, axis=2)
    mh, mw = mask.shape
    ink_small = np.asarray(Image.fromarray((ink * 255).astype("uint8"))
                           .resize((mw, mh), Image.BILINEAR)
                           .filter(ImageFilter.MaxFilter(3))) > 0
    return int((ink_small & mask).sum())


def _measure(layer: dict, w_frac: float, scale: float, canvas_w: int, canvas_h: int,
             pack) -> tuple[float, float, float, float] | None:
    """(ink_w, ink_h, dx, dy) of ``layer`` set ``w_frac`` wide at ``scale``,
    where (dx, dy) is the ink centre's offset from an ``mc`` anchor point."""
    probe = {**layer, "pinned": True, "x": 0.5, "y": 0.5, "w": w_frac, "anchor": "mc",
             "size_pct": float(layer["size_pct"]) * scale, "offset": (0, 0)}
    probe = _normalized([probe], canvas_w, canvas_h)[0]
    box = text_overlay.layer_ink_bbox(probe, canvas_w, canvas_h, pack=pack)
    if not box:
        return None
    x0, y0, x1, y1 = box
    if x0 <= 0 or y0 <= 0 or x1 >= canvas_w or y1 >= canvas_h:
        return None  # clipped — larger than the canvas at this size
    return (x1 - x0, y1 - y0, (x0 + x1) / 2 - canvas_w / 2, (y0 + y1) / 2 - canvas_h / 2)


def _free_column(mask: np.ndarray, side: str, top: float, bottom: float,
                 canvas_w: int, canvas_h: int) -> tuple[float, float]:
    """(x0, x1) in canvas px of the clean column on ``side`` between rows
    ``top``..``bottom`` (canvas px)."""
    mh, mw = mask.shape
    r0 = max(0, int(top * mh / canvas_h))
    r1 = min(mh, int(np.ceil(bottom * mh / canvas_h)))
    rows = mask[r0:r1]
    pad = (PAD + 2 * _PLAN_SLACK) * canvas_w  # more than the plan check's clearance
    lo, hi = MARGIN * canvas_w, (1 - MARGIN) * canvas_w
    cols = np.where(rows.any(axis=0))[0] if rows.size else np.array([], dtype=int)
    if side == "left":
        edge = cols.min() * canvas_w / mw - pad if cols.size else hi
        return lo, min(hi, edge)
    edge = (cols.max() + 1) * canvas_w / mw + pad if cols.size else lo
    return max(lo, edge), hi


def _stack(text: list[dict], x0: float, x1: float, y_top: float, scale: float,
           canvas_w: int, canvas_h: int, pack) -> dict[str, dict] | None:
    """Left-aligned stack of ``text`` in [x0, x1] from ``y_top`` down. Returns
    ``{id: {"x","y","w","anchor","size_pct","box"}}`` or None if it overflows."""
    w_frac = (x1 - x0) / canvas_w
    y = y_top
    out: dict[str, dict] = {}
    prev = None
    for layer in text:
        if prev is not None:
            gap = (GAP_AFTER_HEADLINE if prev == "headline"
                   else GAP_BEFORE_CTA if layer["type"] == "cta" else GAP_BETWEEN)
            y += gap * canvas_h
        size = _measure(layer, w_frac, scale, canvas_w, canvas_h, pack)
        if size is None:
            return None
        iw, ih, dx, dy = size
        cx, cy = x0 + iw / 2, y + ih / 2
        out[layer["id"]] = {
            "x": round((cx - dx) / canvas_w, 4), "y": round((cy - dy) / canvas_h, 4),
            "w": round(w_frac, 4), "anchor": "mc",
            "size_pct": round(float(layer["size_pct"]) * scale, 3),
            "box": (x0, y, x0 + iw, y + ih),
        }
        y += ih
        prev = layer["id"]
    if y > (1 - MARGIN) * canvas_h:
        return None
    return out


def _subject_top(mask: np.ndarray, canvas_h: int) -> float:
    rows = np.where(mask.any(axis=1))[0]
    return rows.min() * canvas_h / mask.shape[0] if rows.size else (1 - MARGIN) * canvas_h


def _clean(placed: dict | None, mask: np.ndarray, canvas_w: int, canvas_h: int) -> bool:
    # Planned with a little extra clearance: the in-place render can land a
    # pixel off the measured box, and the final check uses ``PAD``.
    return bool(placed) and all(
        _box_hits(mask, v["box"], canvas_w, canvas_h, PAD + _PLAN_SLACK) == 0
        for v in placed.values())


def _column_stack(text, mask, side, y_top, scale, canvas_w, canvas_h, pack, probe=0.35):
    """Stack ``text`` in the clean column on ``side`` from ``y_top`` down,
    re-fitting the column to the rows the stack really occupies."""
    cx0, cx1 = _free_column(mask, side, y_top, y_top + probe * canvas_h, canvas_w, canvas_h)
    for _ in range(6):
        if cx1 - cx0 < MIN_COLUMN * canvas_w:
            return None
        trial = _stack(text, cx0, cx1, y_top, scale, canvas_w, canvas_h, pack)
        if trial is None:
            return None
        bottom = max(v["box"][3] for v in trial.values())
        nx0, nx1 = _free_column(mask, side, y_top, bottom + PAD * canvas_w, canvas_w, canvas_h)
        if (round(nx0), round(nx1)) == (round(cx0), round(cx1)):
            return trial  # the column is clean for exactly the rows the stack uses
        cx0, cx1 = nx0, nx1
    return None


def _plans(text, mask, prefer, canvas_w, canvas_h, pack, start_y=None, subject=None):
    """Arrangement plans, best first, each a ``(name, scale -> placed|None)``:
    one column beside the subject (preferred side, then the other), the band
    above the subject, then a split — headline across the band above the
    subject, the rest in a column beside it."""
    top = start_y if start_y is not None else MARGIN * canvas_h
    sides = [prefer, "right" if prefer == "left" else "left"]
    subject_top = _subject_top(subject if subject is not None else mask, canvas_h)
    full = (MARGIN * canvas_w, (1 - MARGIN) * canvas_w)

    def column(side):
        return lambda sc: _column_stack(text, mask, side, top, sc, canvas_w, canvas_h, pack)

    def band(sc):
        placed = _stack(text, *full, top, sc, canvas_w, canvas_h, pack)
        if placed and max(v["box"][3] for v in placed.values()) <= subject_top - PAD * canvas_w:
            return placed
        return None

    def split(side):
        def plan(sc):
            head = _stack(text[:1], *full, top, sc, canvas_w, canvas_h, pack)
            if not head or len(text) < 2:
                return None
            hb = max(v["box"][3] for v in head.values())
            if hb > subject_top - PAD * canvas_w:
                return None
            rest = _column_stack(text[1:], mask, side, hb + GAP_AFTER_HEADLINE * canvas_h,
                                 sc, canvas_w, canvas_h, pack)
            return {**head, **rest} if rest else None
        return plan

    def contour(sc):
        return _contour_stack(text, mask, top, sc, canvas_w, canvas_h, pack)

    plans = [(sides[0], column(sides[0]))]
    if sides[0] == "left":
        plans.append(("left-contour", contour))
    plans.append((sides[1], column(sides[1])))
    if sides[1] == "left":
        plans.append(("left-contour", contour))
    plans += [("top", band)]
    plans += [(f"top+{s}", split(s)) for s in sides]
    return plans


def _contour_stack(text, mask, y_top, scale, canvas_w, canvas_h, pack):
    """Left-aligned stack on the frame's left margin where EACH element gets the
    clean width beside the subject at its own rows — the headline beside the
    head is wider than the sub-headings beside the shoulders. One shared left
    edge, so the copy still reads as one aligned block."""
    y = y_top
    out: dict[str, dict] = {}
    prev = None
    for layer in text:
        if prev is not None:
            gap = (GAP_AFTER_HEADLINE if prev == "headline"
                   else GAP_BEFORE_CTA if layer["type"] == "cta" else GAP_BETWEEN)
            y += gap * canvas_h
        one = _column_stack([layer], mask, "left", y, scale, canvas_w, canvas_h, pack,
                            probe=0.12)
        if one is None:
            return None
        out.update(one)
        y = one[layer["id"]]["box"][3]
        prev = layer["id"]
    return out


def arrange(layers: list[dict], mask: np.ndarray, canvas_w: int, canvas_h: int,
            pack=None, prefer: str = "left", start_y: float | None = None,
            subject: np.ndarray | None = None,
            steps=SCALE_STEPS) -> tuple[dict[str, dict], dict] | None:
    """A clean arrangement for the movable copy, or None if none exists.
    Largest size first across every plan, the preferred side breaking ties — a
    roomy column on the far side beats a cramped one on the near side."""
    text = [l for l in _text_layers(layers) if l["id"] not in _FOOTERS]
    if not text:
        return None
    order = {"headline": 0, "cta": 99}
    text.sort(key=lambda l: order.get(l["id"], 1 + int(str(l["id"]).rsplit("-", 1)[-1])
                                      if str(l["id"]).startswith("subheading-") else 50))
    for head_scale, body_scale in steps:
        scaled = [{**l, "size_pct": float(l["size_pct"])
                   * (head_scale if l["id"] == "headline" else body_scale)} for l in text]
        plans = _plans(scaled, mask, prefer, canvas_w, canvas_h, pack, start_y, subject)
        for name, plan in plans:
            placed = plan(1.0)
            if _clean(placed, mask, canvas_w, canvas_h):
                xs = [v["box"][0] for v in placed.values()] + [v["box"][2] for v in placed.values()]
                how = {"side": name, "scale": head_scale,
                       "column": [round(min(xs) / canvas_w, 3), round(max(xs) / canvas_w, 3)]}
                if body_scale != head_scale:
                    how["body_scale"] = body_scale
                return placed, how
    return None


def _with_reserve(mask: np.ndarray, reserve) -> np.ndarray:
    """``mask`` plus the reserved rectangles (fractions of the frame)."""
    if not reserve:
        return mask
    out = mask.copy()
    mh, mw = out.shape
    for x0, y0, x1, y1 in reserve:
        out[max(0, int(y0 * mh)):min(mh, int(np.ceil(y1 * mh))),
            max(0, int(x0 * mw)):min(mw, int(np.ceil(x1 * mw)))] = True
    return out


def enforce(layers: list[dict], stage1_png: bytes | None, stage2_png: bytes,
            canvas_w: int, canvas_h: int, pack=None,
            reserve: list[tuple[float, float, float, float]] | None = None,
            alternatives: list[tuple[str, tuple[float, float, float, float]]] | None = None,
            ) -> tuple[list[dict], dict | None]:
    """Keep the copy off the subject. Returns ``(layers, record)``: the layers
    unchanged and ``record`` None when already clean; re-flowed layers plus a
    ``placement_guard`` record when moved. Raises :class:`SubjectGuardError`
    when no clean arrangement exists."""
    if stage1_png is None:
        return layers, None
    mask = subject_mask(stage1_png, stage2_png)
    ground_rows = mask.any(axis=1)
    above = mask[: int(np.where(ground_rows)[0].max()) + 1] if ground_rows.any() else mask
    if above.size and above.mean() > MAX_SUBJECT_SHARE:
        raise SubjectGuardError(
            "Stage 2 changed most of the background, so the subject can't be told "
            "apart from it and the text can't be kept off her/him reliably — "
            "regenerate Stage 2.")
    subject = mask
    # The logo's future box is kept clear too (``reserve``): copy re-flowed
    # into the top band would otherwise land exactly where Stage 4 pastes it.
    mask = _with_reserve(subject, reserve)
    before = overlap_report(layers, mask, canvas_w, canvas_h, pack)
    if not any(before.values()):
        return layers, None
    head = next((l for l in layers if l.get("id") == "headline"), None)
    prefer = "right" if head is not None and float(head.get("x", 0.0)) > 0.5 else "left"
    # Where the logo may go instead (``alternatives``, only corners clear of the
    # subject). Readability beats the logo corner, the logo corner beats size:
    # every arrangement at >= 80% is tried with the requested logo spot, then
    # with each alternative, before anything smaller is tried. A requested spot
    # that already sits on the subject is skipped when an alternative exists —
    # Stage 4 would move the logo off the subject anyway, into a corner the copy
    # was never kept clear of.
    def clear_of_subject(boxes) -> bool:
        return all(_box_hits(subject, _frac_to_px(b, canvas_w, canvas_h), canvas_w, canvas_h) == 0
                   for b in boxes or [])

    others = [(pos, [box]) for pos, box in (alternatives or []) if clear_of_subject([box])]
    options = ([(None, reserve)] if clear_of_subject(reserve) or not others else []) + others
    readable = tuple(st for st in SCALE_STEPS if min(st) >= 0.8)
    small = tuple(st for st in SCALE_STEPS if min(st) < 0.8)
    found, logo_position, mask_used = None, None, mask
    for steps in (readable, small):
        for pos, res in options:
            m = _with_reserve(subject, res)
            found = arrange(layers, m, canvas_w, canvas_h, pack, prefer=prefer,
                            start_y=_start_below(res, prefer, canvas_w, canvas_h),
                            subject=subject, steps=steps)
            if found is not None:
                logo_position, mask_used = pos, m
                break
        if found is not None:
            break
    mask = mask_used
    if found is None:
        hit = sorted(k for k, v in before.items() if v)
        raise SubjectGuardError(
            "The text would cover the subject (" + ", ".join(hit) + ") and no clean "
            "column fits it beside or above the subject — move the subject in Stage 2 "
            "(subject placement) or shorten the copy.")
    placed, how = found
    moved = []
    for layer in layers:
        p = placed.get(layer.get("id"))
        if not p:
            continue
        layer.update({"x": p["x"], "y": p["y"], "w": p["w"], "anchor": "mc",
                      "pinned": True, "size_pct": p["size_pct"], "offset": (0, 0)})
        moved.append(layer["id"])
    after = overlap_report(layers, mask, canvas_w, canvas_h, pack)
    if any(after.values()):  # belt and braces: never return a colliding layout
        raise SubjectGuardError("The text could not be placed clear of the subject.")
    record = {
        "moved": moved, **how,
        **({"logo_position": logo_position} if logo_position else {}),
        "overlap_before": {k: v for k, v in before.items() if v},
        "reason": (f"Text sat on the subject or the logo's spot; re-flowed into the clean "
                   f"{how['side']} area at {round(how['scale'] * 100)}% size"
                   + (f" (sub-headings and CTA at {round(how['body_scale'] * 100)}%)."
                      if "body_scale" in how else ".")
                   + (f" The logo goes {logo_position} so the copy keeps a readable size."
                      if logo_position else "")),
    }
    return layers, record


def _frac_to_px(box, canvas_w: int, canvas_h: int) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    return (x0 * canvas_w, y0 * canvas_h, x1 * canvas_w, y1 * canvas_h)


def _start_below(reserve, prefer: str, canvas_w: int, canvas_h: int) -> float | None:
    """Copy starts below a top logo on the copy's preferred side, with the same
    clearance the check uses. A logo in the opposite top corner does not push
    the copy down — the column beside it already keeps clear of it."""
    top = [r for r in (reserve or []) if r[1] < 0.25
           and (r[0] < 0.5 if prefer == "left" else r[2] > 0.5)]
    if not top:
        return None
    return max(MARGIN * canvas_h,
               (max(r[3] for r in top) + GAP_BETWEEN) * canvas_h
               + (PAD + _PLAN_SLACK) * canvas_w + 1)


# --------------------------------------------------------------------------- #
# Stage 4: the logo must not land on the copy (or the subject) either. The
# guard above moves copy into the clean band at the top — exactly where the
# default top-left logo goes — so the compositor checks its box against the
# Stage-3 text ink before pasting.
# --------------------------------------------------------------------------- #

LOGO_POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left",
                  "top-center", "bottom-center", "middle-right", "middle-left")


def text_ink_mask(layers: list[dict], canvas_w: int, canvas_h: int, pack=None,
                  px_scale: float = 1.0) -> np.ndarray:
    """Boolean mask (canvas px) of every pixel the Stage-3 copy inks — pinned or
    legacy-stacked — rendered on a flat key colour by the real renderer."""
    key = (255, 0, 255)
    buf = BytesIO()
    Image.new("RGB", (canvas_w, canvas_h), key).save(buf, "PNG")
    only_text = _normalized(_text_layers(layers), canvas_w, canvas_h)
    png = text_overlay.render_layers(buf.getvalue(), only_text, canvas_w, canvas_h, pack=pack,
                                     px_scale=px_scale)
    return np.any(np.asarray(Image.open(BytesIO(png)).convert("RGB")) != key, axis=2)


def logo_clear_position(*, requested: str, place, base_w: int, base_h: int,
                        ink: np.ndarray, subject: np.ndarray | None) -> tuple[str, dict | None]:
    """The logo position to use: ``requested`` when its box is clear of the copy
    (and the subject), else the first clear one from :data:`LOGO_POSITIONS`.
    ``place(position) -> {"x","y","w","h"}`` is the compositor's own box math.
    Returns ``(position, record)``; record None when nothing moved. Raises
    :class:`SubjectGuardError` when no position is clear."""

    def hits(pos: str) -> int:
        b = place(pos)
        pad = PAD * base_w
        box = (b["x"] - pad, b["y"] - pad, b["x"] + b["w"] + pad, b["y"] + b["h"] + pad)
        ih, iw = ink.shape
        sx, sy = iw / base_w, ih / base_h
        c0, c1 = max(0, int(box[0] * sx)), min(iw, int(np.ceil(box[2] * sx)))
        r0, r1 = max(0, int(box[1] * sy)), min(ih, int(np.ceil(box[3] * sy)))
        n = int(ink[r0:r1, c0:c1].sum()) if c1 > c0 and r1 > r0 else 0
        if subject is not None:
            n += _box_hits(subject, (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]),
                           base_w, base_h)
        return n

    before = hits(requested)
    if before == 0:
        return requested, None
    for pos in LOGO_POSITIONS:
        if pos != requested and hits(pos) == 0:
            return pos, {"from": requested, "to": pos,
                         "reason": f"The logo at {requested} would sit on the text or the "
                                   f"subject; placed {pos} instead."}
    raise SubjectGuardError(
        "There is no corner where the logo stays clear of the text and the subject — "
        "shrink the logo (size) or move the text in Stage 3.")
