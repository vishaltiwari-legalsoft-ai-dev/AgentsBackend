"""Which key the Marketing Research workbook data is stored and read under.

The problem this closes
-----------------------
``mr_runs`` is keyed by a ``user_id`` field, and the three kinds that come out
of the shared workbook — ``dataset``, ``official_spend``, ``lead_analysis`` —
used to be written under whoever pulled. The 15-minute cron pulls for exactly
one account (``MR_CRON_USER_ID``), so that account's copy was the only one that
stayed fresh, and every other signed-in account read its own — usually empty —
copy: an empty Overview, an empty month picker, an empty board-report builder,
all in front of a workbook the whole team already shares (``POST /mr/ask``,
``/mr/workbook`` and ``/mr/config`` serve it to everyone).

The fix
-------
When sharing is ON, the router resolves ONE key for workbook-derived reads and
writes, from the server's own configuration and never from anything the client
sends:

1. ``MR_WORKSPACE_ID`` — an explicit workspace key, when set;
2. else ``MR_CRON_USER_ID`` — the account the cron already refreshes, whose
   existing runs are stamped with exactly that value. Pointing every read at it
   moves no data and backfills nothing;
3. else the caller's own id — today's behaviour, byte for byte.

Sharing is OFF unless someone turns it on
-----------------------------------------
Sharing is ON only when ``MR_WORKSPACE_SHARED`` is explicitly one of ``1``,
``true``, ``yes``, ``on`` (any case, surrounding whitespace ignored). Unset —
or anything else, a typo included — is OFF, and OFF is per-user behaviour
exactly as it was before this module existed.

That polarity is deliberate and it is the whole safety story. Production already
has ``MR_CRON_USER_ID`` set, so a switch that defaulted ON would have made
merely DEPLOYING this code the decision to show one account's workbook data to
every member — with nobody having made that decision. And a switch that only
recognised ``0``/``false``/``off`` as "off" would fail OPEN: ``MR_WORKSPACE_SHARED=ture``
would leave sharing on. So enabling it is one deliberate env change, and every
value the code does not positively recognise leaves it off. Rollback is unsetting
the variable (or setting ``0``) — no redeploy of code and no data moved.

What stays private is decided by the ROUTER, not here: the reports a person
builds, their run list, their targets and their schedule are still keyed on the
caller. This module only names the key; it holds no store, no document id and
no persistence, so it cannot become the ``state.py``-style module that takes raw
doc-id strings.

Environment is read on EVERY call (never cached at import) so a deployment can
flip the switch and a test can monkeypatch it.
"""

from __future__ import annotations

import os

#: Sharing is ON only for one of :data:`_ON_VALUES`. Unset or anything else is
#: OFF: this is an opt-in, and it fails closed.
_SWITCH = "MR_WORKSPACE_SHARED"
_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: Highest precedence first.
_KEY_VARS = ("MR_WORKSPACE_ID", "MR_CRON_USER_ID")


def sharing_enabled() -> bool:
    """The switch. ``MR_WORKSPACE_SHARED`` must positively say ``1`` / ``true`` /
    ``yes`` / ``on`` (any case, whitespace ignored). Unset, blank, ``0`` and every
    unrecognised value — ``ture``, ``enabled``, ``2`` — are OFF."""
    return os.environ.get(_SWITCH, "").strip().lower() in _ON_VALUES


def configured_key() -> str:
    """The deployment's workspace key, stripped, or ``""`` when none is set.

    Whitespace-only counts as unset: a stray space in a deployment's env must
    fall back to per-caller keys, never become a key nobody can match.
    """
    for var in _KEY_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return ""


def is_shared() -> bool:
    """Whether workbook-derived data is currently one workspace-wide copy: the
    switch is explicitly on AND a workspace key is configured."""
    return sharing_enabled() and bool(configured_key())


def workspace_id(user_id):
    """The key ``user_id``'s workbook-derived runs are stored and read under.

    Shared -> the deployment's configured key, whoever is asking. Otherwise the
    caller's own id, handed back UNCHANGED — type included. MR compares tenant
    ids raw (``7`` and ``"7"`` are two tenants; ``test_tenant_id_type_contract``
    pins it), so normalising here would quietly change that in the unshared mode
    this module promises not to touch.

    Raises :class:`ValueError` for a missing or blank ``user_id`` when it has to
    fall back to it — the same contract as ``runs.list_runs``, so a blank key
    can never widen a query. The shared branch needs no caller id at all.
    """
    if is_shared():
        return configured_key()
    if user_id is None or not str(user_id).strip():
        raise ValueError(
            "workspace_id needs the caller it is resolving for — a blank id "
            "would widen every read that is keyed on it")
    return user_id
