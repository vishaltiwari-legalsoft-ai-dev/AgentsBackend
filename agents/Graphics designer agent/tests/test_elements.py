import pytest
from graphics_designer_agent import elements


def test_sanitize_elements_none_returns_empty():
    assert elements.sanitize_elements(None) == []


def test_sanitize_clamps_coords_and_defaults():
    out = elements.sanitize_elements([
        {"kind": "emoji", "ref": "😀", "x": 5, "y": -2, "w": 9, "h": 0.1},
    ])
    assert len(out) == 1
    e = out[0]
    assert e["kind"] == "emoji"
    assert e["ref"] == "😀"
    assert 0.0 <= e["x"] <= 1.0 and e["x"] == 1.0
    assert 0.0 <= e["y"] <= 1.0 and e["y"] == 0.0
    assert 0.0 < e["w"] <= 1.0
    assert e["anchor"] == "mc"
    assert e["opacity"] == 1.0
    assert e["rotation"] == 0.0
    assert e["id"]


def test_sanitize_rejects_unknown_kind():
    with pytest.raises(ValueError):
        elements.sanitize_elements([{"kind": "hologram", "ref": "x"}])


def test_sanitize_rejects_missing_ref():
    with pytest.raises(ValueError):
        elements.sanitize_elements([{"kind": "emoji"}])


def test_sanitize_caps_count():
    many = [{"kind": "emoji", "ref": "😀"} for _ in range(50)]
    assert len(elements.sanitize_elements(many)) == elements.MAX_ELEMENTS


def test_sanitize_icon_fill_validated():
    out = elements.sanitize_elements([{"kind": "icon", "ref": "rocket", "fill": "not-a-color"}])
    assert out[0]["fill"] == "#1746A2"  # invalid → brand default


def test_emoji_catalog_nonempty_and_shaped():
    cat = elements.emoji_catalog()
    assert len(cat) > 100
    row = cat[0]
    assert set(row) >= {"char", "name", "category", "file"}


def test_emoji_png_path_resolves_for_grinning():
    p = elements._emoji_png_path("😀")
    assert p is not None and p.exists()


def test_icon_catalog_lists_svg_stems():
    cat = elements.icon_catalog()
    assert isinstance(cat, list) and all(isinstance(k, str) for k in cat)
    if cat:
        assert elements._icon_svg_path(cat[0]) is not None


def test_sticker_catalog_shape():
    cat = elements.sticker_catalog()
    assert isinstance(cat, list)
