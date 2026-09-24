"""Sign-in allowlist tests for /api/auth/google. Fully offline.

The backend is deployed --allow-unauthenticated, so this endpoint is the only
membership check in the system: a valid Google token proves who you are, never
that you belong here. Everything below pins that boundary — including the case
that matters most, an *unconfigured* allowlist rejecting rather than admitting.

Only the outer seams are faked: Google token verification and the Firestore
user upsert. The router, the settings properties and the gate itself run real.

Note what this module deliberately does *not* do: install a ``get_current_user``
override. This endpoint is how a caller becomes authenticated, so a pre-authed
caller would make the allowlist untestable. The shared harness in ``conftest.py``
installs a caller only when a suite asks for one, so nothing here is authed —
and its autouse guard still makes sure no sibling suite's override leaks in.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.config import settings
from app.routers import auth
from app.routers.tests.conftest import client
from app.services import firestore_repo

CREDENTIAL = "a-google-id-token-that-the-verifier-will-accept"


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    """Verified-Google-account harness with a known allowlist.

    ``created`` records every upsert so a test can assert that a refused
    sign-in never reached Firestore.
    """
    created: list[str] = []

    def fake_verify(credential: str) -> dict[str, str]:
        # Stands in for a *successful* verification — audience and
        # email_verified checks live in app.security and are exercised there.
        return {
            "sub": "google-sub-1",
            "email": fake_verify.email,
            "name": "Test User",
            "picture": "",
        }

    fake_verify.email = "someone@legalsoft.com"

    def fake_upsert(*, email: str, name: str, picture: str, google_sub: str) -> dict:
        created.append(email)
        return {"id": "u1", "email": email, "name": name, "picture": picture}

    monkeypatch.setattr(auth, "verify_google_id_token", fake_verify)
    monkeypatch.setattr(firestore_repo, "get_or_create_google_user", fake_upsert)
    monkeypatch.setattr(firestore_repo, "new_session_id", lambda: "sess-1")
    monkeypatch.setattr(settings, "jwt_secret", "test-only-signing-key-" + "0" * 32)
    monkeypatch.setattr(settings, "allowed_email_domains", "legalsoft.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    yield {"verify": fake_verify, "created": created}


def login(harness, email: str):
    harness["verify"].email = email
    return client.post("/api/auth/google", json={"credential": CREDENTIAL})


def test_allowed_domain_signs_in(_harness):
    resp = login(_harness, "colleague@legalsoft.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["user"]["email"] == "colleague@legalsoft.com"
    assert _harness["created"] == ["colleague@legalsoft.com"]


def test_domain_match_ignores_case_and_padding(_harness, monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", " LegalSoft.com , acme.io ")
    assert login(_harness, "Colleague@LEGALSOFT.com").status_code == 200
    assert login(_harness, "partner@acme.io").status_code == 200


def test_exception_email_signs_in(_harness, monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "contractor@outside.dev")
    # Matching is case-insensitive; the stored address keeps Google's casing.
    resp = login(_harness, "Contractor@Outside.dev")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"].lower() == "contractor@outside.dev"


def test_stranger_is_refused_with_403(_harness):
    resp = login(_harness, "randomperson@gmail.com")
    assert resp.status_code == 403
    assert _harness["created"] == []  # no user document for a refused account


def test_refusal_message_does_not_leak_account_state(_harness):
    stranger = login(_harness, "never-seen@gmail.com").json()["detail"]
    other = login(_harness, "also-never-seen@example.org").json()["detail"]
    # Same message for every refusal — a caller cannot tell a de-provisioned
    # colleague from an account we have never heard of.
    assert stranger == other
    assert "never-seen" not in stranger
    assert "gmail" not in stranger.lower()


def test_subdomain_does_not_inherit_the_allowlist(_harness):
    assert login(_harness, "attacker@evil-legalsoft.com").status_code == 403
    assert login(_harness, "attacker@mail.legalsoft.com").status_code == 403


def test_empty_allowlist_fails_closed(_harness, monkeypatch):
    """Unset config must mean "no one", not "everyone"."""
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    monkeypatch.setattr(settings, "allowed_emails", "")
    assert login(_harness, "colleague@legalsoft.com").status_code == 403
    assert login(_harness, "randomperson@gmail.com").status_code == 403
    assert _harness["created"] == []


def test_owner_is_never_locked_out(_harness, monkeypatch):
    """A Creator keeps access even with an empty/hostile allowlist — otherwise a
    bad ALLOWED_EMAIL_DOMAINS deploy locks the project owners out of the panel
    that fixes it."""
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "creator_emails", "owner@elsewhere.test")
    resp = login(_harness, "Owner@Elsewhere.test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["is_creator"] is True


def _pristine_settings(monkeypatch):
    """Settings built from the shipped defaults only — no .env, no exported
    variable from whoever is running the suite."""
    from app.config import Settings

    for name in ("APP_ENV", "ALLOWED_EMAIL_DOMAINS", "ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_the_login_response_tells_the_console_who_is_a_geo_editor(_harness, monkeypatch):
    """Without this the six being onboarded and everyone else see the same
    controls, and a non-editor finds out by pressing a button and getting a 403.

    Asserted for all three roles at once, because the flag only helps if it
    tracks the others: a GEO editor is NOT an admin and NOT a creator, and the
    console renders three different surfaces off exactly these booleans.
    """
    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")

    editor = login(_harness, "editor@legalsoft.com").json()["user"]
    assert editor["is_geo_editor"] is True
    assert editor["is_admin"] is False and editor["is_creator"] is False

    plain = login(_harness, "colleague@legalsoft.com").json()["user"]
    assert plain["is_geo_editor"] is False

    # A Creator is one implicitly, so the console shows them the editor surface
    # without anybody having to list them twice in config.
    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    boss = login(_harness, "boss@legalsoft.com").json()["user"]
    assert boss["is_geo_editor"] is True and boss["is_creator"] is True


def test_the_geo_editor_flag_is_derived_not_carried_in_the_token(_harness, monkeypatch):
    """The display hint must not become a second, staler source of truth.

    ``create_token`` stamps no ``geo_editor`` claim; the login response computes
    it from config at response time, the same as ``is_admin``/``is_creator``.
    Decoding the issued token and finding the role in it would mean a revoked
    editor kept the editor UI for the 7-day life of that token.
    """
    import jwt as pyjwt

    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    body = login(_harness, "editor@legalsoft.com").json()
    assert body["user"]["is_geo_editor"] is True

    claims = pyjwt.decode(body["token"], settings.jwt_secret, algorithms=["HS256"])
    assert "geo_editor" not in claims and "is_geo_editor" not in claims


def test_the_login_response_tells_the_console_who_is_scoped_to_geo(
    _harness, monkeypatch,
):
    """The console's half of the scope wall.

    143 routes now answer 403 to a GEO-only caller, and the console cannot see
    that from the outside — so it keeps offering panels that fail on click. This
    flag is what lets it stop drawing them. It is a display hint only:
    ``app.scopes.deny_outside_geo`` re-derives the scope per request and remains
    the enforcement whatever this payload says.

    The key is asserted PRESENT in every case, including ``False``. The console
    reads a missing key as "session predates the wall" and a present ``False``
    as "not scoped"; omitting it for the common case would merge those.
    """
    monkeypatch.setattr(settings, "geo_only_emails", "scoped@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")

    scoped = login(_harness, "scoped@legalsoft.com").json()["user"]
    assert scoped["is_geo_only"] is True

    plain = login(_harness, "colleague@legalsoft.com").json()["user"]
    assert "is_geo_only" in plain and plain["is_geo_only"] is False

    # The exemption, at the response too: an owner who fat-fingers their own
    # address into GEO_ONLY_EMAILS must not be shown a locked-down console.
    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    monkeypatch.setattr(settings, "geo_only_emails", "boss@legalsoft.com")
    boss = login(_harness, "boss@legalsoft.com").json()["user"]
    assert boss["is_geo_only"] is False and boss["is_creator"] is True


def test_the_geo_only_flag_is_derived_not_carried_in_the_token(_harness, monkeypatch):
    """A scope in the token would outlive its own revocation by seven days.

    The direction that matters is scoping someone DOWN: that has to bite on the
    next request, not next week. ``create_token`` therefore stamps no scope
    claim, and ``get_current_user`` re-derives it from the email claim — the
    same reasoning as the geo-editor test above, one step more load-bearing
    because this one takes access away.
    """
    import jwt as pyjwt

    monkeypatch.setattr(settings, "geo_only_emails", "scoped@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")

    body = login(_harness, "scoped@legalsoft.com").json()
    assert body["user"]["is_geo_only"] is True

    claims = pyjwt.decode(body["token"], settings.jwt_secret, algorithms=["HS256"])
    assert "geo_only" not in claims and "is_geo_only" not in claims


#: The GEO editors on outside domains, admitted one address at a time.
EXTERNAL_GEO_EDITORS = {
    "lynie.t@aivirtual.com",
    "miguel@usimmigration.ai",
    "yans.suarez@medvirtual.ai",
    "franceska@aianswering.ai",
}


#: Domains admitted wholesale. legalsoft.com is the company; aivirtual.com was
#: added by owner decision on 2026-09-24 so every mailbox there gets the hub.
ALLOWED_DOMAINS = {"legalsoft.com", "aivirtual.com"}


def test_allowlist_defaults_are_closed(monkeypatch):
    defaults = _pristine_settings(monkeypatch)
    assert defaults.allowed_email_domain_set == ALLOWED_DOMAINS
    # Named individuals, not more domains. The exception list growing is
    # expected — it is the mechanism for contractors and clients — so this
    # pins WHO is on it rather than that it is empty.
    assert defaults.allowed_email_set == EXTERNAL_GEO_EDITORS


def test_the_geo_editor_roster_is_pinned(monkeypatch):
    """Who can edit GEO, stated once so growth is deliberate.

    ``allowed_emails`` decides who may sign in; this decides who may then
    change prompt universes, add brands and switch the paid weekly check on.
    They are separate lists and drift apart easily: a legalsoft.com address
    signs in on the domain rule and needs no allowlist entry, so forgetting it
    here is silent — the person lands on a panel with every control missing and
    no explanation. Pinning the roster turns that into a red test instead.
    """
    defaults = _pristine_settings(monkeypatch)
    assert defaults.geo_editor_email_set == {
        # Whole domains, by owner decision 2026-09-25 — every mailbox at an
        # allowed domain edits GEO, current and future, with no per-person
        # entry to forget. Individual legalsoft/aivirtual names do not belong
        # here any more: the domain rule already covers them.
        "@legalsoft.com",
        "@aivirtual.com",
    } | (EXTERNAL_GEO_EDITORS - {"lynie.t@aivirtual.com"})
    # Every outside-domain editor must also be able to reach the door.
    assert EXTERNAL_GEO_EDITORS <= defaults.allowed_email_set


#: The five lists that decide who gets what. Pinned together so a test about
#: "what ships" reads every one of them off the class defaults and none off the
#: developer's ``.env``.
_ACCESS_FIELDS = (
    "allowed_email_domains", "allowed_emails", "admin_emails",
    "geo_editor_emails", "geo_only_emails",
)


def _ship_the_defaults(monkeypatch):
    pristine = _pristine_settings(monkeypatch)
    for field in _ACCESS_FIELDS:
        monkeypatch.setattr(settings, field, getattr(pristine, field))
    monkeypatch.setattr(settings, "creator_emails", "")


def test_every_aivirtual_mailbox_edits_geo_and_is_not_scoped(_harness, monkeypatch):
    """Owner decision 2026-09-24: aivirtual.com gets the whole hub — every
    agent, GEO editing, and admin — from the shipped defaults alone.

    Admin is the one that ALSO has to be on the service: both Cloud Run
    services set ADMIN_EMAILS as an env var, and env replaces the default
    rather than merging with it (see the field's comment in ``app.config``).
    """
    from app.security import is_admin, is_geo_editor, is_geo_only

    _ship_the_defaults(monkeypatch)

    for member in ("lynie.t@aivirtual.com", "new.hire@aivirtual.com", "Ops@AIVirtual.com"):
        body = login(_harness, member).json()["user"]
        assert body["is_admin"] is True, member
        assert body["is_geo_editor"] is True, member
        assert body["is_geo_only"] is False, member
        assert body["is_creator"] is False, member

    # The domain rule is exact: a subdomain gets nothing from it.
    assert not is_admin("x@mail.aivirtual.com")
    assert not is_geo_editor("x@mail.aivirtual.com")
    # The outside editors: editor, not admin, and — since 2026-09-25 — not
    # scoped by any default either. Scoping is opt-in via GEO_ONLY_EMAILS on
    # the service (tests/test_allowlist_live_routes.py pins the mechanism).
    assert is_geo_editor("miguel@usimmigration.ai") is True
    assert is_admin("miguel@usimmigration.ai") is False
    assert is_geo_only("miguel@usimmigration.ai") is False
    assert is_geo_only("lynie.t@aivirtual.com") is False


def test_a_fresh_mailbox_at_either_allowed_domain_gets_the_whole_hub(_harness, monkeypatch):
    """Owner decision 2026-09-25, and the reason: a production screenshot of
    an aivirtual.com account on a "GEO ONLY" console, after a code default had
    quietly named it. Every user at legalsoft.com and aivirtual.com — the two
    domains the door admits wholesale — gets the full platform by default,
    with zero per-user diagnosis: sign-in, every agent, GEO editing. Nobody
    is GEO-only unless GEO_ONLY_EMAILS on the service names their exact
    address, so the class default is pinned EMPTY here; a developer's .env
    cannot make this pass.
    """
    from app.config import Settings

    assert Settings.model_fields["geo_only_emails"].default == ""
    _ship_the_defaults(monkeypatch)

    # Nobody has ever heard of these two — that is the point.
    for newcomer in ("someone.new@aivirtual.com", "someone.new@legalsoft.com"):
        resp = login(_harness, newcomer)
        assert resp.status_code == 200, (newcomer, resp.text)
        body = resp.json()["user"]
        assert body["is_geo_editor"] is True, newcomer
        assert "is_geo_only" in body and body["is_geo_only"] is False, newcomer
        assert body["is_creator"] is False, newcomer

    # Admin differs by domain, on purpose: aivirtual.com is admin wholesale
    # (2026-09-24); legalsoft.com admins are named on the service's
    # ADMIN_EMAILS, so a fresh legalsoft mailbox is a member, not an admin.
    assert login(_harness, "someone.new@aivirtual.com").json()["user"]["is_admin"] is True
    assert login(_harness, "someone.new@legalsoft.com").json()["user"]["is_admin"] is False


def test_the_other_outside_domains_were_not_admitted_wholesale(monkeypatch):
    """Only the domains the owner chose are open; the rest stay named addresses.

    Three GEO editors are at usimmigration.ai, medvirtual.ai and aianswering.ai.
    Adding those DOMAINS would open sign-in to every mailbox at three companies
    whose accounts nobody here provisions or de-provisions — on a service
    Cloud Run serves --allow-unauthenticated, where this list is the only door.
    aivirtual.com is the one exception, by decision, and is pinned above.
    """
    defaults = _pristine_settings(monkeypatch)
    assert defaults.allowed_email_domain_set == ALLOWED_DOMAINS
    for address in EXTERNAL_GEO_EDITORS - {"lynie.t@aivirtual.com"}:
        domain = address.rpartition("@")[2]
        assert domain not in defaults.allowed_email_domain_set


def test_shipped_domains_admit_aivirtual_but_no_other_outside_company(
    _harness, monkeypatch,
):
    """The same statement at the door instead of in the config.

    The harness pins a one-domain allowlist; this restores the SHIPPED defaults
    (read from a pristine ``Settings``, never retyped here) so the assertion is
    about what the service actually deploys with.
    """
    pristine = _pristine_settings(monkeypatch)
    monkeypatch.setattr(settings, "allowed_email_domains", pristine.allowed_email_domains)
    monkeypatch.setattr(settings, "allowed_emails", pristine.allowed_emails)

    # Anyone at aivirtual.com — not just the named editor — gets in.
    for member in ("lynie.t@aivirtual.com", "someone.else@aivirtual.com",
                   "Billing@AIVirtual.com"):
        assert login(_harness, member).status_code == 200, member

    # A colleague of the other named outside editors still does not.
    for stranger in ("ceo@usimmigration.ai", "intern@medvirtual.ai",
                     "support@aianswering.ai", "attacker@mail.aivirtual.com"):
        assert login(_harness, stranger).status_code == 403, stranger
    # Refused before the upsert, so no junk user document either.
    assert _harness["created"] == [
        "lynie.t@aivirtual.com", "someone.else@aivirtual.com", "Billing@AIVirtual.com",
    ]


def test_app_env_defaults_to_production(monkeypatch):
    """APP_ENV is set in no deploy config, so the default is what prod actually
    runs — and it must be the one that hides raw exception text."""
    assert _pristine_settings(monkeypatch).app_env == "production"
