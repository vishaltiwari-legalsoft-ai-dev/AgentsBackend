"""``firestore.indexes.json`` must stay in step with the queries in the code.

Every composite index in this project was created by hand from the link
Firestore prints on first failure, and nothing recorded which ones the code
depends on. This file is that record; this test is what stops it going stale.

Static only — it parses JSON and greps source. Nothing here touches Firestore.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
INDEX_FILE = BACKEND / "firestore.indexes.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def test_the_index_file_exists_and_parses(spec):
    assert isinstance(spec.get("indexes"), list) and spec["indexes"]


def test_every_index_names_a_collection_and_at_least_two_fields(spec):
    for index in spec["indexes"]:
        assert index.get("collectionGroup"), index
        fields = index.get("fields") or []
        assert len(fields) >= 2, f"a single-field index needs no entry: {index}"
        for field in fields:
            assert field.get("fieldPath")
            assert field.get("order") in ("ASCENDING", "DESCENDING"), field


def test_every_index_says_which_query_needs_it(spec):
    """An index nobody can tie back to a query is one nobody dares delete."""
    for index in spec["indexes"]:
        assert index.get("_why"), f"undocumented index: {index}"


def _has(spec: dict, collection: str, *paths: str) -> bool:
    for index in spec["indexes"]:
        if index["collectionGroup"] != collection:
            continue
        if tuple(f["fieldPath"] for f in index["fields"]) == paths:
            return True
    return False


def test_the_composites_the_code_requires_today_are_recorded(spec):
    assert _has(spec, "creative_events", "user_id", "created_at"), (
        "firestore_repo.list_usage_events needs this one — it is an equality "
        "plus an inequality, which Firestore cannot serve without a composite")
    assert _has(spec, "creatives", "brand_id", "creative_metadata.author"), (
        "firestore_repo.delete_ingested_creatives needs this one")
    assert _has(spec, "runs", "user_id", "created_at"), (
        "firestore_repo.list_runs_for_user needs this one — an equality filter "
        "plus an ordering on a different field. Without it the console's Runs "
        "panel falls back to reading unordered and sorting in process, which is "
        "correct but capped at 3000 rows per caller")


def test_the_mr_read_path_queries_stayed_index_free():
    """The MR cost fix used equality-only filters on purpose, so it needed no
    index build to ship. If an ``order_by`` or a ``limit`` appears in those
    query builders, the equality-only assumption is gone and the mr_runs
    composites in the index file must be BUILT before that ships.
    """
    runs_src = (BACKEND / "agents" / "Marketing Research agent"
                / "marketing_research_agent" / "runs.py").read_text(encoding="utf-8")
    snaps_src = (BACKEND / "agents" / "Marketing Research agent"
                 / "marketing_research_agent" / "snapshots.py").read_text(encoding="utf-8")
    builder = runs_src.split("def _cloud_query")[1].split(chr(10) + "def ")[0]
    assert ".order_by(" not in builder and ".limit(" not in builder, (
        "mr_runs query now orders/limits server-side - build the "
        "(user_id, generated_at DESC) composite from firestore.indexes.json first")
    assert 'FieldFilter("user_id", "=="' in builder, (
        "the mr_runs query stopped filtering by workspace server-side")
    cloud_list = snaps_src.split("def _cloud_list")[1].split(chr(10) + "def ")[0]
    assert ".order_by(" not in cloud_list and ".limit(" not in cloud_list, (
        "mr_snapshots query now orders/limits server-side - it needs a composite")
