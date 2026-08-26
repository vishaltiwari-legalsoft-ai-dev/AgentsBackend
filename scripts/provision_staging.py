#!/usr/bin/env python
"""Provision the staging layer: its own database, bucket and Cloud Run service.

Why this exists
---------------
Until now there has been exactly one layer. ``main`` was production: every push
built and deployed straight to the service real people use, against the one
Firestore database holding their real data. There was nowhere to land a change
and look at it first, which is why a GA4 misattribution ran for two months
before anyone saw it, and why test fixtures (``u1``, ``admin1``) ended up as
1,516 rows in the production ``runs`` collection.

What isolation actually means here
----------------------------------
Copying the service is the easy half. The half that matters is that staging
cannot touch production data or be driven by production callers:

* **Its own Firestore database.** The single most important line in this file.
  A staging bug writes to ``agentos-staging`` and production never notices.
* **Its own JWT secret.** A production token must not authenticate against
  staging, and a staging token must not authenticate against production. Same
  secret would make the two layers one trust boundary wearing two URLs.
* **Its own cron keys.** So the production scheduler cannot accidentally drive
  staging sweeps, and a leaked staging key buys nothing in production.
* **Its own GCS bucket.** Artefacts from a half-finished experiment do not land
  in the library people actually use.

Deliberately *shared*: the OpenRouter/Serper keys and the Google OAuth client.
Model calls from staging bill the same account — that is a real cost and worth
knowing — but minting separate provider keys is an account-level decision, not
something a provisioning script should invent.

Idempotent: every step checks for the resource first and skips it if present,
so re-running after a partial failure is safe and does not disturb what already
exists. Nothing production-facing is ever modified — the only writes are to
resources whose names carry the staging suffix.

Usage (from ``backend/``)::

    .venv/Scripts/python scripts/provision_staging.py
    .venv/Scripts/python scripts/provision_staging.py --apply
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT = "helpful-charmer-498509-v5"
REGION = "us-central1"
LOCATION = "nam5"  # matches the production database

PROD_SERVICE = "agentsbackend"
STAGING_SERVICE = "agentsbackend-staging"
STAGING_DATABASE = "agentos-staging"
STAGING_BUCKET = "ls_agent_team_staging"

#: Env vars that must NOT be inherited from production — each gets a fresh value
#: so the two layers never share a trust boundary. See the module docstring.
REGENERATED = ("JWT_SECRET", "GEO_CRON_KEY", "SEO_CRON_KEY", "MR_CRON_KEY")

#: Env vars that must point somewhere different in staging.
def _overrides() -> dict[str, str]:
    return {
        "APP_ENV": "staging",
        "FIRESTORE_DATABASE": STAGING_DATABASE,
        "GCS_BUCKET_NAME": STAGING_BUCKET,
    }


def _session():
    import google.auth
    import google.auth.transport.requests as greq

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return greq.AuthorizedSession(creds)


# --------------------------------------------------------------------------- #
# Firestore
# --------------------------------------------------------------------------- #

def database_exists(session) -> bool:
    r = session.get(
        f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases", timeout=30
    )
    r.raise_for_status()
    names = {d["name"].split("/")[-1] for d in r.json().get("databases", [])}
    return STAGING_DATABASE in names


def create_database(session) -> None:
    """Create the staging database with the same protections as production.

    Production has PITR and delete protection enabled; staging gets delete
    protection too. It is cheap, and "it was only staging" is exactly the
    sentence that precedes losing a week of somebody's test setup.
    """
    r = session.post(
        f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases",
        params={"databaseId": STAGING_DATABASE},
        json={
            "type": "FIRESTORE_NATIVE",
            "locationId": LOCATION,
            "deleteProtectionState": "DELETE_PROTECTION_ENABLED",
            "pointInTimeRecoveryEnablement": "POINT_IN_TIME_RECOVERY_ENABLED",
        },
        timeout=120,
    )
    r.raise_for_status()
    op = r.json().get("name", "")
    print(f"    create started: {op}")
    for _ in range(60):
        if database_exists(session):
            print("    database is live")
            return
        time.sleep(5)
    print("    still provisioning — Firestore creation can take a few minutes")


# --------------------------------------------------------------------------- #
# Cloud Storage
# --------------------------------------------------------------------------- #

def bucket_exists(session) -> bool:
    r = session.get(
        f"https://storage.googleapis.com/storage/v1/b/{STAGING_BUCKET}", timeout=30
    )
    return r.status_code == 200


def create_bucket(session) -> None:
    r = session.post(
        "https://storage.googleapis.com/storage/v1/b",
        params={"project": PROJECT},
        json={
            "name": STAGING_BUCKET,
            "location": "US",
            "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
            # Staging artefacts are disposable by definition; without this the
            # bucket grows forever for output nobody will look at again.
            "lifecycle": {"rule": [
                {"action": {"type": "Delete"},
                 "condition": {"age": 30}},
            ]},
        },
        timeout=60,
    )
    if r.status_code not in (200, 409):
        r.raise_for_status()


# --------------------------------------------------------------------------- #
# Cloud Run
# --------------------------------------------------------------------------- #

def get_service(session, name: str) -> dict | None:
    r = session.get(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services/{name}",
        timeout=30,
    )
    return r.json() if r.status_code == 200 else None


def staging_env(prod: dict) -> list[dict]:
    """Production's environment, with the isolating values replaced."""
    container = prod["template"]["containers"][0]
    env = {e["name"]: e.get("value", "") for e in container.get("env", [])}
    env.update(_overrides())
    for key in REGENERATED:
        if key in env or key in ("JWT_SECRET",):
            env[key] = secrets.token_urlsafe(48)
    # The staging API is called by the staging frontend, which does not exist
    # yet; localhost keeps it usable from a dev machine on day one.
    env["CORS_ORIGINS"] = "http://localhost:3000,http://localhost:3001"
    env["APP_PUBLIC_URL"] = ""  # filled in after the service reports its URI
    return [{"name": k, "value": v} for k, v in sorted(env.items())]


def create_service(session, prod: dict) -> str:
    container = prod["template"]["containers"][0]
    body = {
        "template": {
            "serviceAccount": prod["template"].get("serviceAccount"),
            "timeout": prod["template"].get("timeout", "900s"),
            "maxInstanceRequestConcurrency": prod["template"].get(
                "maxInstanceRequestConcurrency", 80
            ),
            # One instance is plenty for a layer nobody is load-testing, and it
            # keeps the staging bill close to zero when idle.
            "scaling": {"maxInstanceCount": 1},
            "containers": [{
                "image": container["image"],
                "resources": container.get("resources"),
                "ports": container.get("ports"),
                "env": staging_env(prod),
            }],
        },
        "ingress": "INGRESS_TRAFFIC_ALL",
        "launchStage": "GA",
    }
    r = session.post(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services",
        params={"serviceId": STAGING_SERVICE},
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    print(f"    deploy started: {r.json().get('name', '')}")
    for _ in range(60):
        svc = get_service(session, STAGING_SERVICE)
        if svc and svc.get("uri"):
            return svc["uri"]
        time.sleep(5)
    return ""


def is_invokable(session, name: str) -> bool:
    r = session.post(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services/{name}:getIamPolicy",
        timeout=30,
    )
    if r.status_code != 200:
        return False
    return any(
        b.get("role") == "roles/run.invoker" and "allUsers" in (b.get("members") or [])
        for b in r.json().get("bindings", [])
    )


def allow_unauthenticated(session, name: str) -> None:
    """Make the service reachable, exactly as production is.

    This is not the security boundary and never was: production runs
    ``--allow-unauthenticated`` too, and what actually stands between the
    internet and any data is ``/api/auth/google`` plus the sign-in allowlist,
    re-checked on every request. Staging mirroring that is the point — a layer
    with different auth semantics to production tests the wrong thing.

    Staging's own JWT secret and allowlist still apply, so reachable here means
    "a legalsoft.com account can sign in", not "anyone can read anything".
    """
    r = session.post(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services/{name}:setIamPolicy",
        json={"policy": {"bindings": [
            {"role": "roles/run.invoker", "members": ["allUsers"]}
        ]}},
        timeout=60,
    )
    if r.status_code != 200:
        # An org policy (constraints/iam.allowedPolicyMemberDomains) commonly
        # refuses allUsers, and the reason only appears in the body. Surface it
        # rather than raising a bare "400 Bad Request" that says nothing.
        raise RuntimeError(f"setIamPolicy failed ({r.status_code}): {r.text[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="create the resources (default reports what is missing)")
    args = parser.parse_args()

    session = _session()
    print(f"project={PROJECT} region={REGION}\n")

    prod = get_service(session, PROD_SERVICE)
    if not prod:
        print(f"Could not read {PROD_SERVICE} — nothing to mirror.")
        return 1
    image = prod["template"]["containers"][0]["image"]
    print(f"mirroring {PROD_SERVICE}")
    print(f"  image: {image}\n")

    have_db = database_exists(session)
    have_bucket = bucket_exists(session)
    have_svc = get_service(session, STAGING_SERVICE) is not None

    print("staging resources:")
    print(f"  firestore/{STAGING_DATABASE:26} {'present' if have_db else 'MISSING'}")
    print(f"  gcs/{STAGING_BUCKET:31} {'present' if have_bucket else 'MISSING'}")
    print(f"  run/{STAGING_SERVICE:31} {'present' if have_svc else 'MISSING'}")

    if not args.apply:
        print("\nDRY RUN — nothing created. Re-run with --apply.")
        return 0

    if not have_db:
        print(f"\ncreating firestore database {STAGING_DATABASE}")
        create_database(session)
    if not have_bucket:
        print(f"\ncreating bucket {STAGING_BUCKET}")
        create_bucket(session)
        print("    created")
    if not have_svc:
        print(f"\ncreating cloud run service {STAGING_SERVICE}")
        uri = create_service(session, prod)
        print(f"    uri: {uri or '(still deploying)'}")
    else:
        print("\nservice already exists — left untouched")

    if not is_invokable(session, STAGING_SERVICE):
        print("\ngranting run.invoker to allUsers (mirrors production)")
        allow_unauthenticated(session, STAGING_SERVICE)
        print("    granted")

    print("\nStaging layer ready. Its JWT secret and cron keys are freshly")
    print("generated, so production tokens and schedulers cannot reach it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
