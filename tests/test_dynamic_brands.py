# backend/tests/test_dynamic_brands.py
"""Unit E: registry dynamic-source injection (Task 10) + Firestore brand spec
source + font materialization (Task 11).

Golden id pinning: current STATIC pack ids, printed via
`.venv\\Scripts\\python.exe -c "import app; from graphics_designer_agent import
registry; print([p['id'] for p in registry.list_packs()])"` ->
['legalsoft', 'medvirtual', 'remote_attorneys']. Pinned in
`test_golden_flag_off_registry_unchanged` below so any accidental change to
the static registry (byte-identical guarantee) fails loudly.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path (see app/__init__.py)
import pytest
from graphics_designer_agent import registry


@pytest.fixture(autouse=True)
def clean_registry():
    registry.refresh()
    yield
    registry.register_dynamic_source(None)
    registry.refresh()


# --------------------------------------------------------------------------- #
# Task 10 — registry dynamic-source injection (flag-gated, static-wins,
# fault-isolated)
# --------------------------------------------------------------------------- #

def test_golden_flag_off_registry_unchanged(monkeypatch):
    monkeypatch.delenv("GD_DYNAMIC_BRANDS", raising=False)
    registry.register_dynamic_source(lambda: [{"id": "ghost"}])
    ids = {p["id"] for p in registry.list_packs()}
    assert ids == {"legalsoft", "medvirtual", "remote_attorneys"}  # exact current ids


def test_flag_on_adds_dynamic_brand(monkeypatch, valid_dyn_spec):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [valid_dyn_spec])
    assert valid_dyn_spec["id"] in {p["id"] for p in registry.list_packs()}


def test_static_wins_on_id_collision(monkeypatch, valid_dyn_spec):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    valid_dyn_spec["id"] = "legalsoft"
    registry.register_dynamic_source(lambda: [valid_dyn_spec])
    pack = registry.get_pack("legalsoft")
    assert pack.name == "Legal Soft"  # static pack untouched


def test_broken_spec_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [{"id": "broken"}])  # missing keys
    assert registry.list_packs()  # registry still works
