"""Integration tests for the SEO agent router (/api/seo-geo). Fully offline.

The caller is a *non*-creator by default, which several tests below depend on:
they assert the 403 first and only then escalate. Both the default caller and
the escalation go through the shared harness in ``conftest.py``, whose autouse
guard restores the overrides map around every test — so escalating is safe
mid-test and needs no teardown here.
"""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest

from app.routers.tests.conftest import client

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
CREATOR = {**USER, "is_creator": True}


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    as_caller(USER)


@pytest.fixture()
def as_creator(as_caller):
    """Escalate the current test to a creator. Safe to call mid-test — the
    conftest guard restores whatever was in place before the test ran."""
    return lambda: as_caller(CREATOR)


def test_overview_lists_default_brand_and_sources():
    body = client.get("/api/seo-geo/overview").json()
    # offline: no grant is read, connect cannot work, nothing has read Search Console
    assert body["sources"] == {"gsc": False, "gsc_connect": False, "serp": False}
    assert body["brands"][0]["brand"]["id"] == "legalsoft"
    assert body["brands"][0]["last_run"] is None
    assert body["brands"][0]["gsc"] == {
        "connected": False, "property": None, "connected_by": None, "connected_at": None,
        "service_account_property": "sc-domain:legalsoft.com", "reading": False,
    }


def test_brand_create_requires_creator(as_creator):
    payload = {"name": "Acme", "domain": "acme.com"}
    assert client.post("/api/seo-geo/brands", json=payload).status_code == 403
    as_creator()
    body = client.post("/api/seo-geo/brands", json=payload).json()
    assert any(b["id"] == "acme" and b["gsc_property"] == "sc-domain:acme.com" for b in body["brands"])


def test_brand_domain_validation(as_creator):
    as_creator()
    r = client.post("/api/seo-geo/brands", json={"name": "Bad", "domain": "not-a-domain"})
    assert r.status_code == 422


def test_brand_domain_strips_www_and_protocol(as_creator):
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


def test_competitors_flow(as_creator):
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


def test_oauth_start_offline_and_disconnect_gate():
    r = client.get("/api/seo-geo/oauth/start/legalsoft")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "gsc_offline"
    assert client.get("/api/seo-geo/oauth/start/nope").status_code == 404
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").status_code == 403  # creator only
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["gsc"]["connected"] is False and detail["gsc"]["property"] is None


def test_site_review_endpoints_offline():
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"] is None
    assert client.post("/api/seo-geo/site-review/legalsoft").status_code == 503  # offline crawl
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["plan"] == [] and detail["site_review"] is None


def test_site_review_with_a_wrong_shape_answer_is_503_not_500(monkeypatch):
    """A model answer that is a JSON list used to hit ``raw.get`` -> AttributeError
    -> an unexplained 500. It is a failed review: an honest 503 with the reason."""
    from seo_geo_agent import site_brain

    monkeypatch.setattr(site_brain, "build_corpus",
                        lambda brand, fetch=None, sitemap=None: {"brand_id": brand["id"], "page_count": 0,
                                                                 "pages": [], "degraded": []})
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: ["not", "an", "object"])
    r = client.post("/api/seo-geo/site-review/legalsoft")
    assert r.status_code == 503
    assert "not the review object" in r.json()["detail"]
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"] is None


def _todo(tid, action, why="w", status="todo"):
    return {"id": tid, "kind": "striking", "page": "https://legalsoft.com/p", "query": "q",
            "action": action, "why": why, "est_monthly_clicks": 10, "position": 8,
            "impressions": 100, "status": status}


def test_this_week_strip_never_repeats_fix_list_first():
    """The strip sits right above the Fix list. Its first move used to be the Fix
    list's #1, so the same action showed twice, one under the other."""
    from seo_geo_agent import state as seo_state

    first = "Refresh the content and add internal links for 'legal virtual assistant'"
    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [], "summary": {},
        "insights": [], "topics": [], "ga": None, "todos": [
            _todo("t1", first),
            _todo("t2", "Refresh the content and add internal links for “legal virtual assistant”"),
            _todo("t3", "Rewrite the title and meta description targeting 'paralegal services'"),
        ]})
    seo_state.save("keywords-legalsoft", {"clusters": [
        {"name": "legal virtual assistant", "tier": "high", "recommendation": "same move as #1"},
        {"name": "law firm answering service", "tier": "high", "recommendation": "a new page"},
    ]})
    seo_state.save("audit-legalsoft", {"issues": [
        {"issue": "Broken or unreachable pages", "severity": "high", "fix": "Redirect them"}],
        "site_checks": []})

    plan = client.get("/api/seo-geo/brands/legalsoft").json()["plan"]
    actions = [p["action"] for p in plan]
    assert actions == [
        "Rewrite the title and meta description targeting 'paralegal services'",  # #2 restates #1
        "Go after “law firm answering service”",                                  # cluster 1 restates #1
        "Fix: Broken or unreachable pages",
    ]
    assert first not in actions


def test_this_week_strip_skips_fix_list_first_even_when_it_is_the_only_todo():
    from seo_geo_agent import state as seo_state

    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [], "summary": {},
        "insights": [], "topics": [], "ga": None, "todos": [_todo("t1", "Fix the pricing page")]})
    assert client.get("/api/seo-geo/brands/legalsoft").json()["plan"] == []


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


def test_cron_partial_sweep_is_207(monkeypatch, as_creator):
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


# ----------------------- Search Console connect (OAuth) -----------------------
#
# Google returns to the console's ``/oauth/google``; the console posts
# ``{code, state}`` to ``POST /api/seo-geo/oauth/complete`` with the member's
# bearer. Google itself is stubbed at ``gsc_oauth._exchange`` / ``_sites``.

OTHER = {"id": "u2", "email": "other@legalsoft.com", "is_admin": False, "is_creator": False}
TOKENS = {"refresh_token": "r1", "access_token": "a1",
          "scope": "https://www.googleapis.com/auth/webmasters.readonly"}
LEGALSOFT_OWNED = [{"siteUrl": "sc-domain:legalsoft.com", "permissionLevel": "siteOwner"}]
GSC_KEYS = {"connected", "property", "connected_by", "connected_at",
            "service_account_property", "reading"}


@pytest.fixture()
def gsc(monkeypatch):
    """Configured connect, in-memory grant store, outbound HTTP refused. Only
    ``gsc_oauth._online`` flips; the brand registry stays on local files."""
    from types import SimpleNamespace

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
    store: dict = {}
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
    monkeypatch.setattr(gsc_oauth, "_exchange", lambda code: dict(TOKENS))
    monkeypatch.setattr(gsc_oauth, "_sites", lambda token: LEGALSOFT_OWNED)
    return SimpleNamespace(store=store, fernet=Fernet(key), oauth=gsc_oauth)


def _start_state(brand_id: str = "legalsoft") -> str:
    from urllib.parse import parse_qs, urlparse

    r = client.get(f"/api/seo-geo/oauth/start/{brand_id}")
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["url"]).query)["state"][0]


def test_there_is_no_public_callback_route_any_more():
    """The console finishes the connect with the member's bearer; nothing on
    this path depends on an anonymous route or Cloud Run's public invoker."""
    from fastapi.routing import APIRoute

    from app.main import app as fastapi_app

    paths = {r.path for r in fastapi_app.routes if isinstance(r, APIRoute)}
    assert not any(p.endswith("/oauth/callback") and "seo-geo" in p for p in paths)
    assert client.get("/api/seo-geo/oauth/callback",
                      params={"code": "c", "state": "s"}).status_code in (404, 405)


def test_connect_unconfigured_is_a_coded_503_and_the_names_stay_in_the_log(
        gsc, monkeypatch, caplog):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_google_client_secret", "", raising=False)
    with caplog.at_level("WARNING", logger="agentos.seo_geo"):
        r = client.get("/api/seo-geo/oauth/start/legalsoft")
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "code": "gsc_oauth_unconfigured",
        "message": "Search Console connect isn't set up on this server yet — an admin "
                   "needs to finish the Google setup.",
    }
    for leak in ("INBOX_", "GOOGLE_CLIENT", "SECRET", "TOKEN_KEY"):
        assert leak not in r.text
    warned = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("INBOX_GOOGLE_CLIENT_SECRET" in rec.getMessage() for rec in warned)

    complete = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": "gsc.x.y"})
    assert complete.status_code == 503
    assert complete.json()["detail"]["code"] == "gsc_oauth_unconfigured"
    assert gsc.store == {}


def test_connect_round_trip_records_who_connected_and_seals_the_token(gsc):
    from urllib.parse import parse_qs, urlparse

    url = client.get("/api/seo-geo/oauth/start/legalsoft").json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["redirect_uri"] == "https://console.example/oauth/google"
    assert q["scope"] == "https://www.googleapis.com/auth/webmasters.readonly"
    assert q["state"].startswith("gsc.")

    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": q["state"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brand_id"] == "legalsoft"
    assert set(body["gsc"]) == GSC_KEYS
    assert body["gsc"]["connected"] is True
    assert body["gsc"]["property"] == "sc-domain:legalsoft.com"
    assert body["gsc"]["connected_by"] == "t@legalsoft.com"
    assert body["gsc"]["connected_at"]
    assert "r1" not in r.text and "refresh_token" not in r.text

    doc = gsc.store["legalsoft"]
    assert "refresh_token" not in doc
    assert gsc.fernet.decrypt(doc["refresh_token_enc"].encode()).decode() == "r1"
    assert doc["connected_by"] == "t@legalsoft.com" and doc["connected_by_user_id"] == "u1"

    overview = client.get("/api/seo-geo/overview").json()
    card = next(c for c in overview["brands"] if c["brand"]["id"] == "legalsoft")
    assert card["gsc_connected"] is True
    assert card["gsc"]["connected_by"] == "t@legalsoft.com"
    assert overview["sources"]["gsc"] is True and overview["sources"]["gsc_connect"] is True
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["gsc"] == card["gsc"]
    assert "r1" not in str(overview) + str(detail)


def test_complete_refuses_a_state_issued_to_someone_else(gsc, as_caller):
    state = _start_state()
    as_caller(OTHER)
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": state})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "gsc_state_other_user"
    assert gsc.store == {}


def test_complete_refuses_a_tampered_or_expired_state(gsc):
    import time as _time

    state = _start_state()
    tampered = client.post("/api/seo-geo/oauth/complete",
                           json={"code": "c", "state": state[:-4] + "0000"})
    assert tampered.status_code == 400
    assert tampered.json()["detail"]["code"] == "gsc_state_invalid"

    stale = gsc.oauth.make_state("legalsoft", "u1",
                                 now=_time.time() - gsc.oauth.STATE_TTL_SECONDS - 5)
    expired = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": stale})
    assert expired.status_code == 400
    assert expired.json()["detail"]["code"] == "gsc_state_expired"
    assert gsc.store == {}


def test_complete_rejects_an_empty_body_at_the_boundary(gsc):
    assert client.post("/api/seo-geo/oauth/complete", json={"code": "", "state": ""}).status_code == 422
    assert client.post("/api/seo-geo/oauth/complete",
                       json={"code": "c", "state": "x" * 5000}).status_code == 422


def test_complete_says_plainly_when_the_account_cannot_see_the_site(gsc, monkeypatch):
    monkeypatch.setattr(gsc.oauth, "_sites", lambda token: [])
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "gsc_no_property",
        "message": "This Google account has no Search Console access to legalsoft.com — "
                   "sign in with an account that does, or ask the site owner to add it.",
    }
    assert gsc.store == {}


def test_any_member_connects_but_only_a_creator_disconnects(gsc, as_creator):
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").status_code == 403
    assert "legalsoft" in gsc.store
    as_creator()
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").json() == {"connected": False}
    assert gsc.store == {}
    overview = client.get("/api/seo-geo/overview").json()
    assert overview["sources"]["gsc"] is False
    assert overview["brands"][0]["gsc"]["connected"] is False


def test_deleting_a_brand_takes_its_search_console_grant_with_it(gsc, as_creator):
    """Hard delete through the route: the brand's sealed token goes with it, so
    no grant outlives the brand that the disconnect route would need to reach it."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    assert "legalsoft" in gsc.store
    assert client.delete("/api/seo-geo/brands/legalsoft").status_code == 403   # creator only
    assert "legalsoft" in gsc.store
    as_creator()
    assert client.delete("/api/seo-geo/brands/legalsoft").status_code == 200
    assert gsc.store == {}


def test_overview_reading_flag_comes_from_the_latest_run_not_from_the_cloud_flag():
    """``sources.gsc`` used to be ``use_cloud()`` — "Connected" in prod with
    nothing connected. It is now true only when a brand really has Search
    Console behind it; here, a latest run that read Search Console rows."""
    from seo_geo_agent import state

    assert client.get("/api/seo-geo/overview").json()["sources"]["gsc"] is False
    state.save("run-legalsoft", {"at": "2026-10-14T00:00:00Z", "summary": {"mode": "search-console"},
                                 "degraded": [], "todos": [], "topics": [], "gsc_rows": 412})
    body = client.get("/api/seo-geo/overview").json()
    card = body["brands"][0]
    assert card["gsc"]["reading"] is True and card["gsc"]["connected"] is False
    assert body["sources"]["gsc"] is True


@pytest.mark.parametrize(("summary", "reading"), [
    ({"mode": "search-console", "impressions_28d": 5400}, True),   # it read rows
    ({"mode": "search-console", "impressions_28d": 0}, False),     # the both-failed summary
    ({"mode": "rank-tracking"}, False),
])
def test_a_run_stored_before_gsc_rows_reads_from_its_impressions(summary, reading):
    from seo_geo_agent import state

    state.save("run-legalsoft", {"at": "2026-10-13T00:00:00Z", "summary": summary,
                                 "degraded": [], "todos": [], "topics": []})
    assert client.get("/api/seo-geo/overview").json()["brands"][0]["gsc"]["reading"] is reading


def _both_sources_fail(monkeypatch):
    from seo_geo_agent import insights
    from seo_geo_agent.sources import CredentialMissing

    def no_search_console(*args, **kwargs):
        raise CredentialMissing("Search Console refused the property")

    def no_serper(brand):
        raise CredentialMissing("Serper credits exhausted")

    monkeypatch.setattr(insights, "gsc_fetch", no_search_console)
    monkeypatch.setattr(insights.competitors, "rank_snapshot", no_serper)


def test_a_run_where_search_console_and_rank_tracking_both_failed_is_not_reading(monkeypatch):
    """The empty summary such a run stores still says mode "search-console";
    Integrations read that as "Connected" with no data behind it."""
    _both_sources_fail(monkeypatch)
    assert client.post("/api/seo-geo/run/legalsoft").status_code == 200
    run = client.get("/api/seo-geo/brands/legalsoft").json()["run"]
    assert run["summary"]["mode"] == "search-console" and run["gsc_rows"] == 0

    body = client.get("/api/seo-geo/overview").json()
    assert body["brands"][0]["gsc"]["reading"] is False
    assert body["sources"]["gsc"] is False
    assert client.get("/api/seo-geo/brands/legalsoft").json()["gsc"]["reading"] is False


def test_a_grant_still_shows_connected_when_its_run_failed_to_read(gsc, monkeypatch):
    """``connected`` is the stored grant; ``reading`` is what the run got. A
    grant whose run read nothing is connected, not reading."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(gsc.oauth, "service", lambda brand_id: object())   # no Google client
    _both_sources_fail(monkeypatch)
    assert client.post("/api/seo-geo/run/legalsoft").status_code == 200

    body = client.get("/api/seo-geo/overview").json()
    assert body["brands"][0]["gsc"]["connected"] is True
    assert body["brands"][0]["gsc"]["reading"] is False
    assert body["sources"]["gsc"] is True   # a grant exists


def test_connecting_a_property_another_brand_reads_is_a_coded_409(gsc, as_creator):
    as_creator()
    assert client.post("/api/seo-geo/brands", json={"name": "Twin", "domain": "legalsoft.com"}
                       ).status_code == 200
    gsc.store["twin"] = {"brand_id": "twin", "property": "sc-domain:legalsoft.com",
                         "refresh_token_enc": "t"}
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "gsc_property_taken",
        "message": "sc-domain:legalsoft.com is already connected to Twin — one Search "
                   "Console property can feed only one brand. Disconnect it there first.",
    }
    assert "legalsoft" not in gsc.store


GEO_ONLY = {"id": "g1", "email": "contractor@aivirtual.com", "is_admin": False,
            "is_creator": False, "is_geo_only": True}


def test_a_geo_only_caller_gets_the_overview_without_any_search_console_block(gsc, as_caller):
    """GEO-only contractors reach this route (the console shell reads it) but
    use no Search Console data, so a staff member's connect — their email, when,
    and the property — never reaches them."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    staff = client.get("/api/seo-geo/overview").json()
    assert staff["brands"][0]["gsc"]["connected_by"] == "t@legalsoft.com"

    as_caller(GEO_ONLY)
    r = client.get("/api/seo-geo/overview")
    assert r.status_code == 200
    for card in r.json()["brands"]:
        assert "gsc" not in card and "gsc_connected" not in card
        assert card["brand"]["id"]                          # the rest of the card is intact
    assert "t@legalsoft.com" not in r.text and "connected_at" not in r.text

