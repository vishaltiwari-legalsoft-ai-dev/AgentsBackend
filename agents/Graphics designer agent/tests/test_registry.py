"""Brand registry seam — resolution, fallback, integrity and font mapping."""

import threading

from graphics_designer_agent import registry, templated_brands


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


def test_registry_build_is_atomic_no_partial_state_visible(monkeypatch):
    """Finding 2: `_registry()` must build into a local dict and rebind
    `_PACKS` only once fully built. Pause the build partway through (right
    after legalsoft is built, before templated/dynamic brands are added) and
    assert a concurrent reader sees either the untouched pre-build state or
    the fully-built registry — never a half-populated dict."""
    registry.refresh()
    entered = threading.Event()
    release = threading.Event()
    real_build_all = templated_brands.build_all

    def slow_build_all():
        entered.set()
        assert release.wait(timeout=5), "test setup: release was never signaled"
        return real_build_all()

    monkeypatch.setattr(templated_brands, "build_all", slow_build_all)

    result: dict = {}

    def worker():
        result["packs"] = registry._registry()

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert entered.wait(timeout=5), "builder thread never reached build_all"
        # The builder thread is now blocked mid-build, holding the lock,
        # having already built legalsoft locally (not yet visible globally).
        mid_state = dict(registry._PACKS)
    finally:
        release.set()
        t.join(timeout=5)

    assert mid_state == {}  # never partially populated while build is in flight
    ids = set(result["packs"].keys())
    assert {"legalsoft", "medvirtual", "remote_attorneys"} <= ids
    registry.refresh()  # leave a clean slate for other tests
