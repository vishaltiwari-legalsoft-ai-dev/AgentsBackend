"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
import os
import pathlib
import sys

AGENT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

os.environ["SEO_OFFLINE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
