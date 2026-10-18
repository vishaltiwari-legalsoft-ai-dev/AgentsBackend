#!/usr/bin/env python
"""Provision the a12 Inbox Triage alerting: log metrics + alert policies, tiered.

Why this exists
---------------
``/api/inbox/cron/poll`` is fired every 5 minutes by Cloud Scheduler and
answers 200 when every connected user's mail was written, 207 when one or more
users failed, and 5xx/403 when the whole service or the platform is at fault.

The first version of this alerting paged on *any* non-200. On 2026-10-18 a
single user's spreadsheet timed out for seven hours (207 on every fire, three
of four users fine) and the policy re-opened ten times, emailing the owner each
time - a one-user degradation dressed as an outage. The same evening a single
GFE-side 502 on the Scheduler job paged once more while the backend had in
fact answered 207.

What this script provisions
---------------------------
Two tiers, same channels as before:

* **PAGE** - ``a12 Inbox Triage cron failing`` (existing policy, updated in
  place so incident history is kept). Whole-service faults only: cron status
  >= 500 or = 403, 2+ fires within 15 min; plus the unchanged "no fire answered
  200 for 30 min" absence condition. Severity ERROR, autoClose 1h.
* **WARN** - ``a12 Inbox Triage cron degraded (some users)`` (new). 207
  *sustained*: 6+ fires not answering 200 within 60 min. Severity WARNING,
  autoClose 24h, so a seven-hour episode is one email, not ten. Its
  documentation says plainly what it is and what to look at.

And one shared policy, folded in because the same incident exposed it:

* ``AgentOS cron - a scheduled job logged an error`` now needs 2 ERROR log
  entries within 15 min **for the same job** (new log metric
  ``agentos_scheduler_error``, grouped by ``job_id``) instead of paging on a
  single entry. Coverage for mr/geo/seo is unchanged; only the threshold moved.
  The shared ``agentos_cron_ok`` / ``agentos_cron_non2xx`` metrics are NOT
  extended to a12 - that would let a dead MR/GEO/SEO cron hide behind a12's
  12 fires an hour.

Log metrics managed (existing ones are kept byte-for-byte so history survives):
``agentos_inbox_cron_200``, ``agentos_inbox_cron_not200`` (existing),
``agentos_inbox_cron_5xx``, ``agentos_scheduler_error`` (new).

Idempotent: resources are matched by name, the desired state is diffed against
what is live, and only differences are written. Dry-run is the default.
Before any ``--apply`` the live policies are snapshotted to a JSON file under
the system temp dir; ``--restore <file>`` puts them back exactly.

Usage (from ``backend/``, needs GOOGLE_APPLICATION_CREDENTIALS)::

    .venv/Scripts/python scripts/provision_a12_monitoring.py            # diff only
    .venv/Scripts/python scripts/provision_a12_monitoring.py --apply
    .venv/Scripts/python scripts/provision_a12_monitoring.py --disable  # a12 policies off
    .venv/Scripts/python scripts/provision_a12_monitoring.py --delete   # a12 policies gone
    .venv/Scripts/python scripts/provision_a12_monitoring.py --restore <snapshot.json>

``--disable`` and ``--delete`` touch only the two a12 policies (label
``managed_by=provision_a12_monitoring``, ``agent=a12``). The shared Scheduler
policy and all log metrics are left alone - say so, do not delete silently.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT = "helpful-charmer-498509-v5"
SERVICE = "agentsbackend"
MANAGED_BY = "provision_a12_monitoring"

MONITORING = "https://monitoring.googleapis.com/v3"
LOGGING = "https://logging.googleapis.com/v2"

CHANNEL_ALERTS = "AgentOS alerts"
CHANNEL_ONCALL = "AgentOS oncall"

PAGE_POLICY = "a12 Inbox Triage cron failing"
WARN_POLICY = "a12 Inbox Triage cron degraded (some users)"
SCHED_POLICY = "AgentOS cron - a scheduled job logged an error"

REQUEST_LOG = (
    f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}" '
    f'AND logName="projects/{PROJECT}/logs/run.googleapis.com%2Frequests" '
    'AND httpRequest.requestUrl:"/api/inbox/cron/poll" '
    'AND httpRequest.userAgent:"Google-Cloud-Scheduler"'
)


def _session():
    import google.auth
    import google.auth.transport.requests as greq

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return greq.AuthorizedSession(creds)


# --------------------------------------------------------------------------- #
# Desired state
# --------------------------------------------------------------------------- #

def desired_metrics() -> dict[str, dict]:
    return {
        "agentos_inbox_cron_200": {
            "name": "agentos_inbox_cron_200",
            "description": "a12 Inbox Triage: Scheduler fires of /api/inbox/cron/poll that answered 200. "
                           "30 min absence = job paused, key rejected or service down.",
            "filter": REQUEST_LOG + " AND httpRequest.status=200",
        },
        "agentos_inbox_cron_not200": {
            "name": "agentos_inbox_cron_not200",
            "description": "a12 Inbox Triage: Scheduler fires of /api/inbox/cron/poll that did not answer 200. "
                           "207 counts - a user's mail was not written.",
            "filter": REQUEST_LOG + " AND httpRequest.status!=200",
        },
        "agentos_inbox_cron_5xx": {
            "name": "agentos_inbox_cron_5xx",
            "description": "a12 Inbox Triage: Scheduler fires of /api/inbox/cron/poll that answered >=500 or 403 - "
                           "every user failed, config broken, or the cron key/invoker rejected. 207 does NOT count.",
            "filter": REQUEST_LOG + " AND (httpRequest.status>=500 OR httpRequest.status=403)",
        },
        "agentos_scheduler_error": {
            "name": "agentos_scheduler_error",
            "description": "Cloud Scheduler ERROR log entries, per job (resource.label.job_id). "
                           "2 within 15 min for one job = that job is really failing; 1 = a transient GFE 5xx.",
            "filter": 'resource.type="cloud_scheduler_job" AND severity>=ERROR',
        },
    }


def _metric_type(name: str) -> str:
    return f"logging.googleapis.com/user/{name}"


def _threshold(display: str, metric: str, *, resource: str, threshold: float,
               align_s: int, group_by: list[str] | None = None) -> dict:
    agg = {"alignmentPeriod": f"{align_s}s", "perSeriesAligner": "ALIGN_SUM",
           "crossSeriesReducer": "REDUCE_SUM"}
    if group_by:
        agg["groupByFields"] = group_by
    return {
        "displayName": display,
        "conditionThreshold": {
            "filter": f'resource.type = "{resource}" AND metric.type = "{_metric_type(metric)}"',
            "comparison": "COMPARISON_GT",
            "thresholdValue": threshold,
            "duration": "0s",
            "trigger": {"count": 1},
            "aggregations": [agg],
        },
    }


def _absent(display: str, metric: str, *, resource: str, duration_s: int) -> dict:
    return {
        "displayName": display,
        "conditionAbsent": {
            "filter": f'resource.type = "{resource}" AND metric.type = "{_metric_type(metric)}"',
            "duration": f"{duration_s}s",
            "trigger": {"count": 1},
            "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM",
                              "crossSeriesReducer": "REDUCE_SUM"}],
        },
    }


STATUS_LEGEND = (
    "Status codes: 207 = some users failed, 502 = every user failed, 503 = could not\n"
    "read who is connected or INBOX_CRON_KEY missing, 403 = cron key rejected (with\n"
    "latency 0s and no instanceId: the platform refused it - invoker flag lost).\n"
)

PAGE_DOC = (
    "The a12 Inbox Triage poll is failing for EVERY user, or the platform is refusing\n"
    "the cron. Mail that arrives is NOT reaching the sheets.\n"
    "\n"
    "This policy pages only on whole-service faults (status >= 500 or 403, or no fire\n"
    "answering 200 for 30 min). One user's failing Sheet (207) does not page here - it\n"
    'raises the WARNING policy "' + WARN_POLICY + '" instead.\n'
    "\n" + STATUS_LEGEND +
    "\n"
    "Look, in this order:\n"
    "1. Which revision is serving, and when did it change.\n"
    '2. Logs Explorer, service agentsbackend, text "a12". A model failure reads\n'
    '   "model call failed"; next to it the httpx line carries the provider\'s status.\n'
    '3. "402 Payment Required" from openrouter.ai = out of credits. Top up.\n'
    "   On 2026-09-22 this went unseen for 44 hours.\n"
    "4. Nothing above: a Sheets or Gmail error names itself in the same log.\n"
    "\n"
    "Provisioned by backend/scripts/provision_a12_monitoring.py.\n"
)

WARN_DOC = (
    "Not an outage. One or more users' Sheet is failing; the poll answers 207 and mail\n"
    "for THOSE users is delayed, not lost - it is picked up on the next successful fire.\n"
    "Other users are unaffected.\n"
    "\n"
    "This fires only when 6 or more fires within 60 min did not answer 200, and it stays\n"
    "ONE incident for the whole episode. (On 2026-10-18 a single user's Sheet timed out\n"
    "for 7 hours and the old any-non-200 policy emailed ten times.)\n"
    "\n"
    "Look, when you get to it:\n"
    '1. Logs Explorer, service agentsbackend, text "a12 fire for user". The line\n'
    "   `a12 fire for user <hash> failed: <reason>` names the user and the reason.\n"
    "2. Usual cause: a Sheets read timeout or 5xx on that ONE spreadsheet. A user filter\n"
    "   hiding rows on the Inbox tab has correlated with this.\n"
    "3. It self-heals once the spreadsheet responds again. A code circuit breaker\n"
    "   (shipping) skips that user after 3 consecutive trips so the rest of the fire\n"
    "   is not slowed.\n"
    "\n" + STATUS_LEGEND +
    '5xx and 403 page via "' + PAGE_POLICY + '", not this policy.\n'
    "\n"
    "Provisioned by backend/scripts/provision_a12_monitoring.py.\n"
)

SCHED_DOC = (
    "A Cloud Scheduler job logged ERROR twice within 15 minutes (same job). A single\n"
    "entry does not page: a one-off GFE-side 502 or a timeout on an otherwise healthy\n"
    "fire is normal background.\n"
    "\n"
    "Scheduler logs ERROR when the target answered >= 400 or did not answer in time.\n"
    "Open the job's log entry: jsonPayload.status carries the HTTP code (or UNAVAILABLE /\n"
    "DEADLINE_EXCEEDED). Then go to the owning service's own cron alert:\n"
    "- inbox-poll-5min -> a12 policies (agentsbackend, /api/inbox/cron/poll)\n"
    "- mr / geo / seo jobs -> \"AgentOS cron failure\" (agentos_cron_non2xx / agentos_cron_ok)\n"
    "\n"
    "Threshold managed by backend/scripts/provision_a12_monitoring.py.\n"
)


def desired_policies(channels: dict[str, str]) -> dict[str, dict]:
    return {
        PAGE_POLICY: {
            "displayName": PAGE_POLICY,
            "combiner": "OR",
            "conditions": [
                _threshold("2+ fires answering >=500 or 403 within 15 min", "agentos_inbox_cron_5xx",
                           resource="cloud_run_revision", threshold=1, align_s=900),
                _absent("no fire answered 200 for 30 min (fires every 5)", "agentos_inbox_cron_200",
                        resource="cloud_run_revision", duration_s=1800),
            ],
            "documentation": {"content": PAGE_DOC, "mimeType": "text/markdown",
                              "subject": "[a12] Inbox Triage cron failing - mail is not being written"},
            "notificationChannels": [channels[CHANNEL_ALERTS]],
            "userLabels": {"managed_by": MANAGED_BY, "agent": "a12", "tier": "page"},
            "enabled": True,
            "alertStrategy": {"autoClose": "3600s"},
            "severity": "ERROR",
        },
        WARN_POLICY: {
            "displayName": WARN_POLICY,
            "combiner": "OR",
            "conditions": [
                _threshold("6+ fires not answering 200 within 60 min", "agentos_inbox_cron_not200",
                           resource="cloud_run_revision", threshold=5, align_s=3600),
            ],
            "documentation": {"content": WARN_DOC, "mimeType": "text/markdown",
                              "subject": "[a12] Inbox Triage degraded for some users - mail delayed, not lost"},
            "notificationChannels": [channels[CHANNEL_ALERTS]],
            "userLabels": {"managed_by": MANAGED_BY, "agent": "a12", "tier": "warn"},
            "enabled": True,
            "alertStrategy": {"autoClose": "86400s"},
            "severity": "WARNING",
        },
        SCHED_POLICY: {
            "displayName": SCHED_POLICY,
            "combiner": "OR",
            "conditions": [
                _threshold("2+ ERROR entries within 15 min for the same job", "agentos_scheduler_error",
                           resource="cloud_scheduler_job", threshold=1, align_s=900,
                           group_by=["resource.label.job_id"]),
            ],
            "documentation": {"content": SCHED_DOC, "mimeType": "text/markdown",
                              "subject": "[cron] a scheduled job is failing repeatedly"},
            "notificationChannels": [channels[CHANNEL_ONCALL]],
            "userLabels": {"managed_by": MANAGED_BY, "scope": "shared"},
            "enabled": True,
            "alertStrategy": {"autoClose": "3600s"},
            "severity": "ERROR",
        },
    }


# --------------------------------------------------------------------------- #
# Live state
# --------------------------------------------------------------------------- #

def _check(r, what: str) -> dict:
    if r.status_code >= 300:
        raise SystemExit(f"{what}: HTTP {r.status_code}\n{r.text[:1500]}")
    return r.json() if r.text else {}


def live_channels(s) -> dict[str, str]:
    j = _check(s.get(f"{MONITORING}/projects/{PROJECT}/notificationChannels", timeout=60), "list channels")
    return {c["displayName"]: c["name"] for c in j.get("notificationChannels", [])}


def live_metrics(s) -> dict[str, dict]:
    j = _check(s.get(f"{LOGGING}/projects/{PROJECT}/metrics", timeout=60), "list metrics")
    return {m["name"]: m for m in j.get("metrics", [])}


def live_policies(s) -> dict[str, dict]:
    out, token = {}, None
    while True:
        params = {"pageToken": token} if token else {}
        j = _check(s.get(f"{MONITORING}/projects/{PROJECT}/alertPolicies", params=params, timeout=60),
                   "list policies")
        for p in j.get("alertPolicies", []):
            out[p["displayName"]] = p
        token = j.get("nextPageToken")
        if not token:
            return out


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #

def _norm_metric(m: dict) -> dict:
    return {k: m.get(k) for k in ("name", "description", "filter")}


def _norm_policy(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k not in ("name", "creationRecord", "mutationRecord")}
    out["conditions"] = [{k: v for k, v in c.items() if k != "name"} for c in p.get("conditions", [])]
    return out


def _diff(label: str, before, after) -> bool:
    a = json.dumps(before, indent=1, sort_keys=True).splitlines() if before is not None else []
    b = json.dumps(after, indent=1, sort_keys=True).splitlines() if after is not None else []
    lines = list(difflib.unified_diff(a, b, fromfile=f"live/{label}", tofile=f"desired/{label}", lineterm=""))
    if not lines:
        print(f"  = {label}: unchanged")
        return False
    verb = "CREATE" if before is None else ("DELETE" if after is None else "UPDATE")
    print(f"  ~ {label}: {verb}")
    for ln in lines:
        print("    " + ln)
    return True


def _carry_condition_names(desired: dict, live: dict | None) -> dict:
    """Keep a live condition's resource name when its displayName matches, so the
    incident history of that condition continues instead of restarting."""
    if not live:
        return desired
    by_display = {c.get("displayName"): c.get("name") for c in live.get("conditions", [])}
    out = json.loads(json.dumps(desired))
    for c in out["conditions"]:
        if by_display.get(c["displayName"]):
            c["name"] = by_display[c["displayName"]]
    return out


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def snapshot(policies: dict[str, dict], names: list[str]) -> Path:
    d = Path(tempfile.gettempdir()) / "agentos-monitoring-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"policies-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    f.write_text(json.dumps([policies[n] for n in names if n in policies], indent=1), encoding="utf-8")
    return f


def put_metric(s, m: dict, exists: bool) -> None:
    if exists:
        _check(s.put(f"{LOGGING}/projects/{PROJECT}/metrics/{m['name']}", json=m, timeout=60),
               f"update metric {m['name']}")
    else:
        _check(s.post(f"{LOGGING}/projects/{PROJECT}/metrics", json=m, timeout=60),
               f"create metric {m['name']}")


def put_policy(s, desired: dict, live: dict | None) -> dict:
    if live:
        body = dict(desired, name=live["name"])
        return _check(s.patch(f"{MONITORING}/{live['name']}", json=body, timeout=60),
                      f"update policy {desired['displayName']}")
    return _check(s.post(f"{MONITORING}/projects/{PROJECT}/alertPolicies", json=desired, timeout=60),
                  f"create policy {desired['displayName']}")


def a12_only(policies: dict[str, dict]) -> list[dict]:
    return [p for p in policies.values()
            if (p.get("userLabels") or {}).get("managed_by") == MANAGED_BY
            and (p.get("userLabels") or {}).get("agent") == "a12"]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the diff shown by a dry run")
    g.add_argument("--disable", action="store_true", help="set enabled=false on the two a12 policies")
    g.add_argument("--delete", action="store_true", help="delete the two a12 policies (metrics + shared policy kept)")
    g.add_argument("--restore", metavar="SNAPSHOT_JSON", help="put policies back exactly as a snapshot recorded them")
    args = ap.parse_args()

    s = _session()
    channels = live_channels(s)
    for need in (CHANNEL_ALERTS, CHANNEL_ONCALL):
        if need not in channels:
            raise SystemExit(f"notification channel '{need}' not found in {PROJECT}; refusing to guess")
    metrics = live_metrics(s)
    policies = live_policies(s)

    if args.restore:
        saved = json.loads(Path(args.restore).read_text(encoding="utf-8"))
        for p in saved:
            live = policies.get(p["displayName"])
            body = {k: v for k, v in p.items() if k not in ("creationRecord", "mutationRecord")}
            if live:
                body["name"] = live["name"]
                _check(s.patch(f"{MONITORING}/{live['name']}", json=body, timeout=60), f"restore {p['displayName']}")
            else:
                body.pop("name", None)
                for c in body.get("conditions", []):
                    c.pop("name", None)
                _check(s.post(f"{MONITORING}/projects/{PROJECT}/alertPolicies", json=body, timeout=60),
                       f"restore (create) {p['displayName']}")
            print(f"  restored: {p['displayName']}")
        print("Metrics were not touched by the snapshot; extra metrics are harmless and keep history.")
        return 0

    if args.disable or args.delete:
        targets = a12_only(policies)
        if not targets:
            print("no a12 policies carry the managed_by label; nothing to do")
            return 0
        snap = snapshot(policies, [p["displayName"] for p in targets])
        print(f"snapshot: {snap}")
        for p in targets:
            if args.disable:
                _check(s.patch(f"{MONITORING}/{p['name']}", params={"updateMask": "enabled"},
                               json={"name": p["name"], "enabled": False}, timeout=60), f"disable {p['displayName']}")
                print(f"  disabled: {p['displayName']}")
            else:
                _check(s.delete(f"{MONITORING}/{p['name']}", timeout=60), f"delete {p['displayName']}")
                print(f"  deleted: {p['displayName']}")
        print(f"left alone on purpose: '{SCHED_POLICY}' (shared with mr/geo/seo) and all log metrics "
              "(agentos_inbox_cron_200/not200/5xx, agentos_scheduler_error) - history is worth more than tidiness.")
        return 0

    want_m = desired_metrics()
    want_p = desired_policies(channels)

    print("== log metrics ==")
    m_changes = {n: _diff(n, _norm_metric(metrics[n]) if n in metrics else None, _norm_metric(m))
                 for n, m in want_m.items()}
    print("== alert policies ==")
    p_changes = {n: _diff(n, _norm_policy(policies[n]) if n in policies else None, _norm_policy(p))
                 for n, p in want_p.items()}

    if not any(m_changes.values()) and not any(p_changes.values()):
        print("\nnothing to do - live state already matches")
        return 0
    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return 0

    snap = snapshot(policies, list(want_p))
    print(f"\nsnapshot of live policies (rollback via --restore): {snap}")
    for n, changed in m_changes.items():
        if changed:
            put_metric(s, want_m[n], exists=n in metrics)
            print(f"  wrote metric {n}")
    for n, changed in p_changes.items():
        if changed:
            res = put_policy(s, _carry_condition_names(want_p[n], policies.get(n)), policies.get(n))
            print(f"  wrote policy {n} -> {res.get('name')}")
    print("\napplied. Re-run without flags to confirm 'nothing to do'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
