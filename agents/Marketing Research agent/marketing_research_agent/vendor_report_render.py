"""Vendor performance report → one self-contained HTML document.

:func:`render` takes the plain dict :func:`vendor_report.compute` returns (or a
stored run's ``structured`` block — the same thing) plus a :class:`Layout`, and
returns a complete ``<!DOCTYPE html>`` page. Pure: no I/O, no clock, no model.

**The template seam.** A layout is theme tokens (colours, fonts) plus an ordered
list of :class:`SectionSpec` — ``{type, title, options}`` — each ``type`` drawn
from :data:`SECTION_REGISTRY`. :data:`DEFAULT_LAYOUT` reproduces the marketing
team's sample PDF. The registry is also the placeholder vocabulary Phase 2's
HTML templates will use: every section declares its block placeholder
(``{{chart:benchmark_movers}}``, ``{{table:vendor_scorecard}}``,
``{{list:watch_items}}``…) and :data:`vendor_report.METRICS` names the scalar
ones (``{{total_spend}}``…). :func:`placeholder_vocabulary`,
:func:`render_block` and :func:`render_scalar` make a placeholder a lookup.

**Self-contained.** No ``<script>``, no ``<link>``, no ``@import``, no remote
URL of any kind: charts are inline SVG built by ``board_report_render``'s
primitives (imported, not copied), fonts are that module's embedded faces.

**Theme scope, stated plainly.** Theme colours drive the stylesheet and every
data mark (bars, pills, tones). Chart scaffolding — gridlines, the zero line,
axis text — comes from ``board_report_render``'s fixed palette, because those
primitives take it as module constants; recolouring scaffolding is a change to
that module, not to a layout.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import vendor_report as vr
from .board_report_render import (
    GRID,
    INK,
    MONO,
    PALETTE,
    SANS,
    SERIF,
    SLATE,
    _CSS_BODY,
    _Series,
    _font_face_css,
    _grouped_vbar,
    _legend,
    _line,
    _nice_max,
    _rect,
    _svg,
    _text,
    _wrap2,
)

#: Bumped when the document changes shape for the same report dict.
RENDERER_VERSION = "mr-vendor-report-render/1"

#: A missing figure: a plain muted em-dash. The tooltip says why on screen; the
#: page itself carries no underline or colour, so it never reads as a glitch.
_ABSENT = ('<span class="dash" title="No figure: not reported, or nothing to divide by '
           'yet">&#8212;</span>')


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --- theme + layout -------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    """Colour tokens (the board palette's keys) and the three font stacks."""

    colors: Mapping[str, str] = field(default_factory=lambda: dict(PALETTE))
    serif: str = SERIF
    sans: str = SANS
    mono: str = MONO

    def color(self, name: str) -> str:
        return self.colors.get(name) or PALETTE[name]


@dataclass(frozen=True)
class SectionSpec:
    type: str
    title: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Layout:
    theme: Theme
    sections: tuple[SectionSpec, ...]


@dataclass(frozen=True)
class SectionType:
    """One registry entry: what a section is, what it reads, how it renders."""

    type: str
    title: str
    kind: str                       # band | tiles | chart | table | list | note
    numbered: bool
    metrics: tuple[str, ...]        # report metric keys this section may be given
    options: Mapping[str, Any]      # option name -> default
    render: Callable[["_Ctx", SectionSpec], str]

    @property
    def placeholder(self) -> str:
        return "{{%s:%s}}" % (self.kind, self.type)


@dataclass
class _Ctx:
    report: Mapping[str, Any]
    theme: Theme
    number: str = ""


def _opt(spec: SectionSpec, name: str) -> Any:
    entry = SECTION_REGISTRY[spec.type]
    return spec.options.get(name, entry.options.get(name))


# --- small formatting helpers ----------------------------------------------------

def _f(v: Any, kind: str) -> str:
    return _ABSENT if v is None else _esc(vr.fmt(v, kind))


def _segments(item: Mapping | None, tag: str = "b") -> str:
    """A ``{text, segments}`` sentence as escaped HTML, emphasis as ``tag``."""
    if not item:
        return ""
    out = []
    for s in item.get("segments") or [{"t": item.get("text", ""), "em": False}]:
        t = _esc(s.get("t"))
        if s.get("em"):
            t = f'<{tag}>{t}</{tag}>' if tag != "metric" else f'<span class="metric">{t}</span>'
        out.append(t)
    return "".join(out)


def _kicker(ctx: _Ctx, title: str) -> str:
    num = f'<span class="num">{_esc(ctx.number)}</span>' if ctx.number else ""
    return f'<div class="kicker">{num}<h2>{_esc(title)}</h2></div>'


def _section(ctx: _Ctx, title: str, body: str) -> str:
    return f'<section><div class="wrap">{_kicker(ctx, title)}{body}</div></section>'


def _note(item: Mapping | str | None) -> str:
    if not item:
        return ""
    inner = _segments(item) if isinstance(item, Mapping) else _esc(item)
    return f'<div class="note">{inner}</div>'


# --- horizontal bars ----------------------------------------------------------------
# board_report_render._hbar is NOT used, for two reasons that are properties of
# that primitive rather than of this report: (1) its axis span is the data's own
# nice ceiling, so a bar near the extreme (show rate at -74%) puts its value
# label on top of the category label; (2) its tick labels round to whole
# thousands, so 1,500 and 2,250 both print "$2K". Fixing either inside _hbar
# changes the board report's charts, so this is the same chart built from the
# same lower-level primitives, with label headroom and exact tick labels.

def _tick_label(v: float, kind: str, top: float) -> str:
    if kind == vr.PCT:
        return f"{v:.0f}%"
    if kind == vr.MONEY:
        if top >= 10_000 and v:
            k = v / 1000
            return f"${k:.1f}K".replace(".0K", "K")
        return f"${v:,.0f}"
    return f"{v:,.0f}"


def _value_label(v: float, kind: str, diverging: bool) -> str:
    if diverging:
        return f"{v:+.1f}%"
    return vr.fmt(v, kind)


def _hbars(categories: Sequence[str], series: Sequence[_Series], *, fmt: str,
           title: str, desc: str, diverging: bool = False,
           bar_colours: Sequence[Sequence[str]] | None = None,
           width: float = 760.0, label_w: float = 200.0, label_size: float = 10.5) -> str:
    n_ser = max(len(series), 1)
    row_h = 26.0 if n_ser == 1 else 34.0
    top = 34.0 if n_ser > 1 else 12.0
    height = top + max(len(categories), 1) * row_h + 34.0
    right = 82.0
    plot_w = width - label_w - right
    flat = [v for s in series for v in s.values if v is not None]
    if diverging:
        span = _nice_max(max([abs(v) for v in flat] + [1.0]) * 1.18)
        lo, hi = -span, span
    else:
        lo, hi = 0.0, _nice_max(max(flat + [0.0]) * 1.12) if any(flat) else 1.0

    def x_of(v: float) -> float:
        return label_w + (v - lo) / (hi - lo) * plot_w

    out: list[str] = []
    if n_ser > 1:
        out.append(_legend(label_w, 18, [(s.label, s.colour) for s in series]))
    plot_bottom = top + len(categories) * row_h
    for i in range(5):
        t = lo + (hi - lo) * i / 4
        x = x_of(t)
        out.append(_line(x, top - 4, x, plot_bottom, GRID))
        out.append(_text(x, plot_bottom + 18, _tick_label(t, fmt, hi), size=9.5,
                         anchor="middle"))
    zero_x = x_of(0.0)
    out.append(_line(zero_x, top - 4, zero_x, plot_bottom, INK, 1.5))
    for ci, cat in enumerate(categories):
        y0 = top + ci * row_h
        lines = _wrap2(cat, int((label_w - 20) / (label_size * 0.57)))
        mid = y0 + row_h / 2 + 3.5
        for line, dy in zip(lines, [0.0] if len(lines) == 1 else [-5.5, 5.5]):
            out.append(_text(label_w - 12, mid + dy, line, size=label_size, anchor="end",
                             sans=True, fill=INK))
        sub_h = (row_h - 12) / n_ser
        for si, s in enumerate(series):
            v = s.values[ci] if ci < len(s.values) else None
            by = y0 + 6 + si * sub_h
            colour = bar_colours[si][ci] if bar_colours is not None else s.colour
            if v is None:
                out.append(_text(zero_x + 6, by + sub_h / 2 + 3.5, "—", size=11))
                continue
            x1, x2 = min(zero_x, x_of(v)), max(zero_x, x_of(v))
            out.append(_rect(x1, by, max(x2 - x1, 1.5), max(sub_h - 2, 3), colour))
            label = _value_label(v, fmt, diverging)
            if v >= 0:
                out.append(_text(x2 + 6, by + sub_h / 2 + 3.5, label, size=9.5, fill=SLATE))
            else:
                out.append(_text(x1 - 6, by + sub_h / 2 + 3.5, label, size=9.5, fill=SLATE,
                                 anchor="end"))
    return _svg(width, height, title, desc, "".join(out))


# --- sections ---------------------------------------------------------------------

def _header(ctx: _Ctx, spec: SectionSpec) -> str:
    r = ctx.report
    title = str(r.get("title") or "")
    head, _, tail = title.rpartition(" ")
    heading = (f"{_esc(head)} <em>{_esc(tail)}</em>" if head else _esc(title))
    kpis = "".join(
        f"<div><span>{_esc(vr.METRICS[k][0].replace('Total ', ''))}</span>"
        f"<b>{_f(r['portfolio'].get(k), vr.METRICS[k][1])}</b></div>"
        for k in _opt(spec, "kpis"))
    thesis = _segments((r.get("notes") or {}).get("thesis"))
    fallback = ""
    if ((r.get("build") or {}).get("template") or {}).get("fallback"):
        fallback = f'<p class="sub"><b>{_esc(FALLBACK_SENTENCE)}</b></p>'
    return ('<header class="cover"><div class="wrap">'
            f'<div class="eyebrow">{_esc(_opt(spec, "kicker"))}</div><h1>{heading}</h1>'
            + fallback
            + (f'<p class="sub">{thesis}</p>' if thesis else "")
            + f'<div class="cover-meta">{kpis}</div></div></header>')


def _glance(ctx: _Ctx, spec: SectionSpec) -> str:
    r, notes = ctx.report, ctx.report.get("notes") or {}
    hl, accent = _opt(spec, "highlight"), _opt(spec, "accent")
    tiles = []
    for k in _opt(spec, "tiles"):
        cls = "card" + (" hl" if k == hl else "") + (" na" if k == accent else "")
        tiles.append(f'<div class="{cls}"><div class="t">{_esc(vr.METRICS[k][0])}</div>'
                     f'<div class="v">{_f(r["portfolio"].get(k), vr.METRICS[k][1])}</div></div>')
    body = (f'<p class="lead">{_esc(notes.get("glance_lead"))}</p>'
            f'<div class="sublabel">{_esc(notes.get("glance_sublabel"))}</div>'
            f'<div class="tiles">{"".join(tiles)}</div>'
            + _note(notes.get("glance_reading")))
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title, body)


def _movers(ctx: _Ctx, spec: SectionSpec) -> str:
    r, notes, t = ctx.report, ctx.report.get("notes") or {}, ctx.theme
    allowed = set(_opt(spec, "metrics"))
    shown = [m for m in r.get("movers") or [] if m["key"] in allowed]
    if shown:
        chart = _hbars([m["label"] for m in shown],
                      [_Series("gap vs target", t.color("slate"),
                               tuple(m["gap_pct"] for m in shown))],
                      fmt=vr.PCT, diverging=True, label_w=210.0,
                      bar_colours=[[t.color("pos") if m["beating"] else t.color("neg")
                                    for m in shown]],
                      title="Percent gap to benchmark",
                      desc="Bar direction is the sign of the gap; green beats the target, "
                           "red misses it.")
        panel = f'<div class="panel full chartcard"><div class="chart-box">{chart}</div></div>'
    else:
        panel = ('<div class="panel full"><p class="cap">No benchmark could be measured for '
                 'this pull — see the note below.</p></div>')
    reading = notes.get("movers_reading")
    used = notes.get("targets_used")
    note = ""
    if reading or used:
        note = ('<div class="note">' + _segments(reading)
                + ("<br><br>" if reading and used else "") + _segments(used) + "</div>")
    body = (f'<p class="lead" style="margin-bottom:20px">{_segments(notes.get("movers_lead"), "strong")}</p>'
            + panel + note)
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title, body)


def _budget_spend(ctx: _Ctx, spec: SectionSpec) -> str:
    r, t = ctx.report, ctx.theme
    rows = list(r.get("vendors") or [])
    if _opt(spec, "sort") == "budget":
        rows.sort(key=lambda v: -(v["budget"] or 0))
    elif _opt(spec, "sort") == "spend":
        rows.sort(key=lambda v: -(v["spend"] or 0))
    if rows:
        chart = _hbars([v["name"] for v in rows],
                      [_Series("Budget", t.color("slate"), tuple(v["budget"] for v in rows)),
                       _Series("Spend", t.color("gold"), tuple(v["spend"] for v in rows))],
                      fmt=vr.MONEY, label_w=190.0, label_size=10.0,
                      title="Budget vs spend by vendor",
                      desc="Budget and month-to-date spend for every paid vendor tab.")
    else:
        chart = '<p class="cap">No paid vendor tabs in this pull.</p>'
    body = ('<div class="panel full chartcard"><h3>Budget vs. spend, by vendor</h3>'
            f'<p class="cap">{_esc((r.get("notes") or {}).get("budget_caption"))}</p>'
            f'<div class="chart-box">{chart}</div></div>')
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title, body)


def _demos(ctx: _Ctx, spec: SectionSpec) -> str:
    r, t = ctx.report, ctx.theme
    floor = _opt(spec, "min_booked")
    rows = [v for v in r.get("vendors") or []
            if (v["qual_demos_booked"] or 0) >= floor or (v["demos_completed"] or 0) > 0]
    rows.sort(key=lambda v: (-(v["qual_demos_booked"] or 0), -(v["demos_completed"] or 0)))
    if rows:
        width = max(760.0, 140.0 * len(rows))
        chart = _grouped_vbar([v["name"] for v in rows],
                              [_Series("Qual. demos booked", t.color("slate"),
                                       tuple(v["qual_demos_booked"] for v in rows)),
                               _Series("Completed", t.color("pos"),
                                       tuple(v["demos_completed"] for v in rows))],
                              fmt=vr.INT, width=width, height=300.0,
                              title="Qualified demos booked vs completed",
                              desc="Raw counts per vendor, vendors with any booked or "
                                   "completed demo only.")
    else:
        chart = '<p class="cap">No vendor has a qualified demo booked yet.</p>'
    body = ('<div class="panel full chartcard"><h3>Demos booked vs. completed, by vendor</h3>'
            f'<p class="cap">{_esc((r.get("notes") or {}).get("demos_caption"))}</p>'
            f'<div class="chart-box">{chart}</div></div>')
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title, body)


def _channel_chart(buckets: Sequence[Mapping], attr: str, colour: str, title: str,
                   empty: str) -> str:
    vals = tuple(b.get(attr) for b in buckets)
    if not any(v for v in vals if v is not None):
        missing = all(v is None for v in vals)
        return f'<p class="cap">{_esc("Not reported for any channel." if missing else empty)}</p>'
    return _hbars([b["channel"] for b in buckets], [_Series(title, colour, vals)],
                 fmt=vr.MONEY, label_w=90.0, width=520.0, title=title,
                 desc=f"{title}, month to date.")


def _channels(ctx: _Ctx, spec: SectionSpec) -> str:
    r, t, notes = ctx.report, ctx.theme, ctx.report.get("notes") or {}
    buckets = (r.get("channels") or {}).get("buckets") or []
    month = str(r.get("month_label") or "").split(" ")[0]
    spend = _channel_chart(buckets, "spend", t.color("slate"), "Spend by channel",
                           "No paid spend recorded in any channel yet.")
    rev = _channel_chart(buckets, "projected_revenue", t.color("gold"),
                         "Projected revenue by channel",
                         "No projected revenue on the books in any channel yet.")
    title = spec.title or f"Spend & projected revenue by channel — {month} only"
    body = (f'<p class="lead" style="margin-bottom:20px">{_esc(notes.get("channel_lead"))}</p>'
            '<div class="chart-grid">'
            '<div class="panel chartcard"><h3>Spend by channel</h3>'
            f'<p class="cap">{_esc(month)} month-to-date, by channel.</p>'
            f'<div class="chart-box">{spend}</div></div>'
            '<div class="panel chartcard"><h3>Projected revenue by channel</h3>'
            '<p class="cap">The tracker\'s “Projected Total Amount Sold ($) Actualized” row, '
            f'{_esc(month)} month-to-date.</p><div class="chart-box">{rev}</div></div></div>'
            + _note(notes.get("channel_reading")) + _note(notes.get("other_channels")))
    return _section(ctx, title, body)


#: Scorecard columns: key -> (vendor-row field, portfolio key, header, format).
SCORECARD_COLUMNS: dict[str, tuple[str, str, str, str]] = {
    "budget": ("budget", "total_budget", "Budget", vr.MONEY),
    "spend": ("spend", "total_spend", "Spend", vr.MONEY),
    "budget_utilized_pct": ("budget_utilized_pct", "budget_utilized_pct", "Util.", vr.PCT),
    "leads": ("leads", "total_leads", "Leads", vr.INT),
    "qualified_leads": ("qualified_leads", "qualified_leads", "Qual. leads", vr.INT),
    "ql_ratio_pct": ("ql_ratio_pct", "ql_ratio_pct", "QL ratio", vr.PCT),
    "demos_booked": ("demos_booked", "demos_booked", "Demos booked (all)", vr.INT),
    "qual_demos_booked": ("qual_demos_booked", "qual_demos_booked", "Qual. demos booked",
                          vr.INT),
    "demos_completed": ("demos_completed", "demos_completed", "Completed", vr.INT),
    "show_rate_pct": ("show_rate_pct", "show_rate_pct", "Show rate ÷ qual. booked", vr.PCT),
    "dnc_bad_leads": ("dnc_bad_leads", "dnc_bad_leads", "DNC", vr.INT),
    "projected_revenue": ("projected_revenue", "projected_revenue", "Proj. revenue", vr.MONEY),
}


def _tone(col: str, value: Any, show_target: float | None, ql_target: float | None) -> str:
    if value is None:
        return ""
    if col == "ql_ratio_pct" and ql_target is not None:
        return "up" if value >= ql_target else "down"
    if col == "show_rate_pct" and show_target is not None:
        return "up" if value >= show_target else "down"
    if col == "dnc_bad_leads" and value > 0:
        return "down"
    return ""


def _scorecard(ctx: _Ctx, spec: SectionSpec) -> str:
    r = ctx.report
    cols = [c for c in _opt(spec, "columns") if c in SCORECARD_COLUMNS]
    items = {i["key"]: i["target"] for i in (r.get("targets") or {}).get("items") or []}
    ql_target = items.get("ql_ratio_pct")
    head = "<th>Vendor</th>" + "".join(f"<th>{_esc(SCORECARD_COLUMNS[c][2])}</th>" for c in cols)
    body = []
    for v in r.get("vendors") or []:
        cells = [f"<td>{_esc(v['name'])}</td>"]
        for c in cols:
            field_, _pk, _h, kind = SCORECARD_COLUMNS[c]
            tone = _tone(c, v.get(field_), v.get("show_rate_target"), ql_target)
            cells.append(f'<td class="{tone}">{_f(v.get(field_), kind)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    p = r.get("portfolio") or {}
    total = ["<td>Portfolio total</td>"]
    for c in cols:
        _fv, pkey, _h, kind = SCORECARD_COLUMNS[c]
        tone = _tone(c, p.get(pkey), items.get("show_rate_pct"), ql_target)
        total.append(f'<td class="{tone}">{_f(p.get(pkey), kind)}</td>')
    table = ('<div class="vwrap"><table class="vtab"><thead><tr>' + head + "</tr></thead><tbody>"
             + "".join(body) + '</tbody><tfoot><tr class="total">' + "".join(total)
             + "</tr></tfoot></table></div>")
    legend = ('<p class="cap" style="margin-top:10px">Show rate = demos completed ÷ qualified '
              'demos booked — the same denominator in every row and in the total. “Demos '
              'booked (all)” includes bookings not marked qualified. Green/red: at or above / '
              'below the target for that vendor\'s channel; an em-dash is a figure with no '
              'denominator yet, never a zero.</p>')
    new = ""
    if _opt(spec, "show_new_this_period"):
        sentences = ((r.get("new_this_period") or {}).get("sentences")) or []
        if sentences:
            new = ('<div class="note"><b>New this period:</b> '
                   + " ".join(_segments(s) for s in sentences) + "</div>")
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title, table + legend + new)


def _list_block(items: Sequence[Mapping], *, positive: bool, tag: str, heading: str) -> str:
    cls = "win" if positive else "miss"
    lis = "".join(f"<li>{_segments(i, 'metric')}</li>" for i in items) or (
        "<li>Nothing to report yet.</li>")
    return (f'<div class="col {cls}"><span class="tag {cls}">{_esc(tag)}</span>'
            f"<h3>{_esc(heading)}</h3><ul>{lis}</ul></div>")


def _standouts_block(ctx: _Ctx, spec: SectionSpec) -> str:
    return _list_block(ctx.report.get("standouts") or [], positive=True,
                       tag=_opt(spec, "tag"), heading=spec.title or "Standouts")


def _watch_block(ctx: _Ctx, spec: SectionSpec) -> str:
    return _list_block(ctx.report.get("watch_items") or [], positive=False,
                       tag=_opt(spec, "tag"), heading=spec.title or "Watch items")


def _wrap_list(ctx: _Ctx, spec: SectionSpec, inner: str) -> str:
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title,
                    f'<div class="ins one">{inner}</div>')


def _highlights(ctx: _Ctx, spec: SectionSpec) -> str:
    s = SectionSpec("standouts", options={"tag": _opt(spec, "standouts_tag")})
    w = SectionSpec("watch_items", options={"tag": _opt(spec, "watch_tag")})
    inner = _standouts_block(ctx, s) + _watch_block(ctx, w)
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title,
                    f'<div class="ins one">{inner}</div>')


_PILL = {vr.STRONG_START: "strong", vr.TOO_EARLY: "early", vr.CHECK_IN: "check"}


def _actions(ctx: _Ctx, spec: SectionSpec) -> str:
    r = ctx.report
    rows = []
    for a in r.get("actions") or []:
        if a.get("rule") == "catch_all" and not _opt(spec, "include_catch_all"):
            continue
        who = _esc(a["vendor"])
        if a.get("rule") == "catch_all":
            who += f' <span class="cnt">({len(a.get("vendors") or [])})</span>'
        rows.append(f"<tr><td>{who}</td>"
                    f'<td><span class="pill {_PILL.get(a["status"], "early")}">'
                    f'{_esc(a["status"])}</span></td><td>{_segments(a)}</td></tr>')
    table = ('<div class="vwrap"><table class="atab"><thead><tr><th>Vendor</th><th>Status</th>'
             '<th>Action</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>")
    rules = (r.get("notes") or {}).get("status_rules")
    return _section(ctx, spec.title or SECTION_REGISTRY[spec.type].title,
                    table + (f'<p class="cap" style="margin-top:10px">{_esc(rules)}</p>'
                             if rules else ""))


def _data_gaps(ctx: _Ctx, spec: SectionSpec) -> str:
    r = ctx.report
    blocks = ['<div class="grp"><span>How to read an em-dash</span><ul><li>An em-dash is a '
              "figure the tracker did not report, or a ratio with nothing to divide by yet. It "
              "is never a zero, and no default is substituted.</li></ul></div>"]
    missing = r.get("missing") or []
    if missing:
        lis = []
        for m in missing:
            who = m["vendors"]
            if who == "portfolio":
                lis.append(f"<li><b>{_esc(m['label'])}</b> (portfolio): {_esc(m['reason'])}</li>")
            else:
                n = len(who)
                lis.append(f"<li><b>{_esc(m['label'])}</b>, {n} {'vendor' if n == 1 else 'vendors'}: "
                           f"{_esc(m['reason'])} — {_esc(', '.join(who))}</li>")
        blocks.append('<div class="grp"><span>Missing figures</span><ul>' + "".join(lis)
                      + "</ul></div>")
    notes = r.get("basis_notes") or []
    if notes:
        blocks.append('<div class="grp"><span>Basis</span><ul>' + "".join(
            f"<li>{_esc(n['vendor'])}: {_esc(n['note'])}</li>" for n in notes) + "</ul></div>")
    left_out = (r.get("notes") or {}).get("left_out") or []
    if left_out:
        blocks.append('<div class="grp"><span>Left out of this report</span><ul>' + "".join(
            f"<li>{_esc(line)}</li>" for line in left_out) + "</ul></div>")
    return ('<section style="padding-top:0"><div class="wrap"><div class="panel gaps">'
            f'<h3>{_esc(spec.title or SECTION_REGISTRY[spec.type].title)}</h3>'
            + "".join(blocks) + "</div></div></section>")


#: Said in the header band and the footer of a report built with the built-in
#: template because the workspace's own template could not be rendered.
FALLBACK_SENTENCE = "Built with the built-in template because the team template failed."


def template_label(template: Mapping[str, Any] | None) -> str:
    """The plain words for which template a report was built with — the
    footer's provenance line and the console's template line. ``template`` is a
    run's template reference: ``{kind, number, id}`` (kind ``builtin``,
    ``layout`` or ``html``), optionally carrying ``fallback`` or ``override_of``
    on a built-in build, or ``{"kind": "preview"}`` for an unsaved preview."""
    tpl = template or {}
    kind = tpl.get("kind")
    if kind == "preview":
        return "a preview of an unsaved template"
    if kind in ("layout", "html"):
        number = tpl.get("number")
        return f"team template, version {number}" if number else "team template"
    fallback = tpl.get("fallback")
    if fallback:
        number = fallback.get("number")
        return ("built-in, because the team template"
                + (f" (version {number})" if number else "") + " failed")
    override = tpl.get("override_of")
    if override:
        number = override.get("number")
        return ("built-in, chosen instead of the team template"
                + (f" (version {number})" if number else ""))
    return "built-in"


def _footer(ctx: _Ctx, spec: SectionSpec) -> str:
    r = ctx.report
    foot = _segments((r.get("notes") or {}).get("footer"))
    build = r.get("build") or {}
    meta = ""
    if build:
        tpl = build.get("template") or {}
        built_at = str(build.get("built_at") or "")[:16].replace("T", " ")
        meta = (f" Built {_esc(built_at)} UTC from the {_esc(build.get('sweep_date'))} pull. "
                f"Template: {_esc(template_label(tpl))}.")
        fallback = tpl.get("fallback")
        if fallback:
            reason = str(fallback.get("reason") or "").strip()
            meta += f" {_esc(FALLBACK_SENTENCE)}" + (f" ({_esc(reason)})" if reason else "")
    return f'<footer><div class="wrap">{foot}{meta}</div></footer>'


# --- the registry -------------------------------------------------------------------

_ALL = tuple(vr.METRICS)
_MOVER_KEYS = tuple(t.key for t in __import__(
    "marketing_research_agent.goals", fromlist=["VENDOR_REPORT_TARGETS"]).VENDOR_REPORT_TARGETS)

SECTION_REGISTRY: dict[str, SectionType] = {s.type: s for s in (
    SectionType("header", "Header band", "band", False, _ALL,
                {"kicker": "Paid marketing channels · Vendor performance",
                 "kpis": ("total_budget", "total_spend", "budget_utilized_pct",
                          "ql_ratio_pct", "demos_completed")}, _header),
    SectionType("portfolio_glance", "Portfolio at a glance", "tiles", True, _ALL,
                {"tiles": ("total_budget", "total_spend", "total_leads", "qualified_leads",
                           "ql_ratio_pct", "qual_demos_booked", "demos_completed",
                           "show_rate_pct", "budget_utilized_pct", "dnc_bad_leads"),
                 "highlight": "ql_ratio_pct", "accent": "show_rate_pct"}, _glance),
    SectionType("benchmark_movers", "Biggest movers vs. benchmark", "chart", True, _MOVER_KEYS,
                {"metrics": _MOVER_KEYS}, _movers),
    SectionType("budget_vs_spend", "Budget allocation vs. spend so far", "chart", True,
                ("total_budget", "total_spend"), {"sort": "tab"}, _budget_spend),
    SectionType("demos_by_vendor", "Demos booked vs. completed", "chart", True,
                ("qual_demos_booked", "demos_completed"), {"min_booked": 1}, _demos),
    SectionType("channel_mix", "Spend & projected revenue by channel", "chart", True,
                ("total_spend", "projected_revenue"), {}, _channels),
    SectionType("vendor_scorecard", "Vendor scorecard", "table", True, _ALL,
                {"columns": ("budget", "spend", "budget_utilized_pct", "leads",
                             "qualified_leads", "ql_ratio_pct", "demos_booked",
                             "qual_demos_booked", "demos_completed", "show_rate_pct",
                             "dnc_bad_leads"),
                 "show_new_this_period": True}, _scorecard),
    SectionType("highlights", "What's working / what needs attention", "block", True, (),
                {"standouts_tag": "Early positive signals",
                 "watch_tag": "Too early / worth a note"}, _highlights),
    SectionType("standouts", "Standouts", "list", True, (),
                {"tag": "Early positive signals"},
                lambda c, s: _wrap_list(c, s, _standouts_block(c, s))),
    SectionType("watch_items", "Watch items", "list", True, (),
                {"tag": "Too early / worth a note"},
                lambda c, s: _wrap_list(c, s, _watch_block(c, s))),
    SectionType("action_summary", "Action summary", "table", True, (),
                {"include_catch_all": True}, _actions),
    SectionType("data_gaps", "Basis & data gaps", "note", False, (), {}, _data_gaps),
    SectionType("footer", "Footer", "band", False, (), {}, _footer),
)}

#: Options whose values must be metric keys the section accepts. Public: the
#: template extractor builds its response schema from it.
METRIC_OPTIONS = frozenset({"kpis", "tiles", "metrics", "highlight", "accent"})

#: What a theme colour may be. Theme values are written verbatim into the
#: stylesheet (``--ink:VALUE``) and into SVG ``fill="VALUE"``, and a layout can
#: arrive from a client, so anything else is refused: no quotes, no ``<``, no
#: ``;``, no ``url(``.
COLOUR_RE = re.compile(
    r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|(?:rgba?|hsla?)\([0-9.,%\s/+-]{1,60}\)"
    r"|[a-zA-Z]{3,24}")
#: A font stack: family names, quotes, commas, spaces. Same reason as above.
FONT_STACK_RE = re.compile(r"[A-Za-z0-9 ,'\"_.-]{1,240}")
_TEXT_MAX = 200

DEFAULT_THEME = Theme()

#: The marketing team's sample PDF ("September 2_Vendor_Performance.pdf").
DEFAULT_LAYOUT = Layout(theme=DEFAULT_THEME, sections=tuple(SectionSpec(t) for t in (
    "header", "portfolio_glance", "benchmark_movers", "budget_vs_spend", "demos_by_vendor",
    "channel_mix", "vendor_scorecard", "highlights", "action_summary", "data_gaps", "footer",
)))


def _option_type_ok(default: Any, value: Any) -> bool:
    """An option value has its default's type: what the section renderers
    compare, iterate and print. Checked here so a bad value is a refusal at
    validation, never a TypeError half-way through a render."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000
    if isinstance(default, str):
        return isinstance(value, str) and len(value) <= _TEXT_MAX
    if isinstance(default, (tuple, list)):
        return (isinstance(value, (tuple, list)) and len(value) <= 50
                and all(isinstance(v, str) for v in value))
    return True


def validate_layout(layout: Layout) -> None:
    """Refuse a layout the registry cannot honour — before anything renders."""
    if not layout.sections:
        raise ValueError("a layout needs at least one section")
    if len(layout.sections) > 40:
        raise ValueError("a layout has at most 40 sections")
    for spec in layout.sections:
        entry = SECTION_REGISTRY.get(spec.type)
        if entry is None:
            raise ValueError(f"unknown section type {spec.type!r} "
                             f"(known: {', '.join(sorted(SECTION_REGISTRY))})")
        if spec.title is not None and (not isinstance(spec.title, str)
                                       or len(spec.title) > _TEXT_MAX):
            raise ValueError(f"section {spec.type!r} title must be text of at most "
                             f"{_TEXT_MAX} characters")
        unknown = set(spec.options) - set(entry.options)
        if unknown:
            raise ValueError(f"section {spec.type!r} does not take option(s) "
                             f"{', '.join(sorted(unknown))}")
        for name, value in spec.options.items():
            if not _option_type_ok(entry.options[name], value):
                raise ValueError(f"section {spec.type!r} option {name!r} has the wrong type "
                                 f"(expected {type(entry.options[name]).__name__})")
        for name in METRIC_OPTIONS & set(spec.options):
            value = spec.options[name]
            keys = [value] if isinstance(value, str) else list(value or ())
            bad = [k for k in keys if k not in entry.metrics]
            if bad:
                raise ValueError(f"section {spec.type!r} option {name!r} names metric(s) it "
                                 f"does not accept: {', '.join(bad)}")
        if "columns" in spec.options:
            bad = [c for c in spec.options["columns"] if c not in SCORECARD_COLUMNS]
            if bad:
                raise ValueError(f"unknown scorecard column(s): {', '.join(bad)}")
    for name, value in layout.theme.colors.items():
        if name not in PALETTE:
            raise ValueError(f"unknown theme colour token {name!r}")
        if not isinstance(value, str) or not COLOUR_RE.fullmatch(value):
            raise ValueError(f"theme colour {name!r} must be a colour such as #14213A")
    for name in ("serif", "sans", "mono"):
        value = getattr(layout.theme, name)
        if not isinstance(value, str) or not FONT_STACK_RE.fullmatch(value):
            raise ValueError(f"theme font {name!r} must be a plain font list")


def layout_from_dict(data: Mapping[str, Any]) -> Layout:
    """A stored/JSON layout (Phase 2 templates) → a validated :class:`Layout`.
    Every refusal — a bad value or a wrong shape — is a ``ValueError`` with a
    reason a person can act on."""
    if not isinstance(data, Mapping):
        raise ValueError("a layout must be an object with a 'sections' list")
    theme_in = data.get("theme") or {}
    sections_in = data.get("sections") or []
    if not isinstance(theme_in, Mapping) or not isinstance(theme_in.get("colors") or {},
                                                           Mapping):
        raise ValueError("a layout's 'theme' must be an object with a 'colors' object")
    if not isinstance(sections_in, (list, tuple)):
        raise ValueError("a layout's 'sections' must be a list")
    theme = Theme(colors={**PALETTE, **(theme_in.get("colors") or {})},
                  serif=theme_in.get("serif") or SERIF, sans=theme_in.get("sans") or SANS,
                  mono=theme_in.get("mono") or MONO)
    sections = []
    for s in sections_in:
        if not isinstance(s, Mapping) or not isinstance(s.get("type"), str):
            raise ValueError("every section must be an object with a 'type'")
        options = s.get("options") or {}
        if not isinstance(options, Mapping):
            raise ValueError(f"section {s['type']!r} options must be an object")
        sections.append(SectionSpec(type=s["type"], title=s.get("title"),
                                    options=dict(options)))
    layout = Layout(theme=theme, sections=tuple(sections))
    validate_layout(layout)
    return layout


def layout_to_dict(layout: Layout) -> dict:
    """The inverse of :func:`layout_from_dict` — the JSON a stored template
    version carries. The built-in template stores NO spec (``runs.builtin_template``
    has ``spec: None``); it is :data:`DEFAULT_LAYOUT` by definition, so the two
    cannot drift. This exists for Phase 2, to seed an editable copy of it."""
    return {
        "theme": {"colors": dict(layout.theme.colors), "serif": layout.theme.serif,
                  "sans": layout.theme.sans, "mono": layout.theme.mono},
        "sections": [{"type": s.type, "title": s.title,
                      "options": {k: (list(v) if isinstance(v, tuple) else v)
                                  for k, v in s.options.items()}}
                     for s in layout.sections],
    }


def placeholder_vocabulary() -> dict:
    """Every placeholder a template may use: scalars (``{{total_spend}}``) and
    blocks (``{{chart:benchmark_movers}}``), each with what it accepts."""
    scalars = {k: {"label": label, "format": kind} for k, (label, kind) in vr.METRICS.items()}
    for k in ("title", "month_label", "as_of", "year_month"):
        scalars[k] = {"label": k.replace("_", " "), "format": "text"}
    blocks = {e.placeholder: {"type": e.type, "title": e.title, "kind": e.kind,
                              "metrics": list(e.metrics),
                              "options": {k: (list(v) if isinstance(v, tuple) else v)
                                          for k, v in e.options.items()}}
              for e in SECTION_REGISTRY.values()}
    return {"scalars": scalars, "blocks": blocks}


def render_scalar(report: Mapping[str, Any], name: str) -> str:
    """One scalar placeholder's text, escaped. Absent → the em-dash marker."""
    if name in vr.METRICS:
        return _f((report.get("portfolio") or {}).get(name), vr.METRICS[name][1])
    if name in ("title", "month_label", "as_of", "year_month"):
        return _esc(report.get(name))
    raise KeyError(f"unknown scalar placeholder {name!r}")


def render_block(report: Mapping[str, Any], spec: SectionSpec, *,
                 theme: Theme = DEFAULT_THEME, number: str = "") -> str:
    """One block placeholder's HTML."""
    validate_layout(Layout(theme=theme, sections=(spec,)))
    return SECTION_REGISTRY[spec.type].render(_Ctx(report, theme, number), spec)


# --- stylesheet + document -----------------------------------------------------------

_FONT_CSS = _font_face_css()

_EXTRA_CSS = """
.cover h1{font-size:clamp(34px,5vw,52px)}
.lead{margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.tiles .card .t{min-height:0}
.tiles .card .v{font-size:23px}
.card.na{border-color:var(--gold-soft)}
.vwrap{border:1px solid var(--line);border-radius:14px;background:#fff;overflow-x:auto;box-shadow:var(--shadow)}
table.vtab,table.atab{border-collapse:collapse;width:100%;font-size:12.5px}
table.vtab th,table.vtab td{padding:9px 9px;text-align:right;border-bottom:1px solid var(--paper-2)}
table.vtab th:first-child,table.vtab td:first-child{text-align:left;min-width:150px}
table.vtab thead th{background:var(--ink);color:var(--paper);font-family:__MONO__;font-size:9.5px;
 letter-spacing:.06em;text-transform:uppercase;font-weight:500;vertical-align:bottom}
table.vtab tbody td:not(:first-child),table.vtab tfoot td:not(:first-child){font-family:__MONO__}
table.vtab tfoot td{background:var(--paper-2);font-weight:700;border-top:2px solid var(--ink)}
table.vtab td.up{color:var(--pos);font-weight:600} table.vtab td.down{color:var(--neg);font-weight:600}
table.atab th,table.atab td{padding:11px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--paper-2)}
table.atab thead th{font-family:__MONO__;font-size:10px;letter-spacing:.12em;text-transform:uppercase;
 color:var(--slate);border-bottom:2px solid var(--ink)}
table.atab tbody tr:nth-child(even) td{background:var(--paper-2)}
table.atab td:first-child{width:22%} table.atab td:nth-child(2){width:15%}
.cnt{color:var(--slate);font-size:11px}
.dash{color:var(--muted);font-weight:400;text-decoration:none;border:0;cursor:default}
.pill{display:inline-block;font-family:__MONO__;font-size:10.5px;font-weight:700;padding:4px 10px;
 border-radius:100px;white-space:nowrap}
.pill.strong{background:var(--pos);color:#fff} .pill.early{background:var(--slate);color:#fff}
.pill.check{background:var(--gold);color:var(--ink)}
footer{margin-top:24px}
@media(max-width:900px){.tiles{grid-template-columns:1fr 1fr}}
@media(max-width:560px){.tiles{grid-template-columns:1fr}}
@media print{
 .vwrap{box-shadow:none;overflow:visible}
 table.vtab{font-size:8.5px} table.vtab th,table.vtab td{padding:5px 4px}
 table.vtab thead th{font-size:7.5px}
 table.atab{font-size:10px}
 tr{break-inside:avoid;page-break-inside:avoid}
 .tiles{break-inside:avoid}
 .panel h3,.panel .cap{break-after:avoid;page-break-after:avoid}
 .chartcard,.ins .col{break-inside:avoid;page-break-inside:avoid}
 .dash{color:var(--slate)}
}
"""


def stylesheet(theme: Theme = DEFAULT_THEME) -> str:
    root = ":root{" + ";".join(f"--{k}:{theme.color(k)}" for k in PALETTE) + (
        ";--shadow:0 1px 2px rgba(20,33,58,.06),0 8px 30px rgba(20,33,58,.06)}")
    body = (_CSS_BODY + _EXTRA_CSS).replace("__SANS__", theme.sans) \
        .replace("__SERIF__", theme.serif).replace("__MONO__", theme.mono)
    return _FONT_CSS + root + body


def render(report: Mapping[str, Any], layout: Layout = DEFAULT_LAYOUT) -> str:
    """The report as one self-contained HTML document. Same input, same bytes."""
    if not isinstance(report, Mapping) or report.get("generator") != vr.GENERATOR_VERSION:
        raise ValueError(
            f"this renderer reads '{vr.GENERATOR_VERSION}' reports, got "
            f"'{(report or {}).get('generator') if isinstance(report, Mapping) else type(report).__name__}'")
    validate_layout(layout)
    parts = []
    n = 0
    for spec in layout.sections:
        entry = SECTION_REGISTRY[spec.type]
        number = ""
        if entry.numbered:
            n += 1
            number = f"{n:02d}"
        parts.append(entry.render(_Ctx(report, layout.theme, number), spec))
    title = f"Vendor Performance — {report.get('title')}, {str(report.get('year_month'))[:4]}"
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            f"<title>{_esc(title)}</title><style>{stylesheet(layout.theme)}</style></head><body>"
            + "".join(parts) + "</body></html>")
