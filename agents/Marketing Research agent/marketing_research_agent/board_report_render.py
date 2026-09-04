"""Board report renderer — a finished ledger as one self-contained HTML file.

:func:`render` is the whole surface: a :class:`~.board_report.ReportLedger` in,
a complete ``<!DOCTYPE html>`` document out. No I/O, no routes, no persistence,
no clock — everything the page says comes from the ledger it was handed.

**Self-contained is the requirement, not a nicety.** The marketing team emails
these files to clients, and the templates this reproduces pulled Chart.js from
a CDN and three families from Google Fonts, so behind a corporate proxy the
whole visual identity collapsed to unstyled text with four blank canvases. The
output here contains no ``<script>``, no ``<link>``, no remote image and no
``http`` of any kind: charts are inline SVG drawn from the ledger's own numbers,
and typography is a fallback stack (see :data:`SERIF` and the note below).

**One shell, N compositions.** The head, the palette and the component classes
are fixed; only the section list changes, and it changes on period count alone:

* one period  — ``01`` scorecard, ``02`` inside the period, ``03`` highlights
* two periods — ``00`` at a glance, ``01`` movers, ``02`` ledger,
  ``03`` channel shift, ``04`` what improved / what dropped

A :class:`ReportLedger` always carries two columns, so "one period" means the
two columns name the same period — :func:`single_period` builds exactly that.

Four rules are load-bearing enough to state up front, because each one is a
defect the supplied templates shipped:

1. **Absent is not zero.** A missing figure renders as an em-dash carrying a
   visible marker, never ``0`` and never a blank cell that reads as zero, and
   every absent row is named again in the "basis & data gaps" panel. Several
   metrics are genuinely absent in production; a ``$0`` in those cells is a
   board slide claiming no revenue was sold.
2. **Colour and glyph are orthogonal.** The templates gave ``-21.8%`` on Spend
   the ``up`` class and got a green ▲ next to a falling number, because one
   class encoded both "favourable" and "rising". Here the colour comes from
   :attr:`LedgerRow.improved` and the arrow comes from the sign of
   :attr:`LedgerRow.delta`, and they are chosen by two different functions.
3. **No invisible bars.** Website's ROAS is 543%/787% against Meta and Google
   near 40%/95%, which on a linear axis renders the three channels that matter
   as three slivers. The ratio chart caps its axis off the median and draws the
   outlier as a torn bar with its true value printed, so the break-even line at
   100% sits where a reader can actually use it.
4. **Every dynamic string is escaped.** Metric labels come from the catalog
   today and prose will come from an LLM tomorrow, on a page that goes to
   clients.

**Fonts: a fallback stack, not embedded faces.** Fraunces alone is a ~110KB
variable woff2; with Inter's four weights and three of IBM Plex Mono, base64 at
+33% would add roughly 700KB–1MB to every file the team emails, and would put
binary blobs plus their licences in this repo. The stacks below name the design's
three faces first — a machine that has them still gets the intended page — and
fall back through faces present by default on Windows, macOS and a headless
Chromium container (Georgia/Palatino, Segoe UI/Helvetica, Consolas/DejaVu Sans
Mono), with ``tabular-nums`` and an explicit ``"tnum"`` feature so figures stay
column-aligned in whichever face resolves. If pixel-exact Fraunces is later
worth the megabyte, the swap is one function: emit an ``@font-face`` block with
data URIs and prepend it to :data:`CSS`.

Deliberately importing nothing but the ledger. ``board_report`` is consumed and
never modified; ``reports``, ``sheets_source`` and ``pdf_export`` are not
imported at all — ``pdf_export`` in particular is a different visual identity
that is not being extended, and the routes that call this belong to another
module entirely.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .board_report import (
    CAC_KEY,
    INT,
    MONEY,
    PCT,
    UNTRACKED_LABEL,
    ChannelRow,
    ChannelTotals,
    LedgerRow,
    PeriodRollup,
    ReportLedger,
    compare,
)

#: Bumped when the rendered document changes shape for the same ledger. The
#: ledger's own ``cache_key`` covers the numbers; this covers the page.
RENDERER_VERSION = "mr-board-report-render/1"

# --- palette and type --------------------------------------------------------
# Fixed by the marketing team's templates and kept as data so the CSS and the
# SVG fills cannot drift apart — every colour below is emitted twice, once as a
# custom property for the stylesheet and once as a literal into chart geometry
# (a presentation attribute cannot rely on var() surviving every PDF engine).

PALETTE: dict[str, str] = {
    "ink": "#14213A",
    "ink-soft": "#243456",
    "paper": "#FBFAF7",
    "paper-2": "#F2EFE8",
    "gold": "#C9A227",
    "gold-soft": "#E7D9A6",
    "slate": "#5A6472",
    "pos": "#2E7D5B",
    "neg": "#B23A3A",
    "line": "#DED9CE",
    "grid": "#EDEAE2",
    "muted": "#8C97AD",
}

INK = PALETTE["ink"]
GOLD = PALETTE["gold"]
SLATE = PALETTE["slate"]
POS = PALETTE["pos"]
NEG = PALETTE["neg"]
GRID = PALETTE["grid"]
PANEL_BG = "#ffffff"

#: Column A / column B, the one colour that differed between the two
#: single-period templates.
SERIES_A, SERIES_B = SLATE, GOLD

SERIF = ("Fraunces,'Iowan Old Style','Palatino Linotype',Palatino,"
         "'Book Antiqua',Georgia,'Times New Roman',serif")
SANS = ("Inter,'Segoe UI',system-ui,-apple-system,'Helvetica Neue',"
        "Helvetica,Arial,sans-serif")
MONO = ("'IBM Plex Mono','SF Mono','Cascadia Mono','DejaVu Sans Mono',"
        "Consolas,'Courier New',monospace")

#: What an absent figure looks like. An em-dash plus a marker class the "basis
#: & data gaps" panel then explains — never 0, never an empty cell.
ABSENT = '<span class="absent" title="not reported for this period">&#8212;</span>'

#: The eight rows the at-a-glance band publishes, by catalog key. Labels come
#: from the catalog, never from a copy kept here.
GLANCE_KEYS: tuple[str, ...] = (
    "spend", "revenue_amount_sold", "revenue_amount_sold_not_actualized",
    "revenue_clients", "roas_pct", CAC_KEY, "qualified_leads", "revenue_target_pct",
)

#: The single-period scorecard, as ``(catalog key, extra card class)``. Two
#: bands of five columns each. Exactly one ``hl`` card in the report, and the
#: not-actualized family is set apart by ``na`` rather than by adjacency.
#:
#: Demos Completed (Direct) sits immediately before Conversion Rate on purpose:
#: the rate divides by *that* count (248/212), not by the all-in 272/238 one
#: card earlier, and each card prints its ``basis`` so the two reconcile.
SCORE_CORE: tuple[tuple[str, str], ...] = (
    ("spend", ""),
    ("qualified_leads", ""),
    ("qual_demos_booked", ""),
    ("demos_completed", ""),
    ("demos_completed_direct", ""),
    ("conversion_rate_pct", ""),
    ("revenue_clients", ""),
    ("revenue_amount_sold", "hl"),
    ("roas_pct", ""),
    (CAC_KEY, ""),
)

SCORE_PIPELINE: tuple[tuple[str, str], ...] = (
    ("revenue_amount_sold_not_actualized", "na"),
    ("revenue_amount_sold_without_setup_fee_not_actualized", "na"),
    ("paying_revenue_amount_sold", "na"),
    ("inbound_pipeline_revenue_amount_sold", "na"),
    ("roas_not_actualized_pct", "na"),
    ("revenue_target_pct", "na"),
    ("projected_new_clients_not_actualized", "na"),
    ("paying_new_clients", "na"),
    ("services_sold_not_actualized", "na"),
    ("average_deal_amount", ""),
)

#: How many rows the movers chart draws before it stops, split evenly between
#: improvements and deteriorations. All comparable rows would make an SVG taller
#: than a page, and ranking the whole set by size alone puts sixteen green bars
#: on a chart called "biggest movers" — on this data every large move is a
#: revenue increase, and the five things that got worse fall off the bottom. The
#: ledger below carries the rest, and the caption says which rule was applied.
MOVERS_LIMIT = 16

BREAK_EVEN = 100.0


@dataclass(frozen=True)
class ReportProse:
    """The optional LLM-written slot — step 8's problem, not this module's.

    Every field is optional and every field is escaped as **plain text**: these
    strings reach a client-facing page, so markup in them is shown, not run.
    With no prose the report is still complete: the cover drops its thesis line
    rather than inventing one, and the win/miss columns fall back to the
    ledger's own arithmetic, labelled as derived.
    """

    thesis: str | None = None
    wins: tuple[str, ...] = ()
    misses: tuple[str, ...] = ()
    win_heading: str = "Gains"
    miss_heading: str = "Slippage"


# --- escaping and formatting -------------------------------------------------

def _esc(value: object) -> str:
    """Everything that reaches the document goes through here."""
    return html.escape("" if value is None else str(value), quote=True)


def _money(v: float) -> str:
    return "$" + f"{round(v):,}"


def _pct(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}%"


def _count(v: float) -> str:
    return f"{round(v):,}"


def _fmt(v: float | None, fmt: str) -> str:
    """A ledger cell. ``None`` is the em-dash marker, never a zero."""
    if v is None:
        return ABSENT
    if fmt == MONEY:
        return _esc(_money(v))
    if fmt == PCT:
        return _esc(_pct(v))
    return _esc(_count(v))


def _fmt_compact(v: float | None, fmt: str) -> str:
    """A card or cover-meta cell — thousands folded above $100K only, so
    ``$239.6K`` and ``$4,991`` both read the way the template printed them."""
    if v is None:
        return ABSENT
    if fmt == MONEY:
        a = abs(v)
        if a >= 1_000_000:
            return _esc(f"${v / 1e6:.1f}M".replace(".0M", "M"))
        if a >= 100_000:
            return _esc(f"${v / 1e3:.1f}K".replace(".0K", "K"))
        return _esc(_money(v))
    if fmt == PCT:
        return _esc(f"{v:.1f}%")
    return _esc(_count(v))


def _fmt_delta(row: LedgerRow) -> str:
    """Percentage rows move in points; everything else moves in its own unit."""
    d = row.delta
    if d is None:
        return ABSENT
    if row.format == PCT:
        return _esc(f"{d:+.2f} pp")
    sign = "+" if d >= 0 else "−"
    body = _money(abs(d)) if row.format == MONEY else _count(abs(d))
    return _esc(sign + body)


def _fmt_change(row: LedgerRow) -> str:
    c = row.change_pct
    return ABSENT if c is None else _esc(f"{c:+.1f}%")


def _tone(improved: bool | None) -> str:
    """Colour — and *only* colour. ``None`` (neutral polarity, no change, or a
    missing value) is never green and never red."""
    if improved is None:
        return "neu"
    return "up" if improved else "down"


def _glyph(delta: float | None) -> str:
    """Arrow — and *only* arrow. Direction is the sign of the move, which is
    why a favourable fall in CAC prints a green ▼ instead of the templates'
    green ▲ beside a negative number."""
    if delta is None or delta == 0:
        return "flat"
    return "rise" if delta > 0 else "fall"


def _trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _wrap2(text: str, limit: int) -> list[str]:
    """One line, or two. SVG text does not wrap, and the catalog's longest label
    is 49 characters — truncating instead would render three different rows as
    the same ``Total Services Sold (Not Actualiz…``."""
    if len(text) <= limit:
        return [text]
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = limit
    return [text[:cut].rstrip(), _trunc(text[cut:].strip(), limit)]


# --- SVG primitives ----------------------------------------------------------
# No <script>, no library, no animation, no devicePixelRatio: the charts are
# geometry computed here and emitted as markup, which is also what makes them
# correct in a headless-Chromium PDF pass.

def _n(v: float) -> str:
    """One decimal place, so the same ledger always emits the same bytes."""
    return f"{v:.1f}"


def _svg(width: float, height: float, title: str, desc: str, body: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {_n(width)} {_n(height)}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" focusable="false">'
        f"<title>{_esc(title)}</title><desc>{_esc(desc)}</desc>{body}</svg>"
    )


def _rect(x: float, y: float, w: float, h: float, fill: str,
          *, rx: float = 3.0, opacity: float | None = None) -> str:
    if w <= 0 or h <= 0:
        return ""
    op = "" if opacity is None else f' fill-opacity="{opacity}"'
    return (f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" '
            f'rx="{_n(min(rx, w / 2, h / 2))}" fill="{fill}"{op}/>')


def _line(x1: float, y1: float, x2: float, y2: float, stroke: str,
          width: float = 1.0) -> str:
    return (f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
            f'stroke="{stroke}" stroke-width="{_n(width)}"/>')


def _text(x: float, y: float, body: str, *, size: float = 10.0, fill: str = SLATE,
          anchor: str = "start", sans: bool = False, weight: str = "400",
          italic: bool = False) -> str:
    """A label. The face comes from a class the document's own stylesheet
    defines — repeating the three-deep fallback stack as an attribute on every
    one of ~200 labels was a fifth of the file."""
    cls = ' class="s"' if sans else ""
    style = ' font-style="italic"' if italic else ""
    return (f'<text{cls} x="{_n(x)}" y="{_n(y)}" font-size="{_n(size)}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}"{style}>{_esc(body)}</text>')


def _nice_max(v: float) -> float:
    """A round axis ceiling at or above ``v``."""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    base = 10.0 ** exp
    for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * base * 1.0000001:
            return m * base
    return 10 * base


def _ticks(maximum: float, steps: int = 4) -> list[float]:
    return [maximum * i / steps for i in range(steps + 1)]


def _axis_label(v: float, fmt: str) -> str:
    if fmt == MONEY:
        if abs(v) >= 1000:
            return f"${v / 1000:.0f}K"
        return f"${v:.0f}"
    if fmt == PCT:
        return f"{v:.0f}%"
    return f"{v:,.0f}"


def _legend(x: float, y: float,
            series: Sequence[tuple[str, str] | tuple[str, str, float]]) -> str:
    """Swatch + label pairs, laid out left to right along one line. A third slot
    carries the swatch opacity, which is how the ratio chart distinguishes its
    two columns without stealing the hue that means "above / below break-even"."""
    out: list[str] = []
    cursor = x
    for entry in series:
        label, colour = entry[0], entry[1]
        opacity = entry[2] if len(entry) > 2 else None
        out.append(_rect(cursor, y - 8, 11, 11, colour, rx=2.5, opacity=opacity))
        out.append(_text(cursor + 16, y + 1, label, size=10.5, sans=True))
        cursor += 22 + len(label) * 5.9
    return "".join(out)


@dataclass(frozen=True)
class _Series:
    label: str
    colour: str
    values: tuple[float | None, ...]


def _grouped_vbar(categories: Sequence[str], series: Sequence[_Series], *,
                  fmt: str, title: str, desc: str,
                  width: float = 760.0, height: float = 300.0) -> str:
    """Chart (a): vertical bars, one group per category, N series per group."""
    top, bottom, left, right = 40.0, 48.0, 78.0, 20.0
    plot_w, plot_h = width - left - right, height - top - bottom
    flat = [v for s in series for v in s.values if v is not None]
    top_v = _nice_max(max(flat)) if flat else 1.0

    def y_of(v: float) -> float:
        return top + plot_h - (v / top_v) * plot_h

    out = [_legend(left, 18, [(s.label, s.colour) for s in series])]
    for t in _ticks(top_v):
        y = y_of(t)
        out.append(_line(left, y, left + plot_w, y, GRID))
        out.append(_text(left - 8, y + 3.5, _axis_label(t, fmt), size=9.5, anchor="end"))
    out.append(_line(left, top + plot_h, left + plot_w, top + plot_h, PALETTE["line"]))

    n_cat, n_ser = max(len(categories), 1), max(len(series), 1)
    group_w = plot_w / n_cat
    inner = min(group_w * 0.74, n_ser * 58.0)
    bar_w = inner / n_ser
    label_bars = n_cat * n_ser <= 8
    for gi, cat in enumerate(categories):
        gx = left + gi * group_w + (group_w - inner) / 2
        for si, s in enumerate(series):
            v = s.values[gi] if gi < len(s.values) else None
            bx = gx + si * bar_w
            if v is None:
                out.append(_text(bx + bar_w / 2 - 1.5, top + plot_h - 6, "—",
                                 size=11, anchor="middle"))
                continue
            y = y_of(v)
            out.append(_rect(bx, y, max(bar_w - 4, 2), top + plot_h - y, s.colour))
            if label_bars:
                out.append(_text(bx + (bar_w - 4) / 2, y - 5, _axis_label(v, fmt),
                                 size=9, anchor="middle"))
        out.append(_text(left + gi * group_w + group_w / 2, top + plot_h + 20,
                         _trunc(cat, 26), size=11, anchor="middle", sans=True,
                         fill=INK))
    return _svg(width, height, title, desc, "".join(out))


def _hbar(categories: Sequence[str], series: Sequence[_Series], *, fmt: str,
          title: str, desc: str, diverging: bool = False,
          italic_rows: frozenset[int] = frozenset(),
          bar_colours: Sequence[Sequence[str]] | None = None,
          width: float = 760.0, label_w: float = 236.0,
          label_size: float = 10.5) -> str:
    """Chart (b): horizontal bars — ``indexAxis:'y``' without the library.

    ``diverging`` puts zero inside the plot and draws it dark, which is what the
    movers chart needs: bar direction is the sign, bar colour is whether the
    move helped.
    """
    n_ser = max(len(series), 1)
    row_h = 26.0 if n_ser == 1 else 34.0
    top = 34.0 if n_ser > 1 else 12.0
    bottom = 34.0
    height = top + max(len(categories), 1) * row_h + bottom
    right = 74.0
    plot_w = width - label_w - right

    flat = [v for s in series for v in s.values if v is not None]
    if diverging:
        lo = min([0.0] + flat)
        hi = max([0.0] + flat)
        span = _nice_max(max(abs(lo), abs(hi))) or 1.0
        lo, hi = -span, span

        def x_of(v: float) -> float:
            return label_w + (v - lo) / (hi - lo) * plot_w
    else:
        hi = _nice_max(max(flat)) if flat else 1.0
        lo = 0.0

        def x_of(v: float) -> float:
            return label_w + (v / hi) * plot_w

    out: list[str] = []
    if n_ser > 1:
        out.append(_legend(label_w, 18, [(s.label, s.colour) for s in series]))

    tick_values = ([-hi + (2 * hi) * i / 4 for i in range(5)] if diverging
                   else _ticks(hi))
    plot_bottom = top + len(categories) * row_h
    for t in tick_values:
        x = x_of(t)
        out.append(_line(x, top - 4, x, plot_bottom, GRID))
        out.append(_text(x, plot_bottom + 18, _axis_label(t, fmt), size=9.5,
                         anchor="middle"))
    zero_x = x_of(0.0)
    out.append(_line(zero_x, top - 4, zero_x, plot_bottom, INK, 1.5))

    for ci, cat in enumerate(categories):
        y0 = top + ci * row_h
        lines = _wrap2(cat, int((label_w - 20) / (label_size * 0.57)))
        mid = y0 + row_h / 2 + 3.5
        offsets = [0.0] if len(lines) == 1 else [-5.5, 5.5]
        for line, dy in zip(lines, offsets):
            out.append(_text(label_w - 12, mid + dy, line, size=label_size,
                             anchor="end", sans=True, fill=INK,
                             italic=ci in italic_rows))
        sub_h = (row_h - 12) / n_ser
        for si, s in enumerate(series):
            v = s.values[ci] if ci < len(s.values) else None
            by = y0 + 6 + si * sub_h
            colour = s.colour
            if bar_colours is not None:
                colour = bar_colours[si][ci]
            if v is None:
                out.append(_text(zero_x + 6, by + sub_h / 2 + 3.5, "—", size=11))
                continue
            x1, x2 = min(zero_x, x_of(v)), max(zero_x, x_of(v))
            out.append(_rect(x1, by, max(x2 - x1, 1.5), max(sub_h - 2, 3), colour))
            label = (f"{v:+.1f}%" if diverging else _axis_label(v, fmt))
            if v >= 0:
                out.append(_text(x2 + 6, by + sub_h / 2 + 3.5, label, size=9.5))
            else:
                out.append(_text(x1 - 6, by + sub_h / 2 + 3.5, label, size=9.5,
                                 anchor="end"))
    return _svg(width, height, title, desc, "".join(out))


def _axis_cap(values: Sequence[float], benchmark: float) -> tuple[float, bool]:
    """Where to stop the ratio axis, and whether anything got clipped.

    Reference is the **median**, not the largest non-outlier — Website is an
    outlier in both columns of a comparison, so a rule that discards one still
    leaves the other flattening everything else to a sliver.
    """
    vs = sorted(values)
    if not vs:
        return _nice_max(benchmark * 2), False
    mid = len(vs) // 2
    median = vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2
    ref = max(median, benchmark)
    if vs[-1] > ref * 3:
        return _nice_max(ref * 2.0), True
    return _nice_max(vs[-1]), False


def _torn_top(x: float, y: float, w: float) -> str:
    """The mark on a bar that runs past the axis: a zigzag in the panel colour
    across its top, so a clipped bar can never be misread as a measured one."""
    d = (f"M{_n(x)},{_n(y + 13)} L{_n(x + w * 0.25)},{_n(y + 5)} "
         f"L{_n(x + w * 0.5)},{_n(y + 13)} L{_n(x + w * 0.75)},{_n(y + 5)} "
         f"L{_n(x + w)},{_n(y + 13)} L{_n(x + w)},{_n(y - 2)} L{_n(x)},{_n(y - 2)} Z")
    return f'<path d="{d}" fill="{PANEL_BG}"/>'


def _ratio_vbar(categories: Sequence[str], series: Sequence[_Series], *,
                title: str, desc: str, benchmark: float = BREAK_EVEN,
                width: float = 760.0, height: float = 310.0) -> tuple[str, list[str]]:
    """Chart (c): vertical bars self-coloured against a benchmark.

    The benchmark gridline is drawn dark at 100% — break-even — and every bar is
    green at or above it and red below. Returns the SVG plus the list of
    channels whose bar was clipped, so the caller can print their true values.
    """
    top, bottom, left, right = 42.0, 48.0, 66.0, 20.0
    plot_w, plot_h = width - left - right, height - top - bottom
    flat = [v for s in series for v in s.values if v is not None]
    cap, clipped_any = _axis_cap(flat, benchmark)

    def y_of(v: float) -> float:
        return top + plot_h - (min(v, cap) / cap) * plot_h

    out: list[str] = []
    if len(series) > 1:
        # Shade separates the columns; hue stays reserved for the benchmark, so
        # the legend swatches are one colour at two opacities.
        out.append(_legend(left, 18, [(series[0].label, POS, 0.55),
                                      (series[1].label, POS, 1.0)]))
    for t in _ticks(cap):
        y = y_of(t)
        out.append(_line(left, y, left + plot_w, y, GRID))
        out.append(_text(left - 8, y + 3.5, _axis_label(t, PCT), size=9.5, anchor="end"))
    out.append(_line(left, top + plot_h, left + plot_w, top + plot_h, PALETTE["line"]))

    n_cat, n_ser = max(len(categories), 1), max(len(series), 1)
    group_w = plot_w / n_cat
    inner = min(group_w * 0.7, n_ser * 54.0)
    bar_w = inner / n_ser
    clipped: list[str] = []
    for gi, cat in enumerate(categories):
        gx = left + gi * group_w + (group_w - inner) / 2
        for si, s in enumerate(series):
            v = s.values[gi] if gi < len(s.values) else None
            bx = gx + si * bar_w
            bw = max(bar_w - 4, 2)
            if v is None:
                out.append(_text(bx + bw / 2, top + plot_h - 6, "—", size=11,
                                 anchor="middle"))
                continue
            colour = POS if v >= benchmark else NEG
            y = y_of(v)
            out.append(_rect(bx, y, bw, top + plot_h - y, colour,
                             opacity=0.55 if (n_ser > 1 and si == 0) else None))
            if v > cap:
                out.append(_torn_top(bx, y, bw))
                clipped.append(f"{cat}{'' if n_ser == 1 else ' ' + s.label} {v:.1f}%")
            out.append(_text(bx + bw / 2, y - 5, f"{v:.0f}%", size=9, anchor="middle"))
        out.append(_text(left + gi * group_w + group_w / 2, top + plot_h + 20,
                         _trunc(cat, 22), size=11, anchor="middle", sans=True,
                         fill=INK))

    y_be = y_of(benchmark)
    out.append(_line(left, y_be, left + plot_w, y_be, INK, 1.6))
    out.append(_text(left + plot_w, y_be - 6, f"break-even {benchmark:.0f}%", size=9.5,
                     anchor="end", fill=INK, weight="600"))
    note: list[str] = []
    if clipped_any and clipped:
        note.append("Axis capped at %s so the channels near break-even stay readable. "
                    "Bars drawn with a torn top run past it: %s."
                    % (_axis_label(cap, PCT), "; ".join(clipped)))
    return _svg(width, height, title, desc, "".join(out)), note


# --- stylesheet --------------------------------------------------------------
# Everything below the :root block is literal: one shell, and the compositions
# differ only in which sections are emitted.

_CSS_BODY = """
*{box-sizing:border-box}
html,body,*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
@page{size:A4;margin:13mm 11mm}
body{margin:0;background:var(--paper);color:var(--ink);font-family:__SANS__;
 line-height:1.55;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
h1,h2,h3{font-family:__SERIF__;font-weight:600;line-height:1.08;margin:0}
.mono,.card .v,table.cmp tbody td:not(:first-child),.gtable tbody td{
 font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.mono{font-family:__MONO__}
.cover{color:var(--paper);background:var(--ink);padding:58px 0 50px;position:relative;overflow:hidden}
.cover::after{content:"";position:absolute;right:-120px;top:-120px;width:420px;height:420px;
 background:radial-gradient(circle,rgba(201,162,39,.22),transparent 62%)}
.eyebrow{font-family:__MONO__;letter-spacing:.32em;text-transform:uppercase;font-size:11px;
 color:var(--gold);margin-bottom:18px}
.cover h1{font-size:clamp(34px,5.5vw,60px);letter-spacing:-.02em;max-width:20ch;position:relative}
.cover h1 em{font-style:italic;color:var(--gold-soft)}
.cover .sub{margin-top:16px;color:#C3CBDA;max-width:64ch;font-size:16.5px;position:relative}
.cover-meta{display:flex;flex-wrap:wrap;gap:32px;margin-top:34px;padding-top:22px;
 border-top:1px solid rgba(255,255,255,.16);position:relative}
.cover-meta div span{display:block;font-family:__MONO__;font-size:11px;letter-spacing:.14em;
 text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.cover-meta div b{font-weight:600;font-size:16px;font-family:__MONO__}
section{padding:48px 0}
.kicker{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.kicker .num{font-family:__MONO__;font-size:12px;color:var(--gold);border:1px solid var(--gold);
 border-radius:100px;padding:4px 11px;letter-spacing:.1em}
.kicker h2{font-size:clamp(22px,3.1vw,31px);letter-spacing:-.01em}
.sublabel{font-family:__MONO__;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
 color:var(--slate);margin:26px 0 12px;display:flex;align-items:center;gap:12px}
.sublabel::after{content:"";flex:1;height:1px;background:var(--line)}
.lead{font-size:17px;max-width:78ch;color:var(--ink-soft)}
.lead strong{color:var(--ink)}
.score{display:grid;grid-template-columns:repeat(5,1fr);gap:13px}
.card{background:#fff;border:1px solid var(--line);border-radius:13px;padding:17px;box-shadow:var(--shadow)}
.card .t{font-family:__MONO__;font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;
 color:var(--slate);min-height:26px;line-height:1.3}
.card .v{font-family:__SERIF__;font-size:25px;font-weight:600;margin-top:7px;letter-spacing:-.01em}
.card .b{font-family:__MONO__;font-size:9px;line-height:1.35;color:var(--slate);margin-top:7px;
 padding-top:7px;border-top:1px dotted var(--line)}
.card.hl{background:var(--ink);color:var(--paper)}
.card.hl .t{color:var(--gold-soft)} .card.hl .b{color:#C3CBDA;border-top-color:rgba(255,255,255,.22)}
.card.na{background:#fff;border-color:var(--gold-soft)} .card.na .t{color:#9A7D1E}
.card.na .v{color:var(--ink)}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:22px;box-shadow:var(--shadow)}
.panel.full{grid-column:1/-1}
.panel h3{font-size:18px;margin-bottom:4px}
.panel .cap{font-size:12.5px;color:var(--slate);margin:0 0 14px}
.chart-box{width:100%}
svg.chart{display:block;width:100%;height:auto}
svg.chart text{font-family:__MONO__;font-variant-numeric:tabular-nums;
 font-feature-settings:"tnum" 1}
svg.chart text.s{font-family:__SANS__}
.chart-note{font-family:__MONO__;font-size:10.5px;line-height:1.5;color:var(--slate);
 margin-top:12px;padding-top:10px;border-top:1px solid var(--paper-2)}
.twrap{overflow-x:auto;border:1px solid var(--line);border-radius:14px;background:#fff;box-shadow:var(--shadow)}
table.cmp{border-collapse:collapse;width:100%;font-size:13px;min-width:820px}
table.cmp th,table.cmp td{padding:10px 12px;text-align:right;white-space:nowrap;
 border-bottom:1px solid var(--paper-2)}
table.cmp th:first-child,table.cmp td:first-child{text-align:left;position:sticky;left:0;
 background:#fff;z-index:1;border-right:1px solid var(--line);min-width:290px;white-space:normal}
table.cmp thead th{background:var(--ink);color:var(--paper);font-family:__MONO__;font-size:11px;
 letter-spacing:.04em;text-transform:uppercase;font-weight:500}
table.cmp thead th.ac{background:var(--ink-soft)}
table.cmp thead th.bc{background:var(--gold);color:var(--ink);font-weight:600}
table.cmp tbody td:not(:first-child){font-family:__MONO__}
table.cmp td.ac{color:var(--slate)}
table.cmp td.bc{background:rgba(201,162,39,.08);font-weight:600}
table.cmp td.dc{font-weight:600}
table.cmp tr.group td{background:var(--ink-soft);color:var(--paper);font-family:__MONO__;
 font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
table.cmp tr.group td:first-child{background:var(--ink-soft);color:var(--paper)}
table.cmp tr.untracked td{background:var(--paper-2);font-style:italic}
table.cmp tr.untracked td:first-child{background:var(--paper-2);border-left:3px solid var(--gold)}
.basis{font-family:__MONO__;font-size:9.5px;line-height:1.4;color:var(--slate);
 margin-top:4px;font-style:normal;white-space:normal}
.glance{background:var(--paper-2);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.gtable{width:100%;border-collapse:collapse;font-size:15px}
.gtable th,.gtable td{padding:15px 14px;text-align:right;border-bottom:1px solid var(--line)}
.gtable th:first-child,.gtable td:first-child{text-align:left;font-weight:500}
.gtable thead th{font-family:__MONO__;font-size:11px;letter-spacing:.12em;text-transform:uppercase;
 color:var(--slate);border-bottom:2px solid var(--ink)}
.gtable tbody td{font-family:__MONO__} .gtable tbody td:first-child{font-family:__SANS__}
.gtable .dcell{font-weight:600} .gtable tr:last-child td{border-bottom:none}
.up{color:var(--pos)} .down{color:var(--neg)} .neu{color:var(--slate)}
.rise::before{content:"\\25B2";font-size:10px;margin-right:5px}
.fall::before{content:"\\25BC";font-size:10px;margin-right:5px}
.flat::before{content:"\\2013";font-size:10px;margin-right:5px}
.absent{color:var(--slate);font-weight:600;letter-spacing:.04em;border-bottom:1px dotted var(--gold);
 cursor:help}
.ins{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.ins.one{grid-template-columns:1fr}
.ins .col{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px;box-shadow:var(--shadow)}
.ins .col.win{border-top:3px solid var(--pos)} .ins .col.miss{border-top:3px solid var(--neg)}
.ins h3{font-size:20px;margin-bottom:4px}
.ins .tag{font-family:__MONO__;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
 margin-bottom:14px;display:block}
.ins .tag.win{color:var(--pos)} .ins .tag.miss{color:var(--neg)}
.ins ul{margin:0;padding:0}
.ins li{list-style:none;padding:11px 0 11px 26px;position:relative;
 border-bottom:1px solid var(--paper-2);font-size:14.5px}
.ins li:last-child{border-bottom:none}
.ins li::before{position:absolute;left:2px;top:11px;font-family:__MONO__;font-weight:700}
.win li::before{content:"+";color:var(--pos)} .miss li::before{content:"\\2212";color:var(--neg)}
.ins .metric{font-family:__MONO__;font-weight:600}
.win .metric{color:var(--pos)} .miss .metric{color:var(--neg)}
.note{font-size:12.5px;color:var(--slate);background:var(--paper-2);border-left:3px solid var(--gold);
 padding:16px 20px;border-radius:0 10px 10px 0;max-width:82ch;margin-top:20px}
.note b{color:var(--ink)}
.gaps{margin-top:20px}
.gaps h3{font-size:17px;margin-bottom:10px}
.gaps .grp{margin-top:16px}
.gaps .grp>span{font-family:__MONO__;font-size:10px;letter-spacing:.14em;text-transform:uppercase;
 color:var(--slate);display:block;margin-bottom:6px}
.gaps ul{margin:0;padding-left:18px;font-size:13px;color:var(--ink-soft)}
.gaps li{padding:3px 0}
.gaps code{font-family:__MONO__;font-size:12px;color:var(--ink)}
footer{background:var(--ink);color:#9AA5BB;padding:32px 0;font-size:12.5px;margin-top:16px}
footer b{color:var(--paper)}
@media(max-width:900px){.score{grid-template-columns:repeat(2,1fr)}
 .chart-grid,.ins{grid-template-columns:1fr}.cover h1{font-size:34px}}
@media print{
 body{background:#fff}
 .cover::after{display:none}
 .cover{padding:34px 0 28px}
 section{padding:22px 0}
 .panel,.card,.ins .col{box-shadow:none;break-inside:avoid}
 .twrap{box-shadow:none;overflow:visible;border-radius:0}
 thead{display:table-header-group}
 tfoot{display:table-footer-group}
 tr{break-inside:avoid;page-break-inside:avoid}
 table.cmp{min-width:0;font-size:9.5px}
 table.cmp th,table.cmp td{padding:5px 6px;white-space:normal}
 table.cmp th:first-child,table.cmp td:first-child{position:static;min-width:0}
 table.cmp thead th{position:static}
 .twrap.wide table.cmp{font-size:8.5px}
 .twrap.wide table.cmp th,.twrap.wide table.cmp td{padding:4px 4px}
 .kicker,.sublabel{break-after:avoid}
 .panel{break-inside:auto}
 .chart-box,.score{break-inside:avoid}
}
"""

CSS = (
    ":root{"
    + ";".join(f"--{k}:{v}" for k, v in PALETTE.items())
    + ";--shadow:0 1px 2px rgba(20,33,58,.06),0 8px 30px rgba(20,33,58,.06)}"
    + _CSS_BODY.replace("__SANS__", SANS).replace("__SERIF__", SERIF).replace("__MONO__", MONO)
)


# --- ledger helpers ----------------------------------------------------------

def _by_key(ledger: ReportLedger) -> dict[str, LedgerRow]:
    return {r.key: r for r in ledger.rows}


def is_single_period(ledger: ReportLedger) -> bool:
    """Two columns naming the same period is the one-period composition."""
    return ledger.periods[0].key == ledger.periods[1].key


def single_period(rollup: PeriodRollup) -> ReportLedger:
    """The ledger for a one-period report.

    :class:`ReportLedger` is always two columns wide, so a standalone report is
    that period against itself: every delta is zero, every ``improved`` is
    ``None``, and :func:`render` picks the one-period section list.
    """
    return compare(rollup, rollup)


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month_name(key: str) -> str:
    """``2026-01`` as ``Jan 2026``, for a document a client reads.

    Anything that does not parse as ``YYYY-MM`` is printed as it came: a period
    is any list of month keys, and inventing a name for one this does not
    recognise would be worse than showing the key.
    """
    parts = key.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return key
    month = int(parts[1])
    if not 1 <= month <= 12:
        return key
    return f"{_MONTH_NAMES[month - 1]} {int(parts[0])}"


def _months_label(period) -> str:
    """The window a column covers — ``Jan – Mar 2026``, or the raw keys when
    they do not parse. The year is printed once when both ends share it."""
    months = period.months
    first, last = _month_name(months[0]), _month_name(months[-1])
    if len(months) == 1:
        return first
    if " " in first and first.rsplit(" ", 1)[1] == last.rsplit(" ", 1)[-1]:
        return f"{first.rsplit(' ', 1)[0]} – {last}"
    return f"{first} – {last}"


def _channel_side(row: ChannelRow, second: bool) -> ChannelTotals | None:
    return row.b if second else row.a


def _absent_rows(ledger: ReportLedger, two: bool) -> list[str]:
    """Every published row with a hole in it, named for the data-gaps panel."""
    out: list[str] = []
    a_label, b_label = ledger.columns
    for row in ledger.rows:
        if row.a is None and row.b is None:
            out.append(f"{row.label} — not reported"
                       + (" in either period" if two else ""))
        elif two and row.a is None:
            out.append(f"{row.label} — not reported for {a_label}")
        elif two and row.b is None:
            out.append(f"{row.label} — not reported for {b_label}")
    return out


# --- document parts ----------------------------------------------------------

def _head(title: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{_esc(title)}</title><style>{CSS}</style></head><body>"
    )


def _cover(ledger: ReportLedger, *, two: bool, brand: str | None,
           prose: ReportProse | None) -> str:
    a_label, b_label = ledger.columns
    rows = _by_key(ledger)
    eyebrow = f"{brand} · Growth marketing" if brand else "Growth marketing"
    if two:
        heading = f"{_esc(a_label)} vs {_esc(b_label)} <em>— the shift</em>"
        rev, cac = rows.get("revenue_amount_sold"), rows.get(CAC_KEY)
        shift_bits = []
        if rev is not None and rev.change_pct is not None:
            shift_bits.append(f"Revenue {rev.change_pct:+.1f}%")
        if cac is not None and cac.change_pct is not None:
            shift_bits.append(f"CAC {cac.change_pct:+.1f}%")
        shift = _esc(" · ".join(shift_bits)) if shift_bits else ABSENT
        meta = [
            (a_label, _esc(_months_label(ledger.periods[0]))),
            (b_label, _esc(_months_label(ledger.periods[1]))),
            ("Headline shift", shift),
            ("View", "All sources"),
        ]
    else:
        heading = f"{_esc(a_label)} <em>performance</em>"
        meta = [
            ("Period", _esc(_months_label(ledger.periods[0]))),
            ("Spend", _fmt_compact(rows["spend"].a, MONEY)),
            ("Revenue sold (act.)", _fmt_compact(rows["revenue_amount_sold"].a, MONEY)),
            ("ROAS (act.)", _fmt_compact(rows["roas_pct"].a, PCT)),
        ]
    sub = ""
    if prose is not None and prose.thesis:
        sub = f'<p class="sub">{_esc(prose.thesis)}</p>'
    slots = "".join(f"<div><span>{_esc(k)}</span><b>{v}</b></div>" for k, v in meta)
    return (
        '<header class="cover"><div class="wrap">'
        f'<div class="eyebrow">{_esc(eyebrow)}</div><h1>{heading}</h1>{sub}'
        f'<div class="cover-meta">{slots}</div></div></header>'
    )


def _kicker(num: str, heading: str) -> str:
    return (f'<div class="kicker"><span class="num">{_esc(num)}</span>'
            f"<h2>{_esc(heading)}</h2></div>")


def _glance(ledger: ReportLedger) -> str:
    """Section 00 — the eight headline rows.

    The change cell carries two independent classes: ``up``/``down``/``neu`` for
    colour, ``rise``/``fall``/``flat`` for the arrow. Spend falling 21.8% is
    therefore green **and** ▼, which is the defect the templates shipped.
    """
    a_label, b_label = ledger.columns
    rows = _by_key(ledger)
    body: list[str] = []
    for key in GLANCE_KEYS:
        row = rows.get(key)
        if row is None:
            continue
        change = _fmt_delta(row) if row.format == PCT else _fmt_change(row)
        cls = f"dcell {_tone(row.improved)} {_glyph(row.delta)}"
        body.append(
            f"<tr><td>{_esc(row.label)}</td>"
            f"<td>{_fmt(row.a, row.format)}</td><td>{_fmt(row.b, row.format)}</td>"
            f'<td class="{cls}">{change}</td></tr>'
        )
    return (
        '<div class="glance"><div class="wrap" style="padding-top:32px;padding-bottom:32px">'
        + _kicker("00", "At a glance")
        + '<table class="gtable"><thead><tr><th>Metric</th>'
        + f"<th>{_esc(a_label)}</th><th>{_esc(b_label)}</th><th>Change</th></tr></thead>"
        + "<tbody>" + "".join(body) + "</tbody></table>"
        + '<p style="font-size:13px;color:var(--slate);margin-top:14px">Colour is whether the '
          "move was favourable — cost and CAC coming <em>down</em> is green. The arrow is the "
          "direction of the move. An em-dash means the figure was not reported; it is not zero."
          "</p>"
        + '<div class="chart-grid" style="margin-top:22px">'
        + _period_bar(ledger, two=True) + "</div></div></div>"
    )


def _disambiguated(ledger: ReportLedger) -> dict[str, str]:
    """Chart labels that stay distinct once the group bands are gone.

    Two catalog labels appear twice — the Projected and Paying not-actualized
    blocks publish identically-named rows and the ledger table tells them apart
    by the band above them. A chart has no bands, so the group qualifies the
    label there instead of three different rows sharing one bar name.
    """
    counts: dict[str, int] = {}
    for row in ledger.rows:
        counts[row.label] = counts.get(row.label, 0) + 1
    out: dict[str, str] = {}
    for row in ledger.rows:
        if counts[row.label] > 1:
            out[row.key] = f"{row.label} · {row.group.split(' — ')[0]}"
        else:
            out[row.key] = row.label
    return out


def _movers_section(ledger: ReportLedger) -> str:
    """Section 01 — every comparable row ranked, drawn as a diverging bar."""
    eligible = [r for r in ledger.rows if r.change_pct is not None and r.improved is not None]
    by_size = sorted(eligible, key=lambda r: abs(r.change_pct or 0.0), reverse=True)
    half = MOVERS_LIMIT // 2
    ups = [r for r in by_size if r.improved][:half]
    downs = [r for r in by_size if not r.improved][:half]
    shown = sorted(ups + downs, key=lambda r: r.change_pct or 0.0, reverse=True)
    a_label, b_label = ledger.columns
    if not shown:
        chart = ('<p class="cap">No row moved between the two columns, so there is nothing '
                 "to rank.</p>")
        note = ""
    else:
        colours = [[POS if r.improved else NEG for r in shown]]
        names = _disambiguated(ledger)
        chart = _hbar(
            [names[r.key] for r in shown],
            [_Series("change", SLATE, tuple(r.change_pct for r in shown))],
            fmt=PCT, diverging=True, bar_colours=colours,
            label_w=272.0, label_size=9.5,
            title=f"Percent change {a_label} to {b_label}",
            desc="Bar direction is the sign of the move; bar colour is whether the move "
                 "was favourable.",
        )
        note = (f'<p class="chart-note">The {len(ups)} largest improvements and '
                f"{len(downs)} largest deteriorations of {len(eligible)} comparable rows. "
                "Both directions are drawn on purpose: ranking the whole set by size alone "
                "fills this chart with revenue increases and drops every row that got "
                "worse. Rows with a missing figure in either column, and rows the report "
                "never colours (Budget, the revenue goal), are not rankable and are absent "
                "here — all of them are in the ledger below.</p>")
    return (
        "<section><div class=\"wrap\">"
        + _kicker("01", "Biggest movers")
        + f'<p class="lead" style="margin-bottom:20px">Percent change {_esc(a_label)} → '
          f"{_esc(b_label)}. <strong>Bar direction is the sign; colour is whether it helped "
          "(green) or hurt (red)</strong> — so CAC falling shows left and green.</p>"
        + f'<div class="panel full"><div class="chart-box">{chart}</div>{note}</div>'
        + "</div></section>"
    )


def _ledger_section(ledger: ReportLedger, num: str = "02") -> str:
    """Section 02 — all 38 published rows under their seven group bands."""
    a_label, b_label = ledger.columns
    body: list[str] = []
    current = None
    for row in ledger.rows:
        if row.group != current:
            current = row.group
            body.append(f'<tr class="group"><td colspan="5">{_esc(row.group)}</td></tr>')
        basis = f'<div class="basis">{_esc(row.basis)}</div>' if row.basis else ""
        cls = _tone(row.improved)
        body.append(
            f"<tr><td>{_esc(row.label)}{basis}</td>"
            f'<td class="ac">{_fmt(row.a, row.format)}</td>'
            f'<td class="bc">{_fmt(row.b, row.format)}</td>'
            f'<td class="dc {cls}">{_fmt_delta(row)}</td>'
            f'<td class="dc {cls}">{_fmt_change(row)}</td></tr>'
        )
    return (
        '<section style="padding-top:0"><div class="wrap">'
        + _kicker(num, "Full comparison ledger")
        + '<p class="lead" style="margin-bottom:20px">Every published metric, both columns, '
          "with change and direction — actualized, not-actualized and inbound pipeline all "
          "included.</p>"
        + '<div class="twrap"><table class="cmp"><thead><tr><th>Metric</th>'
        + f'<th class="ac">{_esc(a_label)}</th><th class="bc">{_esc(b_label)}</th>'
        + "<th>&Delta;</th><th>% change</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + '<div class="note">For percentage metrics the &Delta; column is percentage points '
          "(pp) and “% change” is the relative move. <b>An em-dash is an absent "
          "figure, not a zero</b> — the rows it appears in are listed in the basis panel "
          "below. Rows carrying a small grey line under their label state the denominator "
          "they use, because the neighbouring row uses a different one.</div>"
        + "</div></section>"
    )


def _channel_table(ledger: ReportLedger, *, two: bool) -> str:
    a_label, b_label = ledger.columns
    head = ["<th>Channel</th>"]
    if two:
        for measure in ("Spend", "Revenue", "ROAS", "CAC"):
            head.append(f'<th class="ac">{_esc(measure)} {_esc(a_label)}</th>')
            head.append(f'<th class="bc">{_esc(measure)} {_esc(b_label)}</th>')
    else:
        head += ["<th>Spend</th>", "<th>Revenue</th>", "<th>Clients</th>",
                 "<th>ROAS</th>", "<th>CAC</th>"]
    cols = 9 if two else 6

    body: list[str] = []
    for ch in ledger.channels:
        untracked = ch.channel == UNTRACKED_LABEL
        tr = ' class="untracked"' if untracked else ""
        cells = [f"<td>{_esc(ch.channel)}</td>"]
        if two:
            a, b = ch.a, ch.b
            for attr, fmt in (("spend", MONEY), ("revenue", MONEY),
                              ("roas_pct", PCT), ("cac", MONEY)):
                cells.append('<td class="ac">%s</td>'
                             % _fmt(getattr(a, attr) if a else None, fmt))
                cells.append('<td class="bc">%s</td>'
                             % _fmt(getattr(b, attr) if b else None, fmt))
        else:
            side = ch.a or ch.b
            for attr, fmt in (("spend", MONEY), ("revenue", MONEY), ("clients", INT),
                              ("roas_pct", PCT), ("cac", MONEY)):
                cells.append("<td>%s</td>" % _fmt(getattr(side, attr) if side else None, fmt))
        body.append(f"<tr{tr}>{''.join(cells)}</tr>")
    if not body:
        body.append(f'<tr><td colspan="{cols}">No per-channel figures for this period '
                    "— attribution cannot be reconciled.</td></tr>")
    return ('<div class="twrap wide" style="margin-bottom:22px"><table class="cmp"><thead><tr>'
            + "".join(head) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")


def _untracked_note(ledger: ReportLedger, *, two: bool) -> str:
    """The reconciliation gap, in words, next to the row that carries it."""
    a_label, b_label = ledger.columns
    parts: list[str] = []
    sides = list(zip(ledger.columns, ledger.untracked))
    if not two:
        sides = sides[:1]
    for label, u in sides:
        if u is None:
            continue
        spend = ABSENT if u.spend is None else _esc(_money(u.spend))
        share = "" if u.spend_pct is None else _esc(f" ({u.spend_pct:.1f}% of spend)")
        clients = ABSENT if u.clients is None else _esc(_count(u.clients))
        revenue = ABSENT if u.revenue is None else _esc(_money(u.revenue))
        parts.append(f"<b>{_esc(label)}:</b> {spend}{share}, {revenue} revenue and "
                     f"{clients} clients sit outside the tracked channels.")
    if not parts:
        return ('<div class="note"><b>Attribution:</b> no reconciliation is available for '
                "this period, because the per-channel figures are incomplete. The channel "
                "table is therefore not a breakdown of the totals.</div>")
    trend = ""
    ua, ub = ledger.untracked
    if two and ua and ub and ua.spend_pct is not None and ub.spend_pct is not None:
        direction = "widening" if ub.spend_pct > ua.spend_pct else "narrowing"
        trend = (f" The unattributed share is {direction}: {ua.spend_pct:.1f}% "
                 f"→ {ub.spend_pct:.1f}%.")
    return ('<div class="note"><b>Attribution — “Other / untracked” is a row, '
            "not a footnote.</b> The tracked channels do not sum to the All-Sources totals. "
            + " ".join(parts) + _esc(trend) + " Read the channel rows as shares, not as the "
            "total.</div>")


def _channel_charts(ledger: ReportLedger, *, two: bool) -> str:
    """Revenue by channel (horizontal) and ROAS by channel (break-even vertical)."""
    if not ledger.channels:
        return ""
    names = [c.channel for c in ledger.channels]
    italics = frozenset(i for i, n in enumerate(names) if n == UNTRACKED_LABEL)
    a_label, b_label = ledger.columns

    def measure(second: bool, attr: str) -> tuple[float | None, ...]:
        return tuple(getattr(_channel_side(c, second), attr, None)
                     if _channel_side(c, second) else None for c in ledger.channels)

    rev_series = [_Series(a_label, SERIES_A, measure(False, "revenue"))]
    if two:
        rev_series.append(_Series(b_label, SERIES_B, measure(True, "revenue")))
    rev_chart = _hbar(names, rev_series, fmt=MONEY, italic_rows=italics, label_w=160.0,
                      title="Revenue sold by channel",
                      desc="Actualized revenue sold, by source, including the untracked "
                           "remainder.")

    # The untracked remainder is money, not a channel's return: dividing the
    # revenue nobody attributed by the spend nobody attributed produces a ratio
    # that is arithmetically true and editorially meaningless, and including it
    # drags the median the axis is scaled from. It stays in the revenue chart,
    # where the requirement is that the money is visible, and out of this one.
    keep = [i for i, n in enumerate(names) if n != UNTRACKED_LABEL]
    roas_names = [names[i] for i in keep]
    roas_series = [_Series(a_label, SERIES_A,
                           tuple(measure(False, "roas_pct")[i] for i in keep))]
    if two:
        roas_series.append(_Series(b_label, SERIES_B,
                                   tuple(measure(True, "roas_pct")[i] for i in keep)))
    roas_chart, roas_note = _ratio_vbar(
        roas_names, roas_series,
        title="ROAS by channel against break-even",
        desc="Actualized revenue divided by spend, by source. The dark line is break-even "
             "at 100%.")
    roas_note.append("Bars at or above break-even are green, below it red.")
    if len(keep) != len(names):
        roas_note.append(
            "“Other / untracked” is a residual, not a channel’s return, so it "
            "is charted in revenue above and left out here. Its figures are in the table.")
    if two:
        roas_note.append(f"{a_label} is the lighter bar of each pair.")
    return (
        '<div class="chart-grid" style="margin-top:20px">'
        f'<div class="panel"><h3>Revenue by channel</h3><p class="cap">Actualized revenue sold '
        f'by source — the untracked remainder is a bar, not a footnote.</p>'
        f'<div class="chart-box">{rev_chart}</div></div>'
        f'<div class="panel"><h3>ROAS by channel</h3><p class="cap">Actualized revenue '
        f'÷ spend, against break-even.</p>'
        f'<div class="chart-box">{roas_chart}</div>'
        f'<p class="chart-note">{_esc(" ".join(roas_note))}</p></div>'
        "</div>"
    )


def _period_bar(ledger: ReportLedger, *, two: bool) -> str:
    """Chart (a) — spend against actualized and not-actualized revenue.

    Drawn per **column**, not per month: :class:`ReportLedger` carries period
    totals and never the monthly cells the roll-up summed, so a by-month chart
    would have to invent its data. The month keys are printed under each column
    so a reader knows what the bar covers.
    """
    rows = _by_key(ledger)
    cats = []
    for i, label in enumerate(ledger.columns):
        cats.append(f"{label} ({_months_label(ledger.periods[i])})")
    if not two:
        cats = cats[:1]

    def vals(key: str) -> tuple[float | None, ...]:
        row = rows.get(key)
        if row is None:
            return tuple(None for _ in cats)
        return (row.a, row.b)[: len(cats)] if two else (row.a,)

    series = [
        _Series("Spend", SLATE, vals("spend")),
        _Series("Revenue sold (actualized)", GOLD, vals("revenue_amount_sold")),
        _Series("Revenue sold (not actualized)", INK,
                vals("revenue_amount_sold_not_actualized")),
    ]
    chart = _grouped_vbar(cats, series, fmt=MONEY,
                          title="Spend against revenue sold",
                          desc="Spend, actualized revenue sold and not-actualized revenue "
                               "sold, per reported period.")
    return (
        '<div class="panel full"><h3>Spend vs revenue sold</h3>'
        '<p class="cap">Actualized and not-actualized revenue against spend.</p>'
        f'<div class="chart-box">{chart}</div>'
        '<p class="chart-note">Period totals. Monthly detail is not part of the ledger this '
        "page renders, so it is not drawn rather than inferred. A bar replaced by an em-dash "
        "is an absent figure, not a zero.</p></div>"
    )


def _score_band(ledger: ReportLedger, cards: Sequence[tuple[str, str]]) -> str:
    rows = _by_key(ledger)
    out: list[str] = []
    for key, extra in cards:
        row = rows.get(key)
        if row is None:
            continue
        notes = []
        if row.a is None:
            notes.append("not reported for this period — this is absent, not zero")
        if row.basis:
            notes.append(row.basis)
        basis = (f'<div class="b">{_esc(" · ".join(notes))}</div>') if notes else ""
        cls = ("card " + extra).strip()
        out.append(
            f'<div class="{cls}"><div class="t">{_esc(row.label)}</div>'
            f'<div class="v mono">{_fmt_compact(row.a, row.format)}</div>{basis}</div>'
        )
    return '<div class="score">' + "".join(out) + "</div>"


def _scorecard_section(ledger: ReportLedger) -> str:
    label = ledger.columns[0]
    months = len(ledger.periods[0].months)
    return (
        "<section><div class=\"wrap\">"
        + _kicker("01", f"{label} standalone")
        + f'<p class="lead" style="margin-bottom:8px">Every headline figure for '
          f"{_esc(label)}, rolled up from {months} reported month"
          f"{'' if months == 1 else 's'}. Counts and amounts are period sums; ratios, "
          "cost-per, ROAS, CAC and conversion are recomputed from those sums rather than "
          "averaged across the months.</p>"
        + '<div class="sublabel">Actualized &amp; core</div>'
        + _score_band(ledger, SCORE_CORE)
        + '<div class="sublabel">Not actualized &amp; pipeline</div>'
        + _score_band(ledger, SCORE_PIPELINE)
        + "</div></section>"
    )


def _charts_section(ledger: ReportLedger, *, two: bool, num: str, heading: str) -> str:
    return (
        '<section style="padding-top:0"><div class="wrap">'
        + _kicker(num, heading)
        + '<div class="chart-grid">' + _period_bar(ledger, two=two) + "</div>"
        + _channel_table(ledger, two=two)
        + _untracked_note(ledger, two=two)
        + _channel_charts(ledger, two=two)
        + '<div class="note"><b>ROAS basis:</b> every ROAS figure is <b>actualized</b> revenue '
          "÷ spend. The not-actualized ROAS row uses Revenue Amount Sold ($, Not "
          "Actualized) ÷ spend. Not-actualized means sold or booked but not yet "
          "actualized.</div>"
        + "</div></section>"
    )


def _channel_shift_section(ledger: ReportLedger) -> str:
    a_label, b_label = ledger.columns
    return (
        '<section style="padding-top:0"><div class="wrap">'
        + _kicker("03", f"Channel shift · {a_label} → {b_label}")
        + '<p class="lead" style="margin-bottom:20px">Where the reallocation shows up, and '
          "how much of the spend never reaches a named channel at all.</p>"
        + _channel_table(ledger, two=True)
        + _untracked_note(ledger, two=True)
        + _channel_charts(ledger, two=True)
        + "</div></section>"
    )


def _derived_bullets(ledger: ReportLedger) -> tuple[list[str], list[str]]:
    """Win / miss bullets straight off the ledger, for when no prose is supplied.

    Column membership is decided by :attr:`LedgerRow.improved`, so a negative
    item can never land in the green column — the defect the single-period
    templates shipped, where “thin 110% ROAS” and “CAC high”
    sat under a green ``+``.
    """
    ranked = sorted((r for r in ledger.rows if r.improved is not None
                     and r.change_pct is not None),
                    key=lambda r: abs(r.change_pct or 0.0), reverse=True)
    # Same reason as the charts: with no group band above it, "Total Services
    # Sold (Not Actualized)" names two different rows.
    names = _disambiguated(ledger)

    def bullet(row: LedgerRow) -> str:
        a = row.a if row.a is not None else None
        b = row.b if row.b is not None else None
        left = "—" if a is None else (_money(a) if row.format == MONEY
                                           else _pct(a) if row.format == PCT else _count(a))
        right = "—" if b is None else (_money(b) if row.format == MONEY
                                            else _pct(b) if row.format == PCT else _count(b))
        move = (f"{row.delta:+.2f} pp" if row.format == PCT
                else f"{row.change_pct:+.1f}%")
        return f"{names[row.key]}: {left} → {right} ({move})"

    wins = [bullet(r) for r in ranked if r.improved][:6]
    misses = [bullet(r) for r in ranked if not r.improved][:6]
    return wins, misses


def _insight_section(ledger: ReportLedger, *, two: bool,
                     prose: ReportProse | None) -> str:
    num = "04" if two else "03"
    wins = list(prose.wins) if prose else []
    misses = list(prose.misses) if prose else []
    # A standalone period has no deltas, so there is nothing to derive bullets
    # from — and a section headed "Highlights" that turns out to hold only the
    # data-gaps panel is a heading lying about its contents.
    heading = ("What improved, what dropped" if two
               else "Highlights" if (wins or misses) else "How to read this report")
    derived = False
    if two and not wins and not misses:
        wins, misses = _derived_bullets(ledger)
        derived = True
    win_head = prose.win_heading if prose else "Gains"
    miss_head = prose.miss_heading if prose else "Slippage"
    a_label, b_label = ledger.columns

    cols: list[str] = []
    if wins:
        tag = (f"Improved {a_label} → {b_label}" if two else "Notable this period")
        if derived:
            tag = "Improved · derived from the ledger"
        cols.append(
            f'<div class="col win"><span class="tag win">{_esc(tag)}</span>'
            f"<h3>{_esc(win_head)}</h3><ul>"
            + "".join(f"<li>{_esc(w)}</li>" for w in wins)
            + "</ul></div>"
        )
    if misses:
        tag = (f"Dropped {a_label} → {b_label}" if two else "Watch items")
        if derived:
            tag = "Dropped · derived from the ledger"
        cols.append(
            f'<div class="col miss"><span class="tag miss">{_esc(tag)}</span>'
            f"<h3>{_esc(miss_head)}</h3><ul>"
            + "".join(f"<li>{_esc(m)}</li>" for m in misses)
            + "</ul></div>"
        )
    block = ""
    if cols:
        one = " one" if len(cols) == 1 else ""
        block = f'<div class="ins{one}">' + "".join(cols) + "</div>"
        if derived:
            block += ('<div class="note">These bullets are arithmetic off the ledger, ranked '
                      "by size of move — not a written narrative. A row lands in the "
                      "green column only when the metric’s own polarity says the move "
                      "was favourable.</div>")
    return (
        '<section style="padding-top:0"><div class="wrap">'
        + _kicker(num, heading)
        + block
        + _basis_panel(ledger, two=two)
        + "</div></section>"
    )


def _basis_panel(ledger: ReportLedger, *, two: bool) -> str:
    """What is missing, what a number is divided by, and what could not be
    reconciled — the honesty surface, always rendered."""
    absent = _absent_rows(ledger, two)
    bases = [(r.label, r.basis) for r in ledger.rows if r.basis]
    gaps = list(ledger.gaps)

    blocks: list[str] = []
    blocks.append(
        '<div class="grp"><span>How to read an em-dash</span><ul><li>An em-dash means the '
        "figure was <b>not reported</b> for that column. It is not zero, and no default has "
        "been substituted. A period total is published only when every month in the period "
        "reports the field.</li></ul></div>"
    )
    if absent:
        blocks.append('<div class="grp"><span>Absent figures in this report</span><ul>'
                      + "".join(f"<li>{_esc(a)}</li>" for a in absent) + "</ul></div>")
    else:
        blocks.append('<div class="grp"><span>Absent figures in this report</span><ul>'
                      "<li>None — every published row reported a figure in every "
                      "column.</li></ul></div>")
    if bases:
        blocks.append(
            '<div class="grp"><span>Denominators worth stating</span><ul>'
            + "".join(f"<li><code>{_esc(label)}</code> — {_esc(basis)}</li>"
                      for label, basis in bases)
            + "</ul></div>")
    if gaps:
        blocks.append('<div class="grp"><span>Roll-up warnings</span><ul>'
                      + "".join(f"<li>{_esc(g)}</li>" for g in gaps) + "</ul></div>")
    return ('<div class="panel gaps"><h3>Basis &amp; data gaps</h3>'
            '<p class="cap">Everything this report could not measure, and every figure whose '
            "denominator differs from the row next to it.</p>" + "".join(blocks) + "</div>")


def _footer(ledger: ReportLedger, *, two: bool, brand: str | None,
            captured_on: date | None) -> str:
    a_label, b_label = ledger.columns
    who = f"{brand} — " if brand else ""
    what = f"{a_label} vs {b_label} comparison" if two else f"{a_label} report"
    when = f" Data captured {captured_on.isoformat()}." if captured_on else ""
    return (
        f'<footer><div class="wrap"><b>{_esc(who + what)}.</b> Counts and amounts are period '
        "sums; ratios, cost-per, ROAS, CAC and conversion are recomputed from the summed "
        "components rather than averaged across months. ROAS = actualized revenue ÷ "
        "spend; CAC = spend ÷ new revenue clients; conversion = revenue clients ÷ "
        "demos completed (Direct); % of goal = revenue sold ($, not actualized) ÷ goal."
        f"{_esc(when)}</div></footer></body></html>"
    )


# --- the seam ----------------------------------------------------------------

def render(ledger: ReportLedger, *, prose: ReportProse | None = None,
           brand: str | None = None, title: str | None = None,
           captured_on: date | None = None) -> str:
    """A finished ledger as one self-contained HTML document.

    Pure: same ledger in, same bytes out. No clock is read (pass ``captured_on``
    if the document should carry a date), no file is written, nothing is fetched.

    ``prose`` is the optional LLM-written slot and is treated as plain text.
    Omit it and the report is still complete: the cover simply has no thesis
    line, and the win/miss columns are derived from the ledger's own arithmetic.
    """
    if len(ledger.columns) != 2 or len(ledger.periods) != 2:
        raise ValueError("a ReportLedger always has two columns and two periods")
    two = not is_single_period(ledger)
    a_label, b_label = ledger.columns
    doc_title = title or (
        f"{brand + ' — ' if brand else ''}"
        + (f"{a_label} vs {b_label}" if two else f"{a_label}")
        + " board report"
    )

    parts = [_head(doc_title), _cover(ledger, two=two, brand=brand, prose=prose)]
    if two:
        parts.append(_glance(ledger))
        parts.append(_movers_section(ledger))
        parts.append(_ledger_section(ledger, "02"))
        parts.append(_channel_shift_section(ledger))
    else:
        parts.append(_scorecard_section(ledger))
        parts.append(_charts_section(ledger, two=False, num="02",
                                     heading=f"Inside {a_label}"))
    parts.append(_insight_section(ledger, two=two, prose=prose))
    parts.append(_footer(ledger, two=two, brand=brand, captured_on=captured_on))
    return "".join(parts)
