"""Shared harness for the router integration suites.

Every module under this directory drives the *same* process-global objects:
``app.main.app`` and the plain dict hanging off it, ``dependency_overrides``.
Before this file existed, each suite carried its own six-line
save / override / restore fixture, and the correctness of the whole directory
rested on all of them agreeing. They did not: one popped where the others
restored, and two installed at import time, which is how 29 router tests came
to pass in alphabetical order and 401 under ``-k``, ``-m``, sharding or random
ordering (the incident is written up in
``tests/test_dependency_override_hygiene.py``).

The fix here is structural rather than clerical. ``_isolated_dependency_overrides``
is autouse and lives in the conftest, so it wraps *every* test in the directory
and — because a conftest autouse fixture is set up before a module-level one and
torn down after it — it also wraps each suite's own harness. Whatever a test or
its fixtures put into the overrides dict, the dict is byte-for-byte what it was
by the time the next test starts. A suite can no longer leak an override even if
its author forgets to clean up, because cleaning up is no longer the author's
job.

Callers are installed with ``as_caller`` / ``as_admin``. Both write the exact
mapping they are given rather than merging over a template: the identity a suite
asserts against (``is_creator`` on the GEO suite, the literal e-mail the browser
status endpoint echoes back) is load-bearing test data and belongs in the suite
that asserts it, not hidden in a shared default.
"""
from __future__ import annotations

from typing import Any, Callable

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user, require_admin

# One client for the whole directory. It holds no per-test state — the state
# that used to leak lives in ``dependency_overrides``, which the fixture below
# owns — so seventeen copies of it bought nothing.
client = TestClient(fastapi_app)

#: The plain authenticated caller: signed in, no elevated role. Suites that
#: assert nothing about the caller's identity use this via a bare
#: ``as_caller()``.
DEFAULT_CALLER: dict[str, Any] = {"id": "u1", "email": "t@legalsoft.com"}

#: The elevated caller, for the suites behind ``require_admin``.
ADMIN_CALLER: dict[str, Any] = {
    "id": "u1", "email": "t@legalsoft.com", "is_admin": True, "is_creator": True,
}


@pytest.fixture(autouse=True)
def _isolated_dependency_overrides():
    """Restore ``app.dependency_overrides`` around every test in this directory.

    Snapshots the whole mapping, not one key. A per-key save/restore cannot
    express "this test added a second override" (``require_admin`` alongside
    ``get_current_user``) and silently leaves the extra key behind; the
    whole-dict swap has no such gap.

    Restoring in place (``clear`` + ``update``) rather than rebinding the
    attribute matters: FastAPI resolves overrides through the dict object it
    was given, and anything holding a reference to the old one would stop
    seeing changes.
    """
    saved = dict(fastapi_app.dependency_overrides)
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(saved)


@pytest.fixture()
def as_caller(_isolated_dependency_overrides) -> Callable[..., dict[str, Any]]:
    """Install the authenticated caller for this test.

    ``as_caller()`` installs :data:`DEFAULT_CALLER`; ``as_caller(USER)`` installs
    exactly ``USER``. Safe to call again mid-test to swap identity — the suite
    does not have to restore anything, because the autouse fixture above does.

    Depends on ``_isolated_dependency_overrides`` explicitly so the ordering the
    guarantee rests on is declared rather than inferred from autouse ordering.
    """

    def _install(user: dict[str, Any] | None = None) -> dict[str, Any]:
        caller = dict(DEFAULT_CALLER if user is None else user)
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(caller)
        return caller

    return _install


@pytest.fixture()
def as_admin(as_caller) -> Callable[..., dict[str, Any]]:
    """Install an admin/creator caller, past ``require_admin`` as well.

    ``require_admin`` depends on ``get_current_user``, so overriding the caller
    alone would already satisfy it. It is overridden too because a suite that
    means "I am not testing the admin gate here" should say so once, rather than
    leaving the gate live and its 403 a possible cause of an unrelated failure.
    """

    def _install(user: dict[str, Any] | None = None) -> dict[str, Any]:
        admin = as_caller(ADMIN_CALLER if user is None else user)
        fastapi_app.dependency_overrides[require_admin] = lambda: dict(admin)
        return admin

    return _install


@pytest.fixture()
def unauthenticated() -> Callable[[], None]:
    """Drop the caller override so the real dependency runs and answers 401/403.

    The suites used to do this inline with a ``try/finally`` that reinstalled the
    caller. The ``finally`` is now redundant — nothing survives the test — but
    naming the operation keeps the intent legible at the call site.
    """

    def _drop() -> None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)

    return _drop
