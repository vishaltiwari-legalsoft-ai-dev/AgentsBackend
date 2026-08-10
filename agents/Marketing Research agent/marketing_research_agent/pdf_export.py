"""Console-faithful PDF exports (2026-07-27).

Three downloads, each mirroring the panel the user is looking at, section for
section, in the console's own order and wording:

- ``report_pdf``  → the Reports panel document (``MrReportDoc``): eyebrow /
  title / verdict stamp, executive summary + red-flag vendors + recommend,
  what-needs-attention, performance detail (KPI strip + channel cards),
  vendor detail cards, vendor insights & actions, appendix.
- ``vendor_pdf``  → the Vendors panel dossier (``VendorsView``): official
  summary grid, day movement, budget & spend + section groups, channel blocks.
- ``leads_pdf``   → the Leads panel (``LeadsView``): month story, red-flag
  card, brand/source mix, per-vendor outcome table, provenance.

Print-friendly white theme (client-forwardable); pure function over the same
dicts the console API returns — no I/O, no LLM, nothing existing is touched.
"""

from __future__ import annotations

import math
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# --- palette (print-friendly mirror of the console tokens) -------------------

INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#64748b")
FAINT = colors.HexColor("#94a3b8")
ACCENT = colors.HexColor("#1746a2")
ACCENT_DARK = colors.HexColor("#123a87")
ACCENT_TINT = colors.HexColor("#eef4ff")
BORDER = colors.HexColor("#e2e8f0")
CARD_BG = colors.HexColor("#f8fafc")
HEAD_BG = colors.HexColor("#f1f5f9")
WHITE = colors.HexColor("#ffffff")
RED = colors.HexColor("#dc2626")
RED_TINT = colors.HexColor("#fdecec")
AMBER = colors.HexColor("#d97706")
AMBER_TINT = colors.HexColor("#fdf3e0")
GREEN = colors.HexColor("#16a34a")
GREEN_TINT = colors.HexColor("#e8f7ee")

# Traffic-light colors for per-metric statuses (goals.status_for): every
# flagged figure renders in red, warnings in amber — the report reads like the
# console's dots, but louder.
_STATUS_COLOR = {"bad": RED, "warn": AMBER, "good": GREEN}


def _hx(c: colors.Color) -> str:
    return c.hexval().replace("0x", "#")

# Same labels the console shows (frontend reportMeta.ts) — the PDF must read
# like the screen, so these strings stay in lockstep with it.
REPORT_META: dict[str, dict[str, str]] = {
    "daily_summary": {"label": "Daily Performance Summary", "eyebrow": "Daily · Marketing"},
    "weekly_summary": {"label": "Weekly Performance Summary", "eyebrow": "Weekly · Marketing"},
    "monthly_summary": {"label": "Monthly Performance Summary", "eyebrow": "Monthly · Marketing"},
    "quarterly_summary": {"label": "Quarterly Performance Summary", "eyebrow": "Quarterly · Marketing"},
    "threshold_alert": {"label": "Campaign Threshold Alert", "eyebrow": "Triggered · Alert"},
    "competitor_digest": {"label": "Competitor Change Digest", "eyebrow": "Weekly · Competitive intel"},
    "opportunity_report": {"label": "Media Opportunity Report", "eyebrow": "Bi-weekly · Partnerships"},
    "utm_attribution": {"label": "UTM Attribution Summary", "eyebrow": "Weekly · Attribution"},
    "icp_signal": {"label": "ICP Audience Signal", "eyebrow": "Monthly · Audience"},
    "daily_movement": {"label": "Daily Movement Report", "eyebrow": "Daily · Snapshots"},
}

CAMPAIGN_KINDS = (
    "daily_summary", "weekly_summary", "monthly_summary", "quarterly_summary", "threshold_alert",
)

# --- display formatters (same rounding the console's format.ts applies) ------


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _round(n: float) -> int:
    return int(math.floor(n + 0.5)) if n >= 0 else -int(math.floor(-n + 0.5))


def _money(n) -> str:
    return f"${_round(n):,}" if _is_num(n) else "—"


def _num(n) -> str:
    return f"{_round(n):,}" if _is_num(n) else "—"


def _pct(n) -> str:
    return f"{n:.1f}%" if _is_num(n) else "—"


def _read_narrative(markdown: str) -> tuple[str, str]:
    """Frontend format.ts readNarrative: summary + trailing "Recommend:" line."""
    import re

    body = re.sub(r"^#\s.*\n+", "", markdown or "").replace("**", "").strip()
    body = re.sub(r"^\[[a-z_]+\]\s*\(offline summary\)\s*", "", body, flags=re.I).strip()
    m = re.search(r"recommend:\s*(.*)$", body, flags=re.I | re.S)
    if m:
        return body[: m.start()].strip(), m.group(1).strip()
    return body, ""


def _verdict(reds: int, warns: int) -> tuple[str, colors.Color]:
    if reds > 0:
        return f"{reds} red flag{'' if reds == 1 else 's'}", RED
    if warns > 0:
        return "Watch", AMBER
    return "On track", GREEN


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- shared building blocks --------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Helvetica", textColor=INK)
    return {
        "eyebrow": ParagraphStyle("eyebrow", fontName="Helvetica-Bold", fontSize=7.5,
                                  textColor=ACCENT, leading=10, spaceAfter=2),
        "band_eyebrow": ParagraphStyle("band_eyebrow", fontName="Helvetica-Bold", fontSize=7.5,
                                       textColor=colors.HexColor("#bcd0f7"), leading=11),
        "band_title": ParagraphStyle("band_title", fontName="Helvetica-Bold", fontSize=20,
                                     textColor=WHITE, leading=24),
        "band_meta": ParagraphStyle("band_meta", fontSize=8, textColor=colors.HexColor("#d7e2f8"),
                                    leading=11, fontName="Helvetica"),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=19,
                                textColor=INK, leading=23, spaceAfter=3),
        "meta": ParagraphStyle("meta", fontSize=8, textColor=MUTED, leading=11),
        "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=11.5,
                              textColor=INK, leading=14),
        "secnum": ParagraphStyle("secnum", fontName="Helvetica-Bold", fontSize=8,
                                 textColor=ACCENT, leading=14),
        "body": ParagraphStyle("body", fontSize=8.6, leading=12.6, **base),
        "small": ParagraphStyle("small", fontSize=7.6, leading=10.5, textColor=MUTED,
                                fontName="Helvetica"),
        "cell": ParagraphStyle("cell", fontSize=8, leading=10.8, **base),
        "cellsmall": ParagraphStyle("cellsmall", fontSize=7.4, leading=10, textColor=MUTED,
                                    fontName="Helvetica"),
        "kpival": ParagraphStyle("kpival", fontName="Helvetica-Bold", fontSize=13,
                                 textColor=INK, leading=15),
        "kpilabel": ParagraphStyle("kpilabel", fontSize=7, textColor=MUTED, leading=9),
        "cardname": ParagraphStyle("cardname", fontName="Helvetica-Bold", fontSize=9.5,
                                   textColor=ACCENT, leading=12),
        "cardspend": ParagraphStyle("cardspend", fontName="Helvetica-Bold", fontSize=15,
                                    textColor=INK, leading=17),
    }


# --- design primitives (accent band, chips, rounded cards, card grids) -------

_PAGE_W = A4[0] - 32 * mm  # printable width inside the 16mm margins


def _band(story: list, S, eyebrow: str, title: str, meta: str,
          chip: tuple[str, colors.Color] | None = None) -> None:
    """Full-width accent header band with an optional colored verdict chip."""
    left = [
        Paragraph(_esc(eyebrow).upper(), S["band_eyebrow"]),
        Spacer(0, 1.2 * mm),
        Paragraph(_esc(title), S["band_title"]),
        Spacer(0, 1.6 * mm),
        Paragraph(_esc(meta), S["band_meta"]),
    ]
    if chip:
        label, col = chip
        chip_t = Table(
            [[Paragraph(f"<b>{_esc(label)}</b>",
                        ParagraphStyle("chip", fontName="Helvetica-Bold", fontSize=8,
                                       textColor=WHITE, leading=10, alignment=1))]],
            colWidths=[34 * mm])
        chip_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), col),
            ("ROUNDEDCORNERS", [5, 5, 5, 5]),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ]))
        row = [[left, chip_t]]
        band = Table(row, colWidths=[_PAGE_W - 44 * mm, 44 * mm])
    else:
        band = Table([[left]], colWidths=[_PAGE_W])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.append(band)


def _rounded_card(content: list, *, width: float, bg=WHITE, border=BORDER,
                  accent: colors.Color | None = None) -> Table:
    """One card: rounded box, light border, optional colored left accent bar."""
    inner = Table([[content]], colWidths=[width - (3.5 if accent else 0)])
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.75, border),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]
    if accent is not None:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 2.5, accent))
    inner.setStyle(TableStyle(style))
    return inner


def _card_grid(cards: list[Table], *, per_row: int = 2, gap: float = 5 * mm) -> list:
    """Cards laid out in rows; each row is KeepTogether so a page break never
    slices through a card (the reason the DOM-capture approach was reverted)."""
    out: list = []
    for i in range(0, len(cards), per_row):
        row = cards[i:i + per_row]
        cells, widths = [], []
        for j, c in enumerate(row):
            if j:
                cells.append(Spacer(gap, 0))
                widths.append(gap)
            cells.append(c)
            widths.append((_PAGE_W - gap * (per_row - 1)) / per_row)
        while len(row) < per_row:  # keep last row left-aligned
            cells.append(Spacer(0, 0))
            widths.append((_PAGE_W - gap * (per_row - 1)) / per_row)
            row.append(None)  # type: ignore[arg-type]
        t = Table([cells], colWidths=widths)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        out.append(KeepTogether([t]))
        out.append(Spacer(0, 3 * mm))
    return out


def _counts_row(pairs: list[tuple[str, str]], S, width: float) -> Table:
    t = Table(
        [[Paragraph(f"<b>{_esc(v)}</b>", S["cell"]) for _, v in pairs],
         [Paragraph(_esc(k), S["kpilabel"]) for k, _ in pairs]],
        colWidths=[width / len(pairs)] * len(pairs))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _metric_line(label: str, value: str, S, *, status: str | None = None,
                 goal: str | None = None) -> Table:
    col = _STATUS_COLOR.get(status or "")
    val = f'<font color="{_hx(col)}"><b>{_esc(value)}</b></font>' if col else f"<b>{_esc(value)}</b>"
    lbl = _esc(label) + (f' <font color="{_hx(FAINT)}" size="6.4">{_esc(goal)}</font>' if goal else "")
    t = Table([[Paragraph(lbl, S["cellsmall"]), Paragraph(val, S["cell"])]],
              colWidths=[None, 24 * mm])
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _section(n: int, title: str, S) -> Table:
    row = [[Paragraph(f"{n:02d}", S["secnum"]), Paragraph(_esc(title), S["sec"])]]
    t = Table(row, colWidths=[9 * mm, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, BORDER),
    ]))
    return t


def _grid_table(rows: list[list], col_widths=None, header=True) -> Table:
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG)]
    t.setStyle(TableStyle(style))
    return t


def _kpi_strip(cells: list[tuple[str, str]], S) -> Table:
    """The console's hero KPI strip: big value over a small muted label."""
    row = [
        [Paragraph(_esc(v), S["kpival"]) for _, v in cells],
        [Paragraph(_esc(label), S["kpilabel"]) for label, _ in cells],
    ]
    t = Table(row)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _doc(buf: BytesIO, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf, pagesize=A4, title=title, author="Marketing Research agent",
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
    )


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(FAINT)
    canvas.drawString(16 * mm, 8 * mm, "Legal Soft · Marketing Research agent")
    canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return str(iso)


def _source_label(platform: str) -> str:
    p = str(platform or "")
    if p.startswith("sheets:"):
        return f"Google Sheets · {p[7:]}"
    if p.startswith("pdf:"):
        return f"PDF upload · {p[4:]}"
    names = {"google_ads": "Google Ads", "meta": "META Ads", "hubspot": "HubSpot"}
    return f"{names.get(p, p)} · CSV upload"


# --- Reports panel PDF -------------------------------------------------------

def _channel_card(name: str, a: dict, S, width: float) -> Table:
    """One channel as the console's card: name, hero spend, counts strip and
    per-metric rows with traffic-light coloring (flagged = red)."""
    st = a.get("status") or {}
    g = a.get("goal") or {}
    inner_w = width - 18 - 3.5  # card paddings + accent bar
    content: list = [
        Paragraph(_esc(name).upper(), S["cardname"]),
        Spacer(0, 1.2 * mm),
        Paragraph(f"<b>{_esc(_money(a.get('spend')))}</b> "
                  f'<font color="{_hx(MUTED)}" size="7">SPEND</font>', S["cardspend"]),
        Spacer(0, 2 * mm),
        _counts_row([("Leads", _num(a.get("leads"))),
                     ("Qualified", _num(a.get("qualified_leads"))),
                     ("Booked", _num(a.get("demos_booked"))),
                     ("Completed", _num(a.get("demos_completed")))], S, inner_w),
        Spacer(0, 2 * mm),
        _metric_line("Cost / Lead", _money(a.get("cost_per_lead")), S),
        _metric_line("Cost / Qualified Lead", _money(a.get("cost_per_qualified_lead")), S,
                     status=st.get("cost_per_qualified_lead"), goal="target $200–400"),
        _metric_line("Cost / Demo Booked", _money(a.get("cost_per_demo_booked")), S,
                     status=st.get("cost_per_demo_booked"),
                     goal=f"goal ${g.get('cpd_booked_low')}–{g.get('cpd_booked_high')}" if g else None),
        _metric_line("Cost / Demo Completed", _money(a.get("cost_per_demo_completed")), S,
                     status=st.get("cost_per_demo_completed"),
                     goal=f"goal ${g.get('cpd_completed_low')}–{g.get('cpd_completed_high')}" if g else None),
        _metric_line("CAC", _money(a.get("cac")), S, status=st.get("cac"), goal="target ≤ $2,500"),
    ]
    return _rounded_card(content, width=width, accent=ACCENT)


def _vendor_card(v: dict, S, width: float, red_reasons: list[str] | None) -> Table:
    """One vendor as the console's card; a red-flagged vendor renders in red —
    name, border and its reasons (house rule: flagged items appear in red)."""
    flagged = bool(red_reasons)
    name_col = RED if flagged else ACCENT
    inner_w = width - 18 - 3.5
    content: list = [
        Paragraph(f'<font color="{_hx(name_col)}"><b>{_esc(v.get("vendor", "")).upper()}</b></font>',
                  S["cardname"]),
        Spacer(0, 1.2 * mm),
        Paragraph(f"<b>{_esc(_money(v.get('spend')))}</b> "
                  f'<font color="{_hx(MUTED)}" size="7">SPEND</font>', S["cardspend"]),
        Spacer(0, 2 * mm),
        _counts_row([("Qualified", _num(v.get("qualified_leads"))),
                     ("Booked", _num(v.get("demos_booked"))),
                     ("Completed", _num(v.get("demos_completed")))], S, inner_w),
        Spacer(0, 2 * mm),
        _metric_line("Cost / Qualified Lead", _money(v.get("cost_per_qualified_lead")), S,
                     status="bad" if flagged else None),
        _metric_line("Cost / Demo Booked", _money(v.get("cost_per_demo_booked")), S),
        _metric_line("Cost / Demo Completed", _money(v.get("cost_per_demo_completed")), S),
    ]
    if flagged:
        content.append(Spacer(0, 1.5 * mm))
        content.append(Paragraph(
            f'<font color="{_hx(RED)}">■ {_esc("; ".join(red_reasons or []))}</font>',
            S["cellsmall"]))
    return _rounded_card(content, width=width,
                         bg=RED_TINT if flagged else WHITE,
                         border=RED if flagged else BORDER,
                         accent=RED if flagged else ACCENT)


def _bullets(items: list, S) -> Paragraph:
    return Paragraph("<br/>".join(f"• {_esc(s)}" for s in items), S["cell"])


def report_pdf(report: dict) -> bytes:
    """Render one persisted report run exactly the way MrReportDoc lays it out."""
    S = _styles()
    kind = str(report.get("kind", ""))
    meta = REPORT_META.get(kind, {"label": kind, "eyebrow": "Report"})
    s = report.get("structured") or {}
    is_campaign = kind in CAMPAIGN_KINDS
    summary, recommend = _read_narrative(report.get("markdown") or "")

    story: list = []
    chip = None
    if is_campaign:
        groups = s.get("flag_summary") or []
        reds = sum(g.get("count", 0) for g in groups if g.get("level") == "red")
        warns = sum(g.get("count", 0) for g in groups if g.get("level") != "red")
        chip = _verdict(reds, warns)
    meta_line = []
    period = s.get("period") or {}
    if period.get("label"):
        meta_line.append(f"Reporting period {period['label']}")
    meta_line.append(f"Generated {_fmt_time(report.get('generated_at'))}")
    meta_line.append("Marketing Research agent")
    _band(story, S, meta["eyebrow"], meta["label"], " · ".join(meta_line), chip)
    story.append(Spacer(0, 6 * mm))

    n = 0

    def sec(title: str):
        nonlocal n
        n += 1
        story.append(Spacer(0, 3.5 * mm))
        story.append(_section(n, title, S))
        story.append(Spacer(0, 2.5 * mm))

    # 01 — Executive summary (same gate as the console: only when narrative exists)
    red_vendors = s.get("red_flag_vendors") or []
    if summary:
        sec("Executive summary")
        for para in [p for p in summary.split("\n") if p.strip()]:
            story.append(Paragraph(_esc(para.strip()), S["body"]))
            story.append(Spacer(0, 1.5 * mm))
        if red_vendors:
            story.append(Spacer(0, 1.5 * mm))
            lines: list = [Paragraph(
                f'<font color="{_hx(RED)}"><b>VENDORS ON RED FLAG</b></font>', S["cellsmall"]),
                Spacer(0, 1 * mm)]
            for r in red_vendors:
                lines.append(Paragraph(
                    f'<font color="{_hx(RED)}">■ <b>{_esc(r.get("vendor"))}</b> — '
                    f'{_esc("; ".join(r.get("reasons") or []))}</font>', S["body"]))
                lines.append(Spacer(0, 0.8 * mm))
            story.append(KeepTogether([
                _rounded_card(lines, width=_PAGE_W, bg=RED_TINT, border=RED, accent=RED)]))
        if recommend:
            story.append(Spacer(0, 2.5 * mm))
            story.append(KeepTogether([_rounded_card(
                [Paragraph(f'<font color="{_hx(ACCENT)}"><b>RECOMMEND</b></font>', S["cellsmall"]),
                 Spacer(0, 1 * mm),
                 Paragraph(_esc(recommend), S["body"])],
                width=_PAGE_W, bg=ACCENT_TINT, border=ACCENT, accent=ACCENT)]))

    # 02 — What needs attention: one tinted card per flag group; red flags read
    # in red (house rule 2026-07-27: every flagged item appears in red).
    groups = s.get("flag_summary") or []
    if is_campaign and groups:
        sec("What needs attention")
        cards = []
        for g in groups:
            is_red = g.get("level") == "red"
            col, tint = (RED, RED_TINT) if is_red else (AMBER, AMBER_TINT)
            # the flag text already carries its count ("2 campaigns over…")
            cards.append(_rounded_card(
                [Paragraph(
                    f'<font color="{_hx(col)}"><b>{"RED" if is_red else "WATCH"}</b>  '
                    f'{_esc(g.get("text", ""))}</font>', S["body"])],
                width=(_PAGE_W - 5 * mm) / 2, bg=tint, border=col, accent=col))
        story += _card_grid(cards, per_row=2)

    # 03 — Performance detail (shape depends on the report kind, as on screen)
    sec("Performance detail")
    if is_campaign:
        totals = s.get("totals") or {}
        if totals:
            kpi_cards = []
            kpi_w = (_PAGE_W - 3 * 5 * mm) / 4
            for label, value in (
                ("Total spend", _money(totals.get("spend"))),
                ("Demos completed", _num(totals.get("demos_completed"))),
                ("Cost / completed", _money(totals.get("cost_per_demo_completed"))),
                ("Qualified leads", _num(totals.get("qualified_leads"))),
            ):
                kpi_cards.append(_rounded_card(
                    [Paragraph(f"<b>{_esc(value)}</b>", S["kpival"]),
                     Spacer(0, 0.6 * mm),
                     Paragraph(_esc(label).upper(), S["kpilabel"])],
                    width=kpi_w, bg=CARD_BG, accent=ACCENT))
            story += _card_grid(kpi_cards, per_row=4)
            if totals.get("spend_source") == "sheet_overall":
                story.append(Paragraph(
                    _esc(f"Official sheet total (Overall Report). Vendor-tab sum "
                         f"{_money(totals.get('spend_computed'))} · delta {_money(totals.get('spend_delta'))}."),
                    S["small"]))
                story.append(Spacer(0, 2.5 * mm))
        channels = s.get("channels") or {}
        if channels:
            card_w = (_PAGE_W - 5 * mm) / 2
            story += _card_grid(
                [_channel_card(name, a, S, card_w) for name, a in channels.items()], per_row=2)
    elif kind == "utm_attribution":
        attr = s.get("attribution") or {}
        if attr:
            story.append(_kpi_strip([
                ("Attributed", _pct(attr.get("pct"))),
                ("Leads attributed", _num(attr.get("attributed"))),
                ("Total leads", _num(attr.get("total"))),
            ], S))
            story.append(Spacer(0, 3 * mm))
        areas = s.get("best_practice_areas") or []
        if areas:
            rows = [[Paragraph("<b>Practice area</b>", S["cellsmall"]),
                     Paragraph("<b>Leads</b>", S["cellsmall"]),
                     Paragraph("<b>Qualified rate</b>", S["cellsmall"])]]
            for r in areas:
                rows.append([Paragraph(_esc(r.get("practice_area") or "—"), S["cell"]),
                             Paragraph(_num(r.get("total")), S["cell"]),
                             Paragraph(f"{_round((r.get('qualified_rate') or 0) * 100)}%", S["cell"])])
            story.append(_grid_table(rows))
    elif kind in ("opportunity_report", "icp_signal"):
        ranked = s.get("ranked") or []
        if not ranked:
            story.append(Paragraph("No opportunities in the current dataset.", S["small"]))
        else:
            rows = [[Paragraph(f"<b>{h}</b>", S["cellsmall"]) for h in ("Name", "Type", "Audience", "ICP fit")]]
            for o in ranked:
                rows.append([Paragraph(_esc(o.get("name")), S["cell"]),
                             Paragraph(_esc(o.get("type")), S["cell"]),
                             Paragraph(_num(o.get("audience_size")), S["cell"]),
                             Paragraph(str(_round((o.get("icp_score") or 0) * 100)), S["cell"])])
            story.append(_grid_table(rows))
    elif kind == "competitor_digest":
        comps = s.get("competitors") or []
        if not comps:
            story.append(Paragraph("No competitor scans in the current dataset.", S["small"]))
        for c in comps:
            story.append(Paragraph(
                f"<b>{_esc(c.get('competitor'))}</b> — "
                f"{'Changed' if c.get('changed') else 'No change'}. {_esc(c.get('summary', ''))}",
                S["body"]))
            story.append(Spacer(0, 1.2 * mm))
    elif kind == "daily_movement":
        vendors_mv = s.get("vendors") or []
        if not vendors_mv:
            story.append(Paragraph("No snapshots captured yet.", S["small"]))
        else:
            cols = [("spend.performance", "Spend", True), ("leads.total", "Leads", False),
                    ("leads.qualified", "Qualified", False), ("demos.total_booked_all", "Booked", False),
                    ("demos.completed_all", "Completed", False)]
            rows = [[Paragraph("<b>Vendor</b>", S["cellsmall"])]
                    + [Paragraph(f"<b>{c[1]}</b>", S["cellsmall"]) for c in cols]
                    + [Paragraph("<b>Window</b>", S["cellsmall"])]]
            for v in vendors_mv:
                additive = ((v.get("blocks") or {}).get("team_overall") or {}).get("additive") or {}

                def cell(path, money):
                    d = (additive.get(path) or {}).get("delta")
                    if d is None:
                        return "—"
                    sign = "+" if d > 0 else ""
                    return f"{sign}{_money(d) if money else _num(d)}"

                win = ("month start" if v.get("month_start")
                       else f"{v.get('days')}d since {v.get('since')}" if (v.get("days") or 0) > 1 else "1d")
                rows.append([Paragraph(_esc(v.get("vendor")) + (" ⚠" if v.get("corrected") else ""), S["cell"])]
                            + [Paragraph(cell(p, m), S["cell"]) for p, _l, m in cols]
                            + [Paragraph(_esc(win), S["cell"])])
            story.append(_grid_table(rows))

    # 04 — Vendor detail: the console's vendor cards; red-flagged vendors in red
    vendors = s.get("vendors") or []
    if vendors:
        sec("Vendor detail")
        red_map = {r.get("vendor"): r.get("reasons") or [] for r in red_vendors}
        card_w = (_PAGE_W - 5 * mm) / 2
        story += _card_grid(
            [_vendor_card(v, S, card_w, red_map.get(v.get("vendor"))) for v in vendors],
            per_row=2)

    # 05 — Vendor insights & actions (accent header, zebra rows)
    insights = s.get("vendor_insights") or []
    if insights:
        sec("Vendor insights & actions")
        white_head = ParagraphStyle("wh", parent=S["cellsmall"], textColor=WHITE,
                                    fontName="Helvetica-Bold")
        rows = [[Paragraph("VENDOR", white_head), Paragraph("INSIGHTS", white_head),
                 Paragraph("ACTIONS", white_head)]]
        for r in insights:
            rows.append([Paragraph(f"<b>{_esc(r.get('vendor'))}</b>", S["cell"]),
                         _bullets(r.get("insights") or [], S),
                         _bullets(r.get("actions") or [], S)])
        t = Table(rows, colWidths=[32 * mm, None, None], repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("ROUNDEDCORNERS", [5, 5, 0, 0]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ]
        for i in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), CARD_BG))
        t.setStyle(TableStyle(style))
        story.append(t)

    # last — Appendix, data & provenance (always, as on screen)
    sec("Appendix — data & provenance")
    sources = report.get("sources") or []
    if sources:
        for src in sources:
            leads = f" · {src.get('leads')} leads" if src.get("leads") else ""
            story.append(Paragraph(
                f"{_esc(_source_label(src.get('platform')))} — pulled "
                f"{_fmt_time(src.get('generated_at'))} · {src.get('metrics', 0)} metrics{leads}",
                S["small"]))
    else:
        story.append(Paragraph(
            "Source details weren't recorded for this run (generated before provenance tracking).",
            S["small"]))
    story.append(Paragraph(f"Run ID · {_esc(report.get('id', '—'))}", S["small"]))

    buf = BytesIO()
    _doc(buf, meta["label"]).build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


# --- Leads panel PDF ---------------------------------------------------------

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


def _month_label(ym: str) -> str:
    try:
        y, m = ym.split("-")
        return f"{_MONTH_NAMES[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return str(ym)


def _outcome_cell(n, rate) -> str:
    return f"{_num(n)} · {rate:.0f}%" if _is_num(rate) else _num(n)


def leads_pdf(run: dict, month: str | None = None) -> bytes:
    """Render the Leads panel for one month exactly as LeadsView lays it out.

    ``run`` is the persisted lead_analysis run; ``month`` picks a specific
    "YYYY-MM" block (default: the latest). An unknown month raises ValueError —
    the router turns that into an honest 422, never a substituted month."""
    S = _styles()
    summary = run.get("summary") or {}
    months = summary.get("months") or {}
    active = month or summary.get("latest_month")
    if not months or active not in months:
        raise ValueError(f"No lead-analysis data for {month or 'any month'}.")
    block = months[active]
    vendors = block.get("vendors") or []
    t = block.get("totals") or {}
    flagged = [v for v in vendors if v.get("flags")]
    flag_count = block.get("flag_count", 0)

    story: list = []
    src = str(run.get("tab") or "Lead sheet") + (
        f" in {run.get('source_label')}" if run.get("source_label") else "")
    _band(story, S, "Lead analysis · Marketing", f"Leads — {_month_label(active)}",
          f"From “{src}” · updated {_fmt_time(run.get('generated_at'))} · Marketing Research agent",
          _verdict(flag_count, 0))
    story.append(Spacer(0, 6 * mm))

    n = 0

    def sec(title: str):
        nonlocal n
        n += 1
        story.append(Spacer(0, 3.5 * mm))
        story.append(_section(n, title, S))
        story.append(Spacer(0, 2.5 * mm))

    # 01 — Month at a glance: the console's story line + outcome counts strip.
    sec("Month at a glance")
    nv = len(vendors)
    line = (f"{_num(t.get('booked'))} demos booked across {nv} vendor{'' if nv == 1 else 's'} — "
            f"{_num(t.get('completed'))} completed, {_num(t.get('no_show'))} no-shows, "
            f"{_num(t.get('canceled'))} canceled, {_num(t.get('bad_lead'))} bad leads, "
            f"{_num(t.get('pending'))} upcoming.")
    if t.get("services_sold"):
        line += (f" {_num(t.get('services_sold'))} service"
                 f"{'' if t.get('services_sold') == 1 else 's'} sold ({_money(t.get('mrr'))} MRR).")
    story.append(Paragraph(_esc(line), S["body"]))
    story.append(Spacer(0, 2.5 * mm))
    story.append(_kpi_strip([
        ("Booked", _num(t.get("booked"))),
        ("Completed", _outcome_cell(t.get("completed"), t.get("completed_rate_pct"))),
        ("No-shows", _outcome_cell(t.get("no_show"), t.get("no_show_rate_pct"))),
        ("Canceled", _outcome_cell(t.get("canceled"), t.get("canceled_rate_pct"))),
        ("Bad leads", _outcome_cell(t.get("bad_lead"), t.get("bad_lead_rate_pct"))),
        ("Upcoming", _num(t.get("pending"))),
    ], S))
    mixes = []
    for label, counts in (("Brand", t.get("brands") or {}), ("Source", t.get("sources") or {})):
        if counts:
            ordered = sorted(counts.items(), key=lambda kv: -kv[1])
            mixes.append(f"<b>{label}</b>  " + " · ".join(f"{_esc(k)} {v}" for k, v in ordered))
    if mixes:
        story.append(Spacer(0, 2 * mm))
        story.append(Paragraph("&nbsp;&nbsp;&nbsp;".join(mixes), S["small"]))

    # 02 — Red flags: same card as the console, grouped by vendor, message
    # verbatim (each message names its fix direction). Green line when clean.
    sec("Red flags")
    if flagged:
        lines: list = [Paragraph(
            f'<font color="{_hx(RED)}"><b>{flag_count} RED FLAG{"" if flag_count == 1 else "S"} '
            f'ACROSS {len(flagged)} VENDOR{"" if len(flagged) == 1 else "S"}</b></font>',
            S["cellsmall"]), Spacer(0, 1 * mm)]
        for v in flagged:
            name = v.get("matched_vendor") or v.get("campaign")
            for f in v.get("flags") or []:
                lines.append(Paragraph(
                    f'<font color="{_hx(RED)}">■ <b>{_esc(name)}</b> — {_esc(f.get("message"))}</font>',
                    S["body"]))
                lines.append(Spacer(0, 0.8 * mm))
        story.append(KeepTogether([
            _rounded_card(lines, width=_PAGE_W, bg=RED_TINT, border=RED, accent=RED)]))
    else:
        story.append(Paragraph(
            f'<font color="{_hx(GREEN)}">No red flags this month — outcomes are inside every line.</font>',
            S["body"]))

    # 03 — Vendor table: same columns as the console; flagged cells in red.
    sec("Vendors")
    _FLAG_COL = {"zero_completed": 2, "no_show_rate": 3, "canceled_rate": 4, "bad_lead_rate": 5}
    head = ("Vendor", "Booked", "Completed", "No-shows", "Canceled", "Bad leads",
            "Upcoming", "Contract sent", "Sold")
    rows = [[Paragraph(f"<b>{h}</b>", S["cellsmall"]) for h in head]]
    for v in vendors:
        tripped = {f.get("metric") for f in v.get("flags") or []}
        cells = [
            _esc(v.get("matched_vendor") or v.get("campaign")),
            _num(v.get("booked")),
            _outcome_cell(v.get("completed"), v.get("completed_rate_pct")),
            _outcome_cell(v.get("no_show"), v.get("no_show_rate_pct")),
            _outcome_cell(v.get("canceled"), v.get("canceled_rate_pct")),
            _outcome_cell(v.get("bad_lead"), v.get("bad_lead_rate_pct")),
            _num(v.get("pending")),
            _num((v.get("deal_stages") or {}).get("contract_sent", 0)),
            _num(v.get("services_sold")),
        ]
        red_cols = {col for metric, col in _FLAG_COL.items() if metric in tripped}
        if tripped:
            cells[0] = f'<font color="{_hx(RED)}"><b>{cells[0]}</b></font>'
        rows.append([Paragraph(
            f'<font color="{_hx(RED)}"><b>{c}</b></font>' if i in red_cols else c,
            S["cell"]) for i, c in enumerate(cells)])
    story.append(_grid_table(rows, col_widths=[36 * mm] + [None] * 8))

    # last — provenance: the honest notes the console shows under the table.
    sec("Data notes & provenance")
    unmatched = summary.get("unmatched_campaigns") or []
    if unmatched:
        story.append(Paragraph(_esc(
            "Not matched to any tracker vendor tab: " + ", ".join(unmatched) +
            " — their QL-ratio / booking-rate check couldn't run."), S["small"]))
    for gap in run.get("gaps") or []:
        story.append(Paragraph(_esc(f"Data note: {gap}"), S["small"]))
    story.append(Paragraph(_esc(
        f"Source: {src} · pulled {_fmt_time(run.get('generated_at'))}"
        f" · refreshes with every sheet pull · months on file: "
        + ", ".join(sorted(months))), S["small"]))

    buf = BytesIO()
    _doc(buf, f"Leads — {_month_label(active)}").build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


# --- Vendors panel PDF -------------------------------------------------------

# Same section list + order as VendorsView's Dossier component.
DOSSIER_SECTIONS = [
    ("leads", "Leads"), ("cost_metrics", "Cost metrics"), ("sdr", "SDR"),
    ("vapi", "VAPI"), ("demos", "Demos"), ("demo_outcomes", "Demo outcomes"),
    ("cost_per_demo", "Cost per demo"), ("projected_revenue", "Projected revenue"),
    ("actualized_revenue", "Actualized revenue"),
    ("not_actualized_revenue", "Not actualized revenue"),
    ("inbound_sales_pipeline", "Inbound sales pipeline"), ("kpis", "KPIs"),
]

_MONEY_KEY_RE = None


def _fmt_val(key: str, v) -> str:
    global _MONEY_KEY_RE
    import re

    if _MONEY_KEY_RE is None:
        _MONEY_KEY_RE = re.compile(r"(spend|budget|cost|amount|revenue|mrr|cac|deal|fees|financial|goal)")
    if v is None:
        return "—"
    if not _is_num(v):
        return str(v)
    if key.endswith("_pct"):
        return f"{_num(v)}%"
    return _money(v) if _MONEY_KEY_RE.search(key) else _num(v)


def _human(key: str) -> str:
    import re

    out = re.sub(r"_pct$", " %", key).replace("_", " ")
    return out[:1].upper() + out[1:]


def _pair_val(node) -> float | None:
    if not isinstance(node, dict):
        return None
    p = node.get("performance")
    return p if _is_num(p) else (node.get("investment") if _is_num(node.get("investment")) else None)


def _vendor_stats(team: dict) -> dict:
    """Port of VendorsView.vendorStats — the official summary grid's math."""
    node = lambda k: team.get(k) if isinstance(team.get(k), dict) else {}
    numv = lambda v: v if _is_num(v) else None
    budget = _pair_val(node("budget")) or 0
    spend = _pair_val(node("spend")) or 0
    ql = numv(node("leads").get("qualified")) or 0
    demos = node("demos")
    qdb = (numv(demos.get("qualified_booked_all")) or 0) or (numv(demos.get("total_booked_all")) or 0)
    completed = numv(demos.get("completed_all")) or 0
    sold = numv(node("actualized_revenue").get("services_sold")) or 0
    div = lambda a, b: round(a / b, 2) if b else None
    return {
        "budget": budget, "spend": spend, "ql": ql, "qdb": qdb,
        "completed": completed, "sold": sold,
        "utilized": div(spend * 100, budget), "cpql": div(spend, ql),
        "cpqdb": div(spend, qdb), "show": div(completed * 100, qdb),
    }


def _dossier_rows(block: dict, S) -> list:
    """One flowable list mirroring Dossier: Budget & spend first, then sections."""
    out: list = []

    def rows_table(node: dict) -> Table:
        rows = []
        for k, v in node.items():
            if isinstance(v, dict):
                val = (f"{_fmt_val(k, v.get('performance'))} "
                       f"<font color='#94a3b8' size='6.6'>/ {_fmt_val(k, v.get('investment'))} inv</font>")
            else:
                val = _fmt_val(k, v)
            rows.append([Paragraph(_esc(_human(k)), S["cellsmall"]),
                         Paragraph(f"<b>{val}</b>", S["cell"])])
        t = Table(rows, colWidths=[58 * mm, None])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 2.4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return t

    top = {k: block[k] for k in ("management_fees_investment", "budget", "spend") if k in block}
    groups = [("Budget & spend", top)] + [
        (title, block.get(key)) for key, title in DOSSIER_SECTIONS
        if isinstance(block.get(key), dict)
    ]
    for title, node in groups:
        if not node:
            continue
        out.append(KeepTogether([
            Paragraph(f"<b>{_esc(title).upper()}</b>", S["cellsmall"]),
            Spacer(0, 1 * mm),
            rows_table(node),
        ]))
        out.append(Spacer(0, 3 * mm))
    return out


def vendor_pdf(detail: dict, benchmarks: dict | None = None) -> bytes:
    """Render the Vendors panel dossier exactly as VendorsView lays it out."""
    S = _styles()
    snap = detail.get("snapshot") or {}
    team = (snap.get("canonical") or {}).get("team_overall") or {}
    channels = (snap.get("canonical") or {}).get("channels") or {}
    dates = detail.get("dates") or []

    story: list = []
    _band(story, S, f"Vendor dossier · GID {detail.get('gid', '—')}",
          detail.get("vendor", "Vendor"),
          f"{len(dates)} day{'' if len(dates) == 1 else 's'} captured · showing {snap.get('date', '—')} (MTD)"
          f" · captured {_fmt_time(snap.get('captured_at'))}")
    story.append(Spacer(0, 6 * mm))

    # Official summary — the same 10 cells, same order, same benchmark logic.
    st = _vendor_stats(team)
    tone_ok = lambda cond_good: GREEN if cond_good else RED
    cells = [
        ("Budget", _money(st["budget"]), None),
        ("Spend", _money(st["spend"]), None),
        ("Budget utilized", _pct(st["utilized"]), None),
        ("Qualified leads", _num(st["ql"]), None),
        ("Qual. demos booked", _num(st["qdb"]), None),
        ("Cost / qual. lead", _money(st["cpql"]),
         RED if benchmarks and st["cpql"] is not None and st["cpql"] >= benchmarks.get("cpql_red", 1e18) else None),
        ("Cost / qual. demo booked", _money(st["cpqdb"]),
         tone_ok(st["cpqdb"] < benchmarks.get("cpqdb_max", 0)) if benchmarks and st["cpqdb"] is not None else None),
        ("Demos completed", _num(st["completed"]), None),
        ("Show rate", _pct(st["show"]),
         tone_ok(st["show"] >= benchmarks.get("show_rate_min", 0)) if benchmarks and st["show"] is not None else None),
        ("Services sold (act.)", _num(st["sold"]), None),
    ]
    story.append(_section(1, f"Official summary · {snap.get('month', '')} MTD", S))
    story.append(Spacer(0, 2.5 * mm))
    for chunk_start in range(0, len(cells), 5):
        chunk = cells[chunk_start:chunk_start + 5]
        vals, labels = [], []
        for label, value, tone in chunk:
            colr = tone.hexval().replace("0x", "#") if tone else "#0f172a"
            vals.append(Paragraph(f"<font color='{colr}'><b>{_esc(value)}</b></font>", S["kpival"]))
            labels.append(Paragraph(_esc(label), S["kpilabel"]))
        t = Table([vals, labels])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ("LINEAFTER", (0, 0), (-2, -1), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(t)
        story.append(Spacer(0, 1.5 * mm))

    # Day movement — same five cells as the console strip.
    delta = detail.get("delta") or {}
    additive = ((delta.get("blocks") or {}).get("team_overall") or {}).get("additive") or {}
    if additive:
        story.append(Spacer(0, 2 * mm))
        window = ("month start" if delta.get("month_start")
                  else f"since {delta.get('since')} ({delta.get('days')}d)"
                  if (delta.get("days") or 0) > 1 else "1 day")
        corrected = " · corrected" if delta.get("corrected") else ""
        story.append(_section(2, f"Day movement · {window}{corrected}", S))
        story.append(Spacer(0, 2.5 * mm))
        moves = [("spend.performance", "Spend", True), ("leads.total", "Leads", False),
                 ("leads.qualified", "Qualified", False), ("demos.total_booked_all", "Booked", False),
                 ("demos.completed_all", "Completed", False)]
        vals, labels = [], []
        for path, label, money in moves:
            d = (additive.get(path) or {}).get("delta")
            if d is None:
                txt = "—"
            elif d == 0:
                txt = "$0" if money else "0"
            else:
                # Console shows ▲/▼; the PDF's standard fonts have no such
                # glyph, so the sign carries the direction instead.
                sign = "-" if d < 0 else "+"
                txt = f"{sign}{_money(abs(d)) if money else _num(abs(d))}"
            vals.append(Paragraph(f"<b>{_esc(txt)}</b>", S["cell"]))
            labels.append(Paragraph(_esc(label), S["kpilabel"]))
        t = Table([vals, labels])
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ("LINEAFTER", (0, 0), (-2, -1), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(t)

    # Full dossier, then each channel block — as on screen.
    story.append(Spacer(0, 3 * mm))
    story.append(_section(3, "Dossier", S))
    story.append(Spacer(0, 2.5 * mm))
    story += _dossier_rows(team, S)

    sec_n = 3
    for name, block in channels.items():
        sec_n += 1
        story.append(Spacer(0, 2 * mm))
        story.append(_section(sec_n, f"{str(name).upper()} channel", S))
        story.append(Spacer(0, 2.5 * mm))
        story += _dossier_rows(block, S)

    buf = BytesIO()
    _doc(buf, f"Vendor dossier — {detail.get('vendor', '')}").build(
        story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
