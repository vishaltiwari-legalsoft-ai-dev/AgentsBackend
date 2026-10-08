"""Creative-Agent orchestration — manual step-by-step *or* fully autonomous.

The four steps (``types.STEPS``):
  1. intent    — gather the goal/audience/message.
  2. strategy  — retrieve brand precedent and set the angle.
  3. layout    — produce the *reviewable* plan (frames/slides/sections).
  4. output    — render the finished PDF / PPTX / image set.

Manual mode pauses for the user between steps (the plan is reviewable before
generation). Autonomous mode runs all four end-to-end — but only after the user
has acknowledged the mandatory warning — logging every decision. A one-click
``take_manual_control`` flips any run back to human control at the current step.

Agent behaviour rules (spec): always retrieve existing brand precedent before
generating; never generate a plan from scratch when precedent exists; log every
decision; keep human override one click away.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from . import runs as cruns
from .. import reference_library as rl
from .. import registry


def _reference_base_dir() -> Path:
    override = os.environ.get("GD_REFERENCE_DIR")
    if override:
        return Path(override)
    # creative/pipeline.py → … → repo root (Data/_reference_mock lives there).
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "Data" / "_reference_mock"


def _pack(run: dict):
    return registry.get_pack(run.get("brand_id"))


logger = logging.getLogger("graphics_designer.creative.pipeline")


class AutonomyError(RuntimeError):
    """Raised when autonomous mode is asked to proceed without acknowledgement."""


class ArtifactStoreUnavailable(RuntimeError):
    """A produced file could not be saved to the run store (GCS in cloud mode).

    The message is safe to show the user; the storage detail is in the log.
    A run that silently lost a file must never be reported as finished."""


#: What a run says when generation died for a reason that is not ours to word
#: for the user (a builder crash, a provider error, a store fault). The cause
#: goes to the log; upstream error text never goes into the run.
_GENERIC_FAILURE = ("Generation stopped partway because of an internal error, so this "
                    "run will not finish — please try again.")


def _record_failure(run: dict, exc: BaseException) -> None:
    """Mark a run that stopped mid-generation as ``progress.state: "failed"``
    with a safe one-sentence ``progress.reason`` and a decision-log entry.

    Without it any exception other than a store fault left the run on
    "generating" for good, and a client watching the run past the relay's
    300 s cut waited out its whole cap for an honest answer. A failure to
    persist this is logged and swallowed: the caller re-raises ``exc``, which
    must never be masked by the bookkeeping."""
    reason = str(exc) if isinstance(exc, ArtifactStoreUnavailable) else _GENERIC_FAILURE
    step = run_step_key(run)
    logger.error("creative run %s failed during %s: %s", run.get("id"), step,
                 type(exc).__name__, exc_info=exc)
    prev = run.get("progress") or {}
    this_attempt = prev.get("state") == "generating"
    run["progress"] = {
        "done": prev.get("done", 0) if this_attempt else 0,
        "total": prev.get("total", 0) if this_attempt else 0,
        "state": "failed",
        "reason": reason,
    }
    try:
        cruns.log_decision(run, step, "Generation failed", reason)
        cruns.save_run(run)
    except Exception:  # noqa: BLE001 - the original error is the report
        logger.exception("creative run %s: could not record the failure", run.get("id"))


# --------------------------------------------------------------------------- #
# Step 2 — retrieve brand precedent (the grounding the agent must use)
# --------------------------------------------------------------------------- #

def retrieve_grounding(run: dict, *, k: int = 3) -> dict:
    """Pull on-brand precedent for this job and stash it on the run.

    Always runs before planning so the agent is grounded in real, prior work
    rather than generating from scratch (spec: "Always reference existing brand
    PDFs before generating anything new").
    """
    records = rl.load_index(_reference_base_dir())
    hits = rl.retrieve(
        records, creative_type=run["creative_type"], brief=run.get("brief", ""),
        brand_id=run.get("brand_id"), k=k,
    )
    run["references"] = [{k2: v for k2, v in r.items() if k2 != "abs_path"} for r in hits]
    run["grounding"] = rl.summarize_for_prompt(hits)
    if hits:
        names = ", ".join(h.get("file_name", "?") for h in hits)
        cruns.log_decision(run, "strategy", f"Referenced {len(hits)} prior creative(s)",
                           f"Grounded the plan on existing brand precedent: {names}.")
    else:
        cruns.log_decision(run, "strategy", "No prior references found",
                           "No indexed precedent for this brand+type; used brand defaults. "
                           "Ingest real brand PDFs to strengthen future grounding.")
    return run


# --------------------------------------------------------------------------- #
# Step 1 — intent
# --------------------------------------------------------------------------- #

def gather_intent(run: dict, *, brief: Optional[str] = None,
                  answers: Optional[dict] = None, source: str = "user") -> dict:
    if brief is not None:
        run["brief"] = brief
    if answers:
        run.setdefault("intent", {}).update(answers)
    cruns.log_decision(run, "intent", "Captured intent",
                       f"Brief: {run.get('brief') or '(none)'}.", source=source)
    cruns.advance_to(run, "strategy")
    cruns.save_run(run)
    return run


# --------------------------------------------------------------------------- #
# Step 3 — plan (reviewable)
# --------------------------------------------------------------------------- #

def make_plan(run: dict, *, count: Optional[int] = None, use_llm: bool = True) -> dict:
    from .planner import plan as build_plan

    if not run.get("grounding"):
        retrieve_grounding(run)
    p = build_plan(
        run["creative_type"], run.get("brief", ""),
        brand_name=run.get("brand_name", _pack(run).name),
        grounding=run.get("grounding", ""), count=count, use_llm=use_llm,
    )
    # Carry the run's text mode on the plan so the document builder knows whether to
    # overlay per-slide copy or render images with the logo only.
    p["text_mode"] = run.get("text_mode", "text")
    run["plan"] = p
    run["plan_approved"] = False
    cruns.log_decisions(run, p.get("decisions", []))
    cruns.advance_to(run, "layout")
    cruns.save_run(run)
    return run


def update_plan_text(run: dict, frames: list[dict], *, source: str = "user") -> dict:
    """Merge user-edited per-slide copy (``[{index, headline, body}]``) into the
    carousel plan before generation, so the finished slides carry the EXACT text the
    user typed. The accent ``highlight`` is re-derived from each edited headline so
    the renderer still colours a sensible key phrase. Only valid for carousels with a
    frame-based plan; a no-op otherwise."""
    from .planner import _highlight

    plan = run.get("plan") or {}
    existing = {f.get("index"): f for f in plan.get("frames", []) or []}
    if not existing:
        raise ValueError("There is no carousel plan to edit yet — plan first.")
    for edit in frames or []:
        fr = existing.get(edit.get("index"))
        if not fr:
            continue
        if "headline" in edit:
            fr["headline"] = (edit.get("headline") or "").strip()
            fr["highlight"] = _highlight(fr["headline"])
        if "body" in edit:
            fr["body"] = (edit.get("body") or "").strip()
    run["plan"] = plan
    cruns.log_decision(run, "layout", "Edited slide copy",
                       "User set the exact headline/sub-text for the carousel slides.",
                       source=source)
    cruns.save_run(run)
    return run


def approve_plan(run: dict, *, source: str = "user") -> dict:
    if not run.get("plan"):
        raise ValueError("There is no plan to approve yet — plan first.")
    run["plan_approved"] = True
    cruns.log_decision(run, "layout", "Plan approved",
                       "Plan reviewed and approved; proceeding to generation.", source=source)
    cruns.advance_to(run, "output")
    cruns.save_run(run)
    return run


# --------------------------------------------------------------------------- #
# Step 4 — produce final artifacts
# --------------------------------------------------------------------------- #

def _expected_artifact_count(ctype: str, plan: dict) -> int:
    """How many primary artifacts the build will yield (the zip is extra) — drives
    the progress bar's denominator."""
    if ctype == "carousel":
        return len(plan.get("frames") or []) or 1
    if ctype == "blog":
        return len(plan.get("inline") or []) + 1  # cover + inline images
    return 1  # brochure / presentation → one file


def _require_ready(run: dict, *, require_approval: bool) -> None:
    """Preconditions, checked before anything starts: a refusal here is the
    request's own 400, not a failed generation, so it leaves the run as it was."""
    if not run.get("plan"):
        raise ValueError("Nothing to generate — create and approve a plan first.")
    if require_approval and not run.get("plan_approved"):
        raise ValueError("Plan must be approved before generation (or run autonomously).")


def produce(run: dict, *, require_approval: bool = True) -> dict:
    """Step 4. Any failure once generation has started is recorded on the run
    (``progress.state: "failed"``, see ``_record_failure``) and re-raised."""
    _require_ready(run, require_approval=require_approval)
    try:
        return _produce(run)
    except Exception as exc:
        _record_failure(run, exc)
        raise


def _produce(run: dict) -> dict:
    import threading

    from . import document_builder as db

    pack = _pack(run)
    ctype = run["creative_type"]
    plan = run["plan"]

    # Publish progress up front so the UI can poll "done/total" while we generate.
    total = _expected_artifact_count(ctype, plan)
    run["artifacts"] = []
    run["progress"] = {"done": 0, "total": total, "state": "generating"}
    cruns.save_run(run)

    # Persist + count each artifact the moment it finishes. Carousel slides arrive
    # concurrently, so guard the shared run with a lock.
    lock = threading.Lock()

    def _on_artifact(art: tuple[str, bytes, str]) -> None:
        name, data, mime = art
        with lock:
            cruns.append_artifact(run, run["id"], name, data, mime,
                                  provenance=db.artifact_provenance(art))
            run["progress"]["done"] = len(run["artifacts"])
            cruns.save_run(run)

    artifacts = db.build(ctype, plan, pack, on_artifact=_on_artifact)
    # The streaming save above is best-effort inside the builders (a failing
    # callback is logged, never fatal mid-build), so a file whose save failed
    # would otherwise just be missing from a run marked DONE. Retry each one
    # once here, where a second failure fails the request honestly.
    to_store = [a for a in artifacts
                if a[0] not in {m["name"] for m in run.get("artifacts", [])}]
    # Multi-file outputs (carousel frames, blog images) also get a single zip so the
    # user can download the whole set in one click — appended after the primaries.
    if len(artifacts) > 1:
        to_store.append(db.zip_artifacts(artifacts, f"{pack.name}-{ctype}"))
    for art in to_store:
        name, data, mime = art
        try:
            with lock:
                cruns.append_artifact(run, run["id"], name, data, mime,
                                      provenance=db.artifact_provenance(art))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            logger.exception("creative run %s: could not store artifact %s", run["id"], name)
            raise ArtifactStoreUnavailable(
                "The creative was generated but its files could not be saved — "
                "file storage is unavailable. Please try again.") from exc

    out_fmt = run.get("output_format", "image")
    cruns.log_decision(run, "output", f"Generated {total} artifact(s)",
                       f"Rendered the {ctype} as {out_fmt} via the structured layout engine, "
                       f"on-brand for {pack.name}.")
    # No fake success: a stand-in the image model did not make is said so in
    # the audit trail too, not only on the artifact.
    for art in artifacts:
        prov = db.artifact_provenance(art)
        if prov and not prov["ai"]:
            cruns.log_decision(run, "output", f"{art[0]} is a stand-in, not an AI image",
                               prov["fallback_reason"])
    run["state"] = "DONE"
    run["progress"] = {"done": total, "total": total, "state": "done"}
    cruns.save_run(run)
    return run


# --------------------------------------------------------------------------- #
# Autonomous mode + override
# --------------------------------------------------------------------------- #

def acknowledge(run: dict) -> dict:
    """Record the user's acknowledgement of the autonomous-mode warning."""
    run["autonomous_ack"] = True
    cruns.log_decision(run, "intent", "Autonomous mode acknowledged",
                       "User acknowledged the autonomous-mode warning.", source="user")
    cruns.save_run(run)
    return run


def run_autonomous(run: dict, *, count: Optional[int] = None, use_llm: bool = True) -> dict:
    """Drive all four steps end-to-end. Requires acknowledgement first (spec)."""
    if not run.get("autonomous"):
        run["autonomous"] = True
    if not run.get("autonomous_ack"):
        raise AutonomyError(
            "Autonomous mode requires the user to acknowledge the warning first."
        )
    cruns.log_decision(run, "intent", "Autonomous run started",
                       "Agent is taking end-to-end control across all four steps "
                       "based on AI recommendations.")
    # From here the run is under way: whichever step fails, the run says so
    # (once — Step 4 uses the unrecorded ``_produce`` for that reason).
    try:
        # Step 1 → 2
        cruns.advance_to(run, "strategy")
        retrieve_grounding(run)
        # Step 3 (plan) — auto-approved in autonomous mode
        make_plan(run, count=count, use_llm=use_llm)
        approve_plan(run, source="agent")
        # Step 4
        _require_ready(run, require_approval=False)
        _produce(run)
    except Exception as exc:
        _record_failure(run, exc)
        raise
    return run


def take_manual_control(run: dict) -> dict:
    """One-click human override: stop autonomy, keep the run at its current step."""
    run["autonomous"] = False
    cruns.log_decision(run, run_step_key(run), "Manual control taken",
                       "User took manual control; the agent will no longer act "
                       "autonomously and awaits the user's direction.", source="user")
    cruns.save_run(run)
    return run


def run_step_key(run: dict) -> str:
    state = run.get("state", "INTENT")
    for key, st in cruns.STATE_FOR_STEP.items():
        if st == state:
            return key
    return "output"
