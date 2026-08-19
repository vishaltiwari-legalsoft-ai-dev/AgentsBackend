"""Integration tests for the SEO agent router (/api/seo-geo). Fully offline.

The auth override is installed per-test by the ``_harness`` fixture and the
previous value is *restored* on teardown. Restore, not ``pop``:
``dependency_overrides`` lives on the one process-global FastAPI app shared by
every test module, so an unconditional ``pop`` here deleted an override a
sibling module was relying on. Paired with modules that installed theirs at
import time, that made 29 router tests pass only in alphabetical order — this
file sorts last today, and anything that reorders (``-k``, ``-m``, sharding,
random order, or just a new file that sorts later) turned them into a 401 storm.
"""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
CREATOR = {**USER, "is_creator": True}

client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


def as_creator():
    """Escalate the current test to a creator. Safe to call mid-test — the
    ``_harness`` teardown restores whatever was in place before it ran."""
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(CREATOR)


def test_overview_lists_default_brand_and_sources():
    body = client.get("/api/seo-geo/overview").json()
    assert body["sources"] == {"gsc": False, "serp": False}
    assert body["brands"][0]["brand"]["id"] == "legalsoft"
    assert body["brands"][0]["last_run"] is None


def test_brand_create_requires_creator():
    payload = {"name": "Acme", "domain": "acme.com"}
    assert client.post("/api/seo-geo/brands", json=payload).status_code == 403
    as_creator()
    body = client.post("/api/seo-geo/brands", json=payload).json()
    assert any(b["id"] == "acme" and b["gsc_property"] == "sc-domain:acme.com" for b in body["brands"])


def test_brand_domain_validation():
    as_creator()
    r = client.post("/api/seo-geo/brands", json={"name": "Bad", "domain": "not-a-domain"})
    assert r.status_code == 422


def test_brand_domain_strips_www_and_protocol():
    as_creator()
    body = client.post("/api/seo-geo/brands",
                       json={"name": "Berry", "domain": "https://www.BerryVirtual.com/"}).json()
    berry = next(b for b in body["brands"] if b["id"] == "berry")
    assert berry["domain"] == "berryvirtual.com"
    assert berry["gsc_property"] == "sc-domain:berryvirtual.com"


def test_run_and_detail_roundtrip():
    r = client.post("/api/seo-geo/run/legalsoft")
    assert r.status_code == 200, r.text
    assert r.json()["degraded"]  # offline: no Search Console
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["run"]["trigger"] == "manual:t@legalsoft.com"


def test_unknown_brand_404():
    assert client.post("/api/seo-geo/run/nope").status_code == 404
    assert client.get("/api/seo-geo/brands/nope").status_code == 404


def test_todo_status_update_validates():
    assert client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "later"}).status_code == 422
    r = client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "assigned"})
    assert r.json() == {"id": "abc123", "status": "assigned"}


def test_keyword_lab_offline_runs_heuristic():
    r = client.post("/api/seo-geo/keywords/legalsoft/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clusters"] and body["degraded"]
    assert client.get("/api/seo-geo/keywords/legalsoft").json()["lab"]["brand_id"] == "legalsoft"


def test_competitors_flow():
    assert client.get("/api/seo-geo/competitors/legalsoft").json()["tracked"] == []
    assert client.put("/api/seo-geo/competitors/legalsoft", json={"domains": ["comp.com"]}).status_code == 403
    as_creator()
    r = client.put("/api/seo-geo/competitors/legalsoft", json={"domains": [" Comp.com "]})
    assert r.json()["tracked"] == ["comp.com"]
    t = client.post("/api/seo-geo/competitors/legalsoft/track")
    assert t.status_code == 200 and t.json()["degraded"]  # offline: no Serper, no fetches


def test_competitor_profiles_endpoints_offline():
    assert client.get("/api/seo-geo/competitors/legalsoft/profiles").json()["profiles"] is None
    r = client.post("/api/seo-geo/competitors/legalsoft/profiles/refresh")
    assert r.status_code == 503  # offline: no rank snapshots to build profiles from


def test_live_analysis_endpoints_offline_503():
    assert client.post("/api/seo-geo/serp/legalsoft", json={"query": "x"}).status_code == 503
    assert client.post("/api/seo-geo/briefs/legalsoft", json={"keyword": "x"}).status_code == 503
    assert client.post("/api/seo-geo/audit/legalsoft/run").status_code == 503
    assert client.get("/api/seo-geo/briefs/legalsoft").json()["briefs"] == []
    assert client.get("/api/seo-geo/audit/legalsoft").json()["report"] is None


def test_oauth_start_callback_and_disconnect(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert client.get("/api/seo-geo/oauth/start/legalsoft").status_code == 503
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    url = client.get("/api/seo-geo/oauth/start/legalsoft").json()["url"]
    assert "accounts.google.com" in url and "webmasters.readonly" in url
    assert client.get("/api/seo-geo/oauth/callback",
                      params={"code": "c", "state": "junk"}).status_code == 400
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").status_code == 403  # creator only
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["gsc"] == {"connected": False, "property": None}


def test_site_review_endpoints_offline():
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"] is None
    assert client.post("/api/seo-geo/site-review/legalsoft").status_code == 503  # offline crawl
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["plan"] == [] and detail["site_review"] is None


def test_pages_endpoints():
    assert client.get("/api/seo-geo/pages/legalsoft").json()["pages"] is None
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 409, r.text  # no corpus yet
    assert r.json()["detail"] == "Run the site analysis first"

    from seo_geo_agent import state as seo_state

    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai"] is False  # offline: no LLM
    assert body["pages"][0]["path"] == "/pricing"
    assert client.get("/api/seo-geo/pages/legalsoft").json()["pages"]["pages"][0]["path"] == "/pricing"


def test_pages_refresh_forwards_gsc_failure_note():
    """A zeroed-out Pages tab must carry the reason it's zero, not read as a
    real all-zero measurement — offline mode means GSC degrades every time."""
    from seo_geo_agent import state as seo_state

    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    assert any(n.startswith("Search Console:") for n in r.json()["notes"])


def test_pages_refresh_forwards_ga_failure_note():
    from seo_geo_agent import insights, state as seo_state

    brand = next(b for b in insights.list_brands() if b["id"] == "legalsoft")
    insights.upsert_brand({**brand, "ga4_property": "properties/999"})
    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    assert any(n.startswith("Google Analytics:") for n in r.json()["notes"])


def test_ask_expert_offline_503():
    r = client.post("/api/seo-geo/ask/legalsoft", json={"question": "kya karna chahiye?"})
    assert r.status_code == 503
    assert client.post("/api/seo-geo/ask/legalsoft", json={"question": "  "}).status_code == 422


def test_draft_score_endpoint():
    r = client.post("/api/seo-geo/draft-score/legalsoft",
                    json={"text": "Buy now.", "keyword": "legal virtual assistant"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "rework"


def test_cron_inert_without_key_then_gated(monkeypatch):
    assert client.post("/api/seo-geo/cron/run").status_code == 503
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    assert client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "wrong"}).status_code == 403
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text  # every brand ran → clean 200
    body = r.json()
    assert body["brands"]["legalsoft"]["ok"] is True
    assert body["status"] == "ok" and body["ok"] == 1 and body["failed"] == 0


def test_cron_all_brands_failed_is_not_200(monkeypatch):
    """C9: the sweep answered 200 even when every brand errored, so Cloud
    Scheduler recorded success, never retried, and the sweep could stay dead for
    weeks with the only evidence inside a body nobody reads."""
    from seo_geo_agent import insights

    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")

    def _boom(brand, trigger=""):
        raise RuntimeError("GSC token revoked")

    monkeypatch.setattr(insights, "run_brand", _boom)
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["status"] == "failed" and body["ok"] == 0 and body["failed"] == 1
    assert body["brands"]["legalsoft"] == {"ok": False, "error": "GSC token revoked"}


def test_cron_partial_sweep_is_207(monkeypatch):
    """One broken brand out of two is neither success nor total failure: 207 so a
    human can act, and still 2xx so a permanently bad brand can't put the job in
    a retry loop."""
    from seo_geo_agent import insights

    as_creator()
    assert client.post("/api/seo-geo/brands",
                       json={"name": "Acme", "domain": "acme.com"}).status_code == 200
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    real = insights.run_brand

    def _one_bad(brand, trigger=""):
        if brand["id"] == "acme":
            raise RuntimeError("GSC token revoked")
        return real(brand, trigger=trigger)

    monkeypatch.setattr(insights, "run_brand", _one_bad)
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["status"] == "partial" and body["ok"] == 1 and body["failed"] == 1


def test_the_oauth_callback_page_escapes_what_it_reflects():
    """``?error=`` is attacker-controlled and lands in the one HTML page this
    backend serves. Confirmed live in 2026-08: ``PROBE<i>`` came back as markup,
    text/html, no CSP, no nosniff — arbitrary HTML from our own domain."""
    r = client.get("/api/seo-geo/oauth/callback",
                   params={"error": "PROBE<i>x</i>'\"onload=alert(1)"})

    assert r.status_code == 400
    assert "<i>" not in r.text and "</i>" not in r.text
    assert "&lt;i&gt;" in r.text                      # reflected as text, not markup
    assert "&quot;" in r.text and "&#x27;" in r.text  # quotes can't break an attribute
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_callback_keeps_its_own_inline_blocks_behind_a_per_response_nonce():
    """The page auto-closes and is styled, and that costs nothing: only the two
    blocks this backend authors carry the nonce, so a reflected <script> is
    inert text under the same ``default-src 'none'`` policy. The nonce is
    generated per response — a constant one would be ``unsafe-inline``."""
    import re as _re

    r = client.get("/api/seo-geo/oauth/callback", params={"error": "<script>alert(1)</script>"})
    csp = r.headers["content-security-policy"]

    assert "default-src 'none'" in csp
    nonce = _re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"

    # our own inline script + style are nonced, and nothing else is
    assert f'<script nonce="{nonce}">' in r.text
    assert f'<style nonce="{nonce}">' in r.text
    assert r.text.count("nonce=") == 2

    # the reflected script never executes: escaped text, and no nonce of its own
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text

    second = client.get("/api/seo-geo/oauth/callback", params={"error": "again"})
    assert nonce not in second.headers["content-security-policy"], "nonce is reused"


def test_the_connected_message_emphasises_the_property_without_trusting_it():
    """The <b> is markup we author around an escaped value — the value itself
    can never carry markup."""
    from app.routers import seo_geo as router_mod

    page = router_mod._close_page("Connected", "reading data from {strong}.",
                                  strong="sc-domain:<evil>")
    text = page.body.decode()
    assert "<b>sc-domain:&lt;evil&gt;</b>" in text
    assert "<evil>" not in text

