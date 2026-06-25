"""Creative-Agent run persistence + the decision log.

A Creative-Agent run is a sibling of a social-editor run (``..runs``) but models
the 4-step Creative flow and the things the spec requires of it: an autonomous
flag with an explicit acknowledgement gate, a reviewable plan, retrieved brand
precedent, the produced artifacts, and — crucially — a **decision log** recording
every choice the agent makes so the user can audit the creative rationale.

Storage mirrors ``..runs``: ``fs`` (local JSON + files, the default, used by tests)
or ``cloud`` (Firestore ``creative_runs`` + GCS artifacts) selected by the same
``GD_STORAGE_BACKEND`` env var. App-service imports stay lazy.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .types import STEP_KEYS

CREATIVE_RUNS_ROOT = Path(
    os.environ.get("GD_CREATIVE_RUNS_DIR")
    or (Path(__file__).resolve().parents[2] / "creative_runs")
)

GD_STORAGE_BACKEND = (os.environ.get("GD_STORAGE_BACKEND") or "fs").strip().lower()
_GCS_PARTITION = "creative"

# State machine: one state per step, plus a terminal DONE. ``state`` names the
# step the run is *currently in / waiting on*.
STATE_FOR_STEP = {
    "intent": "INTENT",
    "strategy": "STRATEGY",
    "layout": "LAYOUT",
    "output": "OUTPUT",
}
STATES = list(STATE_FOR_STEP.values()) + ["DONE"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _use_cloud() -> bool:
    return GD_STORAGE_BACKEND == "cloud"


def _collection():
    from app.services.firestore_repo import _db

    return _db().collection("creative_runs")


def create_run(
    user_id: str,
    creative_type: str,
    *,
    brand_id: Optional[str] = None,
    brief: str = "",
    autonomous: bool = False,
) -> dict:
    from .. import reference_library as rl
    from .. import registry

    pack = registry.get_pack(brand_id)
    run = {
        "id": uuid.uuid4().hex[:12],
        "user_id": user_id,
        "brand_id": pack.id,
        "brand_name": pack.name,
        "creative_type": creative_type,
        "output_format": rl.output_format_for(creative_type),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "autonomous": bool(autonomous),
        # Gate: autonomous mode may not proceed until the user acknowledges the
        # mandatory warning (spec). Manual runs don't need it.
        "autonomous_ack": False,
        "state": "INTENT",
        "brief": brief,
        "intent": {},
        "plan": None,
        "plan_approved": False,
        "references": [],
        "grounding": "",
        "decision_log": [],
        "artifacts": [],
    }
    log_decision(run, "intent", "Run created",
                 f"User chose a {creative_type}"
                 + (" in autonomous mode." if autonomous else " in manual mode."),
                 source="user")
    save_run(run)
    return run


# --------------------------------------------------------------------------- #
# Decision log — the audit trail the spec requires
# --------------------------------------------------------------------------- #

def log_decision(run: dict, step: str, decision: str, rationale: str,
                 *, source: str = "agent") -> None:
    """Record one decision. ``source`` is ``"agent"`` or ``"user"``."""
    run.setdefault("decision_log", []).append({
        "step": step,
        "decision": decision,
        "rationale": rationale,
        "source": source,
        "timestamp": now_iso(),
    })


def log_decisions(run: dict, decisions: list[dict[str, Any]]) -> None:
    for d in decisions:
        log_decision(run, d.get("step", "layout"), d.get("decision", ""),
                     d.get("rationale", ""), source=d.get("source", "agent"))


def advance_to(run: dict, step: str) -> None:
    run["state"] = STATE_FOR_STEP.get(step, run.get("state", "INTENT"))


def next_step(step: str) -> Optional[str]:
    i = STEP_KEYS.index(step) if step in STEP_KEYS else -1
    if i < 0 or i + 1 >= len(STEP_KEYS):
        return None
    return STEP_KEYS[i + 1]


# --------------------------------------------------------------------------- #
# Persistence (fs default, cloud seam mirrors ..runs)
# --------------------------------------------------------------------------- #

def run_dir(run_id: str) -> Path:
    return CREATIVE_RUNS_ROOT / run_id


def _run_json(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def save_run(run: dict) -> None:
    run["updated_at"] = now_iso()
    if _use_cloud():
        _collection().document(run["id"]).set(run)
        return
    d = run_dir(run["id"])
    d.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp + os.replace) so a concurrent reader — e.g. the UI polling
    # the run mid-generation while worker threads stream frames in — never sees a
    # half-written file.
    path = _run_json(run["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def get_run(run_id: str) -> Optional[dict]:
    if _use_cloud():
        doc = _collection().document(run_id).get()
        return doc.to_dict() if doc.exists else None
    path = _run_json(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_artifact(run_id: str, name: str, data: bytes, content_type: str) -> str:
    """Persist one produced artifact; return an opaque reference (fs path or gs URI)."""
    if _use_cloud():
        from app.services import storage

        gs_uri, _signed = storage.upload_generated(
            partition=f"{_GCS_PARTITION}/{run_id}",
            file_name=name,
            data=data,
            content_type=content_type,
        )
        return gs_uri
    rel = f"artifacts/{name}"
    abspath = run_dir(run_id) / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_bytes(data)
    return rel


def read_artifact(run_id: str, ref: str) -> bytes:
    if ref.startswith("gs://"):
        from app.services import storage

        return storage.download_bytes(ref)
    base = run_dir(run_id).resolve()
    target = (run_dir(run_id) / ref).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("invalid artifact path")
    return target.read_bytes()


def record_artifacts(run: dict, run_id: str, artifacts: list[tuple[str, bytes, str]]) -> list[dict]:
    """Persist each (name, bytes, mime) and append metadata to the run."""
    out: list[dict] = []
    for name, data, mime in artifacts:
        ref = save_artifact(run_id, name, data, mime)
        meta = {"name": name, "mime": mime, "ref": ref, "bytes": len(data)}
        out.append(meta)
    run.setdefault("artifacts", [])
    run["artifacts"] = out  # latest generation replaces prior outputs
    return out


def append_artifact(run: dict, run_id: str, name: str, data: bytes, mime: str) -> dict:
    """Persist ONE artifact and append its metadata to the run (incremental output).

    Used while a multi-file creative streams in — each frame is saved + recorded the
    moment it finishes, so a poller sees the set grow instead of all-or-nothing."""
    ref = save_artifact(run_id, name, data, mime)
    meta = {"name": name, "mime": mime, "ref": ref, "bytes": len(data)}
    run.setdefault("artifacts", []).append(meta)
    return meta
