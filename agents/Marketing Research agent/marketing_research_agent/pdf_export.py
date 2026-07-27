"""Console-faithful PDF exports (2026-07-27).

Two downloads, each mirroring the panel the user is looking at, section for
section, in the console's own order and wording:

- ``report_pdf``  → the Reports panel document (``MrReportDoc``): eyebrow /
  title / verdict stamp, executive summary + red-flag vendors + recommend,
  what-needs-attention, performance detail (KPI strip + channel cards),
  vendor detail cards, vendor insights & actions, appendix.
- ``vendor_pdf``  → the Vendors panel dossier (``VendorsView``): official
  summary grid, day movement, budget & spend + section groups, channel blocks.

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
BORDER = colors.HexColor("#e2e8f0")
CARD_BG = colors.HexColor("#f8fafc")
HEAD_BG = colors.HexColor("#f1f5f9")
RED = colors.HexColor("#dc2626")
AMBER = colors.HexColor("#d97706")
GREEN = colors.HexColor("#16a34a")

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
                                  textColor=ACCENT, leading=10, spaceAfter=2,
                                  # console eyebrow is uppercase-tracked
                                  ),
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
        "cardname": ParagraphStyle("cardname", fontName="Helvetica-Bold", fontSize=9,
                                   textColor=ACCENT, leading=12),
    }


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

def _channel_rows(channels: dict, S) -> list[list]:
    head = ["Channel", "Spend", "Leads", "Qualified", "Booked", "Completed",
            "C/Lead", "C/Qual. lead", "C/Booked", "C/Completed", "CAC"]
    rows: list[list] = [[Paragraph(f"<b>{_esc(h)}</b>", S["cellsmall"]) for h in head]]
    for name, a in channels.items():
        rows.append([
            Paragraph(f"<b>{_esc(name)}</b>", S["cell"]),
            Paragraph(_money(a.get("spend")), S["cell"]),
            Paragraph(_num(a.get("leads")), S["cell"]),
            Paragraph(_num(a.get("qualified_leads")), S["cell"]),
            Paragraph(_num(a.get("demos_booked")), S["cell"]),
            Paragraph(_num(a.get("demos_completed")), S["cell"]),
            Paragraph(_money(a.get("cost_per_lead")), S["cell"]),
            Paragraph(_money(a.get("cost_per_qualified_lead")), S["cell"]),
            Paragraph(_money(a.get("cost_per_demo_booked")), S["cell"]),
            Paragraph(_money(a.get("cost_per_demo_completed")), S["cell"]),
            Paragraph(_money(a.get("cac")), S["cell"]),
        ])
    return rows


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
    story.append(Paragraph(_esc(meta["eyebrow"]).upper(), S["eyebrow"]))
    title_bits = [Paragraph(_esc(meta["label"]), S["title"])]
    if is_campaign:
        groups = s.get("flag_summary") or []
        reds = sum(g.get("count", 0) for g in groups if g.get("level") == "red")
        warns = sum(g.get("count", 0) for g in groups if g.get("level") != "red")
        label, col = _verdict(reds, warns)
        title_bits.append(Paragraph(
            f'<font color="{col.hexval().replace("0x", "#")}"><b>{_esc(label)}</b></font>',
            S["meta"]))
    story += title_bits
    meta_line = []
    period = s.get("period") or {}
    if period.get("label"):
        meta_line.append(f"Reporting period {period['label']}")
    meta_line.append(f"Generated {_fmt_time(report.get('generated_at'))}")
    meta_line.append("Marketing Research agent")
    story.append(Paragraph(_esc(" · ".join(meta_line)), S["meta"]))
    story.append(Spacer(0, 5 * mm))

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
            story.append(Spacer(0, 1 * mm))
            story.append(Paragraph("<b>Vendors on red flag</b>", S["body"]))
            for r in red_vendors:
                story.append(Paragraph(
                    f"• <b>{_esc(r.get('vendor'))}</b> — {_esc('; '.join(r.get('reasons') or []))}",
                    S["body"]))
        if recommend:
            story.append(Spacer(0, 2 * mm))
            rec = Table([[Paragraph("<b>Recommend</b>", S["cellsmall"]),
                          Paragraph(_esc(recommend), S["cell"])]], colWidths=[22 * mm, None])
            rec.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(rec)

    # 02 — What needs attention (campaign kinds, when flags exist)
    groups = s.get("flag_summary") or []
    if is_campaign and groups:
        sec("What needs attention")
        for g in groups:
            # The flag text already carries its count ("2 campaigns over…") —
            # the console's count badge becomes a level-colored bullet here.
            col = RED if g.get("level") == "red" else AMBER
            story.append(Paragraph(
                f'<font color="{col.hexval().replace("0x", "#")}"><b>•</b></font>'
                f'  {_esc(g.get("text", ""))}', S["body"]))
            story.append(Spacer(0, 1 * mm))

    # 03 — Performance detail (shape depends on the report kind, as on screen)
    sec("Performance detail")
    if is_campaign:
        totals = s.get("totals") or {}
        if totals:
            story.append(_kpi_strip([
                ("Total spend", _money(totals.get("spend"))),
                ("Demos completed", _num(totals.get("demos_completed"))),
                ("Cost / completed", _money(totals.get("cost_per_demo_completed"))),
                ("Qualified leads", _num(totals.get("qualified_leads"))),
            ], S))
            if totals.get("spend_source") == "sheet_overall":
                story.append(Spacer(0, 1.2 * mm))
                story.append(Paragraph(
                    _esc(f"Official sheet total (Overall Report). Vendor-tab sum "
                         f"{_money(totals.get('spend_computed'))} · delta {_money(totals.get('spend_delta'))}."),
                    S["small"]))
            story.append(Spacer(0, 3 * mm))
        channels = s.get("channels") or {}
        if channels:
            story.append(_grid_table(_channel_rows(channels, S)))
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

    # 04 — Vendor detail (the console's vendor cards, one row per vendor)
    vendors = s.get("vendors") or []
    if vendors:
        sec("Vendor detail")
        head = ["Vendor", "Spend", "Qualified", "Booked", "Completed",
                "C/Qual. lead", "C/Demo booked", "C/Demo completed"]
        rows = [[Paragraph(f"<b>{h}</b>", S["cellsmall"]) for h in head]]
        for v in vendors:
            rows.append([
                Paragraph(f"<b>{_esc(v.get('vendor'))}</b>", S["cell"]),
                Paragraph(_money(v.get("spend")), S["cell"]),
                Paragraph(_num(v.get("qualified_leads")), S["cell"]),
                Paragraph(_num(v.get("demos_booked")), S["cell"]),
                Paragraph(_num(v.get("demos_completed")), S["cell"]),
                Paragraph(_money(v.get("cost_per_qualified_lead")), S["cell"]),
                Paragraph(_money(v.get("cost_per_demo_booked")), S["cell"]),
                Paragraph(_money(v.get("cost_per_demo_completed")), S["cell"]),
            ])
        story.append(_grid_table(rows))

    # 05 — Vendor insights & actions
    insights = s.get("vendor_insights") or []
    if insights:
        sec("Vendor insights & actions")
        rows = [[Paragraph("<b>Vendor</b>", S["cellsmall"]),
                 Paragraph("<b>Insights</b>", S["cellsmall"]),
                 Paragraph("<b>Actions</b>", S["cellsmall"])]]
        for r in insights:
            rows.append([Paragraph(f"<b>{_esc(r.get('vendor'))}</b>", S["cell"]),
                         _bullets(r.get("insights") or [], S),
                         _bullets(r.get("actions") or [], S)])
        story.append(_grid_table(rows, col_widths=[32 * mm, None, None]))

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
    story.append(Paragraph(f"VENDOR DOSSIER · GID {_esc(detail.get('gid', '—'))}", S["eyebrow"]))
    story.append(Paragraph(_esc(detail.get("vendor", "Vendor")), S["title"]))
    story.append(Paragraph(_esc(
        f"{len(dates)} day{'' if len(dates) == 1 else 's'} captured · showing {snap.get('date', '—')} (MTD)"
        f" · captured {_fmt_time(snap.get('captured_at'))}"), S["meta"]))
    story.append(Spacer(0, 5 * mm))

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
