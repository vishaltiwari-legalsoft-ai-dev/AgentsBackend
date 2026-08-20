"""2026 performance goals + red-flag thresholds, encoded verbatim as data.

Source: the requirements doc "2026 Goals" section. The verbatim values are the
DEFAULTS; the team can edit any figure from the UI. Edits persist to Firestore
(``mr_config/targets__{user_id}``) with a JSON file (``MR_TARGETS_FILE``) as the
local / offline copy — Cloud Run's disk is ephemeral, so a file-only store
silently reset the desk's edits on every redeploy. All threshold logic lives in
``evaluate`` and reads the effective (merged) targets.

**Targets are per workspace.** They were one shared document until 2026-08-21,
so any desk's edit re-flagged every other desk's dashboard. Every entry point
here now takes the workspace it is answering for; the helpers below take the
resolved targets dict instead, so nothing downstream can reach the store and
pick a workspace by accident.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schemas import CampaignMetric, Flag

logger = logging.getLogger(__name__)

_TARGETS_FILE_DEFAULT = Path(__file__).resolve().parents[1] / "targets.json"
_MR_CONFIG_COLLECTION = "mr_config"
#: The pre-tenancy document every workspace shared. Read-only from here on:
#: seed source for the migration in ``_seed_from_legacy``, never written,
#: never deleted by this module.
LEGACY_TARGETS_DOC_ID = "targets"

# --- Report rules (requirements §3.1) -------------------------------------
COST_PER_BOOKING_FLAG = 150.0   # flag campaigns where cost-per-booking > $150
CONVERSION_DROP_PCT = 0.30      # flag >30% drop vs prior 7-day average

# --- Red-flag thresholds (requirements "2026 Goals" metric table) ---------
SPEND_NO_DEMO_LIMIT = 3000.0    # $3000+ spend with no demo
CPQL_RED = 600.0                # cost per qualified lead red flag
CAC_RED = 3000.0                # CAC red flag
MGMT_FEE_LIMIT = 3000.0         # management fees under $3000/month

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "cost_per_booking_flag": COST_PER_BOOKING_FLAG,
    "conversion_drop_pct": CONVERSION_DROP_PCT,
    "spend_no_demo_limit": SPEND_NO_DEMO_LIMIT,
    "cost_per_qualified_lead_red": CPQL_RED,
    "cost_per_qualified_lead_target_low": 200.0,
    "cost_per_qualified_lead_target_high": 400.0,
    "cac_red": CAC_RED,
    "cac_target": 2500.0,
    "mgmt_fee_limit": MGMT_FEE_LIMIT,
    # Lead-analysis flag lines (requirements doc 2026-08-10). Percent units;
    # rates are over resolved demos (see lead_analysis.py).
    "bad_lead_rate_red": 30.0,
    "no_show_rate_red": 30.0,
    "canceled_rate_red": 20.0,
    "zero_completed_min_demos": 3.0,
    "ql_ratio_great": 75.0,
    "booking_rate_broken": 15.0,
}


@dataclass(frozen=True)
class ChannelGoal:
    channel: str
    cpd_booked_low: float
    cpd_booked_high: float
    cpd_completed_low: float
    cpd_completed_high: float
    completed_demo_pct: float  # e.g. 0.55 == 55%


# Verbatim from "PER CHANNEL PERFORMANCE GOAL".
CHANNEL_GOALS: dict[str, ChannelGoal] = {
    "Email": ChannelGoal("Email", 350, 400, 450, 600, 0.55),
    "META": ChannelGoal("META", 400, 550, 700, 850, 0.55),
    "Google": ChannelGoal("Google", 550, 750, 850, 1000, 0.75),
    "Websites": ChannelGoal("Websites", 60, 75, 100, 125, 0.65),
    "Total": ChannelGoal("Total", 500, 650, 850, 1000, 0.63),
}

# Collective all-brand targets (requirements "2026 ALL BRAND COLLECTIVE TOTAL").
COLLECTIVE = {
    "qualified_demos_goal": (2800, 3000),
    "completed_demos_goal": 2000,
    "cost_per_qualified_demo_booked": (500, 650),
    "cost_per_demo_completed": (850, 1000),
    "qualified_lead_ratio": 0.75,
    "cost_per_qualified_lead_target": (200, 400),
    "revenue_sold_goal": 185000,
}


# --- editable targets (per-workspace overrides store) -----------------------
#
# Targets were ONE document for the whole deployment until 2026-08-21: any
# workspace's edit re-flagged every other workspace's dashboard, and
# ``GET /mr/config`` mirrored the changed figure back to everyone. They are
# keyed per workspace now — ``mr_config/targets__{user_id}`` in Firestore, a
# ``users`` map in the local file — and the old shared document is kept as a
# READ-ONLY seed source so nobody lost the figures they were relying on.

def _targets_path() -> Path:
    return Path(os.environ.get("MR_TARGETS_FILE") or _TARGETS_FILE_DEFAULT)


def targets_doc_id(user_id: object) -> str:
    """One workspace's targets document. Written down here and nowhere else.

    The tenant key is stringified because a document id *is* a string, which
    means ``7`` and ``"7"`` land on one document where MR's run endpoints keep
    them apart (see ``test_tenant_id_type_contract.py``). Nothing in the sign-in
    path mints a non-string id, so this is a note for whoever unifies the tenant
    key, not a live divergence.
    """
    return f"targets__{user_id}"


def _legacy_seed_enabled() -> bool:
    """Whether a workspace with no document of its own may inherit the legacy one.

    Set ``MR_TARGETS_LEGACY_SEED=0`` once the desks that were using the shared
    document have signed in; after that a new workspace starts from the verbatim
    2026 defaults instead of inheriting another desk's edits.
    """
    return os.environ.get("MR_TARGETS_LEGACY_SEED", "1") != "0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _doc(doc_id: str):
    from app.services import firestore_repo

    return firestore_repo._db().collection(_MR_CONFIG_COLLECTION).document(doc_id)


def _load_file() -> dict:
    """The whole local targets file: the per-workspace ``users`` map plus the
    pre-tenancy top-level block that seeds it."""
    p = _targets_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _legacy_overrides() -> dict:
    """The pre-tenancy shared overrides, or ``{}``. Never written to from here."""
    if _use_cloud():
        try:
            snap = _doc(LEGACY_TARGETS_DOC_ID).get()
            if snap.exists:
                data = snap.to_dict()
                return data if isinstance(data, dict) else {}
            return {}
        except Exception:
            logger.warning("MR legacy targets cloud read failed; falling back to disk")
    file = _load_file()
    return {k: file[k] for k in ("thresholds", "channel_goals")
            if isinstance(file.get(k), dict)}


def _load_own(user_id: object) -> tuple[dict | None, bool]:
    """``(overrides, store_was_readable)`` for one workspace.

    ``(None, True)`` means this workspace genuinely has no document yet — the
    ONLY state the legacy seed may act on. A failed read must never look like
    "absent": seeding on top of an unreadable store would overwrite a desk's
    real edits with the pre-tenancy ones. Same ``None``-means-failed contract as
    ``runs._cloud_list``.
    """
    if _use_cloud():
        try:
            snap = _doc(targets_doc_id(user_id)).get()
            if not snap.exists:
                return None, True
            data = snap.to_dict()
            return (data if isinstance(data, dict) else {}), True
        except Exception:
            logger.warning("MR targets cloud read failed; falling back to disk")
            row = (_load_file().get("users") or {}).get(str(user_id))
            return (row if isinstance(row, dict) else None), False
    row = (_load_file().get("users") or {}).get(str(user_id))
    return (row if isinstance(row, dict) else None), True


def _save_own(user_id: object, data: dict) -> None:
    """Persist one workspace's overrides to both stores."""
    file = _load_file()
    file.setdefault("users", {})[str(user_id)] = data
    _targets_path().write_text(json.dumps(file, indent=1), encoding="utf-8")
    # Deliberately NOT best-effort: on Cloud Run the file above is ephemeral, so
    # a swallowed cloud failure would report "saved" for an edit that is already
    # gone. Let it raise and tell the desk the truth.
    if _use_cloud():
        _doc(targets_doc_id(user_id)).set(data)


def _seed_from_legacy(user_id: object) -> dict:
    """Copy the pre-tenancy shared overrides into this workspace's own document.

    A COPY, not an alias: once it lands, this desk's edits are theirs alone and
    nobody else's reach them. Nothing is written when there is nothing to
    inherit — a read must not create documents, and an empty answer is the same
    answer next time, which is what makes this safe to repeat.

    The write is best-effort on purpose. A migration that could not persist must
    not turn a dashboard read into a 500; the desk sees the right figures now
    and the next read tries again. A real *edit* still raises (``set_targets``).
    """
    if not _legacy_seed_enabled():
        return {}
    legacy = _legacy_overrides()
    payload = {k: legacy[k] for k in ("thresholds", "channel_goals")
               if isinstance(legacy.get(k), dict) and legacy[k]}
    if not payload:
        return {}
    payload["seeded_from"] = LEGACY_TARGETS_DOC_ID
    payload["seeded_at"] = _now()
    try:
        _save_own(user_id, payload)
        logger.info("MR: seeded targets for %s from the legacy %s document",
                    user_id, LEGACY_TARGETS_DOC_ID)
    except Exception:
        logger.warning("MR targets seed for %s could not be persisted",
                       user_id, exc_info=True)
    return payload


# The overrides doc is ONE small document that every threshold check re-read.
# ``evaluate`` alone called ``get_targets`` twice per metric row and
# ``campaign_reporting.flag_all`` runs every row through it, so a single report
# read the same document ~120 times. Cached exactly like
# ``firestore_repo._app_config_cache``: a short process TTL, invalidated on every
# write, keyed on the store the values came from so a test (or a redeploy that
# moves MR_TARGETS_FILE) can never be served another store's values — and keyed
# per WORKSPACE, so one instance serving two desks can never hand one of them
# the other's cached figures.
_TARGETS_TTL_SECONDS = 30.0
_targets_cache: dict[str, tuple[float, str, dict]] = {}


def _store_key() -> str:
    """Identity of the store the current process/env reads targets from."""
    return f"{'cloud' if _use_cloud() else 'disk'}:{_targets_path()}"


def invalidate_targets_cache(user_id: object | None = None) -> None:
    """Drop cached targets — for one workspace, or (no argument) for all."""
    if user_id is None:
        _targets_cache.clear()
    else:
        _targets_cache.pop(str(user_id), None)


def _load_overrides(user_id: object) -> dict:
    """This workspace's saved edits.

    Firestore is the source of truth when configured; the file is the local copy
    (and the only store offline). A cloud read failure falls back to disk rather
    than silently serving defaults over the desk's real edits — and, because the
    failure is reported back by :func:`_load_own`, never triggers the legacy
    seed on top of a document it simply could not see.
    """
    own, readable = _load_own(user_id)
    if own is not None:
        return own
    if not readable:
        return {}
    return _seed_from_legacy(user_id)


_GOAL_FIELDS = ("cpd_booked_low", "cpd_booked_high",
                "cpd_completed_low", "cpd_completed_high", "completed_demo_pct")


def default_thresholds() -> dict[str, float]:
    """The verbatim 2026 threshold table, untouched by anyone's edits.

    The base for callers that are handed a workspace's thresholds by their
    caller (``lead_analysis.summarize``) — they must never reach the store for a
    default, because reaching the store means picking a workspace.
    """
    return dict(_DEFAULT_THRESHOLDS)


def get_targets(user_id: object, *, use_cache: bool = True) -> dict:
    """Effective targets for ONE workspace: the verbatim 2026 defaults merged
    with that workspace's saved edits.

    Served from a short process cache (see ``_TARGETS_TTL_SECONDS``) so the
    threshold checks stop paying a Firestore document read each. An edit made on
    another Cloud Run instance is visible within the TTL; edits made here
    invalidate immediately.
    """
    cache_key = str(user_id)
    key = _store_key()
    hit = _targets_cache.get(cache_key)
    if (
        use_cache
        and hit
        and hit[1] == key
        and (time.monotonic() - hit[0]) < _TARGETS_TTL_SECONDS
    ):
        return hit[2]
    ov = _load_overrides(user_id)
    thresholds = dict(_DEFAULT_THRESHOLDS)
    for k, v in (ov.get("thresholds") or {}).items():
        if k in thresholds and isinstance(v, (int, float)):
            thresholds[k] = float(v)
    channel_goals: dict[str, dict] = {}
    goal_ov = ov.get("channel_goals") or {}
    for name, g in CHANNEL_GOALS.items():
        merged = {f: getattr(g, f) for f in _GOAL_FIELDS}
        for k, v in (goal_ov.get(name) or {}).items():
            if k in merged and isinstance(v, (int, float)):
                merged[k] = float(v)
        channel_goals[name] = merged
    effective = {"thresholds": thresholds, "channel_goals": channel_goals,
                 "edited": bool(ov.get("thresholds") or goal_ov)}
    _targets_cache[cache_key] = (time.monotonic(), key, effective)
    return effective


def set_targets(user_id: object, update: dict) -> dict:
    """Merge an edit into ONE workspace's overrides; returns its effective
    targets. Unknown keys and non-numeric values are rejected."""
    ov = _load_overrides(user_id)
    invalidate_targets_cache(user_id)
    thr_in = update.get("thresholds") or {}
    goals_in = update.get("channel_goals") or {}
    for k, v in thr_in.items():
        if k not in _DEFAULT_THRESHOLDS:
            raise ValueError(f"unknown threshold '{k}'")
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"threshold '{k}' must be a non-negative number")
    for name, fields in goals_in.items():
        if name not in CHANNEL_GOALS:
            raise ValueError(f"unknown channel '{name}'")
        for k, v in (fields or {}).items():
            if k not in _GOAL_FIELDS:
                raise ValueError(f"unknown goal field '{k}'")
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"goal '{name}.{k}' must be a non-negative number")
    if thr_in:
        ov.setdefault("thresholds", {}).update({k: float(v) for k, v in thr_in.items()})
    for name, fields in goals_in.items():
        if fields:
            ov.setdefault("channel_goals", {}).setdefault(name, {}).update(
                {k: float(v) for k, v in fields.items()})
    ov["updated_at"] = _now()
    _save_own(user_id, ov)
    invalidate_targets_cache(user_id)
    return get_targets(user_id)


def reset_targets(user_id: object) -> dict:
    """Back to the verbatim 2026 defaults for ONE workspace.

    Writes an EMPTY overrides document rather than deleting one. An absent
    document is exactly what :func:`_seed_from_legacy` acts on, so deleting
    would hand this desk the pre-tenancy figures straight back on their next
    read — the opposite of what "reset to the defaults" means.
    """
    _save_own(user_id, {"reset_at": _now()})
    invalidate_targets_cache(user_id)
    return get_targets(user_id)


def thresholds(targets: dict) -> dict[str, float]:
    """Effective thresholds out of ONE resolved :func:`get_targets` result.

    Takes the resolved dict rather than fetching: a function that can reach the
    store is a function that has to choose a workspace, and every caller here is
    already inside a request that knows which one. It also keeps the read cost
    at one per report — see ``tests/test_targets_read_cost.py``.
    """
    return targets["thresholds"]


def channel_goal(channel: str, targets: dict) -> ChannelGoal | None:
    goals_map = targets["channel_goals"]
    for key, fields in goals_map.items():
        if key.lower() == (channel or "").lower():
            return ChannelGoal(key, **fields)
    return None


def _band(value: float | None, good_max: float, warn_max: float) -> str:
    """Traffic-light status: good <= good_max < warn <= warn_max < bad."""
    if value is None:
        return "na"
    if value <= good_max:
        return "good"
    if value <= warn_max:
        return "warn"
    return "bad"


def status_for(channel: str, agg: dict, targets: dict) -> dict:
    """Per-cost-metric traffic-light status for an aggregated channel row, judged
    against the effective goals/thresholds. Consumed by the report UI for coloring."""
    g = channel_goal(channel, targets)
    t = thresholds(targets)
    status = {
        # Target $200–400; red at $600+ (defaults — all editable).
        "cost_per_qualified_lead": _band(
            agg.get("cost_per_qualified_lead"),
            t["cost_per_qualified_lead_target_high"], t["cost_per_qualified_lead_red"]),
        # Target ≤ $2,500; red at $3,000+ (defaults — editable).
        "cac": _band(agg.get("cac"), t["cac_target"], t["cac_red"]),
    }
    if g:
        status["cost_per_demo_booked"] = _band(
            agg.get("cost_per_demo_booked"), g.cpd_booked_high, g.cpd_booked_high * 1.5
        )
        status["cost_per_demo_completed"] = _band(
            agg.get("cost_per_demo_completed"), g.cpd_completed_high, g.cpd_completed_high * 1.5
        )
    return status


def goal_dict(channel: str, targets: dict) -> dict | None:
    g = channel_goal(channel, targets)
    if not g:
        return None
    return {
        "cpd_booked_low": g.cpd_booked_low,
        "cpd_booked_high": g.cpd_booked_high,
        "cpd_completed_low": g.cpd_completed_low,
        "cpd_completed_high": g.cpd_completed_high,
        "completed_demo_pct": g.completed_demo_pct,
    }


def evaluate(metric: CampaignMetric, *, targets: dict,
             prior_conversion: float | None = None) -> list[Flag]:
    """Return every flag this metric trips against ONE workspace's thresholds.

    ``targets`` is required and is the resolved :func:`get_targets` dict for the
    workspace whose report this is. Callers running a whole dataset through this
    (``campaign_reporting.flag_all``) resolve it once and pass it down — which
    is both why a report costs one store read instead of ~120, and why a flag
    can no longer be computed against whoever last edited the shared document.
    """
    flags: list[Flag] = []
    t = thresholds(targets)

    if metric.spend >= t["spend_no_demo_limit"] and metric.demos_booked == 0:
        flags.append(Flag("red", f"${metric.spend:.0f} spend with no demo booked", "spend_no_demo"))

    cpb = metric.cost_per_demo_booked
    if cpb is not None and cpb > t["cost_per_booking_flag"]:
        flags.append(Flag("warn", f"Cost per booking ${cpb:.0f} exceeds ${t['cost_per_booking_flag']:.0f}", "cost_per_booking"))

    cpql = metric.cost_per_qualified_lead
    if cpql is not None and cpql >= t["cost_per_qualified_lead_red"]:
        flags.append(Flag("red", f"Cost per qualified lead ${cpql:.0f} at/above ${t['cost_per_qualified_lead_red']:.0f}", "cost_per_qualified_lead"))

    cac = metric.cac
    if cac is not None and cac >= t["cac_red"]:
        flags.append(Flag("red", f"CAC ${cac:.0f} at/above ${t['cac_red']:.0f}", "cac"))

    if prior_conversion:
        current = (metric.demos_booked / metric.leads) if metric.leads else 0.0
        if current < prior_conversion * (1 - t["conversion_drop_pct"]):
            drop = (prior_conversion - current) / prior_conversion
            flags.append(Flag("warn", f"Conversion dropped {drop * 100:.0f}% vs prior 7-day average", "conversion_drop"))

    goal = channel_goal(metric.channel, targets)
    if goal and cpb is not None and cpb > goal.cpd_booked_high:
        flags.append(Flag("warn", f"{metric.channel} cost/demo booked ${cpb:.0f} over goal ${goal.cpd_booked_high:.0f}", "channel_goal"))

    return flags
