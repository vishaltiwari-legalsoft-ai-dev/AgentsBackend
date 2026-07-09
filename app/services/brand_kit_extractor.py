# backend/app/services/brand_kit_extractor.py
"""Extract exact brand colors + fonts from a brand-kit PDF.

Pure module: filesystem in, dataclasses out. No Firestore/GCS/HTTP imports —
the enrichment orchestrator (brand_enrichment.py) owns all I/O.
"""
from __future__ import annotations

import json
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF — existing backend dependency
from PIL import Image  # Pillow — existing backend dependency

_HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})\b")
_BARE_HEX_RE = re.compile(r"\b([0-9A-Fa-f]{6})\b")
_HEX_WORD_RE = re.compile(r"\bhex\b", re.I)
_RGB_RE = re.compile(
    r"\bR\s*:?\s*(\d{1,3})\s*[,/ ]\s*G\s*:?\s*(\d{1,3})\s*[,/ ]\s*B\s*:?\s*(\d{1,3})", re.I
)


@dataclass
class ColorHit:
    hex: str      # "#1A2B3C" — normalized uppercase
    page: int     # 1-based
    context: str  # the text line it appeared on (feeds role labeling)


def extract_colors(pdf_path: Path) -> list[ColorHit]:
    hits: list[ColorHit] = []
    seen: set[str] = set()
    with fitz.open(pdf_path) as doc:
        for page_no, page in enumerate(doc, start=1):
            for line in page.get_text("text").splitlines():
                for m in _HEX_RE.finditer(line):
                    _add(hits, seen, m.group(1), page_no, line)
                if _HEX_WORD_RE.search(line):
                    for m in _BARE_HEX_RE.finditer(line):
                        _add(hits, seen, m.group(1), page_no, line)
                for m in _RGB_RE.finditer(line):
                    r, g, b = (min(int(v), 255) for v in m.groups())
                    _add(hits, seen, f"{r:02X}{g:02X}{b:02X}", page_no, line)
    return hits


def _add(hits: list[ColorHit], seen: set[str], raw: str, page_no: int, line: str) -> None:
    hx = f"#{raw.upper()}"
    if hx not in seen:
        seen.add(hx)
        hits.append(ColorHit(hex=hx, page=page_no, context=line.strip()))


def extract_svg_colors(svg_path: Path) -> list[ColorHit]:
    """Regex-scan an SVG's raw XML text for hex color literals (vector
    fill/stroke values). Deduped, first-seen order. An unreadable file
    (missing, permission error, etc.) yields an empty list rather than
    raising — SVGs are one rung of an optional source ladder."""
    try:
        text = Path(svg_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    hits: list[ColorHit] = []
    seen: set[str] = set()
    for m in _HEX_RE.finditer(text):
        hx = f"#{m.group(1).upper()}"
        if hx not in seen:
            seen.add(hx)
            hits.append(ColorHit(hex=hx, page=0, context=f"svg:{svg_path.name}"))
    return hits


_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")


@dataclass
class FontHit:
    family: str            # "BeVietnamPro"
    style: str             # "Bold" | "Regular" | ...
    raw_name: str          # "ABCDEF+BeVietnamPro-Bold"
    embedded: bool
    pages: list[int] = field(default_factory=list)


def _clean_basefont(basefont: str) -> tuple[str, str]:
    clean = _SUBSET_RE.sub("", basefont)
    family, _, style = clean.partition("-")
    return family, (style or "Regular")


def extract_fonts(pdf_path: Path) -> list[FontHit]:
    found: dict[tuple[str, str], FontHit] = {}
    with fitz.open(pdf_path) as doc:
        for page_no, page in enumerate(doc, start=1):
            # page.get_fonts() tuples: (xref, ext, type, basefont, name, encoding)
            for f in page.get_fonts(full=False):
                basefont = f[3]
                if not basefont:
                    continue
                family, style = _clean_basefont(basefont)
                key = (family, style)
                hit = found.setdefault(key, FontHit(
                    family=family, style=style, raw_name=basefont,
                    embedded=bool(f[1] and f[1] != "n/a")))
                if page_no not in hit.pages:
                    hit.pages.append(page_no)
    return list(found.values())


_SIZE_TOKEN_RE = re.compile(r"\b\d+pt\b", re.I)


def _parse_font_filename(stem: str) -> tuple[str, str]:
    """Split a font filename stem into (family, style).

    Rule: split on the LAST '-'; the trailing segment is the style (else
    "Regular" if no '-' at all). When there is no '-', split on the LAST '_'
    instead. The remaining (family) portion has '_' turned into spaces and
    any "NNpt" size tokens dropped.
    """
    if "-" in stem:
        family_raw, _, style = stem.rpartition("-")
    elif "_" in stem:
        family_raw, _, style = stem.rpartition("_")
    else:
        family_raw, style = stem, "Regular"
    family_raw = family_raw.replace("_", " ")
    family_raw = _SIZE_TOKEN_RE.sub("", family_raw)
    family = re.sub(r"\s+", " ", family_raw).strip()
    return family, (style or "Regular")


def fonts_from_files(paths: list[Path]) -> list[FontHit]:
    """Derive FontHits purely from filenames (no font file is opened) —
    used when a brand-kit dir has loose .ttf/.otf files but no PDF/SVG that
    names them. Deduped on (family, style); first occurrence wins."""
    found: dict[tuple[str, str], FontHit] = {}
    for path in paths:
        family, style = _parse_font_filename(path.stem)
        key = (family, style)
        found.setdefault(key, FontHit(
            family=family, style=style, raw_name=path.name,
            embedded=True, pages=[]))
    return list(found.values())


def _rgb(hx: str) -> tuple[float, float, float]:
    return tuple(int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _lum(hx: str) -> float:
    r, g, b = _rgb(hx)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sat(hx: str) -> float:
    r, g, b = _rgb(hx)
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def derive_palette(hexes: list[str]) -> dict:
    """Map exact brand hexes onto the palette contract that
    templated_brands.build_templated_pack expects. Deterministic."""
    if len(hexes) < 3:
        raise ValueError("need at least 3 brand colors to derive a palette")
    ordered = sorted(hexes, key=_lum, reverse=True)
    light, deep = ordered[0], ordered[-1]
    ink = deep if _lum(deep) < 0.2 else "#161511"
    middles = [h for h in ordered[1:-1]] or [ordered[0]]
    accent = max(middles, key=_sat)
    mid = next((h for h in middles if h != accent), accent)
    return {
        "light": light, "mid": mid, "deep": deep, "accent": accent, "ink": ink,
        "hl_from": accent, "hl_to": deep, "cta_from": mid, "cta_to": deep,
    }


def _flattened_pixels(img):
    """Pillow-version seam: ``get_flattened_data()`` is the current (>=10.x)
    API; older-yet-``pillow>=10.0``-satisfying installs may only have the
    deprecated ``getdata()``. Prefer the new name when present; otherwise
    fall back, suppressing just its own DeprecationWarning locally so test
    output (and any future strict-warnings CI gate) stays pristine."""
    if hasattr(img, "get_flattened_data"):
        return img.get_flattened_data()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return img.getdata()


def extract_pixel_colors(image_paths: list[Path], min_share: float = 0.02) -> list[ColorHit]:
    """Flat-pixel frequency analysis across ALL given images: exact observed
    RGB values (no quantization) that cover >= min_share of the combined
    pixel count. Images are downscaled (long side <= 256px, deterministic
    ``Image.thumbnail``) purely for speed before counting. ``#FFFFFF`` is
    excluded as an assumed background color. Corrupt/unreadable images are
    skipped silently rather than raising, since this rung of the source
    ladder runs over a whole folder of creatives of unknown provenance.
    Returned in descending share order.
    """
    counts: Counter[tuple[int, int, int]] = Counter()
    for path in image_paths:
        try:
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((256, 256))
                counts.update(_flattened_pixels(rgb))
        except (OSError, Image.DecompressionBombError, ValueError):
            # Only genuine per-image data problems are skipped — a corrupt
            # file, a decompression-bomb guard trip, or a bad value from a
            # malformed image. Programming errors (TypeError, AttributeError,
            # ...) must raise, not vanish silently into a dead pixel rung.
            continue
    total = sum(counts.values())
    if not total:
        return []
    scored: list[tuple[float, ColorHit]] = []
    for (r, g, b), n in counts.items():
        hx = f"#{r:02X}{g:02X}{b:02X}"
        if hx == "#FFFFFF":
            continue
        share = n / total
        if share >= min_share:
            scored.append((share, ColorHit(hex=hx, page=0, context=f"pixel-share={share:.1%}")))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [hit for _, hit in scored]


@dataclass
class KitSources:
    kit_pdf: Path | None = None
    svg_files: list[Path] = field(default_factory=list)
    font_files: list[Path] = field(default_factory=list)
    image_files: list[Path] = field(default_factory=list)


_ROLE_WORDS = {"primary": "primary", "main": "primary",
               "secondary": "secondary",
               "accent": "accent", "highlight": "accent"}


@dataclass
class BrandKitProfile:
    brand_name: str
    colors: list[ColorHit]
    fonts: list[FontHit]
    primary_colors: list[str]
    secondary_colors: list[str]
    accent_colors: list[str]
    font_family: str | None
    tone_of_voice: str | None
    palette: dict
    confidence: str          # "high" | "medium" | "low"
    provenance: dict         # {"kit_pdf": str | None, "svg_files": [...], "image_files_sampled": int, "pages_scanned": int}


def _label_by_context(colors: list[ColorHit]) -> tuple[dict[str, list[str]], bool]:
    roles: dict[str, list[str]] = {"primary": [], "secondary": [], "accent": []}
    matched = False
    for hit in colors:
        low = hit.context.lower()
        for word, role in _ROLE_WORDS.items():
            if word in low:
                roles[role].append(hit.hex)
                matched = True
                break
    if not roles["primary"] and colors:
        roles["primary"].append(colors[0].hex)  # first-seen fallback
    return roles, matched


def _label_with_llm(colors, fonts, llm) -> dict | None:
    allowed = {c.hex for c in colors}
    prompt = (
        "You are labeling a brand kit. ONLY use hex codes from this list — never "
        f"invent one: {sorted(allowed)}. Fonts seen: "
        f"{sorted({f.family for f in fonts})}. Color contexts: "
        f"{[(c.hex, c.context) for c in colors]}. "
        'Reply with JSON only: {"primary": [], "secondary": [], "accent": [], '
        '"tone_of_voice": ""}'
    )
    try:
        raw = llm(prompt)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None
    out = {role: [h.upper() for h in data.get(role, []) if h.upper() in allowed]
           for role in ("primary", "secondary", "accent")}
    out["tone_of_voice"] = (data.get("tone_of_voice") or "").strip() or None
    return out


def _merge_color_hits(*groups: list[ColorHit]) -> list[ColorHit]:
    """Dedupe on hex across priority-ordered groups — the first group that
    contains a given hex wins (its page/context is kept)."""
    merged: list[ColorHit] = []
    seen: set[str] = set()
    for group in groups:
        for hit in group:
            if hit.hex not in seen:
                seen.add(hit.hex)
                merged.append(hit)
    return merged


def _merge_font_hits(*groups: list[FontHit]) -> list[FontHit]:
    """Dedupe on (family, style) across priority-ordered groups — the first
    group that contains a given key wins."""
    merged: list[FontHit] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for hit in group:
            key = (hit.family, hit.style)
            if key not in seen:
                seen.add(key)
                merged.append(hit)
    return merged


def build_profile(brand_name: str, sources: KitSources,
                  llm: Callable[[str], str] | None = None) -> BrandKitProfile:
    """Assemble a BrandKitProfile via the extraction source ladder (highest
    fidelity first): brand-kit PDF text/fonts > SVG vector fills > font-file
    names > flat-pixel frequency analysis of PNG creatives. Colors and fonts
    from higher-priority sources win on collision (dedupe on hex / on
    (family, style) respectively). LLM role/tone labeling may only ever pick
    from hexes actually extracted, across the union of all sources
    (anti-hallucination invariant)."""
    pdf_colors = extract_colors(sources.kit_pdf) if sources.kit_pdf else []
    pdf_fonts = extract_fonts(sources.kit_pdf) if sources.kit_pdf else []
    svg_colors = [hit for svg in sources.svg_files for hit in extract_svg_colors(svg)]
    pixel_colors = extract_pixel_colors(sources.image_files)
    file_fonts = fonts_from_files(sources.font_files)

    colors = _merge_color_hits(pdf_colors, svg_colors, pixel_colors)
    fonts = _merge_font_hits(file_fonts, pdf_fonts)

    roles, _matched = _label_by_context(colors)
    tone = None
    if llm is not None:
        labeled = _label_with_llm(colors, fonts, llm)
        if labeled:
            tone = labeled.pop("tone_of_voice")
            if any(labeled.values()):
                roles = labeled
    all_hexes = [c.hex for c in colors]
    palette = derive_palette(all_hexes) if len(all_hexes) >= 3 else {}
    family = None
    if fonts:
        best = max(fonts, key=lambda f: (f.embedded, len(f.pages)))
        family = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", best.family)  # BeVietnamPro → Be Vietnam Pro
    if pdf_colors or svg_colors:
        confidence = "high"
    elif pixel_colors:
        confidence = "medium"
    else:
        confidence = "low"
    return BrandKitProfile(
        brand_name=brand_name, colors=colors, fonts=fonts,
        primary_colors=roles["primary"], secondary_colors=roles["secondary"],
        accent_colors=roles["accent"], font_family=family, tone_of_voice=tone,
        palette=palette, confidence=confidence,
        provenance={
            "kit_pdf": str(sources.kit_pdf) if sources.kit_pdf else None,
            "svg_files": [p.name for p in sources.svg_files],
            "image_files_sampled": len(sources.image_files),
            "pages_scanned": _page_count(sources.kit_pdf) if sources.kit_pdf else 0,
        },
    )


def _page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count
