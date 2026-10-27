"""PR Writer tests — offline. No model call, no Firestore, no network.

The repo-root conftest already blanks the OpenRouter key and blocks
Firestore; every model call here goes through an injected fake ``invoke``.
Helpers live in ``pr_writer_testkit`` (import them from there, never from
``conftest``); this file only turns them into fixtures.
"""
from __future__ import annotations

import copy

import pytest

from pr_writer_testkit import EVIDENCE, FakeInvoke, golden_press_release, load_card


@pytest.fixture
def card() -> dict:
    return load_card()


@pytest.fixture
def evidence() -> list[dict]:
    return copy.deepcopy(EVIDENCE)


@pytest.fixture
def golden() -> dict:
    return golden_press_release()


@pytest.fixture
def fake():
    return FakeInvoke
