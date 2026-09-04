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

Tenancy: WORKSPACE-SHARED, on purpose (2026-09-05)
--------------------------------------------------
This registry is **one document for the whole workspace**, and it stays that
way. Three facts decide it, and none of them is "nobody got round to it":

1. The PRIMARY workbook is a single deployment-wide constant
   (``MR_SHEETS_SPREADSHEET_ID``). ``/mr/workbook``, ``/mr/workbook/scan`` and
   ``/mr/ask`` read it for every caller already. Siloing only the *secondary*
   sheets would produce a model where the big shared tracker is visible to
   everyone and a pasted side-sheet is private — incoherent, and it would not
   make the Ask surface any less shared.
2. Access is granted to a SHARED IDENTITY, not to a person. Connecting a sheet
   means sharing it with ``service_account_email()`` as Viewer; the caller's own
   Google identity is never involved. There is no per-user credential to scope
   by — a ``user_id`` filter here would be a fiction the store cannot back.
3. ``_build_lead_analysis`` scans every connected workbook, and the nightly cron
   runs it as ``MR_CRON_USER_ID``. Per-user sources would silently narrow that
   scan to one desk's sheets, with no error anywhere.

So READS are shared, and that is now written down: ``ROUTE_LEDGER`` classifies
``/mr/sources`` WORKSPACE_SHARED. It used to claim TENANT_SCOPED, which was
false — the ledger, not the sharing, was the defect.

What WAS broken is the destructive path. ``remove_source`` took no caller at
all, so any signed-in user could disconnect a sheet somebody else had
connected, and nothing recorded who had connected it. Both halves close here:

* :func:`add_source` stamps ``added_by``.
* :func:`remove_source` REQUIRES the caller and refuses (``PermissionError``)
  unless they added the row or hold an elevated role.
* Rows written before this change carry no ``added_by``. They are treated as
  workspace-owned: still listed for everyone (dropping them from view would be
  data loss by another name), removable only by an admin/creator. Nothing is
  backfilled — no store recorded an owner, so any guess would be a lie.

Kill-switch: set ``MR_MULTI_SHEET=0`` and the feature disappears —
``extra_sources()`` returns nothing, add/remove endpoints refuse, and every
read path behaves exactly as the single-sheet agent did. It defaults to ON;
``.env.example`` says what turning it off costs.
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


# --- ownership --------------------------------------------------------------
#
# One field, ``added_by``, and one predicate that reads it. The predicate lives
# HERE rather than in the router because MR's other ownership checks are three
# hand-written copies of the same comparison spread across 1,200 lines (see
# ``app/routers/tests/test_mr_cross_tenant.py``). This one gets exactly one
# home, and the router is not allowed to hold an opinion of its own.


def owner_of(src: dict) -> str | None:
    """Who connected this sheet, or ``None`` for a pre-attribution row.

    ``None`` means "no store ever recorded an owner", NOT "owned by nobody" —
    the difference is the whole migration question, and it is why these rows
    stay listed for everyone.
    """
    who = str((src or {}).get("added_by") or "").strip()
    return who or None


def may_remove(src: dict, *, user_id: object, privileged: bool = False) -> bool:
    """Whether ``user_id`` may disconnect ``src``.

    * You added it -> yes.
    * An admin/creator -> yes, including the unowned legacy rows. Somebody has
      to be able to clean those up, and the honest answer to "who owns a row
      with no recorded owner" is the workspace, whose caretakers are its admins.
    * Anyone else -> no. That is the hole the September 2026 review walked
      through: before this, every signed-in caller could delete every row.
    """
    if privileged:
        return True
    owner = owner_of(src)
    if owner is None:
        return False
    return owner == str(user_id)


def removal_refusal(src: dict) -> str:
    """The 403 wording, kept here so the router and the tests cannot drift."""
    if owner_of(src) is None:
        return ("This sheet was connected before we started recording who "
                "connected it. An admin can disconnect it.")
    return "Only the person who connected this sheet, or an admin, can disconnect it."


# --- API --------------------------------------------------------------------

def _stored_extras() -> list[dict]:
    out = []
    for s in _load().get("sources") or []:
        if isinstance(s, dict) and s.get("id") and s["id"] != mr_config.SHEETS_SPREADSHEET_ID:
            out.append(s)
    return out


def find_source(spreadsheet_id: str) -> dict | None:
    """One stored secondary source, or ``None``. The primary is not in here."""
    for s in _stored_extras():
        if s["id"] == spreadsheet_id:
            return s
    return None


def list_sources(*, viewer_id: object = None, privileged: bool = False) -> list[dict]:
    """Every connected workbook, primary first — the WHOLE workspace's, by
    design (see the module docstring).

    ``added_by`` and ``can_remove`` ride along so the console can show who
    connected a sheet and hide a button that would only earn a 403.
    ``viewer_id=None`` means "no caller identified", which yields
    ``can_remove: False`` everywhere rather than a permissive default.
    """
    primary = {
        "id": mr_config.SHEETS_SPREADSHEET_ID,
        "label": "Primary marketing tracker",
        "primary": True,
        "include_in_dashboard": True,
        "added_by": None,
        "can_remove": False,  # the primary can never be removed, by anyone
    }
    extras = [
        {
            **s,
            "primary": False,
            "include_in_dashboard": bool(s.get("include_in_dashboard", False)),
            "added_by": owner_of(s),
            "can_remove": (
                viewer_id is not None
                and may_remove(s, user_id=viewer_id, privileged=privileged)
            ),
        }
        for s in _stored_extras()
    ]
    return [primary] + extras


def extra_sources() -> list[dict]:
    """Secondary workbooks for the read paths — empty when the kill-switch is
    off, so every consumer reverts to single-sheet behaviour with no code.

    Takes no caller ON PURPOSE. Every read path in MR (the Ask substrate, the
    tab catalog, the lead-analysis tab scan, the cron) reads the whole
    workspace's workbooks, exactly as it reads the one shared primary tracker.
    Adding a caller here would not narrow a leak; it would break the cron.
    """
    if not multi_sheet_enabled():
        return []
    return _stored_extras()


def add_source(spreadsheet_id: str, *, label: str, added_by: object = None) -> dict:
    """Connect a secondary workbook, stamped with who connected it.

    ``added_by`` is what makes :func:`remove_source` enforceable. It defaults to
    ``None`` so a script or a test can seed a row shaped the way the
    pre-attribution store wrote them; such a row is then admin-removable, like
    every other legacy row.
    """
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
        "added_by": str(added_by) if added_by is not None else None,
    }
    _save({"sources": extras + [src]})
    return {**src, "primary": False}


def remove_source(spreadsheet_id: str, *, requested_by: object,
                  privileged: bool = False) -> bool:
    """Disconnect a secondary workbook on behalf of ``requested_by``.

    ``requested_by`` is keyword-only and REQUIRED: a call site that forgets the
    caller must fail loudly at the call rather than delete somebody else's sheet
    quietly. Raises ``PermissionError`` when the caller may not remove this row
    (see :func:`may_remove`), ``ValueError`` for the primary, and returns
    ``False`` when there is nothing stored under that id.
    """
    if spreadsheet_id == mr_config.SHEETS_SPREADSHEET_ID:
        raise ValueError("The primary tracker can't be removed.")
    extras = _stored_extras()
    target = next((s for s in extras if s["id"] == spreadsheet_id), None)
    if target is None:
        return False
    if not may_remove(target, user_id=requested_by, privileged=privileged):
        raise PermissionError(removal_refusal(target))
    _save({"sources": [s for s in extras if s["id"] != spreadsheet_id]})
    return True
