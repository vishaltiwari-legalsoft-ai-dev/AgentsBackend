# backend/app/enrich_brands.py
"""CLI: enrich Firestore brands from a local per-brand content folder, or
backfill one brand from a hardcoded static spec (Amendment A rung 5).

Usage (dry-run is the default — review the report before writing):
    python -m app.enrich_brands --root "C:/Users/ACER/Downloads/<folder>"
    python -m app.enrich_brands --root "..." --write
    python -m app.enrich_brands --root "..." --brand "Acme Health" --write

    python -m app.enrich_brands --backfill-static medvirtual
    python -m app.enrich_brands --backfill-static medvirtual --write

NEVER run this against real Firestore/GCS except for the controller's real
dry-run / the user's G2-approved --write run — it is excluded from the
offline unit test suite's scope by design.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.brand_enrichment import backfill_static, enrich_root

# Human labels for the enrichment.source_ladder flags, in display order.
_LADDER_LABELS = (("kit_pdf", "pdf"), ("svg", "svg"),
                   ("font_files", "fonts"), ("pixel", "pixel"))


def _sources_label(report: dict) -> str:
    """"sources=..." console suffix (R4): which extraction rungs contributed,
    e.g. "svg+fonts+pixel". "-" when there is no patch (a skipped brand) or no
    source_ladder (a static backfill's own "static_spec" marker is shown
    instead)."""
    enrichment = ((report.get("patch") or {}).get("enrichment")) or {}
    if enrichment.get("source") == "static_spec":
        return "static_spec"
    ladder = enrichment.get("source_ladder")
    if not ladder:
        return "-"
    active = [label for key, label in _LADDER_LABELS if ladder.get(key)]
    return "+".join(active) if active else "-"


def _print_report(reports: list[dict]) -> None:
    for r in reports:
        status = ("SKIP: " + r["skipped_reason"]) if r["skipped_reason"] else \
                 ("WROTE" if r["wrote"] else "DRY-RUN")
        print(f"{r['brand_name']:<30} {status:<24} confidence={r['confidence']}"
              f" sources={_sources_label(r)}"
              f"{'  FONT-FALLBACK' if r['font_fallback'] else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root", type=Path, help="brand content folder root")
    mode.add_argument("--backfill-static", metavar="PACK_ID",
                       help="backfill one brand from a templated_brands static "
                            "spec id (e.g. 'medvirtual') instead of scanning --root")
    ap.add_argument("--write", action="store_true", help="perform Firestore/GCS writes")
    ap.add_argument("--brand", help="only this brand folder name (--root mode only)")
    ap.add_argument("--report", type=Path, default=Path("enrichment_report.json"))
    args = ap.parse_args()

    now_iso = datetime.now(timezone.utc).isoformat()

    if args.backfill_static:
        try:
            reports = [backfill_static(args.backfill_static, dry_run=not args.write,
                                        now_iso=now_iso)]
        except ValueError as exc:
            raise SystemExit(f"[enrich_brands] {exc}")
    else:
        reports = enrich_root(args.root, dry_run=not args.write, now_iso=now_iso)
        if args.brand:
            reports = [r for r in reports if r["brand_name"].lower() == args.brand.lower()]

    args.report.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    _print_report(reports)
    print(f"\nreport -> {args.report}")


if __name__ == "__main__":
    main()
