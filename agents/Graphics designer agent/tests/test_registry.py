"""Brand registry seam — resolution, fallback, integrity and font mapping."""

from graphics_designer_agent import registry


def test_default_none_and_unknown_resolve_to_legalsoft():
    assert registry.get_pack().id == "legalsoft"
    assert registry.get_pack(None).id == "legalsoft"
    assert registry.get_pack("does-not-exist").id == registry.DEFAULT_BRAND_ID


def test_list_packs_includes_registered_brands():
    ids = {p["id"] for p in registry.list_packs()}
    assert {"legalsoft", "medvirtual", "remote_attorneys"} <= ids


def test_every_pack_passes_integrity():
    for meta in registry.list_packs():
        pack = registry.get_pack(meta["id"])
        assert pack.verify_integrity() == [], (meta["id"], pack.verify_integrity())


def test_font_file_resolves_known_and_falls_back():
    pack = registry.get_pack("medvirtual")
    assert pack.default_font in pack.font_names()
    assert pack.font_file(pack.default_font).endswith(".ttf")
    # unknown name falls back to the default font's file (never raises)
    assert pack.font_file("Totally Unknown Font") == pack.font_file(pack.default_font)


def test_pack_default_styles_use_brand_font():
    pack = registry.get_pack("medvirtual")
    styles = pack.default_stage3_styles()
    assert styles["headline"]["font"] == pack.default_font
    subs = pack.default_subheadings()
    assert subs and all(s["font"] == pack.default_font for s in subs)
