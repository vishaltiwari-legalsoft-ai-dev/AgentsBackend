"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
import os
import pathlib
import sys

AGENT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
# seo_blog_agent imports seo_geo_agent.sources — its root must be importable too.
GEO_ROOT = AGENT_ROOT.parent / "SEO GEO agent"
if str(GEO_ROOT) not in sys.path:
    sys.path.insert(0, str(GEO_ROOT))
BACKEND_ROOT = AGENT_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ["SEO_OFFLINE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_BLOG_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
