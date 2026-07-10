"""Design-library variants — append-only Stage-1/Stage-2 extras distilled from
each brand's real creatives (prompt-library/). Canonical variants and frozen
prompt hashes must stay byte-identical; library extras ride along inline."""

import pytest

from graphics_designer_agent import library_variants, registry
from graphics_designer_agent.pipeline import build_prompt
from graphics_designer_agent.runs import create_run

BRANDS_WITH_LIBRARY = ["legalsoft", "medvirtual", "remote_attorneys"]


@pytest.mark.parametrize("bid", BRANDS_WITH_LIBRARY)
def test_library_variants_appended_after_canonical(bid):
    pack = registry.get_pack(bid)
    lib = library_variants.LIBRARY[bid]
    ids1 = [v["id"] for v in pack.stage1_variants]
    ids2 = [v["id"] for v in pack.stage2_variants]
    for item in lib["stage1"]:
        assert item["id"] in ids1
    for item in lib["stage2"]:
        assert item["id"] in ids2
    # appended, never shadowing: ids stay unique
    assert len(ids1) == len(set(ids1))
    assert len(ids2) == len(set(ids2))


@pytest.mark.parametrize("bid", BRANDS_WITH_LIBRARY)
def test_library_stage1_prompts_carry_ar_anchor_and_serve_inline(bid):
    pack = registry.get_pack(bid)
    for item in library_variants.LIBRARY[bid]["stage1"]:
        v = pack.stage1_variant(item["id"])
        text = pack.load_prompt(v["prompt_file"])
        assert "16:9 aspect ratio" in text  # stage1 AR substitution contract
        assert text == item["prompt"]


@pytest.mark.parametrize("bid", BRANDS_WITH_LIBRARY)
def test_library_stage2_categories_are_known(bid):
    pack = registry.get_pack(bid)
    for item in library_variants.LIBRARY[bid]["stage2"]:
        assert item["category"] in pack.stage2_categories


def test_integrity_unchanged_with_library_extras():
    for bid in BRANDS_WITH_LIBRARY:
        assert registry.get_pack(bid).verify_integrity() == []


def test_build_prompt_works_for_library_variants():
    run = create_run("user-lib", "legalsoft")
    s1 = build_prompt(run, 1, "M")
    assert "royal blue" in s1["text"]
    s2 = build_prompt(run, 2, "T")
    assert "headset" in s2["text"]


def test_pack_without_library_entry_passes_through():
    pack = registry.get_pack("legalsoft")
    import dataclasses
    ghost = dataclasses.replace(pack, id="ghost-brand")
    assert library_variants.extend_pack(ghost) is ghost
