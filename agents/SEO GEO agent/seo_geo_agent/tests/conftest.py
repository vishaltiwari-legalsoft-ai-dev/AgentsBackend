"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
import os
import pathlib
import sys
from types import SimpleNamespace

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


@pytest.fixture()
def gsc_connect(monkeypatch):
    """The Search Console connect, configured, against an in-memory grant store.

    Only ``gsc_oauth._online`` is flipped — the brand registry and every other
    SEO document stay on local files under ``SEO_OFFLINE=1`` — and the three
    ``firestore_repo`` grant accessors are replaced, so the root conftest's
    Firestore guard is never reached. Outbound HTTP raises: a test that wants
    Google stubs ``gsc_oauth._exchange`` / ``_sites`` or ``httpx`` itself."""
    import httpx
    from cryptography.fernet import Fernet

    from app.config import settings
    from app.services import firestore_repo
    from seo_geo_agent import gsc_oauth

    key = Fernet.generate_key()
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", key.decode(), raising=False)
    monkeypatch.setattr(settings, "app_public_url", "https://console.example", raising=False)
    monkeypatch.setattr(gsc_oauth, "_online", lambda: True)
    monkeypatch.setattr(gsc_oauth, "_sleep", lambda seconds: None)

    store: dict[str, dict] = {}
    monkeypatch.setattr(firestore_repo, "get_gsc_grants",
                        lambda ids: {b: dict(store[b]) for b in ids if b in store})
    monkeypatch.setattr(firestore_repo, "save_gsc_grant",
                        lambda brand_id, grant: store.__setitem__(brand_id, dict(grant)))
    monkeypatch.setattr(firestore_repo, "delete_gsc_grant",
                        lambda brand_id: store.pop(brand_id, None))

    def _no_network(*args, **kwargs):
        raise AssertionError("outbound network attempted from a test")

    monkeypatch.setattr(httpx, "post", _no_network)
    monkeypatch.setattr(httpx, "get", _no_network)
    return SimpleNamespace(store=store, fernet=Fernet(key))
