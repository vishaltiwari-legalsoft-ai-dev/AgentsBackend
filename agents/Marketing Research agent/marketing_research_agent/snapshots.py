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
    ("demos.show_up_rate_qualified_pct", "total show up rate (%) (qualified demos)", 1, "first"),
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
        # Same source of truth firestore_repo connects with (GCP_PROJECT_ID env);
        # Cloud Run does NOT set GOOGLE_CLOUD_PROJECT/GCP_PROJECT.
        from app.config import settings
        from app.services import firestore_repo  # noqa: F401
        return bool(settings.gcp_project_id)
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


def _cloud_get(doc_id: str) -> dict | None:
    try:
        from app.services import firestore_repo
        doc = firestore_repo._db().collection(_COLLECTION).document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None


def _cloud_list() -> list[dict]:
    try:
        from app.services import firestore_repo
        return [d.to_dict() for d in firestore_repo._db().collection(_COLLECTION).stream()]
    except Exception:
        logger.warning("snapshot cloud list failed")
        return []


def get_snapshot(slug: str, date_iso: str) -> dict | None:
    p = _root() / f"{_doc_id(slug, date_iso)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if _use_cloud():  # disk is ephemeral on Cloud Run — Firestore is durable
        return _cloud_get(_doc_id(slug, date_iso))
    return None


def list_snapshots(slug: str | None = None, month: str | None = None,
                   meta_only: bool = False) -> list[dict]:
    by_id: dict[str, dict] = {}
    if _use_cloud():  # durable history first; local same-day copies override
        for snap in _cloud_list():
            if isinstance(snap, dict) and snap.get("vendor_slug") and snap.get("date"):
                by_id[_doc_id(snap["vendor_slug"], snap["date"])] = snap
    for p in sorted(_root().glob("*.json")):
        try:
            by_id[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    out = []
    for snap in by_id.values():
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


# --- delta engine (computed on read) -----------------------------------------

# A leaf is non-additive (a rate) when its path matches one of these tokens.
_RATE_TOKENS = ("_pct", "ratio", "cost_", "average", "goal", "roas", "cac", "rate")

# Rates the engine can recompute from day components: path -> (numerator, denominator, scale)
_RECOMPUTE = {
    "cost_metrics.cost_per_lead_performance": ("spend.performance", "leads.total", 1.0),
    "cost_metrics.cost_per_lead": ("spend.performance", "leads.total", 1.0),
    "cost_metrics.cost_per_qualified_lead": ("spend.performance", "leads.qualified", 1.0),
    "cost_per_demo.demo_booked_all": ("spend.performance", "demos.total_booked_all", 1.0),
    "cost_per_demo.demo_completed_all": ("spend.performance", "demos.completed_all", 1.0),
    "leads.qualified_ratio_pct": ("leads.qualified", "leads.total", 100.0),
    "demos.show_up_rate_all_pct": ("demos.completed_all", "demos.total_booked_all", 100.0),
}


def _leaves(node: dict, prefix: str = "") -> dict[str, float | None]:
    """Flatten a canonical block to dot-path -> numeric leaf."""
    out: dict[str, float | None] = {}
    for k, v in (node or {}).items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_leaves(v, path + "."))
        else:
            out[path] = v
    return out


def _is_rate(path: str) -> bool:
    return any(t in path for t in _RATE_TOKENS)


def _block_delta(curr_block: dict, prev_block: dict | None) -> dict:
    cur = _leaves(curr_block)
    prv = _leaves(prev_block or {})
    additive: dict = {}
    rates: dict = {}
    day: dict[str, float | None] = {}
    for path, value in cur.items():
        if _is_rate(path):
            continue
        prev_v = prv.get(path)
        if value is None and prev_v is None:
            delta = None
        elif prev_v is None:
            delta = value
        elif value is None:
            delta = None
        else:
            delta = round(value - prev_v, 2)
        day[path] = delta
        additive[path] = {"delta": delta, "mtd": value,
                          "corrected": bool(delta is not None and delta < 0)}
    for path, value in cur.items():
        if not _is_rate(path):
            continue
        rule = _RECOMPUTE.get(path)
        if rule:
            num, den, scale = rule
            n, d = day.get(num), day.get(den)
            v = round(n / d * scale, 2) if (n is not None and d not in (None, 0)) else None
            rates[path] = {"value": v, "mode": "recomputed"}
        else:
            rates[path] = {"value": value, "mode": "mtd"}
    return {"additive": additive, "rates": rates}


def compute_delta(curr: dict, prev: dict | None) -> dict:
    """Movement between two snapshots of the same vendor (prev may be None at
    month start). Never fabricates per-day numbers across gaps — `days` says
    how many days the delta spans."""
    month_start = prev is None
    days = 0 if month_start else (
        (date.fromisoformat(curr["date"]) - date.fromisoformat(prev["date"])).days)
    blocks = {"team_overall": _block_delta(
        curr["canonical"].get("team_overall", {}),
        (prev or {}).get("canonical", {}).get("team_overall"))}
    channels = {}
    for name, block in (curr["canonical"].get("channels") or {}).items():
        channels[name] = _block_delta(
            block, ((prev or {}).get("canonical", {}).get("channels") or {}).get(name))
    blocks["channels"] = channels
    corrected = any(
        f["corrected"] for f in blocks["team_overall"]["additive"].values()
    ) or any(
        f["corrected"] for ch in channels.values() for f in ch["additive"].values()
    )
    return {
        "vendor": curr["vendor"],
        "vendor_slug": curr["vendor_slug"],
        "date": curr["date"],
        "since": None if month_start else prev["date"],
        "days": days,
        "month_start": month_start,
        "corrected": corrected,
        "blocks": blocks,
    }


def deltas_for(date_iso: str | None = None) -> list[dict]:
    """Per-vendor movement for `date_iso` (default: latest captured date)."""
    all_snaps = list_snapshots()
    if not all_snaps:
        return []
    if date_iso is None:
        date_iso = max(s["date"] for s in all_snaps)
    out = []
    by_vendor: dict[str, list[dict]] = {}
    for s in all_snaps:
        by_vendor.setdefault(s["vendor_slug"], []).append(s)
    for slug, snaps in sorted(by_vendor.items()):
        curr = next((s for s in snaps if s["date"] == date_iso), None)
        if curr is None:
            continue
        prior = [s for s in snaps if s["month"] == curr["month"] and s["date"] < date_iso]
        prev = max(prior, key=lambda s: s["date"]) if prior else None
        out.append(compute_delta(curr, prev))
    return out


def vendor_detail(slug: str, date_iso: str | None = None) -> dict | None:
    """One vendor's dossier: all captured dates + the requested (default latest)
    snapshot with its day movement. Pure read."""
    snaps = list_snapshots(slug=slug)
    if not snaps:
        return None
    dates = [s["date"] for s in snaps]
    if date_iso is None:
        date_iso = dates[-1]
    curr = next((s for s in snaps if s["date"] == date_iso), None)
    if curr is None:
        return None
    prior = [s for s in snaps if s["month"] == curr["month"] and s["date"] < date_iso]
    prev = max(prior, key=lambda s: s["date"]) if prior else None
    return {"vendor": curr["vendor"], "vendor_slug": slug, "gid": curr["gid"],
            "dates": dates, "snapshot": curr, "delta": compute_delta(curr, prev)}


def portfolio(date_iso: str | None = None) -> dict | None:
    """Official cross-vendor totals for the Vendors tab summary bar.

    Paid vendors only (the Overall roll-up snapshot is excluded), each vendor's
    latest snapshot, summed on the Performance basis — the way the team reads
    the sheet's "official" figures."""
    import calendar as _cal

    latest: dict[str, dict] = {}
    for s in list_snapshots():
        if "overall" in s["vendor_slug"]:
            continue
        if date_iso and s["date"] > date_iso:
            continue
        latest[s["vendor_slug"]] = s  # list is date-sorted per vendor; last wins
    if not latest:
        return None

    def val(node: dict, *path, pair: bool = False) -> float:
        cur = node
        for p in path:
            cur = (cur or {}).get(p)
        if pair and isinstance(cur, dict):
            v = cur.get("performance")
            cur = v if v is not None else cur.get("investment")
        return float(cur or 0)

    budget = spend = 0.0
    leads = qualified = qdb = completed = sold = 0
    newest = max(s["date"] for s in latest.values())
    for s in latest.values():
        t = s["canonical"].get("team_overall", {})
        budget += val(t, "budget", pair=True)
        spend += val(t, "spend", pair=True)
        leads += int(val(t, "leads", "total"))
        qualified += int(val(t, "leads", "qualified"))
        b = val(t, "demos", "qualified_booked_all")
        qdb += int(b if b else val(t, "demos", "total_booked_all"))
        completed += int(val(t, "demos", "completed_all"))
        sold += int(val(t, "actualized_revenue", "services_sold"))

    div = lambda n, d: round(n / d, 2) if d else None
    day = int(newest[8:10])
    year, month = int(newest[:4]), int(newest[5:7])
    days_in_month = _cal.monthrange(year, month)[1]
    return {
        "date": newest,
        "month": newest[:7],
        "vendors": len(latest),
        "total_budget": round(budget, 2),
        "total_spend": round(spend, 2),
        "budget_utilized_pct": div(spend * 100, budget),
        "leads": leads,
        "qualified_leads": qualified,
        "cost_per_qualified_lead": div(spend, qualified),
        "qual_demos_booked": qdb,
        "cost_per_qual_demo_booked": div(spend, qdb),
        "demos_completed": completed,
        "show_rate_pct": div(completed * 100, qdb),
        "services_sold": sold,
        "pacing": {"day": day, "days_in_month": days_in_month,
                   "expected_pct": round(day / days_in_month * 100)},
        "benchmarks": {"cpqdb_max": 500, "ql_ratio_min": 40,
                       "show_rate_min": 80, "cac_target": 2500, "cpql_red": 600},
    }


# --- GCS export (the user's per-vendor month JSON) ----------------------------

_SCHEMA_VERSION = "1.0.0"


def month_export(slug: str, month: str) -> dict | None:
    """One vendor-month in the user's snapshot schema (see spec: hand-made JSON
    is the golden shape). Regenerable at any time from the store."""
    snaps = list_snapshots(slug=slug, month=month)
    if not snaps:
        return None
    latest = snaps[-1]
    team = latest["canonical"].get("team_overall", {})
    kpis = team.get("kpis") or {}
    budget = team.get("budget") or {}
    return {
        "metadata": {
            "schema_version": _SCHEMA_VERSION,
            "vendor": latest["vendor"],
            "vendor_slug": slug,
            "description": ("Daily marketing performance tracker. Each daily_snapshots entry "
                            "holds cumulative month-to-date values captured from the spreadsheet. "
                            "Subtract the previous day's snapshot to get a single day's movement."),
            "last_updated": latest["date"],
            "months_tracked": [month],
        },
        "months": {
            month: {
                "label": datetime.strptime(month, "%Y-%m").strftime("%B %Y"),
                "targets": {
                    "budget_performance": budget.get("performance"),
                    "budget_investment": budget.get("investment"),
                    "revenue_sold_goal": kpis.get("revenue_sold_goal"),
                },
                "daily_snapshots": {
                    s["date"]: {"team_overall": s["canonical"].get("team_overall", {}),
                                "channels": s["canonical"].get("channels", {})}
                    for s in snaps
                },
            }
        },
    }


def export_all_to_gcs(today: date) -> list[str]:
    """Write mr-snapshots/<slug>/<month>.json for every vendor captured this
    month. Export failure never fails a capture — files are regenerable."""
    if os.environ.get("MR_OFFLINE") == "1":
        return []
    try:
        from app.services import storage
        if not storage.is_configured():
            logger.info("snapshot export skipped: GCS not configured")
            return []
    except Exception:
        return []
    month = f"{today.year:04d}-{today.month:02d}"
    written = []
    slugs = sorted({s["vendor_slug"] for s in list_snapshots(month=month, meta_only=True)})
    for slug in slugs:
        doc = month_export(slug, month)
        if doc is None:
            continue
        path = f"mr-snapshots/{slug}/{month}.json"
        try:
            storage._upload(path, json.dumps(doc, indent=1).encode("utf-8"), "application/json")
            written.append(path)
        except Exception:
            logger.warning("snapshot GCS export failed for %s (will rebuild next capture)", path)
    return written
