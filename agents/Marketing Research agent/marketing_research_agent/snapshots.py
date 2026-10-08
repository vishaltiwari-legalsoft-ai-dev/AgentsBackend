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
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
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


class SnapshotStoreError(RuntimeError):
    """The durable snapshot store could not be read.

    Raised rather than returning the disk-only (on Cloud Run: empty) list, so a
    Firestore outage cannot render as "this vendor has no snapshots". Same
    contract as ``firestore_repo.count_collection``; the HTTP layer turns it
    into a 502."""


def _cloud_list(slug: str | None = None, month: str | None = None) -> list[dict] | None:
    """Durable snapshots, or ``None`` when the read FAILED (``[]`` = none stored).

    ``slug``/``month`` are pushed into the query so a per-vendor or per-month
    read no longer bills and ships the whole collection. Both are equality
    filters on stored fields, so Firestore serves them from the automatic
    single-field indexes — no composite index is required, and the result is
    identical to the Python filter that ``list_snapshots`` still applies to the
    disk copies.

    NOTE: ``mr_snapshots`` carries NO tenant key (its doc id is
    ``{slug}_{date}``), so this query is still cross-workspace. Fixing that
    needs a schema change + backfill — see docs/db-target-design.html.
    """
    try:
        from google.cloud import firestore as _fs

        from app.services import firestore_repo
        query = firestore_repo._db().collection(_COLLECTION)
        if slug:
            query = query.where(filter=_fs.FieldFilter("vendor_slug", "==", slug))
        if month:
            query = query.where(filter=_fs.FieldFilter("month", "==", month))
        return [d.to_dict() for d in query.stream()]
    except Exception:
        logger.warning("snapshot cloud list failed", exc_info=True)
        return None


def _merged(slug: str | None = None, month: str | None = None) -> dict[str, dict]:
    """Durable snapshots merged with the local copies, keyed by doc id (the
    local copy of a same-day capture wins). Raises :class:`SnapshotStoreError`
    when the durable store could not be read."""
    by_id: dict[str, dict] = {}
    if _use_cloud():  # durable history first; local same-day copies override
        cloud = _cloud_list(slug, month)
        if cloud is None:
            raise SnapshotStoreError("the snapshot store could not be read")
        for snap in cloud:
            if isinstance(snap, dict) and snap.get("vendor_slug") and snap.get("date"):
                by_id[_doc_id(snap["vendor_slug"], snap["date"])] = snap
    for p in sorted(_root().glob("*.json")):
        try:
            by_id[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return by_id


def get_snapshot(slug: str, date_iso: str) -> dict | None:
    p = _root() / f"{_doc_id(slug, date_iso)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if _use_cloud():  # disk is ephemeral on Cloud Run — Firestore is durable
        return _cloud_get(_doc_id(slug, date_iso))
    return None


def list_snapshots(slug: str | None = None, month: str | None = None,
                   meta_only: bool = False) -> list[dict]:
    """Vendor snapshots, date-sorted.

    Every doc ever stored under the filters, same-day stale captures included;
    for "the vendors as of a date" use :func:`vendor_sweep`."""
    by_id = _merged(slug, month)
    out = []
    for snap in by_id.values():
        # Rollup-tab snapshots duplicate the vendor tabs — never list them.
        if "overall" in (snap.get("vendor_slug") or ""):
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
    """Capture every tracker-format tab; skip the rest; never abort the run.

    Workbook layout rule (user, 2026-07-27): vendors are the tabs BEFORE the
    Overall Report. The roll-up itself is captured last — the portfolio bar
    reads it as the official totals — and nothing after it is touched.

    Hidden tabs are skipped, the same rule the dataset path applies
    (``sheets_source.fetch_all_trackers``): a hidden tab is an archive or a
    Looker dump, never a live vendor, and the two paths must agree on what a
    vendor is or the snapshot totals drift from the dataset's.

    Every doc this run writes is stamped with one ``sweep_id`` (and ``hidden``,
    always ``False`` given the skip — present so a reader can tell a stamped doc
    from a legacy one). The refresh cron captures every 15 minutes and each run
    overwrites ``{slug}_{date}``, so a tab renamed or deleted mid-day leaves its
    earlier capture behind under the old slug; :func:`_split_sweep` uses the
    ``sweep_id`` to keep only the day's final run."""
    results = []
    sweep_id = uuid.uuid4().hex[:12]
    for g in grids:
        try:
            if getattr(g, "hidden", False):
                results.append({"tab": g.title, "skipped": True, "reason": "hidden"})
            else:
                snap = capture_tab(g.rows, title=g.title, gid=g.gid, year=year, today=today)
                if snap is None:
                    results.append({"tab": g.title, "skipped": True})
                else:
                    snap["hidden"] = False
                    snap["sweep_id"] = sweep_id
                    save_snapshot(snap)
                    results.append({"tab": g.title, "slug": snap["vendor_slug"], "captured": True})
            # Checked even for a hidden roll-up: stopping here is the safe side,
            # because everything after the Overall tab is ops sheets.
            if "overall" in slugify(g.title):
                break
        except Exception as exc:  # one bad tab must not kill the daily run
            logger.exception("snapshot capture failed for tab %s", g.title)
            results.append({"tab": g.title, "error": str(exc)})
    return results


# --- one day's sweep (bounded reads) ------------------------------------------
#
# A "sweep" is the set of tabs the day's FINAL capture run wrote. Readers that
# want "the vendors as of date D" must read exactly that set, and nothing else:
#
# * Reading the whole collection to find it (what ``portfolio()`` used to do)
#   costs every doc ever captured (~1,650 docs / ~46 MB in 2026-10, +15-20/day).
#   A sweep is one ``date ==`` query (14-28 docs) plus, to find the date, one
#   ``order_by(date) limit 1`` read. Both run on ``date``'s automatic
#   single-field index: NO composite index is needed.
# * The docs of one date are NOT all one sweep. The refresh cron captures every
#   15 minutes and each run overwrites ``{slug}_{date}``, so a tab renamed,
#   deleted or moved past the Overall tab during the day keeps its earlier
#   capture under its old slug. On 2026-09-02 eight such docs ("Copy of Flytech
#   Meta LS", "AB Twitter LS (New)", "DanteAgency LI Googlex", …) sat beside the
#   final run's 20 and lifted the vendor sum from $95,600 / $2,737 / 18 leads
#   (the team's published figures) to $117,200 / $2,906 / 20. Same pattern on
#   09-08, 09-16, 09-22, 09-29 and 10-01.
#
# Exclusion rule, in order:
#   1. ``hidden`` is True                      -> reason "hidden"
#   2. not written by the day's final run      -> reason "stale_capture"
#      * stamped docs: a different ``sweep_id`` than the newest doc's;
#      * legacy docs (no ``sweep_id``): outside the chain of captures, walking
#        back from the newest, whose consecutive ``captured_at`` gaps are all
#        <= :data:`_RUN_GAP_SECONDS`. Measured over 2026-08-20..10-08: one run
#        spans <= 2.5 s with gaps <= 0.3 s; separate runs are >= 15 min apart.
# Works for every historical day with no backfill and no Sheets call.

#: Largest gap between two consecutive tab writes that still counts as one
#: capture run (legacy docs only). Observed max 0.3 s; runs are 900 s apart.
_RUN_GAP_SECONDS = 120.0

#: Fields read when only a sweep's MEMBERSHIP is needed (not its numbers).
_SWEEP_META_FIELDS = ("vendor", "vendor_slug", "gid", "date", "month",
                      "captured_at", "hidden", "sweep_id")

_YM_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_IN_STEM = re.compile(r"_(\d{4}-\d{2}-\d{2})$")


def _is_rollup_slug(slug: str | None) -> bool:
    return "overall" in (slug or "")


def _cloud_newest_date(lo: str | None, hi: str | None, *, oldest: bool = False) -> str | None:
    """Newest (or, with ``oldest``, oldest) ``date`` in ``[lo, hi]`` — ONE
    document read, ``date`` field only.

    A range and an ``order_by`` on the SAME field are served by its automatic
    single-field index. Raises :class:`SnapshotStoreError` on a failed read."""
    try:
        from google.cloud import firestore as _fs

        from app.services import firestore_repo
        query = firestore_repo._db().collection(_COLLECTION)
        if lo:
            query = query.where(filter=_fs.FieldFilter("date", ">=", lo))
        if hi:
            query = query.where(filter=_fs.FieldFilter("date", "<=", hi))
        direction = _fs.Query.ASCENDING if oldest else _fs.Query.DESCENDING
        query = query.order_by("date", direction=direction).limit(1).select(["date"])
        for doc in query.stream():
            return (doc.to_dict() or {}).get("date")
        return None
    except Exception as exc:
        logger.warning("snapshot newest-date read failed", exc_info=True)
        raise SnapshotStoreError("the snapshot store could not be read") from exc


def _cloud_on_date(date_iso: str, fields: tuple[str, ...] | None = None) -> list[dict]:
    """Every doc captured on ``date_iso`` (``date ==``: 14-28 docs). ``fields``
    projects the read down to membership metadata. Raises on a failed read."""
    try:
        from google.cloud import firestore as _fs

        from app.services import firestore_repo
        query = (firestore_repo._db().collection(_COLLECTION)
                 .where(filter=_fs.FieldFilter("date", "==", date_iso)))
        if fields:
            query = query.select(list(fields))
        return [d.to_dict() for d in query.stream()]
    except Exception as exc:
        logger.warning("snapshot date read failed for %s", date_iso, exc_info=True)
        raise SnapshotStoreError("the snapshot store could not be read") from exc


def _disk_dates() -> set[str]:
    out = set()
    for p in _root().glob("*.json"):
        m = _DATE_IN_STEM.search(p.stem)
        if m:
            out.add(m.group(1))
    return out


def _newest_date(lo: str | None = None, hi: str | None = None) -> str | None:
    """Newest captured date within ``[lo, hi]`` across the durable store and
    this instance's local copies."""
    found = [d for d in _disk_dates() if (lo is None or d >= lo) and (hi is None or d <= hi)]
    if _use_cloud():
        d = _cloud_newest_date(lo, hi)
        if d:
            found.append(d)
    return max(found) if found else None


def _oldest_date() -> str | None:
    found = list(_disk_dates())
    if _use_cloud():
        d = _cloud_newest_date(None, None, oldest=True)
        if d:
            found.append(d)
    return min(found) if found else None


def _docs_on(date_iso: str, fields: tuple[str, ...] | None = None) -> list[dict]:
    """The docs of one date, durable merged with local (local same-id wins)."""
    by_id: dict[str, dict] = {}
    if _use_cloud():
        for snap in _cloud_on_date(date_iso, fields):
            if isinstance(snap, dict) and snap.get("vendor_slug"):
                by_id[_doc_id(snap["vendor_slug"], date_iso)] = snap
    for p in _root().glob(f"*_{date_iso}.json"):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(snap, dict) and snap.get("date") == date_iso:
            by_id[p.stem] = snap
    return list(by_id.values())


def _ts(snap: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(snap["captured_at"])
    except Exception:
        return None


def _split_sweep(docs: list[dict]) -> tuple[list[dict], dict | None, list[dict]]:
    """``(vendor docs, roll-up doc or None, excluded)`` for one date's docs.
    See the rule at the top of this section."""
    excluded: list[dict] = []

    def drop(snap: dict, reason: str) -> None:
        excluded.append({"tab": snap.get("vendor") or snap.get("vendor_slug"),
                         "slug": snap.get("vendor_slug"), "reason": reason})

    live = []
    for s in docs:
        if s.get("hidden") is True:
            drop(s, "hidden")
        else:
            live.append(s)

    timed = sorted((s for s in live if _ts(s) is not None), key=_ts, reverse=True)
    if timed:
        anchor = timed[0]
        if anchor.get("sweep_id"):
            in_run = {id(s) for s in live if s.get("sweep_id") == anchor["sweep_id"]}
        else:
            in_run = {id(anchor)}
            for newer, older in zip(timed, timed[1:]):
                if older.get("sweep_id") or (
                        (_ts(newer) - _ts(older)).total_seconds() > _RUN_GAP_SECONDS):
                    break
                in_run.add(id(older))
        # A doc with no captured_at cannot be placed in time; it predates every
        # run that stamps one, so it is kept only on a day with no timed docs.
        kept = []
        for s in live:
            if id(s) in in_run:
                kept.append(s)
            else:
                drop(s, "stale_capture")
        live = kept

    rollups = [s for s in live if _is_rollup_slug(s.get("vendor_slug"))]
    vendors = sorted((s for s in live if not _is_rollup_slug(s.get("vendor_slug"))),
                     key=lambda s: s.get("vendor_slug") or "")
    rollup = max(rollups, key=lambda s: s.get("captured_at") or "") if rollups else None
    excluded.sort(key=lambda e: (e["reason"], e["slug"] or ""))
    return vendors, rollup, excluded


def _sweep(date_iso: str, fields: tuple[str, ...] | None = None) -> dict:
    vendors, rollup, excluded = _split_sweep(_docs_on(date_iso, fields))
    return {"date": date_iso, "docs": vendors, "excluded": excluded, "rollup": rollup}


def _month_bounds(year_month: str) -> tuple[str, str]:
    if not isinstance(year_month, str) or not _YM_RE.match(year_month):
        raise ValueError(f"year_month must be 'YYYY-MM', got {year_month!r}")
    return f"{year_month}-01", f"{year_month}-31"   # string bound; no day 32


def vendor_sweep(year_month: str | None = None) -> dict | None:
    """Newest sweep date within ``year_month`` ('YYYY-MM'; None = newest overall).

    Returns ``{'date': 'YYYY-MM-DD', 'docs': [<snapshot doc dicts, same shape as
    stored, roll-up EXCLUDED, sorted by vendor_slug>], 'excluded': [{'tab':
    <title>, 'slug': <slug>, 'reason': 'hidden' | 'stale_capture'}], 'rollup':
    <the Overall tab's doc from the same run, or None>}``, or ``None`` when no
    sweep exists in range — including when the newest day's docs are ALL
    excluded or roll-up only (a month of nothing but copy tabs is no month).

    Cost: 1 read to find the date + one ``date ==`` query (14-28 docs). No
    composite index. Raises :class:`SnapshotStoreError` when the store cannot be
    read, and ``ValueError`` for a malformed ``year_month``."""
    lo, hi = _month_bounds(year_month) if year_month is not None else (None, None)
    d = _newest_date(lo, hi)
    if not d:
        return None
    sweep = _sweep(d)
    return sweep if sweep["docs"] else None


#: How far past ``limit`` :func:`sweep_months` walks back through months with
#: no sweep before giving up — the bound on a store with gaps.
_MONTH_WALK_SLACK = 12


def _prev_ym(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def _month_has_sweep(ym: str) -> bool:
    """:func:`vendor_sweep` would return something for ``ym`` — checked on a
    MEMBERSHIP projection (no numbers shipped): 1 + 14-28 small reads."""
    d = _newest_date(*_month_bounds(ym))
    return bool(d) and bool(_sweep(d, _SWEEP_META_FIELDS)["docs"])


def sweep_months(limit: int = 12) -> list[str]:
    """YYYY-MM months that have at least one vendor sweep, newest first.

    Exactly the months for which :func:`vendor_sweep` returns a sweep (same
    exclusion rule). Never scans the collection: 2 reads for the store's
    newest/oldest dates, then per month walked 1 date probe + one projected
    ``date ==`` membership read (14-28 docs). The per-month checks run
    concurrently. A full year is ~2 + 12 x ~22 = ~270 small projected reads,
    against ~1,650 full docs for the old whole-collection read. Walks at most
    ``limit + 12`` months back, and never past the oldest captured date.
    Raises :class:`SnapshotStoreError` when the store cannot be read."""
    if limit < 1:
        return []
    newest, oldest = _newest_date(), _oldest_date()
    if newest is None or oldest is None:
        return []
    months, ym = [], newest[:7]
    while ym >= oldest[:7] and len(months) < limit + _MONTH_WALK_SLACK:
        months.append(ym)
        ym = _prev_ym(ym)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, len(months))) as pool:
        has = list(pool.map(_month_has_sweep, months))   # re-raises a store error
    return [m for m, ok in zip(months, has) if ok][:limit]


def previous_month_sweep(year_month: str) -> dict | None:
    """Same shape as :func:`vendor_sweep`: the newest sweep of the month BEFORE
    ``year_month`` (its month-end MTD state), or ``None``."""
    _month_bounds(year_month)
    return vendor_sweep(_prev_ym(year_month))


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
    """Per-vendor movement for `date_iso` (default: latest captured date).

    Rollup-tab snapshots are excluded — their numbers duplicate the vendors."""
    all_snaps = [s for s in list_snapshots() if "overall" not in s["vendor_slug"]]
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
    the sheet's "official" figures.

    The bar is stamped "<month> MTD · as of <date>", so only the tabs the
    newest sweep captured may be in it — one :func:`_sweep` (a renamed or
    offboarded tab's last snapshot never leaks into a later bar, and a same-day
    stale capture never doubles a vendor). It used to read the WHOLE collection
    per call to find that sweep. Now: 1 read for the date + one ``date ==``
    query, plus a projected membership read of the sweep before it so a vendor
    that dropped out is still NAMED in ``vendors_excluded``, never silently
    discarded."""
    import calendar as _cal

    newest_date = _newest_date(hi=date_iso)
    if newest_date is None:
        return None
    sweep = _sweep(newest_date)
    latest = {s["vendor_slug"]: s for s in sweep["docs"]}
    if not latest:
        return None
    newest = newest_date
    gone = {e["slug"] for e in sweep["excluded"] if e.get("slug")}
    prior_date = _newest_date(
        hi=(date.fromisoformat(newest) - timedelta(days=1)).isoformat())
    if prior_date:
        gone |= {s["vendor_slug"] for s in _sweep(prior_date, _SWEEP_META_FIELDS)["docs"]}
    excluded = sorted(gone - set(latest))

    def raw(node: dict, *path, pair: bool = False) -> float | None:
        """The cell as the sheet reports it — ``None`` when the ROW IS ABSENT,
        ``0.0`` when the row says zero. Collapsing those two is what let a
        genuine zero fall through to a different basis below."""
        cur = node
        for p in path:
            cur = (cur or {}).get(p)
        if pair and isinstance(cur, dict):
            v = cur.get("performance")
            cur = v if v is not None else cur.get("investment")
        return None if cur is None or isinstance(cur, dict) else float(cur)

    def val(node: dict, *path, pair: bool = False) -> float:
        return raw(node, *path, pair=pair) or 0.0

    budget = spend = 0.0
    leads = qualified = qdb = completed = sold = 0
    for s in latest.values():
        t = s["canonical"].get("team_overall", {})
        # Non-media vendors (Website) stay out of blended spend/budget — the
        # sheet's own total works that way; their funnel counts still add up.
        if s["vendor_slug"] not in config.NON_MEDIA_VENDOR_SLUGS:
            budget += val(t, "budget", pair=True)
            spend += val(t, "spend", pair=True)
        leads += int(val(t, "leads", "total"))
        qualified += int(val(t, "leads", "qualified"))
        b = val(t, "demos", "qualified_booked_all")
        qdb += int(b if b else val(t, "demos", "total_booked_all"))
        completed += int(val(t, "demos", "completed_all"))
        sold += int(val(t, "actualized_revenue", "services_sold"))

    # Official override (2026-07-27): the Overall tab aggregates ledger/raw
    # sources with no vendor tab (Referral, Websites, …), so the vendor sum
    # above always undercounts. When its snapshot exists for this month, ITS
    # figures are the summary bar; the vendor sum stays as the audit trail.
    source, computed_spend, computed_budget = "vendor_sum", round(spend, 2), round(budget, 2)
    # The roll-up is the last tab of the same capture run, so it is read from
    # the same sweep. A run whose roll-up failed falls back to the vendor sum
    # and says so via `source`, rather than borrowing an earlier day's roll-up.
    rollup = sweep["rollup"]
    if rollup:
        t = (rollup.get("canonical") or {}).get("team_overall", {})
        o_spend = raw(t, "spend", pair=True)
        if o_spend is not None:  # an empty parse must never blank the whole bar
            # Take the roll-up's figures as a SET. Each field used to fall back
            # to the vendor sum on a falsy value, so a month the sheet honestly
            # reports as 0 came back as the vendor sum instead — and the bar then
            # divided the roll-up's spend by the vendor sum's counts, printing a
            # cost-per figure that exists nowhere on the sheet. A row that is
            # genuinely absent (None) still falls back, and says so via `source`.
            fell_back = False

            def official(default: float, *path, pair: bool = False) -> float:
                nonlocal fell_back
                v = raw(t, *path, pair=pair)
                if v is None:
                    fell_back = True
                    return default
                return v

            o_qdb = raw(t, "demos", "qualified_booked_all")
            if not o_qdb:  # the roll-up reports 0/absent qualified — use all-demos
                o_qdb = raw(t, "demos", "total_booked_all")
            spend = o_spend
            budget = official(budget, "budget", pair=True)
            leads = int(official(leads, "leads", "total"))
            qualified = int(official(qualified, "leads", "qualified"))
            qdb = int(qdb if o_qdb is None else o_qdb)
            fell_back = fell_back or o_qdb is None
            completed = int(official(completed, "demos", "completed_all"))
            sold = int(official(sold, "actualized_revenue", "services_sold"))
            source = "sheet_overall_partial" if fell_back else "sheet_overall"

    div = lambda n, d: round(n / d, 2) if d else None
    day = int(newest[8:10])
    year, month = int(newest[:4]), int(newest[5:7])
    days_in_month = _cal.monthrange(year, month)[1]
    return {
        "date": newest,
        "month": newest[:7],
        "vendors": len(latest),
        # Tabs with an older last snapshot — renamed, offboarded, or missed by
        # the newest sweep. Named so a shrinking vendor count is explainable.
        "vendors_excluded": excluded,
        "total_budget": round(budget, 2),
        "total_spend": round(spend, 2),
        "budget_utilized_pct": div(spend * 100, budget),
        "leads": leads,
        "qualified_leads": qualified,
        "cost_per_qualified_lead": div(spend, qualified),
        "qual_demos_booked": qdb,
        "cost_per_qual_demo_booked": div(spend, qdb),
        "demos_completed": completed,
        "cost_per_demo_completed": div(spend, completed),
        "show_rate_pct": div(completed * 100, qdb),
        "services_sold": sold,
        "source": source,
        "computed_spend": computed_spend,
        "computed_budget": computed_budget,
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
