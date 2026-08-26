"""One-off repair: unpin the GA4 property that was misattributed to berry-virtual.

Why
---
``ga_discover_property`` falls back to "the only property the service account can
see" when nothing matches the brand domain. berryvirtual.com matched nothing, the
one visible property was Legal Soft's, and ``_ga_section`` pinned it with
``upsert_brand``. Berry Virtual's SEO panel has been reporting Legal Soft's
traffic (25,595 sessions, labelled "Legal Soft") ever since.

``insights._ga_section`` now refuses to claim a property another brand has
pinned — but that guard only runs on *discovery*, and discovery only runs when
the brand has no ``ga4_property``. berry-virtual has one, so the guard never
fires and the wrong value persists. It has to be cleared once, by hand.

After this runs
---------------
The next SEO sweep re-discovers for berry-virtual and hits the new guard, so the
run degrades honestly — "GA property 'Legal Soft' is already pinned to legal
soft" lands in ``degraded`` — instead of reporting another brand's numbers. If
Berry Virtual has its own GA4 property shared with the service account, discovery
finds it by name and pins that instead. Either outcome is correct; the current
one is not.

Usage
-----
    cd backend && .venv/Scripts/python -m scripts.repair_berry_virtual_ga4 --dry-run
    cd backend && .venv/Scripts/python -m scripts.repair_berry_virtual_ga4 --apply

Writes exactly one Firestore document (``seo_geo/brands``) and only the two GA
fields on the one brand. Prints the before/after so the change is auditable.
"""
from __future__ import annotations

import argparse
import sys

BRAND_ID = "berry-virtual"
GA_FIELDS = ("ga4_property", "ga4_property_name")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    group.add_argument("--apply", action="store_true", help="perform the write")
    args = parser.parse_args()

    # Importing ``app`` is what puts the agent folders on sys.path — the same
    # side effect the router tests rely on. Without it ``seo_geo_agent`` is not
    # importable from a bare script.
    import app  # noqa: F401
    from seo_geo_agent import state

    doc = state.load("brands") or {}
    brands = doc.get("brands") or []
    if not brands:
        print("seo_geo/brands is empty or unreadable — nothing to do.")
        return 1

    target = next((b for b in brands if b.get("id") == BRAND_ID), None)
    if target is None:
        print(f"No brand with id {BRAND_ID!r}. Nothing to do.")
        return 1

    print(f"before: {BRAND_ID} "
          f"ga4_property={target.get('ga4_property')!r} "
          f"ga4_property_name={target.get('ga4_property_name')!r}")

    if not any(target.get(f) for f in GA_FIELDS):
        print("Already unpinned — nothing to do.")
        return 0

    # Guard against clearing a *correct* pin: only unpin when another brand holds
    # the same property, which is the misattribution this script exists for.
    clash = next(
        (b for b in brands
         if b.get("id") != BRAND_ID and b.get("ga4_property") == target.get("ga4_property")),
        None,
    )
    if clash is None:
        print(f"{BRAND_ID}'s property is not shared with any other brand — this does "
              f"not look like the misattribution. Refusing to clear it.")
        return 1
    print(f"clash:  same property is pinned to {clash.get('id')!r} ({clash.get('name')!r})")

    updated = [
        {k: v for k, v in b.items() if not (b.get("id") == BRAND_ID and k in GA_FIELDS)}
        for b in brands
    ]

    if args.dry_run:
        after = next(b for b in updated if b.get("id") == BRAND_ID)
        print(f"after:  {BRAND_ID} keys={sorted(after)}")
        print("\nDRY RUN — nothing written. Re-run with --apply to perform it.")
        return 0

    state.save("brands", {**doc, "brands": updated})
    print(f"after:  {BRAND_ID} GA pin cleared; wrote seo_geo/brands")
    print("Next SEO sweep will re-discover and degrade honestly if it cannot "
          "identify a property for berryvirtual.com.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
