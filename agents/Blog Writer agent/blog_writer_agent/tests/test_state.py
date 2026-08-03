"""State gate: offline JSON fallback must round-trip and stay Firestore-safe."""
from __future__ import annotations

from datetime import date

from blog_writer_agent import state


def test_save_load_roundtrip():
    state.save("run-x", {"id": "x", "topic": "virtual assistants"})
    assert state.load("run-x") == {"id": "x", "topic": "virtual assistants"}


def test_load_missing_returns_none():
    assert state.load("run-nope") is None


def test_delete_removes_doc():
    state.save("run-gone", {"id": "gone"})
    state.delete("run-gone")
    assert state.load("run-gone") is None


def test_json_unsafe_values_survive_via_roundtrip():
    state.save("run-d", {"created": date(2026, 8, 3)})
    assert state.load("run-d") == {"created": "2026-08-03"}
