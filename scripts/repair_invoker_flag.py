#!/usr/bin/env python
"""Restore public invocation on the Cloud Run services when the invoker flag is lost.

Why this exists
---------------
On 2026-08-31 an env patch was applied with a minimal v2 ``Services.UpdateService``
body (only ``name`` + ``template``, no ``updateMask``). v2 UpdateService is
full-replace: every service-level field absent from the request reset to its
default — including the annotation ``run.googleapis.com/invoker-iam-disabled``,
which is the ONLY thing that makes these services publicly invokable, because the
org policy ``iam.allowedPolicyMemberDomains`` bans ``allUsers`` from any IAM
policy. Result: Google's front end rejected every anonymous request (crons AND
browsers) with a 403 before it reached the container, for a full day, and IAM
could not fix it.

The symptom to recognise: request logs show 403 with ``latency: 0s`` and no
``instanceId`` (the platform answered, not the app). An app-level 403 has real
latency and a JSON body.

The law this incident wrote (also in CLAUDE.md): never patch these services with
a minimal v2 UpdateService body — always read-modify-write the FULL service via
v1 ReplaceService, which is exactly what this script does. It adds the one
annotation and changes nothing else; no new revision is created, env vars are
untouched. Idempotent: a service that already has the flag is skipped.

Usage (from ``backend/``)::

    .venv/Scripts/python scripts/repair_invoker_flag.py            # dry-run
    .venv/Scripts/python scripts/repair_invoker_flag.py --apply agentsbackend
    .venv/Scripts/python scripts/repair_invoker_flag.py --apply agentsbackend agentsbackend-staging

Rollback of the repair itself: run again with ``--revoke`` to set the annotation
to ``"false"`` (returns the service to the locked state).

Credentials: uses application-default credentials; if none are configured, falls
back to the key file named by ``GOOGLE_APPLICATION_CREDENTIALS`` in
``backend/.env``.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

PROJECT = "helpful-charmer-498509-v5"
REGION = "us-central1"
BASE = (
    f"https://{REGION}-run.googleapis.com/apis/serving.knative.dev/v1"
    f"/namespaces/{PROJECT}/services"
)
ANN = "run.googleapis.com/invoker-iam-disabled"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _session():
    import google.auth
    import google.auth.transport.requests as greq

    try:
        creds, _ = google.auth.default(scopes=SCOPES)
    except Exception:
        # Fall back to the key file the backend itself uses, named in .env.
        import google.oauth2.service_account as sa

        env_path = Path(__file__).resolve().parents[1] / ".env"
        key_path = None
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GOOGLE_APPLICATION_CREDENTIALS"):
                key_path = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if not key_path:
            sys.exit("no application-default credentials and no "
                     "GOOGLE_APPLICATION_CREDENTIALS in backend/.env")
        resolved = Path(key_path)
        if not resolved.is_absolute():
            # .env stores the path relative to backend/, like the app does.
            resolved = env_path.parent / resolved
        creds = sa.Credentials.from_service_account_file(str(resolved), scopes=SCOPES)
    return greq.AuthorizedSession(creds)


def main() -> None:
    apply_mode = "--apply" in sys.argv
    revoke = "--revoke" in sys.argv
    want = "false" if revoke else "true"
    targets = [a for a in sys.argv[1:] if not a.startswith("--")] or ["agentsbackend"]
    S = _session()

    for svc_name in targets:
        r = S.get(f"{BASE}/{svc_name}", timeout=60)
        r.raise_for_status()
        svc = r.json()
        anns = svc.setdefault("metadata", {}).setdefault("annotations", {})
        cur = anns.get(ANN)
        print(f"{svc_name}: current {ANN} = {cur!r}")
        if cur == want:
            print("  already set — nothing to do")
            continue
        fixed = copy.deepcopy(svc)
        fixed["metadata"]["annotations"][ANN] = want
        fixed.pop("status", None)  # output-only
        if not apply_mode and not revoke:
            print("  DRY RUN: would PUT ReplaceService with only this annotation changed")
            continue
        resp = S.put(f"{BASE}/{svc_name}", json=fixed, timeout=120)
        print(f"  ReplaceService -> HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:500])
            sys.exit(1)
        v2 = S.get(
            f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}"
            f"/services/{svc_name}",
            timeout=60,
        ).json()
        print(f"  verify: v2 invokerIamDisabled = {v2.get('invokerIamDisabled')}")
        print(f"  verify: latestReadyRevision   = "
              f"{v2.get('latestReadyRevision', '').split('/')[-1]} (expect unchanged)")


if __name__ == "__main__":
    main()
