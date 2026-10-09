"""Sample report -> vendor-report layout (MR Phase 2).

A team member uploads a report they like, as a PDF or a PNG/JPEG. One model call
reads it and maps it onto :data:`vendor_report_render.SECTION_REGISTRY`: which of
our section types appear and in what order, their titles, which metrics they
show, and theme tokens (hex colours, a font family from the ones we embed).
:func:`extract_layout` is the whole public surface.

**What the model decides, and what it cannot.** It picks layout and nothing
else. Numbers never come from it; the layout is rendered later against real
data. Its output is constrained by a JSON schema generated from the registry
(the types are an enum, the metric choices are enums, every other field is a
string, empty when the sample does not show it), and every value then goes through this module's normaliser
and :func:`vendor_report_render.layout_from_dict` before anything is returned.
So the blast radius of a confused or manipulated model is a layout built from
our own catalog, with plain-text titles that are capped here and escaped by the
renderer.

**Uploaded documents are hostile input.** A sample can say "ignore your
instructions". The prompt tells the model to treat everything in the images as
data, and the schema means it has nothing else to emit. PDFs are rasterised
here (pypdfium2) and only the pixels are sent. A PDF's text layer, including
any hidden white-on-white text, never reaches the model. The upload's filename
is not sent either.

**Honest failure.** A missing key, an offline flag, a failed or timed-out call,
output that is still invalid after one repair, an encrypted / unreadable /
over-long upload, an upload whose worst-case cost exceeds the ceiling, or a
sample with nothing we can fill: each raises :class:`TemplateExtractionError`
with a reason a user can read. There is no fallback layout. A default layout
handed back as "what the AI read" would be exactly the fake output this repo
forbids.

**What the code adds, stated in the result.** ``data_gaps`` (the panel that
explains every em-dash) is pinned into every layout if the sample lacked it.
It is the report's honesty panel, and a sample cannot opt us out of explaining
missing figures. Sample colours that would make body text unreadable are
dropped back to our defaults. Both are listed in ``notes``, never done
silently.

Run logging: the route that calls this declares the a6 trail
(``trail.records(...)``, unit ``OUTPUT``) and passes its ``Activity`` in as
``activity``; on success this module notes the one-line summary into it, and
the trail writes it through ``run_tracking.record_activity``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypedDict

from . import config
from . import vendor_report as vr
from . import vendor_report_render as vrr
from .analysis import is_offline
from .board_report_fonts import FACES
from .board_report_render import MONO, PALETTE, SANS, SERIF

logger = logging.getLogger("agentos.mr.template_extract")

#: Bumped whenever the prompt, the schema or the normaliser changes what a
#: given sample maps to. Stamped into every result.
PROMPT_VERSION = "mr-template-extract/1"

_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "template_extract.txt"

Kind = Literal["pdf", "png", "jpeg"]

# --- limits ------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 10
#: Images sent per upload, after tall pages are cut into strips.
MAX_IMAGES = 10
#: An uploaded image larger than this is refused before it is decoded.
MAX_SOURCE_PIXELS = 40_000_000
#: Each image sent is at most this many pixels (~1,530 Claude image tokens),
#: which is also the size Claude would downscale to; sending more buys nothing.
IMAGE_MAX_PIXELS = 1_150_000
IMAGE_MAX_EDGE = 1568
#: A page taller than this many times its width is cut into strips rather than
#: shrunk until its text is unreadable (a full-page screenshot, a one-page PDF
#: printed from a long web page).
TALL_RATIO = 1.6
STRIP_WIDTH = 1000
STRIP_HEIGHT = 1150
STRIP_OVERLAP = 60
#: Model-supplied text caps. Everything model-written is plain text; the
#: renderer escapes it, and these keep a hostile sample from writing a novel
#: into a heading.
TITLE_MAX = 80
DESCRIPTION_MAX = 240
TEXT_OPTION_MAX = 60
#: A sample colour this close (Euclidean RGB) to our default IS our default:
#: colours read off a rendered page come back a few units off, and a sample
#: that uses our palette should map to our theme, not to a near-copy of it.
SAME_COLOUR_DISTANCE = 24.0
#: WCAG AA for body text.
MIN_TEXT_CONTRAST = 4.5

#: Not a registry type: the model's word for "a section we cannot fill".
UNSUPPORTED = "unsupported"

#: Allowed values for registry string options that are really enums. The
#: registry carries only the default; the renderer's code defines the rest.
#: ``test_template_extract`` pins that every key here is a registry option.
_ENUM_OPTIONS: dict[str, tuple[str, ...]] = {"sort": ("tab", "budget", "spend")}

#: What each section type looks like in a sample: the part of the catalog the
#: registry does not carry. The test suite pins that this covers the registry
#: exactly, so a new section type cannot ship without a description here.
SECTION_HINTS: dict[str, str] = {
    "header": "The cover band at the top: a small kicker line, the report title "
              "or period, a short summary, and a strip of 3-6 headline KPIs.",
    "portfolio_glance": "A grid of KPI tiles (label + big number) for the whole "
                        "portfolio, often with one tile emphasised.",
    "benchmark_movers": "A horizontal bar chart of the percentage gap between "
                        "portfolio metrics and their benchmark targets.",
    "budget_vs_spend": "A per-vendor bar chart comparing budget allocation with "
                       "spend so far.",
    "demos_by_vendor": "Per-vendor bars comparing demos booked with demos "
                       "completed.",
    "channel_mix": "Charts of spend and projected revenue broken down by "
                   "marketing channel (Google, Meta, Email ...).",
    "vendor_scorecard": "A table with one row per vendor and a portfolio total "
                        "row; the columns are metrics.",
    "highlights": "One block holding both a positive list (standouts, what's "
                  "working) and a cautionary list (watch items, needs attention).",
    "standouts": "A list of positive signals shown on its own.",
    "watch_items": "A list of concerns or watch items shown on its own.",
    "action_summary": "A table of vendor, status pill and recommended action.",
    "data_gaps": "A panel explaining missing figures, em-dashes and the basis "
                 "of the data.",
    "footer": "The closing band with methodology or provenance text.",
}

#: What each theme colour token does in our report. Pinned to cover PALETTE.
TOKEN_ROLES: dict[str, str] = {
    "ink": "main dark colour: body text, the cover band background, dark table headers",
    "ink-soft": "secondary dark text (lead paragraphs)",
    "paper": "page background",
    "paper-2": "subtle second background (zebra rows, note boxes)",
    "gold": "accent: section number badges, kicker text, the second chart series",
    "gold-soft": "light accent: accent tile border, emphasised word in the title",
    "slate": "muted text (captions, labels) and the first chart series",
    "pos": "positive / on-target marks (usually green)",
    "neg": "negative / off-target marks (usually red)",
    "line": "hairline borders around cards and panels",
    "grid": "chart gridlines",
    "muted": "small labels on the dark cover band",
}

_WHITE = "#FFFFFF"
#: (text token, background token or literal) pairs that carry body text in
#: our stylesheet: body on page, text on white cards/panels/tables, lead
#: paragraphs, the cover band, captions.
BODY_TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("ink", "paper"), ("ink", _WHITE), ("ink-soft", "paper"),
    ("paper", "ink"), ("slate", _WHITE),
)


# --- public types ----------------------------------------------------------------------

class Usage(TypedDict):
    input_tokens: int
    output_tokens: int
    cost_usd: float
    calls: int


class ExtractionResult(TypedDict):
    layout: dict                    # passes vendor_report_render.layout_from_dict
    unsupported: list[dict]         # [{title, description}], shown to the user
    matched_count: int              # sample sections mapped to a catalog type
    model: str                      # the model OpenRouter says served the call
    usage: Usage
    notes: list[dict]               # [{kind, detail, ...}] what the code changed and why
    source: dict                    # {filename, kind, pages, images}
    prompt_version: str


class TemplateExtractionError(Exception):
    """Extraction failed. ``reason`` is written for the person who uploaded the
    file; ``code`` is a stable slug for the caller; ``usage`` is what was spent
    before the failure (``None`` when nothing was); ``unsupported`` carries the
    sample's sections when the failure is that none of them could be filled."""

    def __init__(self, reason: str, *, code: str, usage: Usage | None = None,
                 unsupported: list[dict] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.usage = usage
        self.unsupported = unsupported or []


# --- fonts ---------------------------------------------------------------------------

def _family(stack: str) -> str:
    return stack.split(",")[0].strip().strip("'\"")


def _generic(stack: str) -> str:
    return stack.split(",")[-1].strip()


_EMBEDDED_FAMILIES = tuple(dict.fromkeys(face[0] for face in FACES))
#: Embedded family -> the font stack it heads, read off the renderer's stacks.
FONT_STACKS: dict[str, str] = {_family(s): s for s in (SERIF, SANS, MONO)
                               if _family(s) in _EMBEDDED_FAMILIES}


# --- the response schema, generated from the registry ------------------------------------

#: "Not shown in the sample" is an empty value, never null: Anthropic's
#: structured output refuses a schema with more than 16 union-typed (nullable)
#: parameters, and this one would have 25. An empty string / empty list is
#: unambiguous here, because no real value of any field is empty.
UNSET = ""


def _metric_option_keys(name: str) -> list[str]:
    accepted: set[str] = set()
    for entry in vrr.SECTION_REGISTRY.values():
        if name in entry.options:
            accepted.update(entry.metrics)
    return [k for k in vr.METRICS if k in accepted]


def _option_schema(name: str, default: Any) -> dict | None:
    """The JSON schema of one registry option's value, or ``None`` when the
    option is behaviour rather than visible layout (booleans, thresholds) and
    so stays at its registry default."""
    if name in vrr.METRIC_OPTIONS:
        keys = _metric_option_keys(name)
        if isinstance(default, str):
            return {"type": "string", "enum": [UNSET, *keys]}
        return {"type": "array", "items": {"type": "string", "enum": keys}}
    if name == "columns":
        return {"type": "array",
                "items": {"type": "string", "enum": list(vrr.SCORECARD_COLUMNS)}}
    if name in _ENUM_OPTIONS:
        return {"type": "string", "enum": [UNSET, *_ENUM_OPTIONS[name]]}
    if isinstance(default, str):
        return {"type": "string"}
    return None


def model_options() -> dict[str, dict]:
    """Every registry option the model may set -> its value schema."""
    out: dict[str, dict] = {}
    for entry in vrr.SECTION_REGISTRY.values():
        for name, default in entry.options.items():
            if name not in out:
                spec = _option_schema(name, default)
                if spec is not None:
                    out[name] = spec
    return out


@lru_cache(maxsize=1)
def response_schema() -> dict:
    """The strict JSON schema the model's reply must satisfy."""
    opts = model_options()
    section = {
        "type": "object", "additionalProperties": False,
        "required": ["type", "title", "description", "options"],
        "properties": {
            "type": {"type": "string", "enum": [*vrr.SECTION_REGISTRY, UNSUPPORTED]},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "options": {"type": "object", "additionalProperties": False,
                        "required": list(opts),
                        "properties": dict(opts)},
        },
    }
    fonts = {"type": "string", "enum": [UNSET, *FONT_STACKS]}
    theme = {
        "type": "object", "additionalProperties": False,
        "required": ["colors", "heading_font", "body_font"],
        "properties": {
            "colors": {"type": "object", "additionalProperties": False,
                       "required": list(PALETTE),
                       "properties": {k: {"type": "string"} for k in PALETTE}},
            "heading_font": fonts,
            "body_font": fonts,
        },
    }
    return {"type": "object", "additionalProperties": False,
            "required": ["sections", "theme"],
            "properties": {"sections": {"type": "array", "items": section},
                           "theme": theme}}


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict, "array": list, "string": str, "null": type(None),
    "boolean": bool, "integer": int,
}


def schema_errors(schema: Mapping, value: Any, path: str = "$") -> list[str]:
    """Check ``value`` against the subset of JSON Schema :func:`response_schema`
    uses. Strict structured output should make this a formality; it is the
    guard for a provider that did not honour the schema."""
    if "anyOf" in schema:
        if any(not schema_errors(s, value, path) for s in schema["anyOf"]):
            return []
        return [f"{path}: matches none of the allowed shapes"]
    errors: list[str] = []
    t = schema.get("type")
    if t and not isinstance(value, _JSON_TYPES[t]):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} is not one of the allowed values"]
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key!r}")
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}: unexpected key {k!r}" for k in value if k not in props)
        for key, sub in props.items():
            if key in value:
                errors.extend(schema_errors(sub, value[key], f"{path}.{key}"))
    elif t == "array":
        for i, item in enumerate(value):
            errors.extend(schema_errors(schema.get("items", {}), item, f"{path}[{i}]"))
    return errors


# --- the prompt, generated from the registry ----------------------------------------------

def _catalog() -> str:
    lines = []
    for entry in vrr.SECTION_REGISTRY.values():
        hint = SECTION_HINTS.get(entry.type, entry.title)
        opts = []
        for name, default in entry.options.items():
            spec = _option_schema(name, default)
            if spec is None:
                continue
            if name in vrr.METRIC_OPTIONS:
                accepts = ("any metric key" if set(entry.metrics) >= set(vr.METRICS)
                           else ", ".join(k for k in vr.METRICS if k in entry.metrics))
                shape = "one metric key" if isinstance(default, str) else "metric keys"
                opts.append(f"{name} ({shape}; accepts: {accepts})")
            elif name == "columns":
                opts.append(f"{name} (scorecard column keys)")
            elif name in _ENUM_OPTIONS:
                opts.append(f"{name} (one of: {', '.join(_ENUM_OPTIONS[name])})")
            else:
                opts.append(f"{name} (short text)")
        tail = f" Options: {'; '.join(opts)}." if opts else " Options: none."
        lines.append(f'- {entry.type}: "{entry.title}". {hint}{tail}')
    return "\n".join(lines)


@lru_cache(maxsize=1)
def system_prompt() -> str:
    metrics = "\n".join(f"- {k}: {label}" for k, (label, _kind) in vr.METRICS.items())
    columns = "\n".join(f"- {k}: {spec[2]}" for k, spec in vrr.SCORECARD_COLUMNS.items())
    tokens = "\n".join(f"- {k}: {TOKEN_ROLES.get(k, k)}" for k in PALETTE)
    fonts = "\n".join(f"- {fam} ({_generic(stack)})" for fam, stack in FONT_STACKS.items())
    return (_PROMPT_FILE.read_text(encoding="utf-8")
            .replace("{catalog}", _catalog()).replace("{metrics}", metrics)
            .replace("{columns}", columns).replace("{tokens}", tokens)
            .replace("{fonts}", fonts))


# --- rasterising ---------------------------------------------------------------------------

def _fail(reason: str, code: str, **kw: Any) -> TemplateExtractionError:
    return TemplateExtractionError(reason, code=code, **kw)


def _resize(img, scale: float):
    from PIL import Image

    if scale >= 1.0:
        return img
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


def _strip_count(width: float, height: float) -> int:
    """How many images one page of this size becomes."""
    if height <= width * TALL_RATIO:
        return 1
    h = height * min(1.0, STRIP_WIDTH / width)
    step = STRIP_HEIGHT - STRIP_OVERLAP
    return max(1, math.ceil((h - STRIP_OVERLAP) / step))


def _to_images(img) -> list:
    """One page -> the images sent for it: shrunk to the per-image budget, or
    cut into overlapping strips when the page is tall."""
    w, h = img.size
    if h <= w * TALL_RATIO:
        scale = min(1.0, math.sqrt(IMAGE_MAX_PIXELS / (w * h)), IMAGE_MAX_EDGE / max(w, h))
        return [_resize(img, scale)]
    img = _resize(img, min(1.0, STRIP_WIDTH / w))
    w, h = img.size
    step = STRIP_HEIGHT - STRIP_OVERLAP
    tops = range(0, max(h - STRIP_OVERLAP, 1), step)
    return [img.crop((0, top, w, min(top + STRIP_HEIGHT, h))) for top in tops]


def _flatten(img):
    """Any decoded image -> opaque RGB on white."""
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _too_many_images(n: int) -> TemplateExtractionError:
    return _fail(f"This file would need {n} images to read in full; the limit is "
                 f"{MAX_IMAGES}. Upload fewer or shorter pages.", "too_large")


def _pdf_pages(data: bytes) -> list:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    encrypted = _fail("This PDF is password-protected or encrypted. Upload an "
                      "unlocked copy.", "encrypted")
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
            raise encrypted from None
        raise _fail("This file could not be read as a PDF.", "unreadable") from None
    try:
        if pdfium_c.FPDF_GetSecurityHandlerRevision(pdf.raw) != -1:
            raise encrypted
        n = len(pdf)
        if n == 0:
            raise _fail("This PDF has no pages.", "unreadable")
        if n > MAX_PDF_PAGES:
            raise _fail(f"This PDF has {n} pages; the limit is {MAX_PDF_PAGES}. "
                        "Upload only the pages that show the layout.", "too_many_pages")
        # Every page renders at the scale that makes it STRIP_WIDTH pixels wide
        # (capped for tiny pages); the strip plan is made on that size first,
        # so an over-long PDF is refused before anything is rasterised.
        scales = [min(3.0, STRIP_WIDTH / w) for w, _h in
                  (pdf.get_page_size(i) for i in range(n))]
        planned = sum(_strip_count(w * s, h * s) for s, (w, h) in
                      zip(scales, (pdf.get_page_size(i) for i in range(n))))
        if planned > MAX_IMAGES:
            raise _too_many_images(planned)
        pages = []
        for i, scale in enumerate(scales):
            page = pdf[i]
            try:
                pages.append(page.render(scale=scale).to_pil())
            except pdfium.PdfiumError:
                raise _fail(f"Page {i + 1} of this PDF could not be rendered.",
                            "unreadable") from None
            finally:
                page.close()
        return pages
    finally:
        pdf.close()


_MAGIC = {"pdf": (b"%PDF-",), "png": (b"\x89PNG\r\n\x1a\n",), "jpeg": (b"\xff\xd8\xff",)}
_PIL_FORMAT = {"png": "PNG", "jpeg": "JPEG"}


def _image_page(data: bytes, kind: str):
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        if img.format != _PIL_FORMAT[kind]:
            raise _fail(f"This file is not a valid {kind.upper()} image.", "unreadable")
        if img.width * img.height > MAX_SOURCE_PIXELS:
            raise _fail(f"This image is {img.width}x{img.height} pixels, larger than "
                        "we read. Upload a smaller screenshot.", "too_large")
        img = ImageOps.exif_transpose(img)
        img.load()
    except TemplateExtractionError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise _fail(f"This file could not be read as a {kind.upper()} image.",
                    "unreadable") from None
    if _strip_count(*img.size) > MAX_IMAGES:
        raise _too_many_images(_strip_count(*img.size))
    return _flatten(img)


def prepare_images(data: bytes, kind: str) -> tuple[list[bytes], int]:
    """The upload as PNG images ready to send, and its page count. Raises
    :class:`TemplateExtractionError` for anything we refuse to read."""
    if kind not in _MAGIC:
        raise _fail(f"Unsupported file type {kind!r}; upload a PDF, PNG or JPEG.",
                    "unreadable")
    if not data:
        raise _fail("The uploaded file is empty.", "unreadable")
    if len(data) > MAX_UPLOAD_BYTES:
        raise _fail(f"The file is {len(data) / 1_048_576:.1f} MB; the limit is "
                    f"{MAX_UPLOAD_BYTES // 1_048_576} MB.", "too_large")
    if not data.startswith(_MAGIC[kind]):
        raise _fail(f"This file is not a valid {kind.upper()}.", "unreadable")
    pages = _pdf_pages(data) if kind == "pdf" else [_image_page(data, kind)]
    out: list[bytes] = []
    for page in pages:
        for img in _to_images(_flatten(page)):
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out.append(buf.getvalue())
    if len(out) > MAX_IMAGES:  # unreachable after the plan checks; kept as the invariant
        raise _too_many_images(len(out))
    return out, len(pages)


def _image_tokens(png: bytes) -> int:
    """Claude's image token estimate (width x height / 750) from the PNG header."""
    w = int.from_bytes(png[16:20], "big")
    h = int.from_bytes(png[20:24], "big")
    return math.ceil(w * h / 750)


# --- cost -----------------------------------------------------------------------------------

def _prices(model: str) -> tuple[float, float]:
    import os

    env_in = os.environ.get("MR_TEMPLATE_PRICE_IN_PER_M")
    env_out = os.environ.get("MR_TEMPLATE_PRICE_OUT_PER_M")
    if env_in and env_out:
        return float(env_in), float(env_out)
    if model in config.TEMPLATE_EXTRACT_PRICES:
        return config.TEMPLATE_EXTRACT_PRICES[model]
    raise _fail(f"No price is configured for model {model!r}, so the per-upload cost "
                "ceiling cannot be enforced. Set MR_TEMPLATE_PRICE_IN_PER_M and "
                "MR_TEMPLATE_PRICE_OUT_PER_M, or use a priced model.", "cost_ceiling")


#: Tokens added by the provider around a structured-output request (measured
#: at ~350 on a tiny probe), with margin.
_REQUEST_OVERHEAD_TOKENS = 1000


def _text_tokens(text: str) -> int:
    """A deliberately high estimate (2.5 chars/token) for the cost ceiling."""
    return math.ceil(len(text) / 2.5)


def worst_case_cost(model: str, images: Sequence[bytes]) -> float:
    """USD if every image token is billed and both calls (read + repair) run
    to their output caps. The ceiling is checked against this, before any call."""
    p_in, p_out = _prices(model)
    fixed = (_text_tokens(system_prompt()) + _text_tokens(json.dumps(response_schema()))
             + _REQUEST_OVERHEAD_TOKENS)
    first_in = fixed + sum(_image_tokens(i) for i in images)
    repair_in = fixed + config.TEMPLATE_EXTRACT_MAX_TOKENS
    tokens_in = first_in + repair_in
    tokens_out = config.TEMPLATE_EXTRACT_MAX_TOKENS + config.TEMPLATE_REPAIR_MAX_TOKENS
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000


# --- the model call ------------------------------------------------------------------------

def _post(body: dict, timeout: float) -> dict:
    """The one network seam (tests replace it)."""
    from app.services.openrouter import chat_completion

    return chat_completion(body, timeout=timeout)


def _body(messages: list[dict], *, model: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "report_layout", "strict": True,
                                            "schema": response_schema()}},
        # Route only to providers that honour every parameter above; without
        # this OpenRouter may silently drop response_format.
        "provider": {"require_parameters": True},
        "reasoning": {"effort": config.TEMPLATE_EXTRACT_EFFORT},
        "usage": {"include": True},
    }


def _first_messages(images: Sequence[bytes]) -> list[dict]:
    content: list[dict] = [{
        "type": "text",
        "text": (f"The sample report follows as {len(images)} image(s) in reading "
                 "order; a tall page arrives as overlapping strips, top to bottom. "
                 "Map it onto the catalog."),
    }]
    for png in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}})
    return [{"role": "system", "content": system_prompt()},
            {"role": "user", "content": content}]


def _repair_messages(previous: str, errors: Sequence[str]) -> list[dict]:
    listed = "\n".join(f"- {e}" for e in list(errors)[:12])
    return [{"role": "system", "content": system_prompt()},
            {"role": "user", "content": (
                "Your previous reply did not validate. Return the corrected JSON object. "
                "Keep every decision that was valid and change only what the errors "
                f"name.\n\nErrors:\n{listed}\n\nPrevious reply:\n{previous[:20000]}")}]


class _Spend:
    def __init__(self, model: str) -> None:
        self.model = model
        self.served = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.calls = 0

    def add(self, payload: Mapping) -> None:
        self.calls += 1
        self.served = str(payload.get("model") or self.served)
        usage = payload.get("usage") or {}
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        self.input_tokens += tin
        self.output_tokens += tout
        cost = usage.get("cost")
        if cost is None:  # provider omitted it: price the tokens ourselves
            p_in, p_out = _prices(self.model)
            cost = (tin * p_in + tout * p_out) / 1_000_000
        self.cost += float(cost)

    def usage(self) -> Usage:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost, 6), "calls": self.calls}


def _call_failure(exc: BaseException, timeout: float, spend: _Spend) -> TemplateExtractionError:
    import httpx

    from app.services.openrouter import OpenRouterHTTPError

    usage = spend.usage() if spend.calls else None
    # The provider's own words go to the operator log (never the request body,
    # which carries the user's document); the user gets the classified reason.
    logger.warning("template_extract: model call failed: %s", str(exc)[:500])
    if isinstance(exc, httpx.TimeoutException):
        return _fail(f"The AI reader did not answer within {timeout:.0f} seconds. Try "
                     "again, or upload fewer pages.", "timeout", usage=usage)
    if isinstance(exc, OpenRouterHTTPError):
        why = {402: "the shared OpenRouter account is out of credit",
               401: "the OpenRouter key was rejected",
               403: "the OpenRouter key was rejected",
               429: "the model provider is rate-limiting us"}.get(
            exc.status, "the model provider returned an error")
        return _fail(f"The AI reader failed: {why} (HTTP {exc.status}). Nothing was "
                     "saved; try again later.", "provider_error", usage=usage)
    return _fail(f"The AI reader failed: {type(exc).__name__}. Nothing was saved; try "
                 "again later.", "provider_error", usage=usage)


def _reply_text(payload: Mapping, spend: _Spend) -> str:
    """The reply's text, or a raise naming why there is none."""
    try:
        choice = payload["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        raise _fail("The AI reader returned an empty response.", "provider_error",
                    usage=spend.usage()) from None
    finish = str(choice.get("finish_reason") or "")
    native = str(choice.get("native_finish_reason") or "")
    if message.get("refusal") or "refusal" in (finish, native):
        raise _fail("The AI reader declined to read this document.", "refused",
                    usage=spend.usage())
    if finish == "length" or native == "max_tokens":
        raise _fail("The AI reader's answer was cut off before it finished. Try a "
                    "shorter sample.", "truncated", usage=spend.usage())
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


# --- normalising the reply -----------------------------------------------------------------

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december")
_PERIOD = re.compile(rf"\b(?:{_MONTHS}|q[1-4]|(?:19|20)\d\d)\b", re.I)
_PERIOD_TAIL = re.compile(r"(?:\s*[—–:|(]|\s+-\s+)[^—–:|(]*$")
_BADGE = re.compile(r"^\s*(?:section\s+)?(?:\d{1,2}|[ivx]{1,4})(?:\s*[.):—–\-]\s*|\s+)",
                    re.I)


def _clean_text(value: Any, cap: int) -> str:
    """Model text -> one line of plain text: control and format characters
    removed, whitespace collapsed, capped at ``cap`` characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
                   for c in value)
    text = " ".join(text.split())
    return text[:cap].rstrip()


def _key(text: str) -> str:
    text = text.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _note(notes: list[dict], kind: str, detail: str, **extra: Any) -> None:
    notes.append({"kind": kind, "detail": detail, **extra})


def _title(entry: vrr.SectionType, raw: str, notes: list[dict]) -> str | None:
    """A sample heading -> the title our section carries, or ``None`` for the
    registry default. Badges and period names come off; a title that only
    restates the default becomes the default, so a channel_mix heading read as
    "... September only" keeps tracking the report's real month."""
    if entry.type in ("header", "footer"):
        return None  # neither band renders a section title
    title = _BADGE.sub("", raw).strip() if raw else ""
    if not title:
        return None
    while _PERIOD.search(title):
        trimmed = _PERIOD_TAIL.sub("", title).strip()
        if trimmed == title or not trimmed:
            break
        title = trimmed
    if _key(title) == _key(entry.title):
        return None
    if _PERIOD.search(title):
        _note(notes, "title", f"The {entry.type} heading {raw!r} names a specific period; "
              "the default title is used so the template stays reusable.",
              section=entry.type)
        return None
    return title[:TITLE_MAX].rstrip()


def _option(entry: vrr.SectionType, name: str, value: Any, notes: list[dict]) -> Any:
    """One model option value -> the value stored, or ``None`` to keep the default."""
    if name in vrr.METRIC_OPTIONS:
        values = [value] if isinstance(value, str) else list(value or [])
        kept = list(dict.fromkeys(v for v in values if v in entry.metrics))
        dropped = [v for v in values if v not in entry.metrics]
        if dropped:
            _note(notes, "option", f"{entry.type} cannot show {', '.join(dropped)} "
                  f"in {name}; dropped.", section=entry.type)
        if isinstance(value, str):
            return kept[0] if kept else None
        return kept or None
    if name == "columns":
        kept = list(dict.fromkeys(v for v in value or [] if v in vrr.SCORECARD_COLUMNS))
        return kept or None
    if name in _ENUM_OPTIONS:
        return value if value in _ENUM_OPTIONS[name] else None
    if isinstance(entry.options.get(name), str):
        return _clean_text(value, TEXT_OPTION_MAX) or None
    return None


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return _key(a) == _key(b)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def _section(entry: vrr.SectionType, raw: Mapping, notes: list[dict]) -> dict:
    options: dict[str, Any] = {}
    for name, value in (raw.get("options") or {}).items():
        if value in (None, UNSET, []):
            continue
        if name not in entry.options:
            _note(notes, "option", f"{entry.type} does not take {name}; ignored.",
                  section=entry.type)
            continue
        kept = _option(entry, name, value, notes)
        if kept is None or _same(kept, entry.options[name]):
            continue
        options[name] = kept
    return {"type": entry.type,
            "title": _title(entry, _clean_text(raw.get("title"), 200), notes),
            "options": options}


def _hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", v):
        v = "".join(c * 2 for c in v)
    elif re.fullmatch(r"[0-9a-fA-F]{8}", v) and v[6:].lower() == "ff":
        v = v[:6]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", v):
        return None
    return "#" + v.upper()


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _distance(a: str, b: str) -> float:
    return math.dist(_rgb(a), _rgb(b))


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_colour)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _theme(raw: Mapping, notes: list[dict]) -> dict:
    defaults = {k: v.upper() for k, v in PALETTE.items()}
    colors: dict[str, str] = {}
    for token, value in (raw.get("colors") or {}).items():
        if token not in PALETTE or value in (None, UNSET):
            continue
        hexed = _hex(value)
        if hexed is None:
            _note(notes, "colour", f"The sample's {token} colour {str(value)[:20]!r} is not "
                  f"a hex colour; kept our default {defaults[token]}.", token=token)
        elif _distance(hexed, defaults[token]) > SAME_COLOUR_DISTANCE:
            colors[token] = hexed

    def eff(token: str) -> str:
        return token if token.startswith("#") else colors.get(token, defaults[token])

    for fg, bg in BODY_TEXT_PAIRS:
        for token in (fg, bg):
            ratio = contrast(eff(fg), eff(bg))
            if ratio >= MIN_TEXT_CONTRAST:
                break
            if token in colors:
                other = bg if token == fg else fg
                _note(notes, "contrast",
                      f"The sample's {token} {colors[token]} gives {ratio:.1f}:1 against "
                      f"{other} {eff(other)} for body text (needs {MIN_TEXT_CONTRAST}:1); "
                      f"kept our default {defaults[token]}.", token=token)
                del colors[token]

    theme: dict[str, Any] = {"colors": colors}
    heading = FONT_STACKS.get(str(raw.get("heading_font")))
    body = FONT_STACKS.get(str(raw.get("body_font")))
    if heading and heading != SERIF:
        theme["serif"] = heading
    if body and body != SANS:
        theme["sans"] = body
    return theme


class _Invalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def interpret(reply: Mapping) -> tuple[dict, list[dict], int, list[dict]]:
    """A schema-valid reply -> ``(layout, unsupported, matched_count, notes)``.

    Raises :class:`_Invalid` when the result would not pass ``layout_from_dict``,
    and :class:`TemplateExtractionError` (``nothing_matched``) when no section of
    the sample is one we can fill."""
    notes: list[dict] = []
    unsupported: list[dict] = []
    sections: list[dict] = []
    seen: set[str] = set()
    for raw in reply.get("sections") or []:
        stype = raw.get("type")
        entry = vrr.SECTION_REGISTRY.get(stype)
        if entry is None:
            unsupported.append({
                "title": _clean_text(raw.get("title"), TITLE_MAX) or "Untitled section",
                "description": _clean_text(raw.get("description"), DESCRIPTION_MAX)})
            continue
        if stype in seen:
            _note(notes, "duplicate", f"The sample has a second {stype} section; only the "
                  "first is kept.", section=stype)
            continue
        seen.add(stype)
        sections.append(_section(entry, raw, notes))
    if "highlights" in seen:
        for part in ("standouts", "watch_items"):
            if part in seen:
                sections = [s for s in sections if s["type"] != part]
                _note(notes, "duplicate", f"{part} is already inside highlights; the "
                      "separate section is dropped.", section=part)
    if not sections:
        raise TemplateExtractionError(
            f"None of the {len(unsupported)} section(s) in this sample show data we can "
            "fill, so there is no layout to build from it.",
            code="nothing_matched", unsupported=unsupported)
    matched = len(sections)
    if "data_gaps" not in {s["type"] for s in sections}:
        at = len(sections) - 1 if sections[-1]["type"] == "footer" else len(sections)
        sections.insert(at, {"type": "data_gaps", "title": None, "options": {}})
        _note(notes, "policy", "Added the Basis & data gaps panel: every report explains "
              "its missing figures, whatever the sample shows.", section="data_gaps")
    layout = {"theme": _theme(reply.get("theme") or {}, notes), "sections": sections}
    try:
        vrr.layout_from_dict(layout)
    except (ValueError, KeyError, TypeError) as exc:
        raise _Invalid([f"layout rejected: {exc}"]) from None
    return layout, unsupported, matched, notes


def _parse(text: str) -> tuple[Any, list[str]]:
    try:
        reply = json.loads(text)
    except ValueError as exc:
        return None, [f"the reply is not valid JSON ({exc.msg} at char {exc.pos})"]
    return reply, schema_errors(response_schema(), reply)


# --- the entry point ------------------------------------------------------------------------

def _summary(filename: str, result: ExtractionResult) -> str:
    return (f"Read sample report {filename!r}: {result['matched_count']} section(s) "
            f"matched, {len(result['unsupported'])} unsupported "
            f"({result['source']['images']} image(s), {result['model']}, "
            f"${result['usage']['cost_usd']:.4f})")


def extract_layout(data: bytes, kind: Kind, *, filename: str,
                   activity: Any = None) -> ExtractionResult:
    """Read one sample report and map it onto the vendor-report section catalog.

    ``kind`` is ``"pdf"``, ``"png"`` or ``"jpeg"``. ``filename`` is for the
    activity trail and the result only; it is never sent to the model.
    ``activity`` is the route's trail row (``run_tracking.Activity``); on
    success the summary is noted into it. Raises
    :class:`TemplateExtractionError` on every failure; never returns a layout
    the model did not produce.
    """
    from app.services import runtime_config

    filename = _clean_text(filename, 120) or "upload"
    started = time.monotonic()
    images, pages = prepare_images(data, kind)

    if is_offline():
        raise _fail("The AI reader is offline here (MR_OFFLINE=1), so the sample was "
                    "not read.", "offline")
    try:
        runtime_config.require("openrouter_api_key")
    except RuntimeError:
        raise _fail("The AI reader is not configured: no OpenRouter key. An admin can "
                    "set it in Settings > Secrets.", "no_key") from None

    model = config.TEMPLATE_EXTRACT_MODEL
    ceiling = config.TEMPLATE_EXTRACT_COST_CEILING_USD
    worst = worst_case_cost(model, images)
    if worst > ceiling:
        raise _fail(f"Reading this upload could cost up to ${worst:.2f}, above the "
                    f"${ceiling:.2f} per-upload limit. Upload fewer pages.", "cost_ceiling")

    timeout = config.TEMPLATE_EXTRACT_TIMEOUT_S
    spend = _Spend(model)
    calls = [(_first_messages(images), config.TEMPLATE_EXTRACT_MAX_TOKENS)]
    errors: list[str] = []
    text = ""
    while calls:
        messages, max_tokens = calls.pop()
        try:
            payload = _post(_body(messages, model=model, max_tokens=max_tokens), timeout)
        except Exception as exc:  # noqa: BLE001 - classified into a user reason
            raise _call_failure(exc, timeout, spend) from None
        spend.add(payload)
        text = _reply_text(payload, spend)
        reply, errors = _parse(text)
        if not errors:
            try:
                layout, unsupported, matched, notes = interpret(reply)
            except _Invalid as bad:
                errors = bad.errors
            except TemplateExtractionError as exc:
                exc.usage = spend.usage()
                raise
            else:
                break
        if spend.calls == 1:
            calls.append((_repair_messages(text, errors), config.TEMPLATE_REPAIR_MAX_TOKENS))
    else:
        raise _fail("The AI reader's answer did not form a valid layout, even after one "
                    f"correction ({errors[0] if errors else 'unknown error'}).",
                    "invalid_output", usage=spend.usage())

    result: ExtractionResult = {
        "layout": layout,
        "unsupported": unsupported,
        "matched_count": matched,
        "model": spend.served,
        "usage": spend.usage(),
        "notes": notes,
        "source": {"filename": filename, "kind": kind, "pages": pages,
                   "images": len(images)},
        "prompt_version": PROMPT_VERSION,
    }
    if spend.cost > ceiling:
        logger.warning("template_extract: actual cost $%.4f exceeded the $%.2f ceiling "
                       "(worst case was estimated at $%.4f)", spend.cost, ceiling, worst)
    logger.info("template_extract: kind=%s pages=%d images=%d model=%s in=%d out=%d "
                "cost=%.4f calls=%d matched=%d unsupported=%d ms=%d", kind, pages,
                len(images), spend.served, spend.input_tokens, spend.output_tokens,
                spend.cost, spend.calls, matched, len(unsupported),
                (time.monotonic() - started) * 1000)
    if activity is not None:
        activity.note(_summary(filename, result))
    return result
