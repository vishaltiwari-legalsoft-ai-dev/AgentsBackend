"""No module may install a FastAPI dependency override at import time.

``app.main.app`` is a single process-global object, and ``dependency_overrides``
is a plain dict hanging off it. Test modules are imported once, during
collection, *before any test runs* — so a module-level
``fastapi_app.dependency_overrides[get_current_user] = ...`` is not scoped to
that module at all. It is a write into shared state that:

  * leaks into every test collected after it, silently authenticating suites
    that meant to assert 401; and
  * is deleted by the first sibling module whose teardown pops the same key,
    silently de-authenticating the module that installed it.

Which of those two you get depends on collection order, so the suite passed in
alphabetical order and failed under ``-k``, ``-m``, ``-p xdist`` sharding or
``-p randomly``. Wave 1 converted all nine call sites to a save/restore fixture.
This file is the part that stops it coming back: the *rule* is asserted, not the
nine files that happened to break it.

Fully static plus one runtime observation — nothing here builds a client or
touches the network.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# Directories that are not ours to police (vendored code, build artefacts).
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", ".pytest_cache",
              "site-packages"}

# Dict methods that change the mapping. ``.get`` / ``.keys`` and friends are
# reads and stay legal at import time — reading the shared dict harms nobody.
_MUTATORS = {"pop", "popitem", "clear", "update", "setdefault"}

# Statement types whose bodies run at import time. ``ClassDef`` is in the list
# on purpose (a class body executes on import); function bodies are not, which
# is exactly the distinction this whole file rests on.
_IMPORT_TIME_BODIES = (ast.If, ast.Try, ast.With, ast.For, ast.While, ast.ClassDef,
                       ast.AsyncWith, ast.AsyncFor, ast.TryStar, ast.Match)


def _is_overrides(node: ast.AST) -> bool:
    """True for an expression naming ``<something>.dependency_overrides``."""
    return isinstance(node, ast.Attribute) and node.attr == "dependency_overrides"


def _mutations_in(stmt: ast.stmt) -> list[str]:
    """Every ``dependency_overrides`` mutation inside one statement."""
    found: list[str] = []
    for node in ast.walk(stmt):
        # d.dependency_overrides[k] = v   /   del d.dependency_overrides[k]
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _is_overrides(target.value):
                found.append("item assignment")
            elif _is_overrides(target):
                found.append("whole-dict replacement")
        # d.dependency_overrides.pop(...) / .clear() / .update(...)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
                and _is_overrides(node.func.value)):
            found.append(f".{node.func.attr}()")
    return found


def _import_time_statements(body: list[ast.stmt]):
    """Walk only the statements that execute when the module is imported."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # a def is not a call — its body runs when someone runs it
        yield stmt
        if isinstance(stmt, _IMPORT_TIME_BODIES):
            yield from _import_time_statements(getattr(stmt, "body", []))
            yield from _import_time_statements(getattr(stmt, "orelse", []))
            yield from _import_time_statements(getattr(stmt, "finalbody", []))
            for handler in getattr(stmt, "handlers", []):
                yield from _import_time_statements(handler.body)
            for case in getattr(stmt, "cases", []):
                yield from _import_time_statements(case.body)


def import_time_override_mutations(source: str) -> list[str]:
    """Descriptions of every import-time ``dependency_overrides`` mutation."""
    tree = ast.parse(source)
    return [
        f"line {stmt.lineno}: {what}"
        for stmt in _import_time_statements(tree.body)
        for what in _mutations_in(stmt)
    ]


def _python_files() -> list[Path]:
    return [
        path for path in BACKEND.rglob("*.py")
        if not _SKIP_DIRS & set(path.parts)
    ]


# --------------------------------------------------------------------------- #
# The detector must be able to fail. A scanner that silently matches nothing
# is a green test that defends nothing, and this one is doing structural
# matching on an AST — exactly the kind of code that rots into a no-op.
# --------------------------------------------------------------------------- #

_BAD_SOURCES = {
    "plain item assignment": (
        "from app.main import app\n"
        "app.dependency_overrides[get_current_user] = lambda: USER\n"
    ),
    "inside a module-level try": (
        "from app.main import app\n"
        "try:\n"
        "    app.dependency_overrides[dep] = fake\n"
        "except Exception:\n"
        "    pass\n"
    ),
    "inside a module-level if": (
        "if TESTING:\n"
        "    fastapi_app.dependency_overrides[dep] = fake\n"
    ),
    "inside a class body": (
        "class Harness:\n"
        "    app.dependency_overrides[dep] = fake\n"
    ),
    "pop at import time": "app.dependency_overrides.pop(dep, None)\n",
    "clear at import time": "app.dependency_overrides.clear()\n",
    "update at import time": "app.dependency_overrides.update({dep: fake})\n",
    "whole-dict replacement": "app.dependency_overrides = {dep: fake}\n",
    "del at import time": "del app.dependency_overrides[dep]\n",
    "aliased app object": "main_app.dependency_overrides[dep] = fake\n",
    "for loop at import time": (
        "for dep in DEPS:\n"
        "    app.dependency_overrides[dep] = fake\n"
    ),
}

_GOOD_SOURCES = {
    "inside a fixture": (
        "@pytest.fixture(autouse=True)\n"
        "def _harness():\n"
        "    prev = app.dependency_overrides.get(dep)\n"
        "    app.dependency_overrides[dep] = fake\n"
        "    yield\n"
        "    app.dependency_overrides.pop(dep, None)\n"
    ),
    "inside a test body": (
        "def test_requires_auth():\n"
        "    app.dependency_overrides.pop(dep, None)\n"
        "    try:\n"
        "        assert client.get('/x').status_code == 401\n"
        "    finally:\n"
        "        app.dependency_overrides[dep] = fake\n"
    ),
    "inside a nested helper": (
        "def outer():\n"
        "    def inner():\n"
        "        app.dependency_overrides[dep] = fake\n"
        "    return inner\n"
    ),
    "import-time read only": (
        "PREVIOUS = app.dependency_overrides.get(dep)\n"
        "HAS_ANY = bool(app.dependency_overrides)\n"
    ),
    "a same-named dict on something else entirely": (
        "def f():\n"
        "    router.dependency_overrides[dep] = fake\n"
    ),
}


@pytest.mark.parametrize("label", sorted(_BAD_SOURCES))
def test_detector_catches_every_shape_of_import_time_override(label):
    """If this ever goes quiet, the scan below is decoration."""
    assert import_time_override_mutations(_BAD_SOURCES[label]), label


@pytest.mark.parametrize("label", sorted(_GOOD_SOURCES))
def test_detector_permits_the_pattern_the_suite_actually_uses(label):
    """Save/restore inside a fixture or a test body is the sanctioned pattern —
    flagging it would make the rule unusable and get the test deleted."""
    assert import_time_override_mutations(_GOOD_SOURCES[label]) == [], label


# --------------------------------------------------------------------------- #
# The rule itself
# --------------------------------------------------------------------------- #

def test_no_module_installs_a_dependency_override_at_import_time():
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        try:
            found = import_time_override_mutations(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
            pytest.fail(f"{path.relative_to(BACKEND)} could not be parsed: {exc}")
        if found:
            offenders[str(path.relative_to(BACKEND)).replace("\\", "/")] = found

    assert offenders == {}, (
        "dependency_overrides mutated at import time — move it into a fixture "
        "that restores the previous value on teardown (see the _harness fixture "
        "in app/routers/tests/test_mr_router.py):\n"
        + "\n".join(f"  {file}: {', '.join(w)}" for file, w in sorted(offenders.items()))
    )


def test_the_scan_actually_reaches_the_modules_that_use_overrides():
    """Non-vacuity for the scan's *file list*, not just its matcher.

    A path glob that quietly stops matching (renamed directory, an over-eager
    skip entry) would make the rule above pass while inspecting nothing.
    """
    scanned = {str(p.relative_to(BACKEND)).replace("\\", "/") for p in _python_files()}
    assert len(scanned) > 200, f"the glob collapsed to {len(scanned)} files"
    users = {
        file for file in scanned
        if "dependency_overrides" in (BACKEND / file).read_text(encoding="utf-8-sig")
    }
    # Every module that touches overrides today, by construction of the wave-1
    # fix. The set may grow; it must never silently empty out.
    assert len(users) >= 9, sorted(users)
    assert "app/routers/tests/test_mr_router.py" in users
    assert "app/routers/tests/test_geo_router.py" in users
    assert "tests/test_admin_refresh_packs.py" in users


def test_the_shared_override_dict_is_empty_between_tests():
    """The runtime half, and the one that needs no matcher to be correct.

    pytest imports every collected module before running the first test, so by
    the time this body executes any import-time override already exists. No
    fixture of another module is active right now, so the only way this dict is
    non-empty is a leak — either installed at import time, or installed by a
    test that never put it back.
    """
    from app.main import app as fastapi_app

    assert fastapi_app.dependency_overrides == {}, (
        "a dependency override leaked out of its owning test/module: "
        f"{sorted(getattr(d, '__name__', repr(d)) for d in fastapi_app.dependency_overrides)}"
    )
