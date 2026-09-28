# 2026-09-28 — Brand domains admitted wholesale: full hub for all five domains

**Commit:** `ba07f84` (branch `claude/vibrant-sagan-2h5wht`, promoted to `main` the same day)
**Decision:** Owner decision 2026-09-28, with CEO sign-off.

## What changed and why

The platform was built for Legal Soft. Its internal brands are now adopting it,
so the owner admitted their domains wholesale: every mailbox at each of these
domains — current and future — gets **exactly what a legalsoft.com mailbox
gets**: sign-in, the whole Agent Hub, every agent at full capacity, and the GEO
editor role. No per-person entries, no per-user diagnosis, ever.

| Domain | Before | After |
|---|---|---|
| `legalsoft.com` | Full hub (domain rule) | Unchanged |
| `aivirtual.com` | Full hub + admin (2026-09-24 decision) | Unchanged |
| `aianswering.ai` | One named editor (`franceska@`) | **Whole domain: full hub + GEO editor** |
| `medvirtual.ai` | One named editor (`yans.suarez@`) | **Whole domain: full hub + GEO editor** |
| `usimmigration.ai` | One named editor (`miguel@`) | **Whole domain: full hub + GEO editor** |

## The code changes (`app/config.py`)

- **`ALLOWED_EMAIL_DOMAINS`** (the sign-in door) now defaults to
  `legalsoft.com,aivirtual.com,aianswering.ai,medvirtual.ai,usimmigration.ai`.
  Subdomains are still NOT implied; a lookalike or subdomain address is still
  refused.
- **`GEO_EDITOR_EMAILS`** is now domain rules only — the same five domains,
  each as an `@domain` entry. The three editors who used to be named one
  address at a time ride their domain rules like everyone else.
- **`ALLOWED_EMAILS`** still names the four original outside editors
  (lynie.t, miguel, yans.suarez, franceska) even though every one of them is
  now covered by a domain rule. That is deliberate (the lynie.t precedent):
  pruning a domain from the list must never silently off-board a named person.
- **`deploy.env.yaml.example`** carries the new five-domain value.

## What deliberately did NOT change

- **Admin** (`ADMIN_EMAILS`): still `@aivirtual.com` only. Parity for the new
  domains is with legalsoft.com *members*, who are not admins by default.
  Admin gates platform administration (analytics, user directory, admin DB
  viewer), not agent features.
- **GEO-only scope** (`GEO_ONLY_EMAILS`): still empty by default — nobody is
  scoped down by code. Scoping remains strictly opt-in, per exact address, via
  the env var on the service.
- **Creator**: unchanged; owners only.

## Deployment checklist (the code change alone is NOT live)

Both Cloud Run services set `ALLOWED_EMAIL_DOMAINS` as an env var, and an env
value **REPLACES** the code default rather than merging with it. So:

1. Update the real `deploy.env.yaml` (and the staging equivalent) to:
   `ALLOWED_EMAIL_DOMAINS: "legalsoft.com,aivirtual.com,aianswering.ai,medvirtual.ai,usimmigration.ai"`
2. Redeploy from current `main`:
   `gcloud run deploy agentsbackend --source . --region us-central1 --env-vars-file deploy.env.yaml`
3. Check the service's `GEO_ONLY_EMAILS` env var: any address named there is
   still scoped to the GEO panel regardless of this change. Remove anyone who
   should have the full hub (this is what was keeping
   `franceska@aianswering.ai` on the GEO-only console).
4. A user already signed in picks the new reach up on their next request (the
   scope and roles are re-derived per request); they only need to sign out and
   back in for the console to redraw the full hub, because the `is_geo_only` /
   `is_geo_editor` display hints are read at login.

There is no invite email anywhere in this system: a newly admitted user simply
opens the app URL and signs in with their company Google account.

## Where it is pinned

- `app/routers/tests/test_auth_router.py` — `ALLOWED_DOMAINS` pins all five
  domains; the GEO editor roster pin is domain rules only; a fresh, never-seen
  mailbox at ANY allowed domain gets the whole hub; the door test admits every
  brand and still refuses strangers, lookalike domains and subdomains.
- `tests/test_allowlist_live_routes.py` — the shipped GEO-only default still
  scopes nobody, and the named editors still sign in with every domain rule
  stripped away.

Full suite at the commit: 4238 passed, 7 skipped, 2 xfailed.
