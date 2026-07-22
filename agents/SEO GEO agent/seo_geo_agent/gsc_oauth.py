"""Per-brand Google Search Console connection — the 2-click "Connect" path.

Product setup happens ONCE on our side (OAuth client + webmasters.readonly
scope + this backend's callback URI). Customers never touch GCP: they click
Connect, pick their Google account, press Allow — and the agent reads their
Search Console data with their own permission (read-only, revocable by them
at myaccount.google.com/permissions any time).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx

from . import state
from .sources import CredentialMissing

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SITES_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"
STATE_TTL = 600  # seconds a consent round-trip may take


def _client() -> tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not cid or not secret:
        raise CredentialMissing(
            "Google OAuth is not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
        )
    return cid, secret


# ------------------------------- state token -------------------------------
# Signs the brand id through the consent round-trip so the unauthenticated
# callback can't be pointed at an arbitrary brand.

def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]


def make_state(brand_id: str) -> str:
    _, secret = _client()
    payload = f"{brand_id}.{int(time.time())}"
    return f"{payload}.{_sign(payload, secret)}"


def read_state(token: str) -> str:
    _, secret = _client()
    try:
        brand_id, ts, sig = token.rsplit(".", 2)
    except ValueError as exc:
        raise ValueError("Bad state token") from exc
    payload = f"{brand_id}.{ts}"
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        raise ValueError("State signature mismatch")
    if time.time() - int(ts) > STATE_TTL:
        raise ValueError("Connect window expired — start again from the dashboard")
    return brand_id


# ------------------------------ consent flow ------------------------------

def auth_url(brand_id: str, redirect_uri: str) -> str:
    cid, _ = _client()
    return AUTH_ENDPOINT + "?" + urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # we need a refresh token for weekly runs
        "prompt": "consent",
        "state": make_state(brand_id),
    })


def _exchange(code: str, redirect_uri: str) -> dict:
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    cid, secret = _client()
    resp = httpx.post(TOKEN_ENDPOINT, data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        raise CredentialMissing(f"Google token exchange failed: {resp.text[:200]}")
    return resp.json()


def _sites(access_token: str) -> list[dict]:
    resp = httpx.get(SITES_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if resp.status_code != 200:
        raise CredentialMissing(f"Could not list Search Console properties: {resp.text[:200]}")
    return resp.json().get("siteEntry", [])


def match_property(domain: str, sites: list[dict]) -> str | None:
    """Pick the customer's property for this brand: domain property first."""
    verified = [s["siteUrl"] for s in sites if s.get("permissionLevel") != "siteUnverifiedUser"]
    if f"sc-domain:{domain}" in verified:
        return f"sc-domain:{domain}"
    for prefix in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        for site in verified:
            if site.startswith(prefix):
                return site
    return next((s for s in verified if domain in s), None)


def complete(brand: dict, code: str, redirect_uri: str) -> dict:
    """Finish the connect: exchange the code, find the property, persist."""
    tokens = _exchange(code, redirect_uri)
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise CredentialMissing(
            "Google did not return a refresh token — remove this app at "
            "myaccount.google.com/permissions and connect again"
        )
    prop = match_property(brand["domain"], _sites(tokens["access_token"]))
    if not prop:
        raise ValueError(
            f"That Google account has no verified Search Console property for {brand['domain']} — "
            "connect with the account that owns the site's Search Console"
        )
    state.save(f"gsc-auth-{brand['id']}", {
        "refresh_token": refresh,
        "property": prop,
        "connected_at": int(time.time()),
    })
    return {"property": prop}


# ------------------------------- data access -------------------------------

def connection(brand_id: str) -> dict | None:
    return state.load(f"gsc-auth-{brand_id}")


def disconnect(brand_id: str) -> None:
    state.delete(f"gsc-auth-{brand_id}")


def service(brand_id: str):
    """Search Console API client using the brand's own OAuth grant, or None."""
    conn = connection(brand_id)
    if not conn:
        return None
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    cid, secret = _client()
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None, refresh_token=conn["refresh_token"], token_uri=TOKEN_ENDPOINT,
        client_id=cid, client_secret=secret, scopes=[SCOPE],
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)
