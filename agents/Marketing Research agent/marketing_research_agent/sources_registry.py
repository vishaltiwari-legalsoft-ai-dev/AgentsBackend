"""Registry of connected Google Sheets workbooks (multi-sheet support).

The primary workbook (``config.SHEETS_SPREADSHEET_ID``) is always entry #0 and
can never be removed — existing users' dashboards keep reading exactly what
they read today. Extra workbooks are added from the UI (link paste) and feed
the Ask/insight layer only; their ``include_in_dashboard`` flag (default OFF)
is reserved for the later dashboard phase so a pasted copy of the tracker can
never silently double-count the official numbers.

Persistence mirrors ``goals.py``: Firestore (``mr_config/sheet_sources``) is
the source of truth when configured, a JSON file (``MR_SOURCES_FILE``) is the
local/offline copy — Cloud Run's disk is ephemeral.

Kill-switch: set ``MR_MULTI_SHEET=0`` and the feature disappears —
``extra_sources()`` returns nothing, add/remove endpoints refuse, and every
read path behaves exactly as the single-sheet agent did.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config as mr_config
from . import goals as _goals

logger = logging.getLogger(__name__)

_SOURCES_FILE_DEFAULT = Path(__file__).resolve().parents[1] / "sources.json"
_MR_CONFIG_COLLECTION = "mr_config"
_SOURCES_DOC_ID = "sheet_sources"

# The identity the agent reads sheets as; users must share their sheet with it
# (Viewer). Overridable per deployment; derived from ADC when possible.
_SA_EMAIL_FALLBACK = "lsagent@helpful-charmer-498509-v5.iam.gserviceaccount.com"


def multi_sheet_enabled() -> bool:
    return os.environ.get("MR_MULTI_SHEET", "1").strip().lower() not in ("0", "false", "off")


def service_account_email() -> str:
    """The email a user must grant Viewer access to. Env override first, then
    the runtime credentials' own identity, then the known project SA."""
    env = os.environ.get("MR_SERVICE_ACCOUNT_EMAIL")
    if env:
        return env
    if os.environ.get("MR_OFFLINE") == "1":
        return _SA_EMAIL_FALLBACK
    try:
        import google.auth

        creds, _ = google.auth.default()
        email = getattr(creds, "service_account_email", None)
        if email and "@" in str(email):
            return str(email)
    except Exception:
        pass
    return _SA_EMAIL_FALLBACK


_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")


def parse_spreadsheet_id(text: str) -> str | None:
    """Spreadsheet id from a pasted Google Sheets URL, or the bare id itself."""
    text = (text or "").strip()
    m = _ID_RE.search(text)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(text):
        return text
    return None


# --- store ------------------------------------------------------------------

def _sources_path() -> Path:
    return Path(os.environ.get("MR_SOURCES_FILE") or _SOURCES_FILE_DEFAULT)


def _doc():
    from app.services import firestore_repo

    return firestore_repo._db().collection(_MR_CONFIG_COLLECTION).document(_SOURCES_DOC_ID)


def _load_disk() -> dict:
    p = _sources_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load() -> dict:
    if _goals._use_cloud():
        try:
            snap = _doc().get()
            if snap.exists:
                data = snap.to_dict()
                return data if isinstance(data, dict) else {}
            return {}
        except Exception:
            logger.warning("MR sheet-sources cloud read failed; falling back to disk")
    return _load_disk()


def _save(data: dict) -> None:
    _sources_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    # NOT best-effort: on Cloud Run the file is ephemeral, so a swallowed cloud
    # failure would report "connected" for a sheet that vanishes on redeploy.
    if _goals._use_cloud():
        _doc().set(data)


# --- API --------------------------------------------------------------------

def _stored_extras() -> list[dict]:
    out = []
    for s in _load().get("sources") or []:
        if isinstance(s, dict) and s.get("id") and s["id"] != mr_config.SHEETS_SPREADSHEET_ID:
            out.append(s)
    return out


def list_sources() -> list[dict]:
    """Every connected workbook, primary first."""
    primary = {
        "id": mr_config.SHEETS_SPREADSHEET_ID,
        "label": "Primary marketing tracker",
        "primary": True,
        "include_in_dashboard": True,
    }
    extras = [
        {**s, "primary": False, "include_in_dashboard": bool(s.get("include_in_dashboard", False))}
        for s in _stored_extras()
    ]
    return [primary] + extras


def extra_sources() -> list[dict]:
    """Secondary workbooks for the read paths — empty when the kill-switch is
    off, so every consumer reverts to single-sheet behaviour with no code."""
    if not multi_sheet_enabled():
        return []
    return _stored_extras()


def add_source(spreadsheet_id: str, *, label: str) -> dict:
    if spreadsheet_id == mr_config.SHEETS_SPREADSHEET_ID:
        raise ValueError("That is the primary tracker — it is already connected.")
    extras = _stored_extras()
    if any(s["id"] == spreadsheet_id for s in extras):
        raise ValueError("That sheet is already connected.")
    src = {
        "id": spreadsheet_id,
        "label": (label or "").strip() or spreadsheet_id,
        "include_in_dashboard": False,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _save({"sources": extras + [src]})
    return {**src, "primary": False}


def remove_source(spreadsheet_id: str) -> bool:
    if spreadsheet_id == mr_config.SHEETS_SPREADSHEET_ID:
        raise ValueError("The primary tracker can't be removed.")
    extras = _stored_extras()
    kept = [s for s in extras if s["id"] != spreadsheet_id]
    if len(kept) == len(extras):
        return False
    _save({"sources": kept})
    return True
