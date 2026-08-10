"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
import os
import pathlib
import sys

_AGENTS_DIR = pathlib.Path(__file__).resolve().parents[3]
# final_geo_agent shares a2's state/sources/insights, so both roots go on the path.
for _root in (_AGENTS_DIR / "Final GEO agent", _AGENTS_DIR / "SEO GEO agent"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

os.environ["SEO_OFFLINE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
    # a real OPENROUTER_API_KEY in local .env must never fire paid polls here;
    # tests that exercise the fallback stub httpx or patch this themselves
    from final_geo_agent import geo_engines
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
