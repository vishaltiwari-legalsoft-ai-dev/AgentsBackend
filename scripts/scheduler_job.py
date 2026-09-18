#!/usr/bin/env python
"""Create, pause, resume or delete a Cloud Scheduler HTTP job shaped exactly
like the three live ones (POST, Content-Type + X-Cron-Key headers, body "{}",
no OIDC, retryCount 0 — the next fire is the retry).

The cron key is read from this process's environment by NAME and is masked in
every print. Dry-run by default; --apply writes.

Lives in backend/scripts/. Run from backend/:

  .venv/Scripts/python scripts/scheduler_job.py inbox-poll-5min-staging \
      --url https://agentsbackend-staging-32ixmby3bq-uc.a.run.app/api/inbox/cron/poll \
      --key-env INBOX_CRON_KEY --schedule "*/5 * * * *" --tz Asia/Kolkata --deadline 300s
  ... --pause | --resume | --delete        (rollback ladder: pause first, delete when pulled)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Beside repair_invoker_flag.py in backend/scripts/; when copied elsewhere,
# "run from backend/" still finds it via the cwd.
for _dir in (Path(__file__).resolve().parent, Path.cwd() / "scripts"):
    if (_dir / "repair_invoker_flag.py").exists():
        sys.path.insert(0, str(_dir))
        break
from repair_invoker_flag import PROJECT, REGION, _session  # noqa: E402

JOBS = f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("name")
    p.add_argument("--url")
    p.add_argument("--key-env", metavar="NAME", default="INBOX_CRON_KEY")
    p.add_argument("--schedule", default="*/5 * * * *")
    p.add_argument("--tz", default="Asia/Kolkata")
    p.add_argument("--deadline", default="300s")
    p.add_argument("--description", default="Inbox Triage (a12): poll the recruiter's inbox")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--delete", action="store_true")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    S = _session()
    full = f"{JOBS}/{a.name}"
    existing = S.get(full, timeout=30)
    print(f"{a.name}: {'exists' if existing.status_code == 200 else 'absent'} "
          f"(state {existing.json().get('state') if existing.status_code == 200 else '-'})")

    if a.pause or a.resume or a.delete:
        verb = "pause" if a.pause else "resume" if a.resume else "delete"
        if existing.status_code != 200:
            sys.exit("nothing to do — job absent")
        if not a.apply:
            print(f"DRY RUN — would {verb} {a.name}. Re-run with --apply.")
            return
        r = S.delete(full, timeout=30) if a.delete else S.post(f"{full}:{verb}", timeout=30)
        print(f"{verb} -> HTTP {r.status_code}", "" if r.ok else r.text[:300])
        return

    if not a.url:
        sys.exit("--url is required to create")
    key = os.environ.get(a.key_env, "")
    if not key:
        sys.exit(f"{a.key_env} is not set in this shell — export it first")
    body = {
        "name": f"projects/{PROJECT}/locations/{REGION}/jobs/{a.name}",
        "description": a.description,
        "schedule": a.schedule,
        "timeZone": a.tz,
        "attemptDeadline": a.deadline,
        "retryConfig": {"retryCount": 0},
        "httpTarget": {
            "uri": a.url,
            "httpMethod": "POST",
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "Google-Cloud-Scheduler",
                "X-Cron-Key": key,
            },
            "body": base64.b64encode(b"{}").decode(),
        },
    }
    shown = json.loads(json.dumps(body))
    shown["httpTarget"]["headers"]["X-Cron-Key"] = f"<{a.key_env}, {len(key)} chars>"
    print(json.dumps(shown, indent=2))
    if existing.status_code == 200:
        sys.exit("job exists — delete it first (or pause/resume); this script does not PATCH")
    if not a.apply:
        print("DRY RUN — no job created. Re-run with --apply.")
        return
    r = S.post(JOBS, json=body, timeout=30)
    print(f"create -> HTTP {r.status_code}", "" if r.ok else r.text[:300])
    if r.ok:
        j = r.json()
        print("  state:", j.get("state"), " next:", j.get("scheduleTime"))


if __name__ == "__main__":
    main()
