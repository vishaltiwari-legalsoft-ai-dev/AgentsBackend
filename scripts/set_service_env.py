#!/usr/bin/env python
"""Set / remove env vars (or move traffic) on a Cloud Run service by a FULL v1
ReplaceService round-trip — the CLAUDE.md law; precedent: repair_invoker_flag.py.

Values never come from the command line: every --from-env NAME is read from
this process's environment, so nothing lands in shell history or output. Names
only are printed. Dry-run by default; --apply writes.

Lives in backend/scripts/ (recurring value: every env
change on either service). Run from backend/:

  .venv/Scripts/python scripts/set_service_env.py agentsbackend-staging \
      --from-env INBOX_TOKEN_KEY INBOX_CRON_KEY INBOX_GOOGLE_CLIENT_ID INBOX_GOOGLE_CLIENT_SECRET
  .venv/Scripts/python scripts/set_service_env.py agentsbackend \
      --from-env INBOX_CRON_KEY INBOX_GOOGLE_CLIENT_ID INBOX_GOOGLE_CLIENT_SECRET \
      --secret-ref INBOX_TOKEN_KEY=inbox-token-key:1
  .venv/Scripts/python scripts/set_service_env.py agentsbackend --remove INBOX_TOKEN_KEY ...   # rollback
  .venv/Scripts/python scripts/set_service_env.py agentsbackend --traffic-to agentsbackend-00143-62k

A changed template creates a NEW revision (unlike the annotation-only repair);
--traffic-to changes only spec.traffic and creates none.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

# Beside repair_invoker_flag.py in backend/scripts/; when copied elsewhere,
# "run from backend/" still finds it via the cwd.
for _dir in (Path(__file__).resolve().parent, Path.cwd() / "scripts"):
    if (_dir / "repair_invoker_flag.py").exists():
        sys.path.insert(0, str(_dir))
        break
from repair_invoker_flag import ANN, BASE, PROJECT, REGION, _session  # noqa: E402

V2 = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("service", choices=["agentsbackend", "agentsbackend-staging"])
    p.add_argument("--from-env", nargs="*", default=[], metavar="NAME",
                   help="env var NAMES whose values are read from this shell")
    p.add_argument("--secret-ref", nargs="*", default=[], metavar="NAME=SECRET:VERSION",
                   help="Secret Manager reference instead of a literal value")
    p.add_argument("--remove", nargs="*", default=[], metavar="NAME")
    p.add_argument("--traffic-to", metavar="REVISION",
                   help="send 100%% of traffic to this existing revision (rollback)")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    S = _session()
    r = S.get(f"{BASE}/{a.service}", timeout=60)
    r.raise_for_status()
    svc = r.json()
    current = svc.get("status", {}).get("latestReadyRevisionName")
    if svc["metadata"].get("annotations", {}).get(ANN) != "true":
        sys.exit(f"refusing: {ANN} is not 'true' on {a.service} — run repair_invoker_flag.py first")

    new = copy.deepcopy(svc)
    new.pop("status", None)  # output-only

    if a.traffic_to:
        new["spec"]["traffic"] = [{"revisionName": a.traffic_to, "percent": 100}]
        print(f"{a.service}: traffic {current} -> {a.traffic_to} (100%)")
    else:
        tmpl = new["spec"]["template"]
        # Let Cloud Run name the next revision; a reused name is rejected.
        tmpl.setdefault("metadata", {}).pop("name", None)
        container = tmpl["spec"]["containers"][0]
        env = {e["name"]: e for e in container.get("env", [])}
        before = set(env)
        for name in a.from_env:
            val = os.environ.get(name, "")
            if not val:
                sys.exit(f"{name} is not set in this shell — export it first")
            env[name] = {"name": name, "value": val}
        for spec in a.secret_ref:
            name, ref = spec.split("=", 1)
            secret, _, version = ref.partition(":")
            env[name] = {"name": name, "valueFrom": {"secretKeyRef": {
                "name": secret, "key": version or "latest"}}}
        for name in a.remove:
            env.pop(name, None)
        container["env"] = [env[k] for k in sorted(env)]
        touched = set(a.from_env) | {s.split("=", 1)[0] for s in a.secret_ref}
        print(f"{a.service}: serving {current}; env {len(before)} -> {len(env)} vars")
        print("  added  :", sorted(set(env) - before))
        print("  updated:", sorted(touched & before))
        print("  removed:", sorted(before - set(env)))

    print(f"  {ANN} preserved: {new['metadata']['annotations'].get(ANN)!r}")
    if not a.apply:
        print("DRY RUN — no PUT sent. Re-run with --apply.")
        return
    resp = S.put(f"{BASE}/{a.service}", json=new, timeout=120)
    print(f"ReplaceService -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        sys.exit(1)
    v2 = S.get(f"{V2}/{a.service}", timeout=60).json()
    print("verify: invokerIamDisabled =", v2.get("invokerIamDisabled"))
    print("verify: latestCreatedRevision =", (v2.get("latestCreatedRevision") or "").split("/")[-1])
    print("verify: latestReadyRevision   =", (v2.get("latestReadyRevision") or "").split("/")[-1],
          "(re-check in ~60s until it equals latestCreated)")
    print(f"rollback target: {current}")


if __name__ == "__main__":
    main()
