"""Every Google API client in this repo must be built with a stated deadline.

W2-C set explicit socket timeouts on the Google clients it found, and each of
those has its own behavioural test (``tests/test_google_client_timeouts.py``,
``marketing_research_agent/tests/test_sheets_transport.py``,
``seo_geo_agent/tests/test_google_client_timeouts.py``,
``browser_agent/tests/test_tools_sheets_transport.py``). Those tests pin the
clients that existed on the day they were written. Nothing pins the *rule*, so
the seventh Google client somebody adds next month starts out unbounded again
and every one of those suites still passes.

This file is the rule. It is structural on purpose:

``build(credentials=...)`` constructs its own httplib2 transport internally, so
``http=`` is the ONLY way to state a socket timeout — this is why the fix took
the shape it did everywhere else in the codebase (see
``sheets_source.timed_http``). A ``build()`` call with no ``http=`` therefore
*is* an unbounded client, statically, with no behaviour to run.

Why unbounded matters here specifically: every one of these calls runs inside a
sync FastAPI handler, i.e. on anyio's 40-slot worker threadpool. A hung Google
call does not fail slowly, it removes a slot from the whole process — enough of
them and /api/health stops answering too.

No client is constructed and no socket is opened: this file only reads source.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", ".pytest_cache",
              "site-packages"}


def _source_files() -> list[Path]:
    return [p for p in BACKEND.rglob("*.py") if not _SKIP_DIRS & set(p.parts)]


def _imports_discovery_build(tree: ast.AST) -> bool:
    """The import is function-local in every current call site, so walk it all."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "googleapiclient.discovery":
            if any(alias.name == "build" for alias in node.names):
                return True
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("googleapiclient") for alias in node.names):
                return True
    return False


def _discovery_build_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named_build = isinstance(func, ast.Name) and func.id == "build"
        dotted_build = isinstance(func, ast.Attribute) and func.attr == "build" and (
            isinstance(func.value, ast.Name) and func.value.id in {"discovery", "googleapiclient"}
        )
        if named_build or dotted_build:
            calls.append(node)
    return calls


def untimed_client_sites() -> dict[str, list[int]]:
    """``{"path/to/module.py": [line, ...]}`` for every unbounded Google client."""
    offenders: dict[str, list[int]] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        if not _imports_discovery_build(tree):
            continue  # `reports.build`, `trends.build`, reportlab's `.build` …
        bad = [
            call.lineno
            for call in _discovery_build_calls(tree)
            if not any(kw.arg == "http" for kw in call.keywords)
        ]
        if bad:
            offenders[str(path.relative_to(BACKEND)).replace("\\", "/")] = bad
    return offenders


# --------------------------------------------------------------------------- #
# Known, reported, NOT fixed here — this file is test code only.
#
# seo_geo_agent/gsc_oauth.py:service() builds a Search Console client with
# `credentials=` and no transport, so it carries no socket deadline. It is
# reached from a live request path (insights.py:337 → the SEO Lab panel and the
# SEO cron), which is the same exposure W2-C closed on the other six clients.
# Owner: senior-python-backend.
#
# This is an equality ratchet, deliberately: a NEW unbounded client fails
# `test_no_new_unbounded_google_client_is_added`, and *fixing* gsc_oauth fails
# `test_the_known_gap_is_still_the_only_one`, which is the signal to delete this
# entry. Neither direction can pass silently.
# --------------------------------------------------------------------------- #
KNOWN_UNBOUNDED = {"agents/SEO GEO agent/seo_geo_agent/gsc_oauth.py"}


def test_the_sweep_finds_the_google_clients_at_all():
    """Non-vacuity. If the detector stops recognising `build()` — an import
    style changes, a directory moves — every assertion below passes while
    inspecting nothing."""
    seen = {
        str(p.relative_to(BACKEND)).replace("\\", "/")
        for p in _source_files()
        if _imports_discovery_build(ast.parse(p.read_text(encoding="utf-8-sig")))
    }
    assert {
        "app/services/drive_source.py",
        "agents/Marketing Research agent/marketing_research_agent/sources/sheets_source.py",
        "agents/SEO GEO agent/seo_geo_agent/sources.py",
        "agents/Browser agent/browser_agent/tools.py",
        "agents/SEO GEO agent/seo_geo_agent/gsc_oauth.py",
    } <= seen, sorted(seen)


def test_the_detector_flags_a_credentials_only_build():
    """The matcher must be able to say no."""
    source = (
        "from googleapiclient.discovery import build\n"
        "def svc(creds):\n"
        "    return build('sheets', 'v4', credentials=creds, cache_discovery=False)\n"
    )
    tree = ast.parse(source)
    assert _imports_discovery_build(tree)
    calls = _discovery_build_calls(tree)
    assert len(calls) == 1
    assert not any(kw.arg == "http" for kw in calls[0].keywords)


def test_the_detector_accepts_a_timed_transport():
    source = (
        "from googleapiclient.discovery import build\n"
        "def svc(creds):\n"
        "    return build('sheets', 'v4', http=timed_http(creds, 30), cache_discovery=False)\n"
    )
    call = _discovery_build_calls(ast.parse(source))[0]
    assert any(kw.arg == "http" for kw in call.keywords)


def test_no_new_unbounded_google_client_is_added():
    """The forward-looking law: whatever Google client lands next carries a
    deadline, or this fails on the commit that adds it."""
    new = {file: lines for file, lines in untimed_client_sites().items()
           if file not in KNOWN_UNBOUNDED}
    assert new == {}, (
        "Google API client built without an explicit socket deadline. "
        "`build(credentials=...)` makes its own transport, so pass "
        "`http=` — see marketing_research_agent.sources.sheets_source.timed_http:\n"
        + "\n".join(f"  {f}: line(s) {ls}" for f, ls in sorted(new.items()))
    )


def test_the_known_gap_is_still_the_only_one():
    """Fails the moment gsc_oauth is fixed — delete the KNOWN_UNBOUNDED entry
    then. A ratchet that never releases is just a permanent exemption."""
    assert set(untimed_client_sites()) == KNOWN_UNBOUNDED, (
        "KNOWN_UNBOUNDED is stale — update it to match reality: "
        f"{sorted(untimed_client_sites())}"
    )


# --------------------------------------------------------------------------- #
# The deadline constants themselves. Per-module tests assert a client equals its
# module's constant; nothing asserted the constants are sane numbers, so
# `SHEETS_TIMEOUT_SECONDS = 0` (falsy → httplib2 treats it as "no timeout") or
# `= 3600` would satisfy every one of them.
# --------------------------------------------------------------------------- #

_DEADLINES = [
    ("app.services.drive_source", "DRIVE_TIMEOUT_SECONDS"),
    ("app.services.storage", "_METADATA_TIMEOUT_SECONDS"),
    ("app.services.storage", "_TRANSFER_TIMEOUT_SECONDS"),
    ("app.services.storage", "_AUTH_TIMEOUT_SECONDS"),
    ("app.security", "GOOGLE_CERT_FETCH_TIMEOUT_SECONDS"),
    ("marketing_research_agent.sources.sheets_source", "SHEETS_TIMEOUT_SECONDS"),
    ("marketing_research_agent.sources.sheets_source", "AUTH_TIMEOUT_SECONDS"),
    ("marketing_research_agent.sources.sheets_source", "EXPORT_TIMEOUT_SECONDS"),
    ("seo_geo_agent.sources", "GOOGLE_API_TIMEOUT_SECONDS"),
    ("browser_agent.tools", "SHEETS_WRITE_TIMEOUT_SECONDS"),
]


@pytest.mark.parametrize("module_name,const", _DEADLINES)
def test_every_declared_deadline_is_a_finite_usable_number(module_name, const):
    import importlib

    import app  # noqa: F401 - registers the agent roots on sys.path

    value = getattr(importlib.import_module(module_name), const)
    assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{const}={value!r}"
    # 0 is falsy and httplib2 reads it as "no timeout at all" — the exact bug
    # this whole class of fix exists to prevent.
    assert 0 < value <= 120, f"{module_name}.{const} = {value}"


def test_the_deadline_list_covers_every_module_that_builds_a_google_client():
    """Keeps the list above honest as modules are added."""
    modules_with_clients = {
        str(p.relative_to(BACKEND)).replace("\\", "/")
        for p in _source_files()
        if _imports_discovery_build(ast.parse(p.read_text(encoding="utf-8-sig")))
        and "tests" not in p.parts and not p.name.startswith("test_")
    }
    checked_tails = {name.rsplit(".", 1)[-1] for name, _ in _DEADLINES}
    unchecked = {
        path for path in modules_with_clients
        if Path(path).stem not in checked_tails
        and path not in KNOWN_UNBOUNDED
    }
    assert unchecked == set(), (
        "these modules build a Google client but declare no deadline constant "
        f"this file checks: {sorted(unchecked)}"
    )
