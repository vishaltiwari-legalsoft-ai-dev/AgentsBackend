"""Report templates (Phase 2): the pure core. No I/O, no clock, no model.

A workspace can give its Vendor Performance report its own look. Every member
may upload or replace the template, every version is kept, and the built-in
default is always one click away (``runs.save_template_version`` and friends).
There are two kinds of template version:

* **spec**: a sample report PDF or image that the AI mapped once onto our fixed
  section catalog (``vendor_report_render.SECTION_REGISTRY``). Rendering it
  is ``render(report, layout_from_dict(spec))``.
* **html**: an HTML file with placeholders, written by a technical user. This
  module checks it (:func:`check_html`) and renders it
  (:func:`render_html_template`).

**Numbers come only from the deterministic report.** A template decides where
the figures go and how the page looks. It never supplies a figure. A missing
value renders as the report's em-dash marker, never as a zero.

**An HTML template is hostile input.** Any member can upload one, and every
member's report then renders through it, so it is treated as an attacker's file
at every step:

1. :func:`check_html` caps the size, decodes strictly, and refuses
   pathological nesting before the parser sees it. html5ever goes quadratic on
   deep nesting: 512 KB of ``<b><div>`` costs about 90 s of CPU. It then
   sanitizes with nh3 (ammonia/html5ever) against an explicit allowlist.
   It cleans the CSS (``<style>`` blocks and every ``style=``) with a CSS
   Syntax 3 tokenizer, so the escape forms ``u\\72l(`` and ``@\\69mport`` are
   seen the way a browser sees them. Finally it rebuilds the page inside a
   fixed document skeleton that this module writes.
2. :func:`render_html_template` checks the stored HTML again, so a version
   saved under an older vocabulary fails loudly. It substitutes placeholders
   in ONE pass over body text nodes only, sanitizes the finished document
   again, and puts a CSP meta first in ``<head>`` that forbids scripts and
   every network fetch.

A failed render raises :class:`TemplateRenderError` with a reason the user can
read. The route offers the built-in template; this module never swaps it in
silently.
"""

from __future__ import annotations

import difflib
import html
import json
import logging
import os
import queue
import re
import struct
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from functools import lru_cache
from typing import Any, Callable, Mapping, NamedTuple

import nh3

from . import vendor_report as vr
from . import vendor_report_render as vrr

logger = logging.getLogger("agentos.mr.report_templates")

# --- limits ---------------------------------------------------------------------

HTML_MAX_BYTES = 512 * 1024
PDF_MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_SIDE = 4096
#: The largest upload of any kind; the route can stop reading past this.
MAX_UPLOAD_BYTES = PDF_MAX_BYTES

#: Open-element depth the raw pass allows before html5ever ever sees the file.
#: Chrome's own parser flattens past 512. html5ever stays well under a second
#: at 256 on a 512 KB file, and real templates sit far below it.
MAX_DEPTH = 256
#: Formatting elements (<b>, <a>, <i>…) opened and never closed.
MAX_OPEN_FORMATTING = 64
#: A formatting element closed implicitly (``<p><b>x</p>``) is re-opened by the
#: parser before every later text run ("reconstruct the active formatting
#: elements"). Sixty such ``<b>``s and 512 KB of ``<p>x</p>`` cost html5ever
#: 26 s, so the raw pass counts those re-openings and stops at this many.
MAX_RECONSTRUCTED = 10_000
#: More errors than this are summarised in one closing line.
MAX_ERRORS = 100

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'

#: Every block the renderer inserts sits in one of these, and the block CSS is
#: scoped under it, so our stylesheet styles our blocks and nothing else.
BLOCK_SCOPE_CLASS = "mrb"


# --- placeholder vocabulary (derived from the registry, never hand-copied) -------

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

#: The em-dash marker, obtained through the renderer's public API so the two
#: can never drift apart. An absent metric renders as exactly this.
ABSENT_MARKER = vrr.render_scalar({}, next(iter(vr.METRICS)))

_FORMAT_WORDS = {vr.MONEY: "dollars", vr.PCT: "percent", vr.INT: "count", "text": "text"}
_TEXT_SCALAR_NOTES = {
    "title": "The report's title, e.g. “September 2”",
    "month_label": "The report month, e.g. “September 2026”",
    "as_of": "The date of the vendor pull the figures come from (YYYY-MM-DD)",
    "year_month": "The report month as YYYY-MM",
}
_KIND_WORDS = {"band": "band", "tiles": "tiles", "chart": "chart", "table": "table",
               "list": "list", "block": "two-column block", "note": "note"}


def _token(name: str) -> str:
    return "{{%s}}" % name


@dataclass(frozen=True)
class Placeholder:
    """One placeholder a template may use."""

    name: str                                   # "total_spend" / "chart:benchmark_movers"
    kind: str                                   # "scalar", or the registry section kind
    description: str
    example: Callable[[Mapping[str, Any]], str]  # the live value for a built report
    section_type: str | None = None             # set for blocks
    format: str | None = None                   # set for scalars
    #: The human name: a block's section title from the registry ("Vendor
    #: scorecard"), a figure's label ("Total Spend").
    title: str = ""

    @property
    def token(self) -> str:
        return _token(self.name)

    @property
    def is_block(self) -> bool:
        return self.section_type is not None


def _scalar_value(report: Mapping[str, Any], name: str) -> Any:
    return dict(vr.iter_scalar_values(report)).get(name)


def _scalar_text(report: Mapping[str, Any], name: str) -> str:
    """The plain-text value the UI shows ("Now $2,737"); absent is "—"."""
    value = _scalar_value(report, name)
    if value is None:
        return "—"
    if name in vr.METRICS:
        return vr.fmt(value, vr.METRICS[name][1])
    return str(value)


def _count(seq: Any, one: str, many: str | None = None) -> str:
    n = len(seq or ())
    return f"{n} {one if n == 1 else (many or one + 's')}"


#: Short live summaries for blocks. A type missing here still gets its registry
#: title, so a new section type never breaks the vocabulary.
_BLOCK_SUMMARIES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "header": lambda r: str(r.get("title") or "—"),
    "portfolio_glance": lambda r: _count(
        vrr.SECTION_REGISTRY["portfolio_glance"].options.get("tiles"), "tile"),
    "benchmark_movers": lambda r: _count(r.get("movers"), "benchmark") + " charted",
    "budget_vs_spend": lambda r: _count(r.get("vendors"), "vendor"),
    "demos_by_vendor": lambda r: _count(r.get("vendors"), "vendor"),
    "channel_mix": lambda r: _count((r.get("channels") or {}).get("buckets"), "channel"),
    "vendor_scorecard": lambda r: _count(r.get("vendors"), "vendor") + " + portfolio total",
    "highlights": lambda r: (_count(r.get("standouts"), "standout") + ", "
                             + _count(r.get("watch_items"), "watch item")),
    "standouts": lambda r: _count(r.get("standouts"), "standout"),
    "watch_items": lambda r: _count(r.get("watch_items"), "watch item"),
    "action_summary": lambda r: _count(r.get("actions"), "action"),
    "data_gaps": lambda r: _count(r.get("missing"), "missing figure"),
    "footer": lambda r: f"Built from the {r.get('as_of') or '—'} pull",
}


def _build_catalog() -> dict[str, Placeholder]:
    """The vocabulary, read off ``vendor_report_render`` at import: scalars from
    ``placeholder_vocabulary()`` (``vendor_report.METRICS`` plus the report's
    text fields), blocks from ``SECTION_REGISTRY``."""
    vocab = vrr.placeholder_vocabulary()
    out: dict[str, Placeholder] = {}
    for name, meta in vocab["scalars"].items():
        fmt = meta.get("format") or "text"
        if name in _TEXT_SCALAR_NOTES:
            desc = _TEXT_SCALAR_NOTES[name]
        else:
            desc = f"{meta.get('label') or name}, the portfolio figure ({_FORMAT_WORDS.get(fmt, fmt)})"
        label = str(meta.get("label") or name)
        out[name] = Placeholder(name=name, kind="scalar", description=desc,
                                example=(lambda r, _n=name: _scalar_text(r, _n)), format=fmt,
                                title=label[:1].upper() + label[1:])
    for token, meta in vocab["blocks"].items():
        name = token[2:-2]
        typ = meta["type"]
        title = meta.get("title") or typ
        summary = _BLOCK_SUMMARIES.get(typ) or (lambda r, _t=title: _t)
        out[name] = Placeholder(
            name=name, kind=meta["kind"],
            description=f"{title}: a {_KIND_WORDS.get(meta['kind'], meta['kind'])} drawn from this report",
            example=summary, section_type=typ, title=title)
    return out


PLACEHOLDERS: dict[str, Placeholder] = _build_catalog()


def vocabulary(report: Mapping[str, Any] | None = None) -> list[dict]:
    """The placeholder list for the UI, as JSON-safe dicts. With a built report
    each entry carries its live ``example`` ("$2,737"); without one, None."""
    return [{"placeholder": p.token, "name": p.name, "kind": p.kind, "title": p.title,
             "description": p.description,
             "example": p.example(report) if report is not None else None}
            for p in PLACEHOLDERS.values()]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@lru_cache(maxsize=512)
def suggest(name: str) -> str | None:
    """The closest known placeholder token to an unknown ``name``, or None."""
    _kind, sep, typ = name.partition(":")
    key = (typ if sep else name).strip().lower()
    if key in vrr.SECTION_REGISTRY:                      # right section, wrong/no kind
        return vrr.SECTION_REGISTRY[key].placeholder
    if sep:
        # A block: match the part after the colon against each section's type
        # AND its title, because "chart:booked_vs_completed" means "Demos booked
        # vs. completed" (type "demos_by_vendor"), not "budget_vs_spend".
        aliases: dict[str, str] = {}
        for entry in vrr.SECTION_REGISTRY.values():
            aliases[entry.type] = entry.placeholder
            aliases[_slug(entry.title)] = entry.placeholder
        hit = difflib.get_close_matches(key, list(aliases), n=1, cutoff=0.55)
        if hit:
            return aliases[hit[0]]
    hit = difflib.get_close_matches(name.strip().lower(), list(PLACEHOLDERS), n=1, cutoff=0.6)
    return PLACEHOLDERS[hit[0]].token if hit else None


# --- results ---------------------------------------------------------------------

#: The ``removed`` counters, always all present.
REMOVED_KEYS = ("scripts", "handlers", "external_urls", "unsafe_urls", "elements",
                "attributes", "css_rules", "comments")


@dataclass(frozen=True)
class TemplateError:
    line: int | None
    placeholder: str | None
    message: str
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {"line": self.line, "placeholder": self.placeholder,
                "message": self.message, "suggestion": self.suggestion}


@dataclass(frozen=True)
class CheckResult:
    """What :func:`check_html` found. ``sanitized_html`` is what a save stores;
    a result with any error cannot be saved."""

    sanitized_html: str
    errors: tuple[TemplateError, ...]
    removed: Mapping[str, int]
    placeholders_used: tuple[str, ...]

    @property
    def can_save(self) -> bool:
        return not self.errors and bool(self.sanitized_html)

    def to_dict(self) -> dict:
        return {"sanitized_html": self.sanitized_html,
                "errors": [e.to_dict() for e in self.errors],
                "removed": dict(self.removed),
                "placeholders_used": list(self.placeholders_used),
                "can_save": self.can_save}


class TemplateRenderError(Exception):
    """A template version could not be rendered. ``reason`` is safe to show the
    user; the route answers 409 and offers the built-in template."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Unverifiable(Exception):
    """The sanitizer's output broke an invariant this module relies on. Never
    expected; when it happens the template is refused rather than trusted."""


# --- the allowlist -----------------------------------------------------------------

_HTML_TAGS = frozenset({
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br",
    "caption", "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div",
    "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hgroup", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol",
    "p", "pre", "q", "rp", "rt", "ruby", "s", "samp", "section", "small", "span", "strong",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr",
    "u", "ul", "var", "wbr",
    # Kept by the sanitizer only so this module can lift them into <head>:
    "style", "title",
})
#: Static SVG only: shapes, text, gradients, clip paths and same-document <use>.
#: No <image>, <foreignObject>, <animate>/<set>, <filter> or <script>.
_SVG_TAGS = frozenset({
    "svg", "g", "defs", "desc", "title", "symbol", "use", "path", "rect", "circle",
    "ellipse", "line", "polyline", "polygon", "text", "tspan", "linearGradient",
    "radialGradient", "stop", "clipPath",
})
#: Removed together with everything inside them.
_CLEAN_CONTENT_TAGS = frozenset({
    "script", "noscript", "template", "iframe", "object", "embed", "applet", "frame",
    "frameset", "noembed", "noframes", "xmp", "plaintext", "textarea", "select", "math",
    "foreignObject", "canvas", "video", "audio", "portal", "fencedframe",
})
_GLOBAL_ATTRS = frozenset({"class", "id", "title", "lang", "dir", "style", "role"})
_HTML_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "headers", "align", "valign"}),
    "th": frozenset({"colspan", "rowspan", "headers", "scope", "abbr", "align", "valign"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span", "width"}),
    "table": frozenset({"width"}),
    "ol": frozenset({"start", "reversed", "type"}),
    "li": frozenset({"value"}),
    "time": frozenset({"datetime"}),
    "del": frozenset({"datetime"}),
    "ins": frozenset({"datetime"}),
    "details": frozenset({"open"}),
}
_SVG_ATTRS = frozenset({
    "viewBox", "preserveAspectRatio", "width", "height", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "fx", "fy", "d", "points", "dx", "dy", "transform",
    "offset", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
    "stroke-linejoin", "opacity", "stop-color", "stop-opacity", "font-size",
    "font-weight", "font-style", "font-family", "text-anchor", "dominant-baseline",
    "clip-path", "clip-rule", "gradientUnits", "gradientTransform", "clipPathUnits",
    "focusable", "visibility",
})
#: The only elements whose ``href`` survives, and only as ``#fragment``.
_HREF_TAGS = frozenset({"a", "use"})


def _nh3_attributes() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"*": set(_GLOBAL_ATTRS)}
    for tag, attrs in _HTML_ATTRS.items():
        out[tag] = set(attrs)
    for tag in _SVG_TAGS:
        out.setdefault(tag, set()).update(_SVG_ATTRS)
    out["use"].add("href")
    return out


_NH3_OPTIONS: dict[str, Any] = {
    "tags": set(_HTML_TAGS | _SVG_TAGS),
    "clean_content_tags": set(_CLEAN_CONTENT_TAGS),
    "attributes": _nh3_attributes(),
    "generic_attribute_prefixes": {"aria-"},
    "strip_comments": True,
    "link_rel": None,
    # ammonia's own scheme gate runs before our filter: only data: (and
    # relative URLs, which the filter narrows to #fragments) get that far.
    "url_schemes": {"data"},
}

#: A same-document reference: "#top", "url(#grad)".
_FRAGMENT_RE = re.compile(r"#[A-Za-z_][A-Za-z0-9_\-:.]*")
#: The one kind of image source kept. The body excludes quotes, angle brackets,
#: backslashes, backticks and control characters (bar whitespace).
_DATA_IMAGE_RE = re.compile(
    r"data:image/(?:png|jpe?g|gif|webp|svg\+xml)(?:;[A-Za-z0-9=._+-]+)*,"
    r"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f\"'<>\\`]*", re.I)

#: Elements a block may be the sole content of. A block is a <section> (or a
#: <header>/<footer>), and inside <p>, <span> or a heading the parser would tear
#: the paragraph open around it.
BLOCK_PARENTS = frozenset({
    "div", "section", "article", "main", "aside", "header", "footer", "figure", "td",
    "th", "li", "dd", "blockquote", "nav", "details",
})


# --- CSS: a CSS Syntax 3 tokenizer, a statement parser, a cleaner ---------------------
# The cleaner never decodes and re-encodes. It drops whole statements (a
# declaration, or a rule/at-rule with its block) and re-emits the kept tokens'
# own source text verbatim, so its output is always a subsequence of its input.
# A statement is dropped when any token in it is unsafe, judged on the decoded
# value the way a browser decodes it (escapes included).

_ESC = r"\\(?:[0-9A-Fa-f]{1,6}[ \t\n]?|[^\n0-9A-Fa-f]|\Z)"
_NMSTART = r"(?:[A-Za-z_\u0080-\U0010FFFF]|" + _ESC + ")"
_NMCHAR = r"(?:[A-Za-z0-9_\-\u0080-\U0010FFFF]|" + _ESC + ")"
_IDENT = r"(?:--" + _NMCHAR + "*|-?" + _NMSTART + _NMCHAR + "*)"
_CSS_TOKEN = re.compile(
    r"(?P<comment>/\*[\s\S]*?(?:\*/|\Z))"
    r"|(?P<ws>[ \t\n]+)"
    r"|(?P<string>\"(?:[^\"\\\n]|\\[\s\S]|\\\Z)*(?:\"|(?=\n)|\Z)"
    r"|'(?:[^'\\\n]|\\[\s\S]|\\\Z)*(?:'|(?=\n)|\Z))"
    r"|(?P<number>[+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?(?:%|" + _IDENT + r")?)"
    r"|(?P<cdc>-->)"
    r"|(?P<cdo><!--)"
    r"|(?P<at>@" + _IDENT + r")"
    r"|(?P<hash>\#" + _NMCHAR + r"+)"
    r"|(?P<ident>" + _IDENT + r")(?P<call>\()?"
    r"|(?P<char>[\s\S])"
)
_URL_BODY = re.compile(
    r"[ \t\n]*((?:[^\"'()\\ \t\n\x00-\x08\x0b\x0e-\x1f\x7f]|" + _ESC + r")*)[ \t\n]*(?:\)|\Z)")
_BAD_URL_REST = re.compile(r"(?:[^)\\]|\\[\s\S])*(?:\)|\Z)")
_ESC_DECODE = re.compile(r"\\(?:([0-9A-Fa-f]{1,6})[ \t\n]?|(\n)|([\s\S])|\Z)")

_ALLOWED_AT_RULES = frozenset({
    "media", "supports", "font-face", "page", "keyframes", "-webkit-keyframes", "layer",
    "container", "counter-style", "property", "font-feature-values",
    "font-palette-values", "scope", "starting-style",
    # @page margin boxes
    "top-left-corner", "top-left", "top-center", "top-right", "top-right-corner",
    "bottom-left-corner", "bottom-left", "bottom-center", "bottom-right",
    "bottom-right-corner", "left-top", "left-middle", "left-bottom", "right-top",
    "right-middle", "right-bottom",
})
#: Functions whose string arguments are URLs.
_URL_FUNCTIONS = frozenset({"url", "src", "image", "image-set", "-webkit-image-set",
                            "cross-fade", "-webkit-cross-fade"})
_BAD_FUNCTIONS = frozenset({"expression", "element", "-moz-element"})
_BAD_PROPERTIES = frozenset({"behavior", "-ms-behavior", "-moz-binding", "-o-link",
                             "-o-link-source"})
#: Matched against a statement's decoded, comment-free, whitespace-free text.
_BAD_TEXT = ("expression(", "javascript:", "vbscript:", "-moz-binding")
_CSS_MAX_NESTING = 24


class _Tok(NamedTuple):
    kind: str
    start: int
    end: int
    value: str = ""


def _css_unescape(text: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            cp = int(m.group(1), 16)
            ok = 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF
            return chr(cp) if ok else "�"
        if m.group(2):
            return ""                      # escaped newline inside a string
        if m.group(3) is not None:
            return m.group(3)
        return "�"                    # backslash at end of input
    return _ESC_DECODE.sub(repl, text)


def _css_preprocess(text: str) -> str:
    return (text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
            .replace("\x00", "�"))


def _css_tokens(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    pos, n = 0, len(src)
    while pos < n:
        m = _CSS_TOKEN.match(src, pos)
        if m.group("ident") is not None:
            name = _css_unescape(m.group("ident"))
            if not m.group("call"):
                out.append(_Tok("ident", pos, m.end(), name))
                pos = m.end()
                continue
            if name.lower() == "url":
                k = m.end()
                while k < n and src[k] in " \t\n":
                    k += 1
                if k < n and src[k] in "\"'":
                    out.append(_Tok("function", pos, m.end(), name))
                    pos = m.end()
                    continue
                um = _URL_BODY.match(src, m.end())
                if um:
                    out.append(_Tok("url", pos, um.end(), _css_unescape(um.group(1))))
                    pos = um.end()
                else:
                    bm = _BAD_URL_REST.match(src, m.end())
                    out.append(_Tok("badurl", pos, bm.end()))
                    pos = bm.end()
                continue
            out.append(_Tok("function", pos, m.end(), name))
            pos = m.end()
            continue
        kind = m.lastgroup
        text = m.group(0)
        if kind == "string":
            if _string_closed(text):
                out.append(_Tok("string", pos, m.end(), _css_unescape(text[1:-1])))
            elif m.end() >= n:
                out.append(_Tok("string", pos, m.end(), _css_unescape(text[1:])))
            else:
                out.append(_Tok("badstring", pos, m.end()))
        elif kind == "at":
            out.append(_Tok("at", pos, m.end(), _css_unescape(text[1:])))
        elif kind == "char":
            out.append(_Tok(text if text in "()[]{};:," else "delim", pos, m.end(), text))
        else:
            out.append(_Tok(kind, pos, m.end(), text))
        pos = m.end()
    return out


def _string_closed(text: str) -> bool:
    """Whether a quoted token's last quote closes it (not an escaped quote)."""
    if len(text) < 2 or text[-1] != text[0]:
        return False
    backslashes = len(text[1:-1]) - len(text[1:-1].rstrip("\\"))
    return backslashes % 2 == 0


@dataclass
class _Stmt:
    """A declaration, a statement at-rule, or a rule with its block."""

    head: list[_Tok]
    block: list["_Stmt"] | None = None
    end: str = ""          # ";" when the statement ended with one
    too_deep: bool = False


def _css_parse(toks: list[_Tok], i: int = 0, depth: int = 0) -> tuple[list[_Stmt], int]:
    """Statements from ``toks[i:]`` up to the ``}`` that closes the enclosing
    block (``depth > 0``) or the end. Brackets nest as CSS Syntax 3 nests them:
    ``;``, ``{`` and ``}`` end a statement only outside every (), [] and {}."""
    out: list[_Stmt] = []
    n = len(toks)
    while i < n:
        if toks[i].kind == "}":
            if depth:
                return out, i
            i += 1                                     # a stray top-level "}"
            continue
        start, stack = i, []
        while i < n:
            k = toks[i].kind
            if k in ("function", "("):
                stack.append(")")
            elif k == "[":
                stack.append("]")
            elif k == "{" and stack:
                stack.append("}")
            elif stack and k == stack[-1]:
                stack.pop()
            elif not stack and k in (";", "{", "}"):
                break
            i += 1
        head = toks[start:i]
        if i < n and toks[i].kind == "{":
            if depth >= _CSS_MAX_NESTING:
                j, level = i + 1, 1
                while j < n and level:
                    level += {"{": 1, "}": -1}.get(toks[j].kind, 0)
                    j += 1
                out.append(_Stmt(head, [], too_deep=True))
                i = j
                continue
            block, j = _css_parse(toks, i + 1, depth + 1)
            out.append(_Stmt(head, block))
            i = j + 1 if j < n else n
        elif i < n and toks[i].kind == ";":
            out.append(_Stmt(head, None, ";"))
            i += 1
        elif head:
            out.append(_Stmt(head))
    return out, i


def _url_target_ok(value: str, *, allow_data: bool) -> bool:
    squeezed = re.sub(r"[\x00-\x20\x7f]", "", value)
    if allow_data and squeezed.lower().startswith("data:"):
        return True
    return bool(_FRAGMENT_RE.fullmatch(value.strip(" \t\n")))


def _significant(toks: list[_Tok]) -> list[_Tok]:
    return [t for t in toks if t.kind not in ("ws", "comment")]


#: Functions allowed INSIDE a URL-taking function: the URL functions themselves
#: (checked the same way) and ``type()``, image-set's MIME hint. Anything else —
#: ``var()``, ``attr()``, ``env()`` — would let the URL come from somewhere the
#: cleaner cannot see: ``image-set(var(--u) 1x)`` with ``--u:"https://…"``.
_URL_ARG_FUNCTIONS = _URL_FUNCTIONS | {"type"}
#: A string that names something fetchable: a network/script scheme, a
#: protocol-relative ``//…``, or a resource path. Checked in custom properties,
#: whose values can reach a URL function later through ``var()``.
_URL_LIKE_STRING = re.compile(
    r"//|^(?:https?|ftps?|file|javascript|vbscript|blob|wss?|about|filesystem):"
    r"|\.(?:png|jpe?g|gif|webp|avif|svg|bmp|ico|css|woff2?|ttf|otf|eot|htc|xml|xsl)"
    r"(?:[?#].*)?$", re.I)


def _custom_property(toks: list[_Tok]) -> bool:
    sig = _significant(toks)
    return (len(sig) >= 2 and sig[0].kind == "ident" and sig[0].value.startswith("--")
            and sig[1].kind == ":")


def _unsafe(toks: list[_Tok], src: str, *, allow_data: bool = True) -> tuple[bool, int]:
    """Whether a statement must go, and how many external URLs it named."""
    bad, external, imports = False, 0, False
    stack: list[str | None] = []
    custom = _custom_property(toks)
    for t in toks:
        k = t.kind
        if k == "at":
            name = t.value.lower()
            if name not in _ALLOWED_AT_RULES:
                bad = True
                imports = imports or name == "import"
        elif k == "url":
            if not _url_target_ok(t.value, allow_data=allow_data):
                bad = True
                external += 1
        elif k in ("badurl", "badstring"):
            bad = True
        elif k == "function":
            name = t.value.lower()
            if name in _BAD_FUNCTIONS:
                bad = True
            if name not in _URL_ARG_FUNCTIONS and any(f in _URL_FUNCTIONS for f in stack if f):
                bad = True                 # var()/attr()/env() feeding a URL function
            stack.append(name)
        elif k in ("(", "[", "{"):
            stack.append(None)
        elif k in (")", "]", "}"):
            if stack:
                stack.pop()
        elif k == "string":
            innermost = next((f for f in reversed(stack) if f), None)
            in_url_function = (innermost != "type"     # type("image/png") is a MIME type
                               and any(f in _URL_FUNCTIONS for f in stack if f))
            url_like = custom and bool(_URL_LIKE_STRING.search(
                re.sub(r"[\x00-\x20\x7f]", "", t.value)))
            if (in_url_function or url_like) and \
                    not _url_target_ok(t.value, allow_data=allow_data):
                bad = True
                external += 1
        elif k == "ident" and t.value.lower() in _BAD_PROPERTIES:
            bad = True
    if imports:
        external = max(external, 1)      # @import "x" names a URL without url()
    if not bad and toks:
        raw = src[toks[0].start:toks[-1].end]
        if "\\" in raw or "/*" in raw:
            raw = "".join(_css_unescape(src[t.start:t.end]) for t in toks if t.kind != "comment")
        flat = "".join(raw.split()).lower()
        bad = any(needle in flat for needle in _BAD_TEXT)
    return bad, external


def _css_filter(stmts: list[_Stmt], src: str, counts: Counter, *, inline: bool,
                allow_data: bool = True) -> list[_Stmt]:
    kept: list[_Stmt] = []
    for s in stmts:
        if not _significant(s.head) and s.block is None:
            kept.append(s)                             # whitespace / comments only
            continue
        bad, external = _unsafe(s.head, src, allow_data=allow_data)
        if s.too_deep or bad or (inline and s.block is not None):
            counts["css_rules"] += 1
            counts["external_urls"] += external
            continue
        if s.block is not None:
            s = _Stmt(s.head, _css_filter(s.block, src, counts, inline=inline,
                                          allow_data=allow_data), s.end)
        kept.append(s)
    return kept


def _css_text(stmts: list[_Stmt], src: str) -> str:
    parts: list[str] = []
    for s in stmts:
        parts.append("".join(src[t.start:t.end] for t in s.head))
        if s.block is not None:
            parts.append("{" + _css_text(s.block, src) + "}")
        else:
            parts.append(s.end)
    return "".join(parts)


def _guard_style_text(css: str) -> str:
    """CSS inside <style> must never close the element. html5ever's raw text
    cannot contain ``</style``; this guard holds for any other input too."""
    return re.sub(r"(?i)</(?=style)", r"<\\/", css)


def clean_css(css: str, counts: Counter | None = None) -> str:
    """A stylesheet with every unsafe statement removed (see module notes)."""
    counts = Counter() if counts is None else counts
    src = _css_preprocess(css)
    stmts, _ = _css_parse(_css_tokens(src))
    return _guard_style_text(_css_text(_css_filter(stmts, src, counts, inline=False), src))


def clean_inline_css(css: str, counts: Counter | None = None) -> str:
    """A ``style=`` declaration list with every unsafe declaration removed."""
    counts = Counter() if counts is None else counts
    src = _css_preprocess(css)
    stmts, _ = _css_parse(_css_tokens(src))
    return _css_text(_css_filter(stmts, src, counts, inline=True), src).strip()


def _svg_value_ok(value: str) -> bool:
    """An SVG presentation attribute is parsed as CSS: same rules, but a url()
    may only point inside the document (``fill="url(#grad)"``)."""
    src = _css_preprocess(value)
    bad, _ = _unsafe(_css_tokens(src), src, allow_data=False)
    return not bad


# --- the sanitizer ------------------------------------------------------------------

def _make_attribute_filter(counts: Counter) -> Callable[[str, str, str], str | None]:
    def keep(tag: str, attr: str, value: str) -> str | None:
        if "<" in value or ">" in value:
            # Never legitimate here, and keeping them out of attribute values
            # is what lets this module walk the sanitized markup exactly.
            counts["attributes"] += 1
            return None
        if "{{" in value or "}}" in value:
            return None                    # check_html reports it as an error
        if attr == "style":
            cleaned = clean_inline_css(value, counts)
            return cleaned or None
        if attr == "href":
            target = value.strip()
            return target if tag in _HREF_TAGS and _FRAGMENT_RE.fullmatch(target) else None
        if attr == "src":
            target = value.strip()
            return target if tag == "img" and _DATA_IMAGE_RE.fullmatch(target) else None
        if tag in _SVG_TAGS and not _svg_value_ok(value):
            counts["external_urls" if "url" in value.lower() else "attributes"] += 1
            return None
        return value
    return keep


_CANON_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)>")
_HTML_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                        "meta", "source", "track", "wbr"})


@dataclass
class _Frame:
    tag: str
    children: int = 0


@dataclass
class _Occurrence:
    name: str
    part: int                 # index into _Doc.parts of the text node
    parent: _Frame | None
    in_svg: bool
    in_title: bool
    alone: bool               # the text node is exactly this placeholder (+ whitespace)


@dataclass
class _Doc:
    """A sanitized template, taken apart: the document title (plain text), the
    cleaned stylesheets, and the body as exact pieces of html5ever's output."""

    title: str | None
    styles: list[str]
    parts: list[str]
    occurrences: list[_Occurrence] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "".join(self.parts)


def _sanitize(text: str, counts: Counter) -> _Doc:
    """Sanitize ``text`` with nh3, then walk the result. The output from
    html5ever is canonical: text is escaped, comments are gone, the only raw
    text element is <style>, and the filter keeps ``<`` and ``>`` out of
    attributes. So one regex walk sees exactly the tree a browser builds. Any
    surprise raises :class:`_Unverifiable`."""
    frag = nh3.clean(text, attribute_filter=_make_attribute_filter(counts), **_NH3_OPTIONS)
    title: str | None = None
    styles: list[str] = []
    parts: list[str] = []
    occurrences: list[_Occurrence] = []
    stack: list[_Frame] = []
    svg_depth = title_depth = 0
    pos, n = 0, len(frag)
    while pos < n:
        if frag[pos] == "<":
            m = _CANON_TAG.match(frag, pos)
            if not m:
                raise _Unverifiable(f"unexpected markup at offset {pos}")
            closing, name = bool(m.group(1)), m.group(2)
            pos = m.end()
            if not closing and name in ("style", "title") and not svg_depth:
                end = frag.find(f"</{name}>", pos)
                if end < 0:
                    raise _Unverifiable(f"unterminated <{name}>")
                inner = frag[pos:end]
                pos = end + len(name) + 3
                if name == "style":
                    cleaned = clean_css(inner, counts)
                    if cleaned.strip():
                        styles.append(cleaned)
                else:
                    if "<" in inner:
                        raise _Unverifiable("markup inside <title>")
                    if title is None:
                        title = html.unescape(inner).strip() or None
                continue
            if not closing and name == "style":            # <style> inside <svg>
                end = frag.find("</style>", pos)
                if end < 0:
                    raise _Unverifiable("unterminated <style>")
                pos = end + len("</style>")
                counts["elements"] += 1
                continue
            if closing:
                if not stack or stack[-1].tag != name:
                    raise _Unverifiable(f"unbalanced </{name}>")
                frame = stack.pop()
                svg_depth -= frame.tag == "svg"
                title_depth -= frame.tag == "title"
                parts.append(m.group(0))
                continue
            if stack:
                stack[-1].children += 1
            parts.append(m.group(0))
            if name in _HTML_VOID and not svg_depth:
                continue
            stack.append(_Frame(name))
            svg_depth += name == "svg"
            title_depth += name == "title"
            continue
        end = frag.find("<", pos)
        end = n if end < 0 else end
        chunk = frag[pos:end]
        pos = end
        if stack and chunk.strip():
            stack[-1].children += 1
        for pm in _PLACEHOLDER_RE.finditer(chunk):
            occurrences.append(_Occurrence(
                name=pm.group(1), part=len(parts), parent=stack[-1] if stack else None,
                in_svg=bool(svg_depth), in_title=bool(title_depth),
                alone=chunk.strip() == pm.group(0)))
        parts.append(chunk)
    if stack:
        raise _Unverifiable("unclosed elements in sanitized output")
    return _Doc(title=title, styles=styles, parts=parts, occurrences=occurrences)


def _document(title: str | None, styles: list[str], body: str, *, csp: bool) -> str:
    """The fixed skeleton. Only its contents come from the template."""
    head = [CSP_META] if csp else []
    head.append('<meta charset="UTF-8">')
    if csp:
        head.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    if title:
        head.append(f"<title>{html.escape(title, quote=False)}</title>")
    head.extend(f"<style>{_guard_style_text(css)}</style>" for css in styles)
    return ('<!DOCTYPE html><html lang="en"><head>' + "".join(head) + "</head><body>"
            + body + "</body></html>")


# --- the work meter: html5ever's worst cases, bounded before it runs -------------
#
# html5ever (inside nh3) has inputs whose cost is superlinear: deep nesting (a
# block start tag walks the open-element stack), formatting elements re-opened
# before every text run, content foster-parented out of a table, and many
# attributes on one tag (a duplicate check per attribute). The meter measures
# each on the RAW text BEFORE nh3 sees it — and it measures with NO notion of
# raw text: every ``<name`` anywhere counts as a tag, inside <style>, <title>,
# <noscript>, <iframe>, <textarea>, comments, everything.
#
# That is the point. Context is exactly where two parsers disagree. The stdlib
# HTMLParser reads ``<svg><style>…`` as text; html5ever, after foreign-content
# breakout, builds every tag in it. A meter that skipped "text" let 512 KB of
# ``<b><div>`` inside ``<svg><style>`` through to 70+ s of html5ever CPU with
# ``can_save=True``. Counting too much costs a refusal of markup no real
# template contains; counting too little costs a CPU core.

#: Tag-like tokens (``<name`` / ``</name``) in the whole file. A designed
#: template is a few hundred; the starter is ~150.
MAX_MARKUP_TOKENS = 30_000
#: Attributes on one tag (html5ever checks each against the ones before it).
MAX_ATTRIBUTES_PER_TAG = 256
#: Content written directly inside a <table> (outside its cells), which the
#: parser moves out in front of the table one node at a time.
MAX_FOSTERED = 2_000

_TAG_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:_.\-]*)")
_ATTR_TOKEN = re.compile(r"""[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")
_NON_SPACE = re.compile(r"\S")

_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small",
                         "strike", "strong", "tt", "u"})
#: Elements a browser closes implicitly when a sibling of the same family opens.
_AUTO_CLOSE = {"p": {"p"}, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"},
               "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"}, "option": {"option"},
               "optgroup": {"optgroup"}, "thead": {"thead", "tbody", "tfoot"},
               "tbody": {"thead", "tbody", "tfoot"}, "tfoot": {"thead", "tbody", "tfoot"},
               "rb": {"rb", "rt", "rp"}, "rt": {"rb", "rt", "rp"}, "rp": {"rb", "rt", "rp"}}
_RAW_VOID = _HTML_VOID | {"param", "keygen", "frame", "basefont", "bgsound"}
#: Start tags that the tree builder inserts WITHOUT first re-opening the active
#: formatting elements (HTML "in body": block-level, list, table and head
#: elements). Every other start tag, and every text run, reconstructs first.
_NO_RECONSTRUCT = frozenset({
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir",
    "div", "dl", "fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main",
    "menu", "nav", "ol", "p", "search", "section", "summary", "ul", "h1", "h2", "h3", "h4",
    "h5", "h6", "pre", "listing", "form", "li", "dd", "dt", "plaintext", "table", "hr",
    "html", "head", "body", "title", "style", "script", "meta", "link", "base", "template",
    "noscript", "textarea", "xmp", "iframe", "noembed", "caption", "colgroup", "col",
    "tbody", "thead", "tfoot", "tr", "td", "th", "frameset", "frame",
})
#: Where foreign (SVG / MathML) content starts.
_FOREIGN_ROOTS = frozenset({"svg", "math"})
#: Start tags that end foreign content: the parser pops back out of the <svg>
#: and handles the tag as HTML (HTML spec, "in foreign content").
_BREAKOUT = frozenset({
    "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt", "em",
    "embed", "h1", "h2", "h3", "h4", "h5", "h6", "head", "hr", "i", "img", "li", "listing",
    "menu", "meta", "nobr", "ol", "p", "pre", "ruby", "s", "small", "span", "strong",
    "strike", "sub", "sup", "table", "tt", "u", "ul", "var", "font",
})
#: Elements whose content the HTML tokenizer reads as text — unless they sit in
#: foreign content, where they are ordinary elements and their content is markup.
_RAWTEXT_LOWER = frozenset({"style", "script", "title", "textarea", "xmp", "iframe",
                            "noembed", "noframes", "noscript", "plaintext"})
_TABLE_CONTEXT = frozenset({"table", "tbody", "thead", "tfoot", "tr"})
_CELL_CONTEXT = frozenset({"td", "th", "caption"})
#: What a <table> may hold directly without the parser moving it out.
_TABLE_CONTENT = frozenset({"caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr",
                            "td", "th", "table", "style", "script", "template", "input",
                            "form"})


class _TooComplex(Exception):
    pass


class _Fmt:
    """A formatting element the parser still has on its active list."""

    __slots__ = ("tag", "in_stack")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.in_stack = True


class _Meter:
    """An approximation of html5ever's tree builder that only keeps the numbers
    its worst cases depend on, erring high."""

    def __init__(self) -> None:
        self.stack: list[tuple[str, _Fmt | None]] = []
        self.formatting: list[_Fmt] = []
        self.orphans = 0          # formatting elements closed implicitly, not by their tag
        self.reconstructed = 0
        self.fostered = 0
        self.foreign = 0          # open <svg>/<math>
        self.tables = 0           # open table-context elements

    def _count(self, tag: str, sign: int) -> None:
        if tag in _FOREIGN_ROOTS:
            self.foreign += sign
        if tag in _TABLE_CONTEXT:
            self.tables += sign

    def _push(self, tag: str, fmt: _Fmt | None = None) -> None:
        self.stack.append((tag, fmt))
        self._count(tag, 1)
        if len(self.stack) > MAX_DEPTH:
            raise _TooComplex(f"Elements are nested more than {MAX_DEPTH} deep. "
                              "Flatten the markup and upload it again.")

    def _pop_to(self, k: int) -> None:
        for tag, fmt in self.stack[k:]:
            self._count(tag, -1)
            if fmt is not None and fmt.in_stack:
                fmt.in_stack = False
                self.orphans += 1
        del self.stack[k:]

    def _find(self, tags) -> int:
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] in tags:
                return k
        return -1

    def _in_table(self) -> bool:
        if not self.tables:
            return False
        for tag, _fmt in reversed(self.stack):
            if tag in _CELL_CONTEXT:
                return False
            if tag in _TABLE_CONTEXT:
                return True
        return False

    def _foster(self) -> None:
        self.fostered += 1
        if self.fostered > MAX_FOSTERED:
            raise _TooComplex("Too much content is written directly inside a <table>, outside "
                              "its cells. Put it inside <td> cells and upload again.")

    def _reconstruct(self) -> None:
        if not self.orphans:
            return
        for fmt in self.formatting:
            if not fmt.in_stack:
                fmt.in_stack = True
                self._push(fmt.tag, fmt)
                self.reconstructed += 1
        self.orphans = 0
        if self.reconstructed > MAX_RECONSTRUCTED:
            raise _TooComplex("Formatting tags (<b>, <i>, <a>…) are left open across too "
                              "many paragraphs. Close each one where it ends and upload again.")

    def text(self, non_space: bool) -> None:
        if non_space and self._in_table():
            self._foster()
        self._reconstruct()

    def start(self, tag: str, self_closing: bool) -> None:
        if self.foreign:
            if tag not in _BREAKOUT:
                if not self_closing:
                    self._push(tag)
                return
            self._pop_to(max(i for i, (t, _f) in enumerate(self.stack) if t in _FOREIGN_ROOTS))
        if self._in_table() and tag not in _TABLE_CONTENT:
            self._foster()
        family = _AUTO_CLOSE.get(tag)
        if family:
            k = self._find(family)
            if k >= 0:
                self._pop_to(k)
        if tag not in _NO_RECONSTRUCT:
            self._reconstruct()
        if tag in _RAW_VOID or (tag in _FOREIGN_ROOTS and self_closing):
            return
        fmt = None
        if tag in _FORMATTING:
            fmt = _Fmt(tag)
            self.formatting.append(fmt)
            if len(self.formatting) > MAX_OPEN_FORMATTING:
                raise _TooComplex(
                    f"More than {MAX_OPEN_FORMATTING} formatting tags (<b>, <i>, <a>…) are "
                    "opened and never closed. Close them and upload again.")
        self._push(tag, fmt)               # an HTML element's "/>" is ignored: it opens

    def end(self, tag: str) -> None:
        if tag in _FORMATTING:
            for i in range(len(self.formatting) - 1, -1, -1):
                fmt = self.formatting[i]
                if fmt.tag != tag:
                    continue
                del self.formatting[i]
                if fmt.in_stack:
                    k = next(j for j in range(len(self.stack) - 1, -1, -1)
                             if self.stack[j][1] is fmt)
                    self._pop_to(k)
                self.orphans -= 1          # its own end tag: not an orphan
                return
        k = self._find((tag,))
        if k >= 0:
            self._pop_to(k)


def _measure(text: str) -> None:
    """Raise :class:`_TooComplex` when ``text`` would cost html5ever more than a
    template ever needs. One pass, linear: every ``>`` search starts after the
    previous tag, and tokens inside a tag's own attributes are skipped."""
    meter = _Meter()
    tokens = 0
    resume = 0
    for m in _TAG_TOKEN.finditer(text):
        start = m.start()
        if start < resume:
            continue                                   # inside the previous tag
        tokens += 1
        if tokens > MAX_MARKUP_TOKENS:
            raise _TooComplex(f"This file has more than {MAX_MARKUP_TOKENS:,} tags. A report "
                              "template needs far fewer; simplify it and upload again.")
        if start > resume:
            meter.text(bool(_NON_SPACE.search(text, resume, start)))
        gt = text.find(">", m.end())
        end = len(text) if gt < 0 else gt
        attrs = 0
        for _ in _ATTR_TOKEN.finditer(text, m.end(), end):
            attrs += 1
            if attrs > MAX_ATTRIBUTES_PER_TAG:
                raise _TooComplex(f"A tag has more than {MAX_ATTRIBUTES_PER_TAG} attributes. "
                                  "Simplify it and upload again.")
        name = m.group(2).lower()
        if m.group(1):
            meter.end(name)
        else:
            meter.start(name, self_closing=gt > 0 and text[gt - 1] == "/")
        resume = end + 1
    if resume < len(text):
        meter.text(bool(_NON_SPACE.search(text, resume)))


# --- the raw pass: line numbers, placeholder positions, removal counts -------------

_URL_ATTRS = frozenset({"href", "src", "xlink:href", "action", "formaction", "poster",
                        "background", "cite", "data", "codebase", "longdesc", "lowsrc",
                        "dynsrc", "ping", "manifest", "icon", "archive", "profile"})
_LOWER_TAGS = frozenset(t.lower() for t in _HTML_TAGS | _SVG_TAGS)
_LOWER_SVG_TAGS = frozenset(t.lower() for t in _SVG_TAGS)
_LOWER_SVG_ATTRS = frozenset(a.lower() for a in _SVG_ATTRS)
_LOWER_CLEAN = frozenset(t.lower() for t in _CLEAN_CONTENT_TAGS)
_UNSAFE_SCHEMES = ("javascript:", "vbscript:", "data:")
_MARKUP_START = re.compile(r"<[A-Za-z/!?]")
#: The only elements the raw pass keeps on its stack: the ones that decide a
#: placeholder's context. Everything else can nest freely without the stack (and
#: its lookups) growing with the file.
_TRACKED = _RAWTEXT_LOWER | _LOWER_CLEAN | _FOREIGN_ROOTS


@dataclass(frozen=True)
class _RawPlaceholder:
    name: str
    line: int
    context: str              # "text" or where it sits instead
    where: str                # human wording for the error


class _RawScan(HTMLParser):
    """A lenient pass over the file as written, for line numbers and counts. It
    decides nothing about safety (the sanitizer does, and the render re-verifies
    on html5ever's own output) and nothing about cost (:func:`_measure` does,
    first). It holds one rule of its own: inside <svg> or <math>, an element the
    HTML tokenizer would read as text (<style>, <title>, <textarea>…) may hold
    ONLY text. There the two parsers disagree about what is markup, and that
    disagreement is never a template's business."""

    def __init__(self) -> None:
        try:
            super().__init__(convert_charrefs=True, scripting=True)
        except TypeError:                                   # Python < 3.14
            super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.foreign = 0
        self.found: list[_RawPlaceholder] = []
        self.counts: Counter = Counter()

    def _foreign_rawtext(self) -> str | None:
        if not self.foreign:
            return None
        return next((t for t in reversed(self.stack) if t in _RAWTEXT_LOWER), None)

    def _refuse_markup_in(self, tag: str) -> None:
        raise _TooComplex(
            f"Inside an <svg> or <math>, a <{tag}> may hold only plain text — browsers read "
            "the tags inside it as real markup. Move the markup out and upload again.")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        inside = self._foreign_rawtext()
        if inside:
            self._refuse_markup_in(inside)
        self._tag(tag, attrs)
        if self.foreign and tag in _BREAKOUT:            # the parser leaves the <svg>
            k = max(i for i, t in enumerate(self.stack) if t in _FOREIGN_ROOTS)
            self.foreign -= sum(t in _FOREIGN_ROOTS for t in self.stack[k:])
            del self.stack[k:]
        if tag in _TRACKED and tag not in _RAW_VOID:
            self.stack.append(tag)
            self.foreign += tag in _FOREIGN_ROOTS

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        inside = self._foreign_rawtext()
        if inside:
            self._refuse_markup_in(inside)
        self._tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _TRACKED and tag in self.stack:
            k = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.foreign -= sum(t in _FOREIGN_ROOTS for t in self.stack[k:])
            del self.stack[k:]

    # -- counts
    def _tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        raw = self.get_starttag_text() or ""
        attr_map = {k: (v or "") for k, v in attrs}
        benign_meta = tag == "meta" and ("charset" in attr_map
                                         or attr_map.get("name", "").lower() == "viewport")
        if tag == "script":
            self.counts["scripts"] += 1
        elif tag not in _LOWER_TAGS and tag not in ("html", "head", "body") and not benign_meta:
            self.counts["elements"] += 1
        allowed = (_LOWER_SVG_ATTRS if tag in _LOWER_SVG_TAGS else frozenset()) \
            | {a.lower() for a in _HTML_ATTRS.get(tag, ())} | _GLOBAL_ATTRS
        for name, value in attrs:
            value = value or ""
            for pm in _PLACEHOLDER_RE.finditer(value):
                offset = max(raw.find(pm.group(0)), 0)
                self._found(pm.group(1), line + raw.count("\n", 0, offset), "attribute",
                            f"inside the {name} attribute of <{tag}>")
            for pm in _PLACEHOLDER_RE.finditer(name):
                self._found(pm.group(1), line, "attribute", f"inside the <{tag}> tag")
            if tag in ("html", "head") or benign_meta:
                continue
            if name.startswith("on"):
                self.counts["handlers"] += 1
            elif name in ("srcset", "imagesrcset"):
                self.counts["external_urls"] += bool(value.strip())
            elif name in _URL_ATTRS:
                self._count_url(tag, name, value)
            elif tag == "body" or (name not in allowed and not name.startswith("aria-")):
                self.counts["attributes"] += 1
            elif tag in _LOWER_SVG_TAGS and "url" in value.lower() and not _svg_value_ok(value):
                self.counts["external_urls"] += 1

    def _count_url(self, tag: str, name: str, value: str) -> None:
        target = value.strip()
        if not target:
            return
        if name == "href" and tag in _HREF_TAGS and _FRAGMENT_RE.fullmatch(target):
            return
        if name == "src" and tag == "img" and _DATA_IMAGE_RE.fullmatch(target):
            return
        squeezed = re.sub(r"[\x00-\x20\x7f]", "", value).lower()
        if squeezed.startswith(_UNSAFE_SCHEMES):
            self.counts["unsafe_urls"] += 1
        else:
            self.counts["external_urls"] += 1

    # -- placeholders
    def _found(self, name: str, line: int, context: str, where: str) -> None:
        self.found.append(_RawPlaceholder(name, line, context, where))

    def _context(self) -> tuple[str, str]:
        for tag in reversed(self.stack):
            if tag == "style":
                return "style", "inside a <style> block"
            if tag == "title":
                return "title", "inside the <title>"
            if tag == "script":
                return "script", "inside a <script>, which is removed"
            if tag in _LOWER_CLEAN:
                return "removed", f"inside <{tag}>, which is removed"
        return "text", ""

    def handle_data(self, data: str) -> None:
        inside = self._foreign_rawtext()
        if inside and _MARKUP_START.search(data):
            self._refuse_markup_in(inside)
        if "{" not in data:
            return
        line = self.getpos()[0]
        context, where = self._context()
        for pm in _PLACEHOLDER_RE.finditer(data):
            self._found(pm.group(1), line + data.count("\n", 0, pm.start()), context, where)

    def handle_comment(self, data: str) -> None:
        self.counts["comments"] += 1
        line = self.getpos()[0]
        for pm in _PLACEHOLDER_RE.finditer(data):
            self._found(pm.group(1), line + data.count("\n", 0, pm.start()), "comment",
                        "inside a comment")

    def _declaration(self, data: str) -> None:
        line = self.getpos()[0]
        for pm in _PLACEHOLDER_RE.finditer(data):
            self._found(pm.group(1), line + data.count("\n", 0, pm.start()), "declaration",
                        "inside a <!…> declaration")

    def handle_decl(self, decl: str) -> None:
        self._declaration(decl)

    def unknown_decl(self, data: str) -> None:
        self._declaration(data)

    def handle_pi(self, data: str) -> None:
        self._declaration(data)


# --- check_html --------------------------------------------------------------------

def _removed(counts: Counter) -> dict[str, int]:
    return {k: int(counts.get(k, 0)) for k in REMOVED_KEYS}


def _refused(message: str, counts: Counter | None = None) -> CheckResult:
    return CheckResult(sanitized_html="", errors=(TemplateError(None, None, message),),
                       removed=_removed(counts or Counter()), placeholders_used=())


def _position_error(p: _RawPlaceholder) -> TemplateError:
    tok = _token(p.name)
    return TemplateError(
        line=p.line, placeholder=tok,
        message=f"{tok} is {p.where}. Placeholders may only appear in the page's visible text.",
        suggestion=None if p.name in PLACEHOLDERS else suggest(p.name))


def _analyse(text: str) -> tuple[CheckResult, _Doc | None]:
    try:
        _measure(text)                     # before ANY parser sees the file
    except _TooComplex as exc:
        return _refused(str(exc)), None
    scan = _RawScan()
    try:
        scan.feed(text)
        scan.close()
    except _TooComplex as exc:
        return _refused(str(exc), scan.counts), None
    except Exception:
        # The stdlib parser has raised (AssertionError) on odd declarations in
        # older Pythons. Without the raw pass the work caps are unchecked, so
        # the file is refused, not passed to html5ever.
        logger.warning("report template refused: the raw HTML pass failed", exc_info=True)
        return _refused("This file's markup could not be read. Check it for unusual "
                        "<!…> declarations and upload it again.", scan.counts), None
    counts = Counter(scan.counts)
    try:
        doc = _sanitize(text, counts)
    except _Unverifiable:
        logger.warning("report template refused: sanitized output failed verification",
                       exc_info=True)
        return _refused("This template could not be verified after cleaning. Simplify its "
                        "markup and upload it again.", counts), None

    errors = [_position_error(p) for p in scan.found if p.context != "text"]
    raw_lines: dict[str, list[int]] = {}
    for p in scan.found:
        if p.context == "text":
            raw_lines.setdefault(p.name, []).append(p.line)

    used: list[str] = []
    seen: Counter = Counter()
    for occ in doc.occurrences:
        idx = seen[occ.name]
        seen[occ.name] += 1
        lines = raw_lines.get(occ.name) or []
        line = lines[idx] if idx < len(lines) else None
        tok = _token(occ.name)
        entry = PLACEHOLDERS.get(occ.name)
        if entry is None:
            hint = suggest(occ.name)
            errors.append(TemplateError(
                line, tok, f"{tok} is not a placeholder this report knows."
                + (f" Did you mean {hint}?" if hint else " See the placeholder list."), hint))
            continue
        if occ.in_title:
            errors.append(TemplateError(line, tok, f"{tok} is inside an SVG <title>. "
                                        "Placeholders may only appear in the page's visible text."))
            continue
        if entry.is_block:
            parent = occ.parent.tag if occ.parent else None
            if occ.in_svg:
                errors.append(TemplateError(line, tok, f"{tok} is a block and can't go inside "
                                            "an <svg>."))
                continue
            if parent not in BLOCK_PARENTS:
                where = f"inside <{parent}>" if parent else "loose in the page body"
                errors.append(TemplateError(
                    line, tok, f"{tok} is a block and can't sit {where}. Put it in its own "
                    f"element, for example <div>{tok}</div>."))
                continue
            if not occ.alone or occ.parent.children != 1:
                errors.append(TemplateError(
                    line, tok, f"{tok} must be the only thing inside its element. Put it in "
                    f"its own element, for example <div>{tok}</div>."))
                continue
        if tok not in used:
            used.append(tok)
    for name, lines in raw_lines.items():
        for line in lines[seen[name]:]:
            tok = _token(name)
            errors.append(TemplateError(line, tok, f"{tok} sits inside markup that is removed "
                                        "when the template is cleaned, so it would never show."))
    if not doc.occurrences and not errors:
        errors.append(TemplateError(
            None, None, "This template has no placeholders, so it would show none of the "
            "report's figures. Typed-in numbers are never used; add placeholders such as "
            "{{total_spend}} where the figures belong."))

    errors.sort(key=lambda e: (e.line is None, e.line or 0))
    if len(errors) > MAX_ERRORS:
        extra = len(errors) - MAX_ERRORS
        errors = errors[:MAX_ERRORS] + [TemplateError(None, None, f"…and {extra} more.")]
    result = CheckResult(sanitized_html=_document(doc.title, doc.styles, doc.body, csp=False),
                         errors=tuple(errors), removed=_removed(counts),
                         placeholders_used=tuple(used))
    return result, doc


def _decode(raw: bytes) -> tuple[str | None, str | None]:
    if len(raw) > HTML_MAX_BYTES:
        return None, f"The HTML file is over the {_limit(HTML_MAX_BYTES)} limit."
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, (f"The HTML file is not valid UTF-8 (first bad byte at offset "
                      f"{exc.start}). Save it as UTF-8 and upload it again.")
    if "\x00" in text:
        return None, "The HTML file contains a NUL byte, so it is not a text file."
    return text.removeprefix("﻿"), None


# --- the wall-clock backstop -------------------------------------------------------
#
# :func:`_measure` bounds every html5ever worst case this module knows. The
# backstop is for the ones it does not: an uploaded template is checked in a
# separate, long-lived worker process, and a check that runs past
# :func:`check_budget` seconds is answered "too complex" while the worker is
# KILLED (and started again on the next check). So a parser divergence nobody has
# found yet costs one bounded wait, never a CPU core held for a minute. The
# render path does not use it: it re-checks HTML this check already produced,
# which is canonical and small.
#
# One worker, one check at a time (a template upload is a person clicking a
# button). The worker announces itself ready before the first check, so its
# start-up is not charged to anyone's budget.

#: Seconds one upload check may take before the worker is killed.
_DEFAULT_CHECK_BUDGET_S = 8.0
#: Seconds the worker may take to start (import this module) before the check
#: is refused as unavailable.
_WORKER_START_TIMEOUT_S = 60.0


class TemplateCheckUnavailable(RuntimeError):
    """The checker itself could not run (the worker would not start or died). Not
    the template's fault: the route answers 503 and nothing is saved."""


def check_budget() -> float:
    raw = (os.environ.get("MR_TEMPLATE_CHECK_BUDGET_S") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_CHECK_BUDGET_S
    except ValueError:
        return _DEFAULT_CHECK_BUDGET_S
    return value if value > 0 else _DEFAULT_CHECK_BUDGET_S


#: How the worker starts: a fresh interpreter that imports only this module. No
#: ``multiprocessing``, deliberately — its "spawn" start method re-runs the
#: caller's own ``__main__`` module in the child, so any script without an
#: ``if __name__ == "__main__"`` guard that checked a template would re-run itself.
_WORKER_BOOT = (
    "import json, os, sys\n"
    "sys.path[:0] = json.loads(os.environ['MR_TEMPLATE_CHECK_PATH'])\n"
    "from marketing_research_agent.report_templates import _serve_stdio\n"
    "_serve_stdio()\n"
)


def _write_frame(stream, payload: Any) -> None:
    data = json.dumps(payload).encode("utf-8")
    stream.write(len(data).to_bytes(4, "big") + data)
    stream.flush()


def _read_frame(stream) -> Any:
    head = stream.read(4)
    if len(head) < 4:
        raise EOFError("the other side closed the pipe")
    size = int.from_bytes(head, "big")
    body = stream.read(size)
    if len(body) < size:
        raise EOFError("the other side closed the pipe mid-frame")
    return json.loads(body.decode("utf-8"))


def _serve_stdio() -> None:
    """The worker's loop: text in, ``CheckResult.to_dict()`` out, one JSON frame
    each way. Anything else a module prints goes to stderr, never into the
    protocol."""
    out, inp = sys.stdout.buffer, sys.stdin.buffer
    sys.stdout = sys.stderr
    _write_frame(out, "ready")
    while True:
        try:
            text = _read_frame(inp)
        except (EOFError, OSError):
            return
        try:
            payload = _analyse(text)[0].to_dict()
        except Exception:  # noqa: BLE001 - a crash must refuse, never pass
            logger.exception("report template check failed inside the worker")
            payload = _refused("This template could not be checked. Simplify it and upload "
                               "it again.").to_dict()
        try:
            _write_frame(out, payload)
        except OSError:
            return


def _result_from(payload: Mapping[str, Any]) -> CheckResult:
    return CheckResult(
        sanitized_html=payload["sanitized_html"],
        errors=tuple(TemplateError(**e) for e in payload["errors"]),
        removed=dict(payload["removed"]),
        placeholders_used=tuple(payload["placeholders_used"]))


class _CheckWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._frames: queue.Queue | None = None

    @staticmethod
    def _pump(stream, frames: queue.Queue) -> None:
        try:
            while True:
                frames.put(_read_frame(stream))
        except (EOFError, OSError, ValueError):
            pass
        finally:
            frames.put(EOFError)

    def _start(self) -> None:
        env = {**os.environ, "MR_TEMPLATE_CHECK_PATH": json.dumps([p for p in sys.path if p])}
        proc = subprocess.Popen([sys.executable, "-c", _WORKER_BOOT], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=env)
        frames: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(proc.stdout, frames), daemon=True,
                         name="mr-template-check-reader").start()
        self._proc, self._frames = proc, frames
        try:
            ready = frames.get(timeout=_WORKER_START_TIMEOUT_S)
        except queue.Empty:
            ready = None
        if ready != "ready":
            self._stop()
            raise TemplateCheckUnavailable("the template checker did not start")

    def _stop(self) -> None:
        proc, self._proc, self._frames = self._proc, None, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(5)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def check(self, text: str, budget: float) -> CheckResult | None:
        """The result, or None when the check ran past ``budget`` (the worker is
        then killed). Raises :class:`TemplateCheckUnavailable` when the worker
        cannot run or another check holds it for too long."""
        if not self._lock.acquire(timeout=max(3 * budget, 30.0)):
            raise TemplateCheckUnavailable("the template checker is busy")
        try:
            if self._proc is None or self._proc.poll() is not None:
                self._stop()
                self._start()
            _write_frame(self._proc.stdin, text)
            try:
                payload = self._frames.get(timeout=budget)
            except queue.Empty:
                logger.warning("report template check ran past %.1fs; the checker was "
                               "stopped", budget)
                self._stop()
                return None
            if payload is EOFError:
                raise EOFError("the template checker exited mid-check")
            return _result_from(payload)
        except TemplateCheckUnavailable:
            raise
        except (EOFError, OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("report template checker failed", exc_info=True)
            self._stop()
            raise TemplateCheckUnavailable("the template checker stopped unexpectedly") from exc
        finally:
            self._lock.release()

    def shutdown(self) -> None:
        with self._lock:
            self._stop()


_CHECKER = _CheckWorker()


def check_html(raw: bytes) -> CheckResult:
    """Check and sanitize an uploaded HTML template. Every problem with the FILE
    is an error in the result, and an erroring result can't be saved
    (``can_save``) — including a check that runs past :func:`check_budget`.
    Raises :class:`TemplateCheckUnavailable` only when the checker itself
    cannot run."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("check_html takes the uploaded bytes")
    text, problem = _decode(bytes(raw))
    if problem:
        return _refused(problem)
    budget = check_budget()
    result = _CHECKER.check(text, budget)
    if result is None:
        return _refused(f"This template is too complex to check within {budget:g} seconds. "
                        "Simplify its markup and upload it again.")
    return result


# --- rendering an HTML template -----------------------------------------------------

#: The renderer's own rule for a theme colour, so a token this module accepts
#: is always one ``validate_layout`` accepts.
_COLOUR_RE = vrr.COLOUR_RE
_NOT_COLOURS = frozenset({"inherit", "initial", "unset", "revert", "currentcolor", "none",
                          "auto", "var", "important"})


def theme_tokens(styles: list[str]) -> dict[str, str]:
    """Colours a template declares for our blocks: custom properties named after
    the theme's tokens (``--ink``, ``--gold``, ``--pos``…) in a top-level
    ``:root`` rule. A value that isn't a plain colour is ignored."""
    found: dict[str, str] = {}
    for css in styles:
        src = _css_preprocess(css)
        stmts, _ = _css_parse(_css_tokens(src))
        for s in stmts:
            if s.block is None:
                continue
            prelude = "".join(src[t.start:t.end] for t in s.head if t.kind != "comment")
            if ":root" not in [p.strip().lower() for p in prelude.split(",")]:
                continue
            for d in s.block:
                if d.block is not None:
                    continue
                text = "".join(src[t.start:t.end] for t in d.head if t.kind != "comment")
                name, sep, value = text.partition(":")
                name, value = name.strip(), re.sub(r"(?i)\s*!important\s*$", "", value.strip())
                if (sep and name.startswith("--") and name[2:] in vrr.PALETTE
                        and _COLOUR_RE.fullmatch(value) and value.lower() not in _NOT_COLOURS):
                    found[name[2:]] = value
    return found


def _scope_selector(selector: str) -> str:
    s = selector.strip()
    m = re.match(r"(?i)(:root|html|body)(?![\w-])", s)
    if m:
        return "." + BLOCK_SCOPE_CLASS + s[m.end():]
    return f".{BLOCK_SCOPE_CLASS} {s}"


def _scope_rules(stmts: list[_Stmt], src: str) -> str:
    out: list[str] = []
    for s in stmts:
        sig = _significant(s.head)
        head_text = "".join(src[t.start:t.end] for t in s.head)
        if s.block is None:
            out.append(head_text + s.end)
            continue
        if sig and sig[0].kind == "at":
            name = sig[0].value.lower()
            if name == "page":
                continue                               # the template owns the page
            if name in ("media", "supports", "container", "layer"):
                out.append(head_text + "{" + _scope_rules(s.block, src) + "}")
            else:                                      # @font-face, @keyframes…
                out.append(head_text + "{" + _css_text(s.block, src) + "}")
            continue
        selectors, current, depth = [], [], 0
        for t in s.head:
            if t.kind == "comment":
                continue
            if t.kind in ("(", "[", "function"):
                depth += 1
            elif t.kind in (")", "]"):
                depth -= 1
            if t.kind == "," and depth == 0:
                selectors.append("".join(current))
                current = []
            else:
                current.append(src[t.start:t.end])
        selectors.append("".join(current))
        scoped = ",".join(_scope_selector(sel) for sel in selectors if sel.strip())
        out.append(scoped + "{" + _css_text(s.block, src) + "}")
    return "".join(out)


def block_stylesheet(theme: vrr.Theme = vrr.DEFAULT_THEME) -> str:
    """The renderer's stylesheet, every selector scoped under ``.mrb``, so it
    styles our blocks and never the template's own markup. The template's
    stylesheets come after it and may restyle ``.mrb …`` on purpose."""
    src = _css_preprocess(vrr.stylesheet(theme))
    stmts, _ = _css_parse(_css_tokens(src))
    return _scope_rules(stmts, src)


def _block_html(report: Mapping[str, Any], section_type: str, theme: vrr.Theme) -> str:
    inner = vrr.render_block(report, vrr.SectionSpec(section_type), theme=theme)
    return f'<div class="{BLOCK_SCOPE_CLASS}">{inner}</div>'


def _scalar_html(report: Mapping[str, Any], name: str) -> str:
    if _scalar_value(report, name) is None:
        return ABSENT_MARKER
    return vrr.render_scalar(report, name)             # escaped by the renderer


def _require_report(report: Any) -> None:
    if not isinstance(report, Mapping) or report.get("generator") != vr.GENERATOR_VERSION:
        got = report.get("generator") if isinstance(report, Mapping) else type(report).__name__
        raise ValueError(f"this renderer reads '{vr.GENERATOR_VERSION}' reports, got '{got}'")


def _default_title(report: Mapping[str, Any]) -> str:
    return f"Vendor Performance — {report.get('title')}, {str(report.get('year_month'))[:4]}"


_REQUIRED_SECTIONS = ("data_gaps", "footer")


def render_html_template(sanitized_html: str, report: Mapping[str, Any]) -> str:
    """Render ``report`` through a stored HTML template.

    One pass over body text nodes swaps each placeholder for its value:
    scalars are escaped text (absent is the em-dash marker), and blocks are
    our own server-rendered HTML/SVG in the template's theme colours. A value
    that itself contains ``{{…}}`` stays literal text. Our data-gaps note and
    provenance footer are appended if the template leaves them out. The whole
    document is then sanitized again, and the CSP meta goes first in <head>.

    Raises :class:`TemplateRenderError` when the stored HTML no longer checks
    clean, and ``ValueError`` for a report this renderer doesn't read."""
    _require_report(report)
    if not isinstance(sanitized_html, str) or not sanitized_html.strip():
        raise TemplateRenderError("This template version has no HTML to render.")
    if "\x00" in sanitized_html:
        raise TemplateRenderError("This template version's HTML is corrupt.")
    result, doc = _analyse(sanitized_html)
    if doc is None or result.errors:
        first = result.errors[0].message if result.errors else "it could not be checked"
        raise TemplateRenderError(f"This template can't be rendered: {first}")

    theme = vrr.Theme(colors={**vrr.PALETTE, **theme_tokens(doc.styles)})

    def fill(m: re.Match) -> str:
        entry = PLACEHOLDERS[m.group(1)]
        if entry.is_block:
            return _block_html(report, entry.section_type, theme)
        return _scalar_html(report, entry.name)

    parts = list(doc.parts)
    for index in sorted({occ.part for occ in doc.occurrences}):
        parts[index] = _PLACEHOLDER_RE.sub(fill, parts[index])     # one pass, no rescan
    used = set(result.placeholders_used)
    for section_type in _REQUIRED_SECTIONS:
        if vrr.SECTION_REGISTRY[section_type].placeholder not in used:
            parts.append(_block_html(report, section_type, theme))

    draft = _document(doc.title or _default_title(report),
                      [block_stylesheet(theme), *doc.styles], "".join(parts), csp=False)
    try:
        final = _sanitize(draft, Counter())            # belt and braces
    except _Unverifiable as exc:
        logger.error("rendered report template failed re-verification", exc_info=True)
        raise TemplateRenderError("This template's output could not be verified, so it "
                                  "was not shown.") from exc
    return _document(final.title, final.styles, final.body, csp=True)


# --- dispatch ------------------------------------------------------------------------

def _is_builtin(version: Mapping[str, Any]) -> bool:
    return (version.get("builtin") is True or version.get("kind") == "builtin"
            or version.get("id") == "builtin")


def enabled() -> bool:
    """Kill switch for team report templates, **default off** — the same
    contract as ``reports.vendor_report_enabled`` (env read per call). Off, the
    template routes answer 404 after auth, builds ignore any saved template, and
    a stored report built with one is not rendered through it."""
    return os.environ.get("MR_REPORT_TEMPLATES", "0").strip().lower() in ("1", "true", "on")


def version_kind(version: Mapping[str, Any] | None) -> str:
    """``builtin``, ``html`` or ``layout`` (a mapped PDF/image or a hand-built
    layout) — the one word the console and a run's template reference use."""
    if not version or _is_builtin(version):
        return "builtin"
    return "html" if version.get("source_kind") == "html" else "layout"


def template_ref(version: Mapping[str, Any] | None) -> dict:
    """What a run records about the template it was built with:
    ``{kind, number, id}``. ``id`` is the stored record the build rendered from
    (``None`` for the built-in), so the run re-renders through exactly it."""
    kind = version_kind(version)
    if kind == "builtin":
        return {"kind": "builtin", "number": None, "id": None}
    return {"kind": kind, "number": version.get("number"), "id": version.get("id")}


def summarize(record: Mapping[str, Any] | None) -> dict:
    """A stored template record as the console shows it. ``id`` is the CONTENT
    version's id — an activation (a revert) reports the version it re-activated,
    so the active entry matches a row of the version list."""
    record = record or {}
    kind = version_kind(record)
    return {
        "id": "builtin" if kind == "builtin" else (record.get("content_id") or record.get("id")),
        "kind": kind,
        "number": record.get("number"),
        "source_kind": record.get("source_kind"),
        "filename": record.get("filename"),
        "created_by": record.get("uploaded_by"),
        "created_by_name": record.get("created_by_name") or record.get("uploaded_by"),
        "created_at": record.get("created_at"),
        "set_by": record.get("set_by"),
        "set_by_name": record.get("set_by_name") or record.get("set_by"),
        "set_at": record.get("set_at"),
    }


def listing(records: list[Mapping[str, Any]]) -> dict:
    """``{"active", "versions"}`` from a workspace's records (newest first).

    ``active`` is the newest record, or the built-in when there is none.
    ``versions`` are the saved CONTENT versions, newest first, each carrying who
    last set it active and when (an activation updates its version's
    ``set_by``/``set_at``) and whether it is the active one now."""
    active = summarize(records[0]) if records else summarize(None)
    last_set: dict[str, tuple] = {}
    for r in records:                                  # newest first: first seen wins
        cid = r.get("content_id") or r.get("id")
        if cid and cid not in last_set and not _is_builtin(r):
            last_set[cid] = (r.get("set_by"), r.get("set_by_name") or r.get("set_by"),
                             r.get("set_at"))
    versions = []
    for r in records:
        if _is_builtin(r) or r.get("reverted_from"):
            continue
        entry = summarize(r)
        entry["set_by"], entry["set_by_name"], entry["set_at"] = last_set.get(
            entry["id"], (entry["set_by"], entry["set_by_name"], entry["set_at"]))
        entry["active"] = entry["id"] == active["id"]
        versions.append(entry)
    return {"active": active, "versions": versions}


def _with_required_sections(layout: vrr.Layout) -> vrr.Layout:
    """A mapped layout always carries the data-gaps note and the provenance
    footer, exactly as an HTML template does."""
    sections = list(layout.sections)
    types = {s.type for s in sections}
    if "data_gaps" not in types:
        at = next((i for i, s in enumerate(sections) if s.type == "footer"), len(sections))
        sections.insert(at, vrr.SectionSpec("data_gaps"))
    if "footer" not in types:
        sections.append(vrr.SectionSpec("footer"))
    return vrr.Layout(theme=layout.theme, sections=tuple(sections))


def render_with_template(report: Mapping[str, Any], template_version: Mapping[str, Any]) -> str:
    """Render ``report`` with a stored template version (``runs`` shape) or the
    build's template record (``{"kind": "builtin"}``).

    builtin → ``render(report)``; a ``spec`` → ``render(report,
    layout_from_dict(spec))``; ``html`` → :func:`render_html_template`. Any
    failure that belongs to the template raises :class:`TemplateRenderError`.
    The built-in is never substituted here."""
    if not isinstance(template_version, Mapping):
        raise TemplateRenderError("No template version was given to render with.")
    if _is_builtin(template_version):
        return vrr.render(report)
    _require_report(report)
    if template_version.get("source_kind") == "html":
        return render_html_template(template_version.get("html"), report)
    spec = template_version.get("spec")
    if not isinstance(spec, Mapping):
        raise TemplateRenderError("This template version has no layout to render.")
    try:
        layout = vrr.layout_from_dict(spec)
    except ValueError as exc:
        raise TemplateRenderError(f"This template's layout can't be used: {exc}.") from exc
    except (KeyError, TypeError, AttributeError) as exc:
        raise TemplateRenderError("This template's layout is malformed: a section is "
                                  "missing its type or has the wrong shape.") from exc
    try:
        return vrr.render(report, _with_required_sections(layout))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        logger.warning("report template layout failed to render: %s", type(exc).__name__,
                       exc_info=True)
        raise TemplateRenderError("This template's section options could not be applied to "
                                  "this report.") from exc


# --- the starter template -----------------------------------------------------------

def _starter_block_order(types: list[str]) -> list[str]:
    """The built-in report's own section order (``DEFAULT_LAYOUT``): header band
    first, provenance footer last. A registry type the built-in does not show
    (standouts, watch items — the two halves of highlights) goes just before the
    data-gaps note; anything else unknown goes there too, so a new section type
    lands in the body, never above the header or below the footer."""
    built_in = [s.type for s in vrr.DEFAULT_LAYOUT.sections]
    order = [t for t in built_in if t in types]
    extras = sorted(t for t in types if t not in built_in)
    at = order.index("data_gaps") if "data_gaps" in order else max(len(order) - 1, 0)
    return order[:at] + extras + order[at:]


def starter_html() -> str:
    """A documented starter template that uses every block placeholder and
    every figure. Generated from the vocabulary, so it can't go stale, and it
    passes :func:`check_html` with zero errors (pinned by a test)."""
    scalars = [p for p in PLACEHOLDERS.values() if not p.is_block]
    blocks = [p for p in PLACEHOLDERS.values() if p.is_block]
    figure_doc = "\n".join(f"    {p.name:<28} {p.description}" for p in scalars)
    block_doc = "\n".join(f"    {p.name:<28} {p.description}" for p in blocks)
    tokens = "\n".join(f"  --{k}: {v};" for k, v in vrr.PALETTE.items())
    token_names = ", ".join(f"--{k}" for k in vrr.PALETTE)
    metric_rows = "\n".join(
        f"        <tr><th>{html.escape(vr.METRICS[p.name][0])}</th><td>{p.token}</td></tr>"
        for p in scalars if p.name in vr.METRICS)
    by_type = {p.section_type: p for p in blocks}
    ranked = _starter_block_order(list(by_type))

    def div(section_type: str, indent: str) -> str:
        p = by_type[section_type]
        return (f'{indent}<!-- {p.description.replace("--", "-")} -->\n'
                f'{indent}<div class="block">{p.token}</div>')

    header = div(ranked[0], "")
    footer = div(ranked[-1], "")
    block_divs = "\n".join(div(t, "    ") for t in ranked[1:-1])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vendor performance</title>
<!--
  VENDOR PERFORMANCE: STARTER TEMPLATE

  How it works
  * Write ordinary HTML and CSS. On upload, scripts, event handlers, forms,
    frames and every external link, image, font or stylesheet are removed.
    Images and fonts are kept only when embedded as data: URLs.
  * A placeholder is a name inside double curly braces, like the ones in the
    body below. Placeholders may only appear in the page's visible text, never
    inside an attribute, a style block, the title or a comment.
  * Figures are filled in from the report, formatted. A figure the tracker did
    not report shows as an em-dash, never as a zero. Never type numbers in:
    they would not update.
  * A block (chart, table, list...) must be the only thing inside its own
    element, such as a div of its own. Blocks can't go inside p, span, a
    heading, or an svg.
  * Colours: set any of these on :root, as below, and the charts and tables use
    your colours: {token_names}.
  * Blocks are wrapped in an element with class "{BLOCK_SCOPE_CLASS}". Restyle them with
    selectors such as ".{BLOCK_SCOPE_CLASS} .panel" or ".{BLOCK_SCOPE_CLASS} .wrap".
  * If you leave out the data-gaps note or the provenance footer, they are
    added at the end of the page automatically.

  Figures
{figure_doc}

  Blocks
{block_doc}
-->
<style>
:root {{
{tokens}
}}
body {{ margin: 0; background: #ffffff; color: var(--ink); font-family: Georgia, 'Times New Roman', serif; }}
.page {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px; }}
.page h1 {{ font-size: 34px; margin: 0 0 6px; }}
.page .as-of {{ color: var(--slate); margin: 0 0 24px; }}
table.figures {{ border-collapse: collapse; width: 100%; margin: 0 0 32px; }}
table.figures th, table.figures td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }}
table.figures td {{ text-align: right; font-variant-numeric: tabular-nums; }}
.block {{ margin: 0 0 24px; }}
.{BLOCK_SCOPE_CLASS} .wrap {{ padding: 0; }}
</style>
</head>
<body>
{header}
<div class="page">
  <h1>Vendor performance, {{{{month_label}}}}</h1>
  <p class="as-of">{{{{title}}}}, figures as of {{{{as_of}}}} ({{{{year_month}}}})</p>

  <section>
    <h2>Key figures</h2>
    <table class="figures">
      <tbody>
{metric_rows}
      </tbody>
    </table>
  </section>

  <section>
{block_divs}
  </section>
</div>
{footer}
</body>
</html>
"""


# --- upload sniffing ------------------------------------------------------------------

@dataclass(frozen=True)
class SniffedUpload:
    kind: str                  # "pdf" | "png" | "jpeg" | "html"
    media_type: str
    size: int
    width: int | None = None
    height: int | None = None


class UploadRejected(ValueError):
    """The upload is not a file we take. ``reason`` is safe to show the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD,
                       0xCE, 0xCF})


def _limit(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def _check_image_size(kind: str, width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise UploadRejected(f"The {kind} image has no size, so it can't be read.")
    if width > IMAGE_MAX_SIDE or height > IMAGE_MAX_SIDE:
        raise UploadRejected(f"The image is {width}×{height} pixels; the limit is "
                             f"{IMAGE_MAX_SIDE} pixels on each side.")


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise UploadRejected("The PNG file is damaged: its header can't be read.")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            break
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue                                   # standalone markers
        if marker in (0xD9, 0xDA) or i + 2 > n:
            break                                      # end of image / scan data
        seg = struct.unpack(">H", data[i:i + 2])[0]
        if seg < 2:
            break
        if marker in _JPEG_SOF:
            if i + 7 > n:
                break
            height, width = struct.unpack(">HH", data[i + 3:i + 7])
            return width, height
        i += seg
    raise UploadRejected("The JPEG file is damaged: its image size can't be read.")


def _unsupported(data: bytes) -> str:
    head = data[:16]
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF images aren't supported. Upload a PNG or JPEG instead."
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP images aren't supported. Upload a PNG or JPEG instead."
    if head.startswith(b"PK\x03\x04"):
        return ("That is a zip or Office document. Export the sample report as a PDF and "
                "upload the PDF.")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "That is an old Office document. Export it as a PDF and upload the PDF."
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF images aren't supported. Upload a PNG or JPEG instead."
    return "That file isn't a PDF, PNG, JPEG or HTML file, judging by its contents."


def sniff_upload(data: bytes) -> SniffedUpload:
    """What an upload IS, from its bytes; the name and declared type are never
    consulted. Accepted: PDF (≤10 MB), PNG/JPEG (≤5 MB, ≤4096 px a side) and
    UTF-8 HTML (≤512 KB). Anything else raises :class:`UploadRejected`."""
    data = bytes(data)
    size = len(data)
    if not size:
        raise UploadRejected("The file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"The file is over the {_limit(MAX_UPLOAD_BYTES)} upload limit.")
    if data.startswith(b"%PDF-"):
        return SniffedUpload("pdf", "application/pdf", size)
    if data.startswith(_PNG_SIGNATURE) or data.startswith(b"\xff\xd8\xff"):
        png = data.startswith(_PNG_SIGNATURE)
        if size > IMAGE_MAX_BYTES:
            raise UploadRejected(f"The image is over the {_limit(IMAGE_MAX_BYTES)} limit "
                                 "for images.")
        width, height = _png_size(data) if png else _jpeg_size(data)
        _check_image_size("PNG" if png else "JPEG", width, height)
        return SniffedUpload("png" if png else "jpeg", "image/png" if png else "image/jpeg",
                             size, width, height)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadRejected(_unsupported(data)) from None
    if "\x00" in text:
        raise UploadRejected(_unsupported(data))
    lead = text.removeprefix("﻿").lstrip()[:512].lower()
    if lead.startswith("<?xml") or re.match(r"<svg[\s>/]", lead) or "<!doctype svg" in lead:
        raise UploadRejected("SVG files aren't supported. Upload a PNG or JPEG of the sample, "
                             "or an HTML template.")
    if not lead.startswith("<"):
        raise UploadRejected(_unsupported(data))
    if size > HTML_MAX_BYTES:
        raise UploadRejected(f"The HTML file is over the {_limit(HTML_MAX_BYTES)} limit.")
    return SniffedUpload("html", "text/html", size)
