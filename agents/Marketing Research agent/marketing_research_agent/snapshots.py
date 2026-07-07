"""Daily vendor snapshots — capture, store, deltas, GCS export (spec 2026-07-08).

The tracker workbook holds cumulative month-to-date values that are overwritten
daily. This module freezes each tracker tab once a day (raw labels + the user's
canonical schema), so history survives and day-over-day movement is computable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .sources.sheets_source import _find_blocks, _month_columns, _num

logger = logging.getLogger("agentos.mr.snapshots")

_DEFAULT_DIR = Path(__file__).resolve().parents[1] / "snapshots"
_COLLECTION = "mr_snapshots"

# --- canonical mapping -------------------------------------------------------
# (dot_path, sheet label lowercase, occurrence within block, value mode)
# mode: "first" = Performance, fallback Investment · "perf" / "inv" = that column
# only · "pair" = {"performance": v, "investment": v}. Matching walks the block
# in row order; duplicate labels are disambiguated by occurrence (1-based).
_TEAM_MAP: list[tuple[str, str, int, str]] = [
    ("management_fees_investment", "management fees", 1, "inv"),
    ("budget", "budget", 1, "pair"),
    ("spend", "spend", 1, "pair"),
    ("leads.total", "leads", 1, "first"),
    ("leads.qualified", "qualified leads", 1, "first"),
    ("leads.qualified_ratio_pct", "qualified lead ratio", 1, "first"),
    ("leads.lost_dnc_bad_lead", "lost dnc (bad lead)", 1, "first"),
    ("leads.not_valid_applicant", "not a valid lead (applicant)", 1, "first"),
    ("leads.not_partnership_fit", "not partnership-fit (lead)", 1, "first"),
    ("leads.wrong_contact_info", "wrong contact info", 1, "first"),
    ("cost_metrics.cost_per_lead_performance", "cost per lead", 1, "perf"),
    ("cost_metrics.cost_per_lead_investment", "cost per lead", 1, "inv"),
    ("cost_metrics.cost_per_qualified_lead", "cost per qualified lead", 1, "first"),
    ("sdr.demos_booked", "sdr demos booked", 1, "first"),
    ("sdr.inbound_demo_booked", "sdr inbound demo booked", 1, "first"),
    ("sdr.rescheduled_demo_booked", "sdr rescheduled demo booked", 1, "first"),
    ("sdr.inbound_bad_lead_helper", "sdr inbound bad lead helper", 1, "first"),
    ("sdr.demos_completed", "sdr demos completed", 1, "first"),
    ("sdr.resched_completed", "sdr resched completed", 1, "first"),
    ("sdr.inbound_completed", "sdr inbound completed", 1, "first"),
    ("sdr.demo_completed_ratio_pct", "sdr demo completed ratio", 1, "first"),
    ("vapi.demos_booked", "vapi demos booked", 1, "first"),
    ("vapi.demos_completed", "vapi demos completed", 1, "first"),
    ("vapi.new_demo", "vapi new demo", 1, "first"),
    ("vapi.new_demos_completed", "vapi new demos completed", 1, "first"),
    ("vapi.resched", "vapi resched", 1, "first"),
    ("vapi.resched_demos_completed", "vapi resched demos completed", 1, "first"),
    ("vapi.demo_completed_ratio_pct", "vapi demo completed ratio", 1, "first"),
    ("demos.total_booked_all", "total demos booked (sdr+vapi+direct)", 1, "first"),
    ("demos.total_booked_direct", "total demos booked (direct)", 1, "first"),
    ("demos.leads_to_demo_booked_overall_pct", "leads to demo booked overall", 1, "first"),
    ("demos.leads_to_qualified_demo_booked_pct", "leads to qualified demo booked", 1, "first"),
    ("demos.qualified_booked_direct", "qualified demos booked (direct)", 1, "first"),
    ("demos.qualified_booked_all", "qualified demos booked (sdr+vapi+direct)", 1, "first"),
    ("demos.qualified_ratio_over_total_pct", "qualified demos ratio over total demos", 1, "first"),
    ("demos.total_completed_direct", "total demos completed (direct)", 1, "first"),
    ("demos.completed_all", "demos completed (sdr+vapi+direct)", 1, "first"),
    ("demos.show_up_rate_all_pct", "total show up rate (%) (sdr+vapi+direct)", 1, "first"),
    ("demos.show_up_rate_direct_pct", "total show up rate (%) (direct)", 1, "first"),
    ("demos.qualified_lead_to_demo_booked_pct", "qualified lead to demo booked (%)", 1, "first"),
    ("demo_outcomes.hot_leads_follow_up_lt_90d", "hot leads - follow up <90 days", 1, "first"),
    ("demo_outcomes.cold_stage_3mo", "cold stage 3 months", 1, "first"),
    ("demo_outcomes.cold_stage_6mo", "cold stage 6 months", 1, "first"),
    ("demo_outcomes.cold_stage_12mo", "cold stage 12 months", 1, "first"),
    ("demo_outcomes.no_show", "no show", 1, "first"),
    ("demo_outcomes.canceled", "canceled", 1, "first"),
    ("cost_per_demo.qualified_demo_booked_direct", "cost per qualified demo booked (direct)", 1, "first"),
    ("cost_per_demo.qualified_demo_booked_all", "cost per qualified demo booked (sdr+vapi+direct)", 1, "first"),
    ("cost_per_demo.demo_booked_all", "cost per demo booked (sdr+vapi+direct)", 1, "first"),
    ("cost_per_demo.demo_completed_direct", "cost per demo completed (direct demos)", 1, "first"),
    ("cost_per_demo.demo_completed_all", "cost per demo completed (sdr+vapi+direct)", 1, "first"),
    ("projected_revenue.new_clients_actualized", "number of projected new clients (actualized)", 1, "first"),
    ("projected_revenue.services_sold_actualized", "total projected services sold (actualized)", 1, "first"),
    ("projected_revenue.total_amount_sold_actualized", "projected total amount sold ($) actualized", 1, "first"),
    ("projected_revenue.mrr_without_setup_fee_actualized", "projected mrr from new sales w/o set up fees (actualized)", 1, "first"),
    ("actualized_revenue.revenue_clients", "number of revenue clients (actualized)", 1, "first"),
    ("actualized_revenue.services_sold", "total services sold (actualized)", 1, "first"),
    ("actualized_revenue.amount_sold", "revenue amount sold (actualized)", 1, "first"),
    ("actualized_revenue.amount_sold_without_setup_fee", "revenue amount sold w/o setup fee (actualized)", 1, "first"),
    ("not_actualized_revenue.projected_new_clients", "number of projected new clients (not actualized)", 1, "first"),
    ("not_actualized_revenue.services_sold", "total services sold (not actualized)", 1, "first"),
    ("not_actualized_revenue.amount_sold", "revenue amount sold ($) (not actualized)", 1, "first"),
    ("not_actualized_revenue.amount_sold_without_setup_fee", "revenue amount sold w/o setup fee (not actualized)", 1, "first"),
    ("not_actualized_revenue.paying_new_clients", "number of paying new clients (not actualized)", 1, "first"),
    ("inbound_sales_pipeline.paying_clients", "number of paying new clients (inbound sales pipeline)", 1, "first"),
    ("inbound_sales_pipeline.services_sold", "total services sold (inbound sales pipeline)", 1, "first"),
    ("inbound_sales_pipeline.amount_sold", "revenue amount sold (inbound sales pipeline)", 1, "first"),
    ("inbound_sales_pipeline.amount_sold_without_setup_fee", "revenue amount sold w/o setup fee (inbound sales pipeline)", 1, "first"),
    ("kpis.revenue_target_pct", "percentage of revenue target goal", 1, "first"),
    ("kpis.revenue_sold_goal", "revenue sold goal amount", 1, "first"),
    ("kpis.revenue_lead_financials", "revenue amount sold (lead financials)", 1, "first"),
    ("kpis.confirmed_all_revenue_mrr", "confirmed all revenue (mrr lead financials)", 1, "first"),
    ("kpis.average_deal_amount", "average deal amount", 1, "first"),
    ("kpis.conversion_rate_pct", "conversion rate (%)", 1, "first"),
    ("kpis.roas_pct", "roas", 1, "first"),
    ("kpis.cac", "cac", 1, "first"),
]

_CHANNEL_MAP: list[tuple[str, str, int, str]] = [
    ("budget", "budget", 1, "pair"),
    ("spend", "spend", 1, "pair"),
    ("leads.total", "leads", 1, "first"),
    ("leads.qualified", "qualified leads", 1, "first"),
    ("leads.qualified_ratio_pct", "qualified lead ratio", 1, "first"),
    ("leads.lost_dnc_bad_lead", "lost dnc (bad lead)", 1, "first"),
    ("cost_metrics.cost_per_lead", "cost per lead", 1, "first"),
    ("cost_metrics.cost_per_qualified_lead", "cost per qualified lead", 1, "first"),
    ("sdr.demos_booked", "sdr demos booked", 1, "first"),
    ("sdr.inbound_demo_booked", "sdr inbound demo booked", 1, "first"),
    ("sdr.rescheduled_demo_booked", "sdr rescheduled demo booked", 1, "first"),
    ("sdr.demos_completed", "sdr demos completed", 1, "first"),
    ("sdr.resched_completed", "sdr resched completed", 1, "first"),
    ("sdr.inbound_completed", "sdr inbound completed", 1, "first"),
    ("sdr.demo_completed_ratio_pct", "sdr demo completed ratio", 1, "first"),
    ("vapi.demos_booked", "vapi demos booked", 1, "first"),
    ("vapi.demos_completed", "vapi demos completed", 1, "first"),
    ("vapi.new_demo", "vapi new demo", 1, "first"),
    ("vapi.new_demos_completed", "vapi new demos completed", 1, "first"),
    ("vapi.resched", "vapi resched", 1, "first"),
    ("vapi.resched_demos_completed", "vapi resched demos completed", 1, "first"),
    ("vapi.demo_completed_ratio_pct", "vapi demo completed ratio", 1, "first"),
    ("demos.total_booked_all", "total demos booked (sdr+vapi+direct)", 1, "first"),
    ("demos.total_booked_direct", "total demos booked (direct)", 1, "first"),
    ("demos.leads_to_demo_booked_pct", "leads to demo booked (overall)", 1, "first"),
    ("demos.qualified_leads_to_qualified_demo_booked_pct", "qualified leads to qualified demo booked (overall)", 1, "first"),
    ("demos.qualified_booked", "qualified demos booked", 1, "first"),
    ("demos.qualified_ratio_over_total_pct", "qualified demos ratio over total demos", 1, "first"),
    ("demos.total_completed_direct", "total demos completed (direct)", 1, "first"),
    ("demos.completed_all", "demos completed (sdr+vapi+direct)", 1, "first"),
    ("demos.show_up_rate_all_pct", "total show up rate (%) (sdr+vapi+direct)", 1, "first"),
    ("demos.show_up_rate_qualified_pct", "total show up rate (%) (qualified)", 1, "first"),
    ("demo_outcomes.no_show", "no show", 1, "first"),
    ("demo_outcomes.canceled", "canceled", 1, "first"),
    ("cost_per_demo.qualified_demo_booked_direct", "cost per qualified demo booked (direct)", 1, "first"),
    ("cost_per_demo.qualified_demo_booked_all", "cost per qualified demo booked (sdr+vapi+direct)", 1, "first"),
    ("cost_per_demo.completed_direct", "cost per demo completed (direct demos)", 1, "first"),
    ("cost_per_demo.completed_all", "cost per demo completed (sdr+vapi+direct)", 1, "first"),
    ("projected_revenue.new_clients_actualized", "number of projected new clients (actualized)", 1, "first"),
    ("projected_revenue.services_sold_actualized", "total projected services sold (actualized)", 1, "first"),
    ("projected_revenue.total_amount_sold_actualized", "projected total amount sold ($) actualized", 1, "first"),
    ("projected_revenue.mrr_without_setup_fee_actualized", "projected mrr from new sales w/o set up fees (actualized)", 1, "first"),
    ("actualized_revenue.revenue_clients", "number of revenue clients (actualized)", 1, "first"),
    ("actualized_revenue.services_sold", "total services sold (actualized)", 1, "first"),
    ("actualized_revenue.amount_sold", "revenue amount sold (actualized)", 1, "first"),
    ("actualized_revenue.amount_sold_without_setup_fee", "revenue amount sold w/o setup fee (actualized)", 1, "first"),
    ("not_actualized_revenue.projected_new_clients", "number of projected new clients (not actualized)", 1, "first"),
    ("not_actualized_revenue.services_sold", "total services sold (not actualized)", 1, "first"),
    ("not_actualized_revenue.amount_sold", "revenue amount sold ($) (not actualized)", 1, "first"),
    ("not_actualized_revenue.paying_new_clients", "number of paying new clients (not actualized)", 1, "first"),
    ("kpis.average_deal_amount", "average deal amount", 1, "first"),
    ("kpis.conversion_rate_pct", "conversion rate (%)", 1, "first"),
    ("kpis.roas_pct", "roas", 1, "first"),
    ("kpis.cac", "cac", 1, "first"),
]


def slugify(title: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower())).strip("-")


def is_tracker_grid(rows: list[list[str]]) -> bool:
    """Tracker = month (Performance)/(Investment) pairs in the header + a Spend row."""
    if not rows:
        return False
    if not _month_columns(rows[0]):
        return False
    labels = {(r[0] if r else "").strip().lower() for r in rows[:60]}
    return "spend" in labels


def _walk_block(rows, start, end, perf_col, inv_col) -> list[dict]:
    out = []
    for i in range(start, min(end, len(rows))):
        label = (rows[i][0] if rows[i] else "").strip()
        if not label:
            continue
        row = rows[i]
        cell = lambda c: (row[c] if 0 <= c < len(row) else "")
        out.append({
            "label": label,
            "performance": _num(cell(perf_col)),
            "investment": _num(cell(inv_col)) if inv_col >= 0 else None,
        })
    return out


def _set_path(root: dict, path: str, value) -> None:
    parts = path.split(".")
    node = root
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def _canonical_block(raw_rows: list[dict], mapping: list[tuple[str, str, int, str]]) -> dict:
    # occurrence counter per normalized label, in row order
    seen: dict[str, int] = {}
    by_label_occ: dict[tuple[str, int], dict] = {}
    for r in raw_rows:
        key = re.sub(r"\s+", " ", r["label"].strip().lower())
        seen[key] = seen.get(key, 0) + 1
        by_label_occ[(key, seen[key])] = r

    out: dict = {}
    unmapped = 0
    for path, label, occ, mode in mapping:
        r = by_label_occ.get((label, occ))
        if r is None:
            unmapped += 1
            _set_path(out, path, None)
            continue
        perf, inv = r["performance"], r["investment"]
        if mode == "pair":
            _set_path(out, path, {"performance": perf, "investment": inv})
        elif mode == "perf":
            _set_path(out, path, perf)
        elif mode == "inv":
            _set_path(out, path, inv)
        else:  # first: Performance, fallback Investment
            _set_path(out, path, perf if perf is not None else inv)
    if unmapped:
        logger.warning("snapshot canonical: %d mapped fields had no matching sheet row", unmapped)
    return out


def capture_tab(rows: list[list[str]], *, title: str, gid: int, year: int, today: date) -> dict | None:
    """Freeze one tracker tab for `today`. Returns None for non-tracker grids or
    when the grid has no column for today's month."""
    if not is_tracker_grid(rows):
        return None
    months = {m: (p, i) for m, p, i in _month_columns(rows[0])}
    cur = months.get(today.month)
    if cur is None:
        return None
    prev = months.get(today.month - 1) if today.month > 1 else None

    # Force the top block to be the roll-up scope: a vendor title like
    # "Meta 360 RA" would otherwise classify the whole team block as META.
    blocks = _find_blocks(rows, "All")
    raw: dict = {"team_overall": [], "channels": {}}
    prev_raw: dict = {"team_overall": [], "channels": {}}
    canonical: dict = {"team_overall": {}, "channels": {}}
    for idx, (channel, start, end) in enumerate(blocks):
        cur_rows = _walk_block(rows, start, end, cur[0], cur[1])
        prev_rows = _walk_block(rows, start, end, prev[0], prev[1]) if prev else []
        if idx == 0:
            raw["team_overall"] = cur_rows
            prev_raw["team_overall"] = prev_rows
            canonical["team_overall"] = _canonical_block(cur_rows, _TEAM_MAP)
        else:
            key = channel.strip().lower()
            raw["channels"][key] = cur_rows
            prev_raw["channels"][key] = prev_rows
            canonical["channels"][key] = _canonical_block(cur_rows, _CHANNEL_MAP)

    return {
        "vendor": title.strip(),
        "vendor_slug": slugify(title),
        "gid": gid,
        "date": today.isoformat(),
        "month": f"{today.year:04d}-{today.month:02d}",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw,
        "canonical": canonical,
        "prev_month_raw": prev_raw,
    }


# --- store (disk source of truth; Firestore mirrored when cloud-configured) ---

def _root() -> Path:
    root = Path(os.environ.get("MR_SNAPSHOTS_DIR") or _DEFAULT_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _use_cloud() -> bool:
    if os.environ.get("MR_OFFLINE") == "1":
        return False
    try:
        from app.services import firestore_repo  # noqa: F401
        return bool(os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT"))
    except Exception:
        return False


def _doc_id(slug: str, date_iso: str) -> str:
    return f"{slug}_{date_iso}"


def save_snapshot(snap: dict) -> None:
    doc_id = _doc_id(snap["vendor_slug"], snap["date"])
    (_root() / f"{doc_id}.json").write_text(
        json.dumps(snap, default=str, indent=1), encoding="utf-8")
    if _use_cloud():
        try:
            from app.services import firestore_repo
            firestore_repo._db().collection(_COLLECTION).document(doc_id).set(snap)
        except Exception:
            logger.warning("snapshot cloud save failed for %s", doc_id)


def get_snapshot(slug: str, date_iso: str) -> dict | None:
    p = _root() / f"{_doc_id(slug, date_iso)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def list_snapshots(slug: str | None = None, month: str | None = None,
                   meta_only: bool = False) -> list[dict]:
    out = []
    for p in sorted(_root().glob("*.json")):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if slug and snap.get("vendor_slug") != slug:
            continue
        if month and snap.get("month") != month:
            continue
        if meta_only:
            snap = {k: snap.get(k) for k in ("vendor", "vendor_slug", "gid", "date", "month", "captured_at")}
        out.append(snap)
    out.sort(key=lambda s: (s.get("vendor_slug") or "", s.get("date") or ""))
    return out


def capture_workbook(grids, *, year: int, today: date) -> list[dict]:
    """Capture every tracker-format tab; skip the rest; never abort the run."""
    results = []
    for g in grids:
        try:
            snap = capture_tab(g.rows, title=g.title, gid=g.gid, year=year, today=today)
            if snap is None:
                results.append({"tab": g.title, "skipped": True})
                continue
            save_snapshot(snap)
            results.append({"tab": g.title, "slug": snap["vendor_slug"], "captured": True})
        except Exception as exc:  # one bad tab must not kill the daily run
            logger.exception("snapshot capture failed for tab %s", g.title)
            results.append({"tab": g.title, "error": str(exc)})
    return results
