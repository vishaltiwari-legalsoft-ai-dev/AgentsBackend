"""Upload intake — what a direct-to-GCS upload must prove before GD keeps it.

Pure logic: no storage, no Firestore, no HTTP. The orchestrator
(``gd_direct_uploads``) reads bytes from the bucket and hands them here; this
module decides what the bytes ARE (never what the client said they were),
whether they are safe and affordable to decode, and derives the bounded
working copy the Graphics Designer reads.

Why the browser uploads straight to GCS: the Vercel relay refuses request
bodies over 4.5 MB and Cloud Run HTTP/1 over 32 MiB, and members need to
upload ~50 MB originals. So the bytes skip the API entirely and this module
is the gate they pass afterwards.

The four rules every decision here follows:

* **The bytes decide.** The type is sniffed from the first 64 KB; the
  client's content type and file name are never consulted.
* **Cost is known before it is paid.** An image's decoded size is estimated
  from its header (after the JPEG DCT draft) and refused over
  :data:`DECODE_BUDGET_BYTES` with the dimensions in the answer.
* **One decode at a time per instance.** :func:`decode_slot` is a
  ``BoundedSemaphore(1)`` acquired with a deadline, never an unbounded queue.
* **Every refusal says why.** :class:`IntakeRejected` carries the HTTP status
  and a stable ``code`` plus the facts (dimensions, limits, what was found and
  what is accepted), never a bare "invalid file".
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any, Iterator

from PIL import Image, ImageOps

# Import the four raster plugins we open, explicitly. ``Image.open(formats=…)``
# calls ``Image.init()`` — which imports EVERY plugin, EPS and PSD included —
# only when a requested format is not registered yet. With these four
# registered up front, the intake path never triggers that import.
from PIL import JpegImagePlugin, PngImagePlugin, TiffImagePlugin, WebPImagePlugin  # noqa: F401

# Pillow warns over this many pixels and refuses at twice it. Process-wide by
# Pillow's design; 125 MP is above Pillow's ~89 MP default so a large but
# legitimate photo is not refused by the library before our own, better
# explained budget check runs.
Image.MAX_IMAGE_PIXELS = 125_000_000

MB = 1024 * 1024

#: Decoded-size ceiling, in bytes, for one image. Decimal on purpose: the
#: design's boundary is "100 MP RGBA" (100,000,000 px x 4 B).
DECODE_BUDGET_BYTES = 400_000_000
#: Longest side of a raster working copy.
WORK_SIDE_PX = 4096
#: Longest side an SVG logo is rasterized to.
SVG_RASTER_PX = 2048
#: Bytes sniffed from the head of every upload.
SNIFF_BYTES = 64 * 1024
#: How far from the end ``%%EOF`` may sit in a PDF.
PDF_TAIL_BYTES = 1024
PDF_MAX_PAGES = 300
JPEG_WORK_QUALITY = 92

#: Per-instance decode slot. One heavy decode at a time: a 400 MB decode plus
#: its working copy is most of an instance's memory.
DECODE_SLOT_TIMEOUT_SECONDS = 30.0
DECODE_RETRY_AFTER_SECONDS = 30
_DECODE_SLOT = threading.BoundedSemaphore(1)

TICKET_TTL_SECONDS = 3600

RASTER_KINDS = ("png", "jpeg", "webp", "tiff")

#: What a sniffed kind is stored as (original file extension + content type).
KIND_EXT = {"png": "png", "jpeg": "jpg", "webp": "webp", "tiff": "tif", "svg": "svg",
            "pdf": "pdf", "ttf": "ttf", "otf": "otf"}
KIND_CONTENT_TYPE = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp",
                     "tiff": "image/tiff", "svg": "image/svg+xml", "pdf": "application/pdf",
                     "ttf": "font/ttf", "otf": "font/otf"}

_RASTER_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/pjpeg",
                                   "image/webp", "image/tiff", "image/tif"})
# A browser reports "" for an extension it does not know (fonts on Windows,
# TIFFs on some systems); the frontend then sends this, and the bytes decide
# at finalize like they always do.
_GENERIC_CONTENT_TYPE = "application/octet-stream"


# --------------------------------------------------------------------------- #
# Per-surface rules
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SurfaceRule:
    """What one upload surface accepts.

    ``scope`` says which route family signs it (a brand or a run).
    ``caps`` is per sniffed kind; ``content_types`` is the allowlist the
    signed ``Content-Type`` header must come from. ``alpha`` picks the working
    copy: PNG keeping transparency, or JPEG for opaque photos."""

    scope: str
    accepted: tuple[str, ...]
    caps: dict[str, int]
    content_types: frozenset[str]
    alpha: bool = True

    @property
    def max_bytes(self) -> int:
        return max(self.caps.values())

    def cap_for_kind(self, kind: str) -> int:
        return self.caps[kind]

    def cap_for_content_type(self, content_type: str) -> int:
        """The cap a signed upload of this declared type gets — the type is a
        claim, so finalize re-checks against the cap of what was sniffed."""
        for kind, ct in KIND_CONTENT_TYPE.items():
            if ct == content_type and kind in self.caps:
                return self.caps[kind]
        if content_type in _RASTER_CONTENT_TYPES:
            raster = [self.caps[k] for k in RASTER_KINDS if k in self.caps]
            if raster:
                return max(raster)
        return self.max_bytes


def _raster_caps(cap: int) -> dict[str, int]:
    return {k: cap for k in RASTER_KINDS}


_FONT_CONTENT_TYPES = frozenset({"font/ttf", "font/otf", "font/sfnt", "application/x-font-ttf",
                                 "application/x-font-otf", "application/font-sfnt",
                                 "application/vnd.ms-opentype", _GENERIC_CONTENT_TYPE})

SURFACES: dict[str, SurfaceRule] = {
    # Brand kit (``POST /api/gd/brands/{brand_id}/uploads``)
    "logo": SurfaceRule(
        "brand", RASTER_KINDS + ("svg",), _raster_caps(50 * MB) | {"svg": 5 * MB},
        _RASTER_CONTENT_TYPES | {"image/svg+xml", _GENERIC_CONTENT_TYPE}, alpha=True),
    "font": SurfaceRule("brand", ("ttf", "otf"), {"ttf": 2 * MB, "otf": 2 * MB},
                        _FONT_CONTENT_TYPES),
    "guidelines": SurfaceRule("brand", ("pdf",), {"pdf": 50 * MB},
                              frozenset({"application/pdf", _GENERIC_CONTENT_TYPE})),
    "reference": SurfaceRule("brand", RASTER_KINDS, _raster_caps(50 * MB),
                             _RASTER_CONTENT_TYPES | {_GENERIC_CONTENT_TYPE}, alpha=False),
    # One run (``POST /api/gd/runs/{run_id}/uploads``)
    "subject": SurfaceRule("run", RASTER_KINDS, _raster_caps(50 * MB),
                           _RASTER_CONTENT_TYPES | {_GENERIC_CONTENT_TYPE}, alpha=True),
    "background": SurfaceRule("run", RASTER_KINDS, _raster_caps(50 * MB),
                              _RASTER_CONTENT_TYPES | {_GENERIC_CONTENT_TYPE}, alpha=False),
    # Prompt images are labelled image/png all the way to the model
    # (``pipeline._prompt_references``), so their working copy is PNG.
    "prompt": SurfaceRule("run", RASTER_KINDS, _raster_caps(50 * MB),
                          _RASTER_CONTENT_TYPES | {_GENERIC_CONTENT_TYPE}, alpha=True),
    "element": SurfaceRule("run", RASTER_KINDS, _raster_caps(50 * MB),
                           _RASTER_CONTENT_TYPES | {_GENERIC_CONTENT_TYPE}, alpha=True),
}

BRAND_SURFACES = tuple(s for s, r in SURFACES.items() if r.scope == "brand")
RUN_SURFACES = tuple(s for s, r in SURFACES.items() if r.scope == "run")


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class IntakeRejected(Exception):
    """A file the service refuses. ``status`` is the HTTP status; ``body`` is
    the response detail (always carries a stable ``code``)."""

    def __init__(self, status: int, code: str, message: str, **facts: Any):
        super().__init__(message)
        self.status = status
        self.body = {"code": code, "message": message, **facts}


class DecodeBusy(Exception):
    """The per-instance decode slot was not free within the deadline."""

    retry_after = DECODE_RETRY_AFTER_SECONDS


@contextlib.contextmanager
def decode_slot(timeout: float | None = None) -> Iterator[None]:
    """Hold this instance's single decode slot, or raise :class:`DecodeBusy`
    after ``timeout`` seconds — callers answer 503 with ``Retry-After``."""
    wait = DECODE_SLOT_TIMEOUT_SECONDS if timeout is None else timeout
    if not _DECODE_SLOT.acquire(timeout=wait):
        raise DecodeBusy()
    try:
        yield
    finally:
        _DECODE_SLOT.release()


# --------------------------------------------------------------------------- #
# Content-type gate at sign time (a claim — finalize sniffs the bytes)
# --------------------------------------------------------------------------- #
_REFUSED_CONTENT_TYPES = {
    "image/heic": "heic", "image/heif": "heif", "image/heic-sequence": "heic",
    "image/heif-sequence": "heif", "image/avif": "avif",
    "image/vnd.adobe.photoshop": "psd", "application/x-photoshop": "psd",
    "image/x-photoshop": "psd", "image/psd": "psd",
    "application/postscript": "eps", "application/eps": "eps", "image/eps": "eps",
    "image/x-eps": "eps", "application/illustrator": "ai",
    "image/gif": "gif", "image/bmp": "bmp", "image/x-ms-bmp": "bmp",
    "font/woff": "woff", "font/woff2": "woff2", "font/collection": "ttc",
}


def check_declared_content_type(surface: str, content_type: str, *, file_name: str | None) -> str:
    """The normalized content type to sign, or :class:`IntakeRejected` (415)
    for a type this surface never accepts — so a HEIC is refused with its
    export advice before a single byte is uploaded."""
    rule = SURFACES[surface]
    ct = (content_type or "").split(";", 1)[0].strip().lower() or _GENERIC_CONTENT_TYPE
    if ct in rule.content_types:
        return ct
    raise unsupported_type(surface, _REFUSED_CONTENT_TYPES.get(ct, "unknown"), file_name,
                           declared=ct)


# --------------------------------------------------------------------------- #
# Sniffing — the bytes say what they are
# --------------------------------------------------------------------------- #
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
                b"mif1", b"msf1", b"mif2"}
_AVIF_BRANDS = {b"avif", b"avis"}


def _iso_bmff_kind(head: bytes) -> str | None:
    """HEIC/HEIF/AVIF all start with an ``ftyp`` box; its brands tell them apart."""
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    size = int.from_bytes(head[0:4], "big")
    box = head[8:max(16, min(size, len(head)))]
    brands = {box[0:4]} | {box[i:i + 4] for i in range(8, len(box) - 3, 4)}
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}:
        return "heic"
    if brands & _HEIF_BRANDS:
        return "heif"
    return None


def _looks_like_svg(head: bytes) -> bool:
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    starts = text.startswith((b"<?xml", b"<svg", b"<!--", b"<!DOCTYPE svg", b"<!doctype svg"))
    return starts and re.search(rb"<svg[\s>/]", head) is not None


def _is_bmp(head: bytes) -> bool:
    return (len(head) >= 18 and head[:2] == b"BM"
            and int.from_bytes(head[14:18], "little") in (12, 40, 52, 56, 64, 108, 124))


def sniff(head: bytes) -> str:
    """The kind the first bytes of a file say it is.

    Accepted somewhere: ``png jpeg webp tiff svg pdf ttf otf``. Refused
    everywhere, but named so the answer can say what to do instead:
    ``heic heif avif psd ai eps gif bmp woff woff2 ttc``. Anything else:
    ``unknown``. Strong binary signatures are tested before the SVG text
    heuristic; Illustrator files are told apart from the PDF / PostScript
    they are wrapped in."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if head.startswith(b"%PDF-"):
        illustrator = (b"Adobe Illustrator" in head or b"AIPrivateData" in head
                       or b"/Illustrator" in head)
        return "ai" if illustrator else "pdf"
    if head.startswith(b"%!PS-Adobe") or head.startswith(b"\xc5\xd0\xd3\xc6"):
        return "ai" if b"Adobe Illustrator" in head else "eps"
    if head.startswith(b"8BPS"):
        return "psd"
    bmff = _iso_bmff_kind(head)
    if bmff:
        return bmff
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if _is_bmp(head):
        return "bmp"
    if head.startswith(b"OTTO"):
        return "otf"
    if head[:4] in (b"\x00\x01\x00\x00", b"true"):
        return "ttf"
    if head[:4] == b"wOFF":
        return "woff"
    if head[:4] == b"wOF2":
        return "woff2"
    if head[:4] == b"ttcf":
        return "ttc"
    if _looks_like_svg(head):
        return "svg"
    return "unknown"


_EXPORT_ADVICE = {
    "heic": "HEIC/HEIF and AVIF images are not supported — export as JPEG/PNG.",
    "heif": "HEIC/HEIF and AVIF images are not supported — export as JPEG/PNG.",
    "avif": "HEIC/HEIF and AVIF images are not supported — export as JPEG/PNG.",
    "psd": "Photoshop, Illustrator and EPS files are not supported — export a flattened PNG/JPEG/TIFF.",
    "ai": "Photoshop, Illustrator and EPS files are not supported — export a flattened PNG/JPEG/TIFF.",
    "eps": "Photoshop, Illustrator and EPS files are not supported — export a flattened PNG/JPEG/TIFF.",
}


def unsupported_type(surface: str, got: str, file_name: str | None, **facts: Any) -> IntakeRejected:
    """The 415 every surface gives for a kind it does not take."""
    accepted = list(SURFACES[surface].accepted)
    message = _EXPORT_ADVICE.get(got) or (
        f"{'This file type' if got == 'unknown' else got.upper()} is not accepted for a "
        f"{surface} upload — use {', '.join(a.upper() for a in accepted)}.")
    return IntakeRejected(415, "unsupported_file_type", message, got=got, accepted=accepted,
                          file=file_name, **facts)


def check_kind(surface: str, head: bytes, size: int, *, file_name: str | None) -> str:
    """Sniff, then enforce the surface's allowlist and the sniffed kind's cap."""
    if size <= 0:
        raise IntakeRejected(422, "empty_file", "The uploaded file is empty.", file=file_name)
    kind = sniff(head)
    rule = SURFACES[surface]
    if kind not in rule.accepted:
        raise unsupported_type(surface, kind, file_name)
    cap = rule.cap_for_kind(kind)
    if size > cap:
        raise too_large(size, cap, file_name)
    return kind


def too_large(size: int, cap: int, file_name: str | None) -> IntakeRejected:
    return IntakeRejected(413, "file_too_large",
                          f"The file is {size / MB:.1f} MB; the limit here is {cap / MB:.0f} MB.",
                          bytes=size, limit_bytes=cap, file=file_name)


# --------------------------------------------------------------------------- #
# Raster images — estimate, decode, derive the working copy
# --------------------------------------------------------------------------- #
#: Bytes Pillow actually allocates per pixel for each mode (RGB is stored
#: padded to 4 bytes) — the honest decode cost, not the channel count.
_BYTES_PER_PIXEL = {"1": 1, "L": 1, "P": 1, "I;16": 2, "I;16B": 2, "I;16L": 2, "I;16N": 2,
                    "LA": 4, "La": 4, "PA": 4, "RGB": 4, "RGBA": 4, "RGBa": 4, "RGBX": 4,
                    "CMYK": 4, "YCbCr": 4, "LAB": 4, "HSV": 4, "I": 4, "F": 4}

_OPEN_FORMATS = ["PNG", "JPEG", "WEBP", "TIFF"]


@dataclass(frozen=True)
class WorkingCopy:
    """The derived, bounded image GD reads, plus what the original was."""

    data: bytes
    ext: str                       # "png" | "jpg"
    content_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    flags: tuple[str, ...] = ()
    pages_used: str | None = None  # "1 of N" for a multi-page TIFF


def _too_big_to_decode(width: int, height: int, mode: str, file_name: str | None,
                       *, pixels: int | None = None) -> IntakeRejected:
    cost = width * height * _BYTES_PER_PIXEL.get(mode, 4) if width and height else None
    return IntakeRejected(
        422, "image_too_large",
        (f"The image is {width}x{height} px; decoding it would need about "
         f"{cost / 1e6:.0f} MB and the limit is {DECODE_BUDGET_BYTES / 1e6:.0f} MB. "
         "Export it smaller (under ~100 megapixels) and upload again.")
        if cost else
        (f"The image has {pixels or 'too many'} pixels — more than this service can decode. "
         "Export it smaller (under ~100 megapixels) and upload again."),
        width=width or None, height=height or None, decoded_bytes=cost,
        limit_bytes=DECODE_BUDGET_BYTES, file=file_name)


def _unreadable(kind: str, file_name: str | None, exc: Exception | None = None) -> IntakeRejected:
    return IntakeRejected(422, "image_unreadable",
                          f"The file starts like a {kind.upper()} but could not be decoded "
                          "(it may be truncated or corrupt).", got=kind, file=file_name)


def _sixteen_to_eight(im: Image.Image) -> Image.Image:
    """16-bit greyscale to 8-bit L by SCALING (``>> 8``), not clipping:
    ``convert("L")`` clips, so every value over 255 — nearly the whole
    16-bit range — would come out white. ``I`` (32-bit, what older Pillow
    opens a 16-bit PNG as) is treated as 16-bit and clipped past it; ``F``
    is clipped to 0-255."""
    import numpy as np

    arr = np.asarray(im)
    if im.mode == "F":
        out = np.clip(arr, 0, 255)
    else:
        out = np.clip(arr.astype(np.int64), 0, 65535) >> 8
    return Image.fromarray(out.astype(np.uint8), "L")


def _to_working_mode(im: Image.Image, *, alpha: bool, flags: list[str]) -> Image.Image:
    """Colour + mode normalization, run AFTER the thumbnail so it touches at
    most ``WORK_SIDE_PX``² pixels."""
    from PIL import ImageCms

    if im.mode == "CMYK":
        icc = im.info.get("icc_profile")
        converted = None
        if icc:
            try:
                converted = ImageCms.profileToProfile(
                    im, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                    ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception:  # noqa: BLE001 - a broken profile degrades to the naive path, flagged
                converted = None
        if converted is None:
            converted = im.convert("RGB")
            flags.append("color_converted_without_profile")
        im = converted
    elif im.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
        im = _sixteen_to_eight(im)

    has_alpha = im.mode in ("RGBA", "RGBa", "LA", "La", "PA") or (
        im.mode == "P") or "transparency" in im.info
    if im.mode == "P" or im.mode == "PA":
        im = im.convert("RGBA")                      # palette -> RGBA
    elif im.mode in ("LA", "La", "RGBa"):
        im = im.convert("RGBA")
    elif im.mode != "RGBA" and "transparency" in im.info:
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")

    if alpha:
        if im.mode == "RGBA" and not has_alpha:
            im = im.convert("RGB")
        return im
    if im.mode == "RGBA":
        # JPEG has no alpha: flatten onto white, and say so.
        if im.getextrema()[3][0] < 255:
            flags.append("alpha_flattened_to_white")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel("A"))
        im = flat
    return im


def derive_working_copy(fp: IO[bytes], kind: str, *, alpha: bool,
                        file_name: str | None = None) -> WorkingCopy:
    """Decode one raster (``png``/``jpeg``/``webp``/``tiff``) from ``fp`` and
    return its working copy. Call inside :func:`decode_slot`.

    Order, and why:
      1. open with ONLY the four raster plugins (EPS/PSD/Ghostscript never run);
      2. JPEG ``draft("RGB", (4096, 4096))`` — a 1/2-1/8 DCT-scale decode;
      3. the decode cost is estimated from the (drafted) header and refused
         over :data:`DECODE_BUDGET_BYTES`, before any pixel is decoded;
      4. ``load``;
      5. ``thumbnail((4096, 4096), reducing_gap=2.0)`` on the source mode;
      6. ``exif_transpose`` — on the thumbnail, not the full decode: the box
         is square so the result is identical, and transposing the full image
         would allocate a second full-size copy (doubling peak memory);
      7. colour/mode: CMYK -> sRGB through the embedded ICC profile (naive
         and flagged without one), 16-bit -> 8-bit, palette -> RGBA;
      8. encode: PNG when the surface keeps alpha, else JPEG q92.

    A multi-page TIFF uses page 1 and records ``pages_used``.
    """
    flags: list[str] = []
    try:
        im = Image.open(fp, formats=_OPEN_FORMATS)
    except Image.DecompressionBombError as exc:
        pixels = re.search(r"\((\d+) pixels\)", str(exc))
        raise _too_big_to_decode(0, 0, "RGBA", file_name,
                                 pixels=int(pixels.group(1)) if pixels else None) from exc
    except Exception as exc:  # noqa: BLE001 - signature matched, Pillow did not
        raise _unreadable(kind, file_name, exc) from exc

    try:
        original_width, original_height = im.size   # the stored pixel grid
        frames = int(getattr(im, "n_frames", 1) or 1)
        if isinstance(im, JpegImagePlugin.JpegImageFile):   # MPO subclasses it
            im.draft("RGB", (WORK_SIDE_PX, WORK_SIDE_PX))
        width, height = im.size
        if width * height * _BYTES_PER_PIXEL.get(im.mode, 4) > DECODE_BUDGET_BYTES:
            raise _too_big_to_decode(original_width, original_height, im.mode, file_name)
        try:
            im.load()
            im.thumbnail((WORK_SIDE_PX, WORK_SIDE_PX), reducing_gap=2.0)
            ImageOps.exif_transpose(im, in_place=True)
        except Exception as exc:  # noqa: BLE001 - truncated / corrupt pixel data
            raise _unreadable(kind, file_name, exc) from exc

        im = _to_working_mode(im, alpha=alpha, flags=flags)
        out = io.BytesIO()
        if alpha:
            im.save(out, format="PNG")
            ext, ctype = "png", "image/png"
        else:
            im.save(out, format="JPEG", quality=JPEG_WORK_QUALITY)
            ext, ctype = "jpg", "image/jpeg"
        return WorkingCopy(
            data=out.getvalue(), ext=ext, content_type=ctype,
            width=im.width, height=im.height,
            original_width=original_width, original_height=original_height,
            flags=tuple(flags),
            pages_used=f"1 of {frames}" if frames > 1 else None,
        )
    finally:
        with contextlib.suppress(Exception):
            im.close()


# --------------------------------------------------------------------------- #
# SVG (logos only)
# --------------------------------------------------------------------------- #
_SVG_FORBIDDEN_ELEMENTS = {"script", "foreignobject", "image"}
_SVG_URL_REF = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.IGNORECASE)
_LENGTH = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$")


@dataclass(frozen=True)
class SvgInfo:
    width: float | None
    height: float | None


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _unsafe_svg(reason: str, file_name: str | None) -> IntakeRejected:
    return IntakeRejected(422, "unsafe_svg",
                          f"This SVG cannot be used as a logo: it contains {reason}. "
                          "Export a plain SVG (shapes and text only) or a PNG.",
                          reason=reason, file=file_name)


def _norm(value: str) -> str:
    return re.sub(r"[\s\x00-\x1f]", "", value or "").lower()


def check_svg(data: bytes, *, file_name: str | None = None) -> SvgInfo:
    """Parse with defusedxml (no DTD, no entities, no external references) and
    refuse anything that can execute, embed or fetch: ``<script>``,
    ``<foreignObject>``, ``<image>``, ``data:`` and ``javascript:`` in any
    attribute or stylesheet, ``on*=`` handlers, any ``href`` that is not
    ``#local``, and any ``url(...)`` that is not ``url(#local)``.

    Checked on the PARSED document, so character references and whitespace
    cannot hide a scheme (``&#106;avascript:`` is ``javascript:`` here), and a
    word in a ``<text>`` element ("Big Data: Inc") is not mistaken for a URI."""
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import fromstring
    from xml.etree.ElementTree import ParseError

    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError, ValueError) as exc:
        reason = ("a DTD or entity declaration"
                  if isinstance(exc, DefusedXmlException) else "malformed XML")
        raise _unsafe_svg(reason, file_name) from exc
    if _local(root.tag) != "svg":
        raise IntakeRejected(415, "unsupported_file_type",
                             "The file is XML but not an SVG image.", got="unknown",
                             accepted=list(SURFACES["logo"].accepted), file=file_name)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _local(el.tag)
        if tag in _SVG_FORBIDDEN_ELEMENTS:
            raise _unsafe_svg(f"an <{el.tag.rsplit('}', 1)[-1]}> element", file_name)
        if tag == "style":
            css = _norm(el.text or "")
            if "@import" in css:
                raise _unsafe_svg("a CSS @import", file_name)
            if "javascript:" in css:
                raise _unsafe_svg("a javascript: URI", file_name)
            if "data:" in css:
                raise _unsafe_svg("a data: URI", file_name)
        for name, value in el.attrib.items():
            attr = _local(name)
            v = _norm(value)
            if attr.startswith("on"):
                raise _unsafe_svg(f"an event handler ({attr})", file_name)
            if "javascript:" in v:
                raise _unsafe_svg("a javascript: URI", file_name)
            if "data:" in v:
                raise _unsafe_svg("a data: URI", file_name)
            if attr == "href" and not v.startswith("#"):
                raise _unsafe_svg("a link to an external resource", file_name)
        for text in [el.text or "", *(el.attrib.values())]:
            for target in _SVG_URL_REF.findall(text):
                if target and not target.startswith("#"):
                    raise _unsafe_svg("a url() reference to an external resource", file_name)
    return SvgInfo(width=_svg_length(root, "width", 2), height=_svg_length(root, "height", 3))


def _svg_length(root, attr: str, viewbox_index: int) -> float | None:
    m = _LENGTH.match(root.attrib.get(attr, ""))
    if m:
        return float(m.group(1)) or None
    box = re.split(r"[\s,]+", (root.attrib.get("viewBox") or "").strip())
    if len(box) == 4:
        try:
            return float(box[viewbox_index]) or None
        except ValueError:
            return None
    return None


def svg_raster_size(info: SvgInfo) -> tuple[int, int]:
    """Output size with the longest side at :data:`SVG_RASTER_PX`, so an
    extreme aspect ratio cannot ask the renderer for a 2048 x 200,000 canvas."""
    w, h = info.width, info.height
    if not w or not h or w <= 0 or h <= 0:
        return SVG_RASTER_PX, SVG_RASTER_PX
    if w >= h:
        return SVG_RASTER_PX, max(1, round(SVG_RASTER_PX * h / w))
    return max(1, round(SVG_RASTER_PX * w / h)), SVG_RASTER_PX


class SvgRendererUnavailable(RuntimeError):
    """cairosvg (native libcairo) is not installed on this host."""


def _refuse_fetch(url: str, *_args: Any, **_kwargs: Any) -> Any:
    raise ValueError(f"external resource refused: {url[:80]}")


def rasterize_svg(data: bytes, info: SvgInfo) -> bytes:
    """Render a CHECKED SVG to PNG with cairosvg. Call inside
    :func:`decode_slot`. No fetching: references were refused by
    :func:`check_svg` and the fetcher refuses anything regardless."""
    try:
        from cairosvg.surface import PNGSurface
    except Exception as exc:  # noqa: BLE001 - OSError when libcairo is missing
        raise SvgRendererUnavailable("cairosvg / libcairo is not available on this host") from exc
    width, height = svg_raster_size(info)
    # ``svg2png`` does not forward ``url_fetcher`` (cairosvg 2.9); ``convert``
    # passes it through to the parser's Tree.
    return PNGSurface.convert(bytestring=data, output_width=width, output_height=height,
                              unsafe=False, url_fetcher=_refuse_fetch)


def svg_working_copy(data: bytes, info: SvgInfo, png: bytes) -> WorkingCopy:
    """Wrap the rasterized PNG (re-read so its size is the truth, not a claim)."""
    with Image.open(io.BytesIO(png), formats=["PNG"]) as im:
        w, h = im.size
    return WorkingCopy(data=png, ext="png", content_type="image/png", width=w, height=h,
                       original_width=round(info.width) if info.width else w,
                       original_height=round(info.height) if info.height else h)


# --------------------------------------------------------------------------- #
# PDF (brand guidelines)
# --------------------------------------------------------------------------- #
def check_pdf_tail(tail: bytes, *, file_name: str | None = None) -> None:
    if b"%%EOF" not in tail:
        raise IntakeRejected(422, "pdf_truncated",
                             "The PDF has no end-of-file marker — it looks truncated. "
                             "Export or download it again.", file=file_name)


def count_pdf_pages(fp: IO[bytes], *, file_name: str | None = None) -> int:
    """Page count from the document catalog (``/Root /Pages /Count``) via the
    xref — no page is rendered and no text is extracted. Call inside
    :func:`decode_slot`. Refuses over :data:`PDF_MAX_PAGES`."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(fp, strict=False)
        count = reader.trailer["/Root"]["/Pages"]["/Count"]
        pages = int(count)
    except Exception as exc:  # noqa: BLE001 - any parse failure is "unreadable"
        raise IntakeRejected(422, "pdf_unreadable",
                             "The PDF could not be read (it may be damaged).",
                             file=file_name) from exc
    if pages < 1:
        raise IntakeRejected(422, "pdf_unreadable", "The PDF has no pages.", file=file_name)
    if pages > PDF_MAX_PAGES:
        raise IntakeRejected(422, "pdf_too_many_pages",
                             f"The PDF has {pages} pages; the limit is {PDF_MAX_PAGES}.",
                             pages=pages, limit_pages=PDF_MAX_PAGES, file=file_name)
    return pages


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
def check_font(data: bytes, *, file_name: str | None = None) -> None:
    """``fontTools`` must read the ``head``, ``name`` and ``cmap`` tables —
    the three the renderer needs to draw a single glyph by name."""
    from fontTools.ttLib import TTFont

    try:
        font = TTFont(io.BytesIO(data), lazy=True)
        try:
            for table in ("head", "name", "cmap"):
                font[table]
            if not font["cmap"].getBestCmap():
                raise ValueError("no usable character map")
        finally:
            font.close()
    except Exception as exc:  # noqa: BLE001 - any table that will not read is a reject
        raise IntakeRejected(422, "font_unreadable",
                             "The font file could not be read (its head, name or cmap table "
                             "is missing or damaged).", file=file_name) from exc


# --------------------------------------------------------------------------- #
# Upload tickets — stateless, HMAC-signed
# --------------------------------------------------------------------------- #
class TicketError(Exception):
    """A ticket the caller may not finalize with; ``code`` is the client code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Ticket:
    sub: str
    surface: str
    target: str
    object: str
    cap: int
    exp: int
    extra: dict = field(default_factory=dict)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_ticket(key: bytes, *, sub: str, surface: str, target: str, object_name: str,
                cap: int, now: float | None = None, ttl: int = TICKET_TTL_SECONDS) -> tuple[str, int]:
    """``(ticket, exp)``: ``base64url(json claims) . base64url(HMAC-SHA256)``."""
    exp = int((time.time() if now is None else now) + ttl)
    claims = {"v": 1, "sub": sub, "surface": surface, "target": target,
              "object": object_name, "cap": int(cap), "exp": exp}
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def read_ticket(key: bytes, token: str, *, now: float | None = None) -> Ticket:
    """Verify the signature (constant time) and the expiry; :class:`TicketError`
    otherwise. Identity / target checks are the caller's, against the request."""
    try:
        body, sig = (token or "").strip().split(".", 1)
        expected = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    except Exception as exc:  # noqa: BLE001 - malformed is the same as forged
        raise TicketError("upload_ticket_invalid", "The upload ticket is malformed.") from exc
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        raise TicketError("upload_ticket_invalid", "The upload ticket's signature does not match.")
    try:
        claims = json.loads(_unb64(body))
        ticket = Ticket(sub=str(claims["sub"]), surface=str(claims["surface"]),
                        target=str(claims["target"]), object=str(claims["object"]),
                        cap=int(claims["cap"]), exp=int(claims["exp"]))
    except Exception as exc:  # noqa: BLE001
        raise TicketError("upload_ticket_invalid", "The upload ticket is malformed.") from exc
    if ticket.surface not in SURFACES:
        raise TicketError("upload_ticket_invalid", "The upload ticket names an unknown surface.")
    if (time.time() if now is None else now) >= ticket.exp:
        raise TicketError("upload_ticket_expired",
                          "The upload ticket has expired — upload the file again.")
    return ticket
