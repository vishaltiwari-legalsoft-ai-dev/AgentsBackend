"""Per-brand Google Search Console connection — the "Connect Search Console" button.

The round trip is the Inbox Triage (a12) one, ``inbox_triage_agent.gmail_oauth``:

1. ``GET /api/seo-geo/oauth/start/{brand}`` → :func:`auth_url`. A Google consent
   URL whose redirect is the FRONTEND page ``<APP_PUBLIC_URL>/oauth/google`` and
   whose ``state`` is HMAC-bound to this purpose, the brand and the signed-in
   member, and expires in ten minutes (:func:`make_state`).
2. Google sends the browser to that page. The console POSTs ``{code, state}``
   with the member's own bearer to ``POST /api/seo-geo/oauth/complete``.
3. :func:`read_state` checks the state against that caller; :func:`complete`
   exchanges the code, finds the brand's verified property, seals the refresh
   token and stores the grant with who connected it.

So there is no public callback route: the step that used to need one now runs
as an authenticated member, and Cloud Run's public-invoker setting is no longer
on this path.

**The OAuth client is a12's** (``INBOX_GOOGLE_CLIENT_ID`` / ``_SECRET``): it is
the client ``<APP_PUBLIC_URL>/oauth/google`` is registered on, so no second
redirect URI or secret has to exist. Two consequences are deliberate:

* :func:`disconnect` and a refused connect never call Google's revoke. A revoke
  ends the account's whole grant to the client — the same grant a12 holds for
  that account's Gmail — so it would disconnect somebody's inbox. Dropping our
  sealed copy is what "disconnect" means here; the account owner can still
  remove the app at myaccount.google.com/permissions.
* ``include_granted_scopes`` is never sent, so a token minted here carries
  Search Console read-only and nothing a12 was granted.

**The refresh token is sealed** with Fernet under ``INBOX_TOKEN_KEY`` (a12's
vault key) before it is stored, through ``firestore_repo``'s grant accessors.
Nothing here returns it to a caller of the API.

Every refusal a member can see is a :class:`ConnectRefused` — a stable ``code``
and one plain sentence. Environment variable names never go in that sentence;
they go in ``log_detail``, which the router writes to the server log.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from . import state
from .sources import GOOGLE_API_TIMEOUT_SECONDS, CredentialMissing

logger = logging.getLogger("agentos.seo_geo.gsc_oauth")

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SITES_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"
#: The frontend page Google returns to — appended to ``settings.app_public_url``.
REDIRECT_PATH = "/oauth/google"

#: Every state minted here starts with this. It is how ``/oauth/google`` tells a
#: Search Console return from an Inbox one (a12's state starts with a hex user
#: id, which can never spell ``gsc.``), and it is inside the signed bytes too.
STATE_PREFIX = "gsc."
#: Domain separation for the HMAC: a12 signs its own states with the same
#: client secret, so the purpose is part of what is signed, not only the prefix.
_STATE_CONTEXT = b"agentos/seo-gsc-connect/v1|"
#: Seconds a consent round trip may take.
STATE_TTL_SECONDS = 600

#: The code is single-use, so the exchange is never retried — a retry after a
#: timeout could be the second use of a code Google already consumed.
EXCHANGE_TIMEOUT_SECONDS = 30
#: Listing the account's properties is a read: one retry on a blip or a 5xx.
SITES_TIMEOUT_SECONDS = 20
SITES_ATTEMPTS = 2
SITES_BACKOFF_SECONDS = 0.5

# --- the refusal codes the API returns (the frontend keys copy on these) ---
UNCONFIGURED = "gsc_oauth_unconfigured"
OFFLINE = "gsc_offline"
STATE_INVALID = "gsc_state_invalid"
STATE_EXPIRED = "gsc_state_expired"
STATE_OTHER_USER = "gsc_state_other_user"
SCOPE_NOT_GRANTED = "gsc_scope_not_granted"
EXCHANGE_FAILED = "gsc_exchange_failed"
GOOGLE_UNAVAILABLE = "gsc_google_unavailable"
NO_PROPERTY = "gsc_no_property"
PROPERTY_TAKEN = "gsc_property_taken"

#: The run's degraded note when a stored grant may not be used for the brand —
#: stored for another brand id, or for a site the brand's domain no longer is.
FOREIGN_GRANT_NOTE = "Search Console connection is for a different site — reconnect it."
#: The run's degraded note when Google refuses the stored grant's refresh token
#: (``invalid_grant``): removed at myaccount.google.com, ended by a12's revoke
#: on the same Google account (the client is shared), or expired.
REVOKED_NOTE = "Search Console connection was removed at Google — reconnect it."

_UNCONFIGURED_MESSAGE = (
    "Search Console connect isn't set up on this server yet — an admin needs to "
    "finish the Google setup."
)
_STATE_INVALID_MESSAGE = "This connect link isn't valid — start again from the SEO panel."

#: Module seam for the retry back-off, so a test does not sleep.
_sleep = time.sleep


class ConnectRefused(Exception):
    """A connect step that cannot go ahead: a stable ``code``, one plain
    ``message`` for the member, and ``log_detail`` for the server log only."""

    def __init__(self, code: str, message: str, *, log_detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.log_detail = log_detail


@dataclass(frozen=True)
class ConnectState:
    """A ``state`` that passed every check in :func:`read_state` — the only way
    to get one, and what :func:`complete` requires."""

    brand_id: str
    user_id: str


def _settings():
    from app.config import settings

    return settings


def _online() -> bool:
    """Whether this deployment may reach Google and the grant store. The SEO
    agent's own offline switch (``SEO_OFFLINE=1``) turns both off: a grant has
    no local-file form, and an offline run never reads one."""
    return state.use_cloud()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _fernet_or_none():
    from cryptography.fernet import Fernet

    key = _settings().inbox_token_key
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        return None


def missing_config() -> list[str]:
    """The environment variables this flow lacks — for the server log, never
    for a response body. Empty means OAuth is configured."""
    cfg = _settings()
    missing = [
        name for name, value in (
            ("INBOX_GOOGLE_CLIENT_ID", cfg.inbox_google_client_id),
            ("INBOX_GOOGLE_CLIENT_SECRET", cfg.inbox_google_client_secret),
        ) if not value
    ]
    if _fernet_or_none() is None:
        missing.append("INBOX_TOKEN_KEY (missing or not a Fernet key)")
    return missing


def connect_ready() -> bool:
    """Whether "Connect Search Console" can work on this server right now."""
    return _online() and not missing_config()


def _unconfigured(missing: list[str]) -> ConnectRefused:
    return ConnectRefused(
        UNCONFIGURED, _UNCONFIGURED_MESSAGE,
        log_detail="Search Console OAuth is not configured — missing: " + ", ".join(missing),
    )


def _client() -> tuple[str, str]:
    cfg = _settings()
    client_id, secret = cfg.inbox_google_client_id, cfg.inbox_google_client_secret
    if not client_id or not secret:
        raise _unconfigured([n for n, v in (("INBOX_GOOGLE_CLIENT_ID", client_id),
                                            ("INBOX_GOOGLE_CLIENT_SECRET", secret)) if not v])
    return client_id, secret


def _require_ready() -> None:
    """Fail before Google is involved: offline, or anything the flow needs to
    finish — including the key the token will be sealed with — is missing."""
    if not _online():
        raise ConnectRefused(
            OFFLINE,
            "Search Console can't be reached from this deployment — it runs in offline mode.",
            log_detail="SEO_OFFLINE=1",
        )
    missing = missing_config()
    if missing:
        raise _unconfigured(missing)


def redirect_uri() -> str:
    return _settings().app_public_url.rstrip("/") + REDIRECT_PATH


# --------------------------------------------------------------------------- #
# The state: purpose + brand + member + issue time + nonce, signed
# --------------------------------------------------------------------------- #

def _same(a: str, b: str) -> bool:
    """Constant-time equality that cannot raise on non-ASCII input — a client
    sends ``state``, and anything in it must be a refusal, not a 500."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), _STATE_CONTEXT + body.encode(), hashlib.sha256).hexdigest()[:32]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_state(brand_id: str, user_id: str, *, now: float | None = None) -> str:
    """``gsc.<body>.<sig>``: ``body`` is unpadded base64url JSON
    ``{"b": brand, "u": member, "t": issued, "n": nonce}`` — readable (the
    console may decode ``b`` to return to the brand), never trusted unsigned."""
    _, secret = _client()
    claims = {"b": brand_id, "u": user_id,
              "t": int(time.time() if now is None else now), "n": secrets.token_hex(8)}
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{STATE_PREFIX}{body}.{_sign(body, secret)}"


def read_state(token: str, *, user_id: str, now: float | None = None) -> ConnectState:
    """Every check the returned ``state`` must pass, or :class:`ConnectRefused`.

    The signature is checked before anything inside is read, so a forged or
    re-purposed state (an Inbox one included) gets the same sentence as junk."""
    _, secret = _client()
    raw = str(token or "")
    if not raw.startswith(STATE_PREFIX):
        raise ConnectRefused(STATE_INVALID, _STATE_INVALID_MESSAGE, log_detail="wrong purpose prefix")
    parts = raw[len(STATE_PREFIX):].split(".")
    if len(parts) != 2 or not _same(parts[1], _sign(parts[0], secret)):
        raise ConnectRefused(STATE_INVALID, _STATE_INVALID_MESSAGE, log_detail="bad signature")
    try:
        claims = json.loads(_unb64(parts[0]))
        brand_id, bound_to, issued = str(claims["b"]), str(claims["u"]), int(claims["t"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ConnectRefused(STATE_INVALID, _STATE_INVALID_MESSAGE, log_detail="unreadable body") from exc
    if (time.time() if now is None else now) - issued > STATE_TTL_SECONDS:
        raise ConnectRefused(STATE_EXPIRED,
                             "The connect window expired — start again from the SEO panel.")
    if not _same(bound_to, user_id):
        raise ConnectRefused(STATE_OTHER_USER,
                             "This connect link was started by a different account — "
                             "start again from the SEO panel.")
    return ConnectState(brand_id=brand_id, user_id=bound_to)


# --------------------------------------------------------------------------- #
# The consent round trip
# --------------------------------------------------------------------------- #

def auth_url(brand_id: str, *, user_id: str) -> str:
    """The Google consent URL for this brand and member.

    ``access_type=offline`` + ``prompt=consent`` is what makes Google return a
    refresh token, which the scheduled sweep needs. ``select_account`` because
    the account that owns a brand's Search Console is often not the one the
    member is signed in to the browser with."""
    _require_ready()
    client_id, _ = _client()
    return AUTH_ENDPOINT + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": make_state(brand_id, user_id),
    })


def _exchange(code: str) -> dict:
    client_id, secret = _client()
    try:
        resp = httpx.post(TOKEN_ENDPOINT, data={
            "code": code, "client_id": client_id, "client_secret": secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        }, timeout=EXCHANGE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise ConnectRefused(
            GOOGLE_UNAVAILABLE,
            "Google couldn't be reached to finish the connection — press Connect again.",
            log_detail=f"token exchange: {type(exc).__name__}",
        ) from exc
    if resp.status_code != 200:
        # An error body carries no token; its first 200 chars name the cause.
        code_ = GOOGLE_UNAVAILABLE if resp.status_code >= 500 else EXCHANGE_FAILED
        raise ConnectRefused(
            code_, "Google didn't accept the sign-in — start the connection again from the SEO panel.",
            log_detail=f"token exchange HTTP {resp.status_code}: {resp.text[:200]}",
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ConnectRefused(
            GOOGLE_UNAVAILABLE, "Google sent back something unreadable — press Connect again.",
            log_detail="token exchange: non-JSON body",
        ) from exc


def _service_disabled(resp: httpx.Response) -> bool:
    """403 because the Search Console API is not enabled on the OAuth client's
    own GCP project — a server setup gap, not anything the member did."""
    body = resp.text[:2000]
    return resp.status_code == 403 and any(
        marker in body for marker in ("SERVICE_DISABLED", "accessNotConfigured", "has not been used")
    )


def _sites(access_token: str) -> list[dict]:
    """Every Search Console property the account can see. A read, so one retry
    on a transport blip, a 429 or a 5xx; any other answer is final."""
    last = ""
    for attempt in range(SITES_ATTEMPTS):
        if attempt:
            _sleep(SITES_BACKOFF_SECONDS * attempt)
        try:
            resp = httpx.get(SITES_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"},
                             timeout=SITES_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
            continue
        if resp.status_code == 200:
            try:
                return list(resp.json().get("siteEntry") or [])
            except (ValueError, AttributeError):
                last = "non-JSON site list"
                break
        if _service_disabled(resp):
            raise ConnectRefused(
                UNCONFIGURED, _UNCONFIGURED_MESSAGE,
                log_detail="the Search Console API is not enabled on the GCP project that owns "
                           f"INBOX_GOOGLE_CLIENT_ID: HTTP 403 {resp.text[:200]}",
            )
        last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code != 429 and resp.status_code < 500:
            break
    raise ConnectRefused(
        GOOGLE_UNAVAILABLE,
        "Google didn't say which Search Console sites this account can see — press Connect again.",
        log_detail=f"site list: {last}",
    )


def _bare(domain: str) -> str:
    return (domain or "").strip().lower().removeprefix("www.").rstrip(".")


def property_matches(prop: str, domain: str) -> bool:
    """Whether Search Console property ``prop`` IS the brand's site ``domain``.

    Exact, never a substring or prefix: ``sc-domain:{domain}``, or a URL-prefix
    property ``http(s)://{domain}/`` / ``http(s)://www.{domain}/`` — scheme http
    or https, host exactly the domain or its www host, path "/" or empty, and
    nothing else in the URL (no port, credentials, query or fragment). So
    ``https://legalsoft.com.attacker.net/``, ``sc-domain:notlegalsoft.com``,
    ``https://attacker.net/legalsoft.com/`` and ``https://legalsoft.com.au/``
    are NOT legalsoft.com — any member can press Connect, so a lookalike the
    account happens to own must not become the brand's numbers."""
    domain = _bare(domain)
    prop = (prop or "").strip()
    if not domain or not prop:
        return False
    if prop.lower().startswith("sc-domain:"):
        return prop[len("sc-domain:"):].strip().lower() == domain
    try:
        parts = urlsplit(prop)
        host = parts.hostname
    except ValueError:
        return False
    return (
        parts.scheme.lower() in ("http", "https")
        and host in (domain, f"www.{domain}")
        and parts.netloc.lower() == host          # no port, no user:pass@
        and parts.path in ("", "/")
        and not parts.query and not parts.fragment
    )


def match_property(domain: str, sites: list[dict]) -> str | None:
    """Pick the account's property for this brand: the domain property, else
    an exact URL-prefix one (https before http, bare host before www). Only a
    property :func:`property_matches` accepts is ever returned."""
    verified = [s["siteUrl"] for s in sites
                if s.get("siteUrl") and s.get("permissionLevel") != "siteUnverifiedUser"]
    matching = [s for s in verified if property_matches(s, domain)]
    bare = _bare(domain)

    def rank(site: str) -> tuple[int, int, int]:
        if site.lower().startswith("sc-domain:"):
            return (0, 0, 0)
        parts = urlsplit(site)
        return (1, parts.scheme.lower() != "https", parts.hostname != bare)

    return min(matching, key=rank, default=None)


def _held_by_another_brand(brand_id: str, prop: str) -> dict | None:
    """The other registered brand whose stored grant already reads ``prop``.

    The GA rule in ``insights._ga_section``, for Search Console: one property's
    numbers belong to one brand, so a second brand connecting the same property
    is refused rather than reporting the first brand's traffic as its own.
    One batched read of the other brands' grants."""
    from . import insights  # the registry; imported here because insights imports this module

    others = {b["id"]: b for b in insights.list_brands() if b.get("id") and b["id"] != brand_id}
    for other_id, grant in _grant_docs(list(others)).items():
        if str(grant.get("property") or "").strip().lower() == prop.strip().lower():
            return others[other_id]
    return None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def complete(verified: ConnectState, brand: dict, *, code: str,
             connected_by: str, user_id: str) -> dict:
    """Finish the connect for a state :func:`read_state` already accepted:
    exchange the code, find the property, seal the token, store the grant.
    Returns the public view (:func:`connection`'s shape) — never the token."""
    if brand.get("id") != verified.brand_id or user_id != verified.user_id:
        raise RuntimeError("complete() called for a brand or member the state was not issued to")
    _require_ready()
    tokens = _exchange(code)
    if SCOPE not in set(str(tokens.get("scope") or "").split()):
        raise ConnectRefused(
            SCOPE_NOT_GRANTED,
            "Google didn't grant read access to Search Console — connect again and allow it.",
        )
    refresh, access = tokens.get("refresh_token"), tokens.get("access_token")
    if not refresh or not access:
        raise ConnectRefused(
            EXCHANGE_FAILED,
            "Google didn't return a lasting permission — remove AgentOS at "
            "myaccount.google.com/permissions and connect again.",
            log_detail=f"exchange answered without {'refresh' if not refresh else 'access'} token",
        )
    prop = match_property(brand["domain"], _sites(str(access)))
    if not prop:
        raise ConnectRefused(
            NO_PROPERTY,
            f"This Google account has no Search Console access to {brand['domain']} — "
            "sign in with an account that does, or ask the site owner to add it.",
        )
    holder = _held_by_another_brand(brand["id"], prop)
    if holder:
        raise ConnectRefused(
            PROPERTY_TAKEN,
            f"{prop} is already connected to {holder.get('name') or holder['id']} — one "
            "Search Console property can feed only one brand. Disconnect it there first.",
            log_detail=f"brand {brand['id']} asked for {prop}, held by brand {holder['id']}",
        )
    grant = {
        "property": prop,
        "refresh_token_enc": _seal(str(refresh)),
        "scope": SCOPE,
        "connected_by": (connected_by or "").strip().lower() or None,
        "connected_by_user_id": user_id,
        "connected_at": _utcnow(),
    }
    _save_grant(brand["id"], grant)
    return _public(grant)


# --------------------------------------------------------------------------- #
# The vault and the store
# --------------------------------------------------------------------------- #

def _seal(plain: str) -> str:
    fernet = _fernet_or_none()
    if fernet is None:  # _require_ready() ran first; this is the belt to its braces
        raise _unconfigured(["INBOX_TOKEN_KEY (missing or not a Fernet key)"])
    return fernet.encrypt(plain.encode()).decode()


def _open_token(grant: dict) -> str:
    """The refresh token from a stored grant: sealed (current) or, for a legacy
    grant, plaintext. :class:`CredentialMissing` — the run's honest degrade —
    when it cannot be had."""
    sealed = grant.get("refresh_token_enc")
    if sealed:
        from cryptography.fernet import InvalidToken

        fernet = _fernet_or_none()
        if fernet is None:
            raise CredentialMissing(
                "the Search Console connection can't be opened on this server — "
                "an admin needs to finish the Google setup"
            )
        try:
            return fernet.decrypt(str(sealed).encode()).decode()
        except InvalidToken as exc:
            raise CredentialMissing(
                "the stored Search Console connection can't be opened — connect it again"
            ) from exc
    plain = grant.get("refresh_token")
    if plain:
        return str(plain)
    raise CredentialMissing("the stored Search Console connection has no token — connect it again")


def _connected_at(value) -> str | None:
    """ISO-8601 UTC. Legacy grants stored epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return str(value)


def _public(grant: dict) -> dict:
    """What leaves this module about a grant: never a token, sealed or not."""
    return {
        "property": grant.get("property"),
        "connected_by": grant.get("connected_by") or None,
        "connected_at": _connected_at(grant.get("connected_at")),
    }


def _grant_docs(brand_ids: list[str]) -> dict[str, dict]:
    if not brand_ids or not _online():
        return {}
    from app.services import firestore_repo

    return firestore_repo.get_gsc_grants(list(brand_ids))


def _save_grant(brand_id: str, grant: dict) -> None:
    from app.services import firestore_repo

    firestore_repo.save_gsc_grant(brand_id, grant)


def connections(brand_ids: list[str]) -> dict[str, dict]:
    """``{brand_id: public view}`` for every brand with a grant — one batched
    read, which is what the overview uses."""
    return {bid: _public(doc) for bid, doc in _grant_docs(brand_ids).items()}


def connection(brand_id: str) -> dict | None:
    """``{"property", "connected_by", "connected_at"}`` or ``None``."""
    return connections([brand_id]).get(brand_id)


def brand_connections(brands: list[dict]) -> dict[str, dict]:
    """``{brand_id: public view + "mismatch"}`` for every brand with a stored
    grant — one batched read. ``mismatch`` is :func:`foreign_grant` against the
    brand as it is NOW, so a grant the run would ignore is never shown as
    connected (``_gsc_view`` in the router)."""
    by_id = {b["id"]: b for b in brands if b.get("id")}
    return {bid: {**_public(doc), "mismatch": foreign_grant(by_id[bid], doc)}
            for bid, doc in _grant_docs(list(by_id)).items()}


def foreign_grant(brand: dict, grant: dict) -> bool:
    """A stored grant this brand must not read with: stored for a different
    brand id, or for a property that is not the brand's CURRENT domain under
    :func:`property_matches` — a slug deleted and recreated for another site,
    or a domain edited after the connect. A grant with no stored ``brand_id``
    (legacy) is judged on its property alone."""
    stored_for = grant.get("brand_id")
    if stored_for and stored_for != brand.get("id"):
        return True
    return not property_matches(str(grant.get("property") or ""), str(brand.get("domain") or ""))


def grant_revoked(exc: BaseException, *, brand_id: str) -> bool:
    """Whether ``exc`` — or anything it wraps — is Google refusing the grant's
    refresh token with ``invalid_grant``. ``sources.gsc_fetch`` wraps every
    query failure, so the cause chain is walked. The raw error goes to the
    server log; the run shows :data:`REVOKED_NOTE` instead of Google's text."""
    from google.auth.exceptions import RefreshError

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, RefreshError) and "invalid_grant" in str(cur):
            logger.warning("Search Console grant for brand %s refused at Google: %s",
                           brand_id, str(cur)[:200])
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def usable_grant(brand: dict) -> tuple[dict | None, str | None]:
    """``(public view, None)`` when the brand has a grant it may read with,
    ``(None, None)`` when it has none, and ``(None, FOREIGN_GRANT_NOTE)`` when
    its stored grant is :func:`foreign_grant` — the run then ignores it, falls
    back to the shared service account, and says so. One grant read."""
    grant = _grant_docs([brand["id"]]).get(brand["id"])
    if not grant:
        return None, None
    if foreign_grant(brand, grant):
        logger.warning("Search Console grant for brand %s ignored: property %s is not %s",
                       brand["id"], grant.get("property"), brand.get("domain"))
        return None, FOREIGN_GRANT_NOTE
    return _public(grant), None


def disconnect(brand_id: str) -> None:
    """Forget the brand's grant. No Google revoke — see the module docstring."""
    if not _online():
        return
    from app.services import firestore_repo

    firestore_repo.delete_gsc_grant(brand_id)


def service(brand_id: str):
    """Search Console API client on the brand's own grant, or ``None`` when it
    has none (the caller then uses the shared service account).

    The transport carries ``GOOGLE_API_TIMEOUT_SECONDS``, and the token refresh
    rides the same transport, so neither can hang a worker thread."""
    grant = _grant_docs([brand_id]).get(brand_id)
    if not grant:
        return None
    refresh_token = _open_token(grant)
    try:
        client_id, secret = _client()
    except ConnectRefused as exc:
        raise CredentialMissing(
            "the connected Search Console account can't be used — an admin needs to "
            "finish the Google setup on this server"
        ) from exc
    import google_auth_httplib2
    import httplib2
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None, refresh_token=refresh_token, token_uri=TOKEN_ENDPOINT,
        client_id=client_id, client_secret=secret, scopes=[SCOPE],
    )
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=GOOGLE_API_TIMEOUT_SECONDS)
    )
    return build("searchconsole", "v1", http=http, cache_discovery=False)
