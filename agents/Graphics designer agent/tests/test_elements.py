import pytest
from graphics_designer_agent.stage3_text import elements


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


from io import BytesIO
from PIL import Image


def _canvas(w=400, h=400):
    return Image.new("RGBA", (w, h), (255, 255, 255, 255))


def test_draw_emoji_changes_pixels():
    cv = _canvas()
    before = cv.tobytes()
    layer = elements.sanitize_elements([
        {"kind": "emoji", "ref": "😀", "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3},
    ])[0]
    elements.draw_element(cv, layer, 400, 400)
    assert cv.tobytes() != before  # something was drawn


def test_draw_image_missing_loader_is_noop():
    cv = _canvas()
    before = cv.tobytes()
    layer = elements.sanitize_elements([
        {"kind": "image", "ref": "runs/x/y.png", "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3},
    ])[0]
    elements.draw_element(cv, layer, 400, 400, image_loader=None)
    assert cv.tobytes() == before  # no loader → safe no-op


def test_draw_element_never_raises_on_bad_ref():
    cv = _canvas()
    layer = {"kind": "icon", "ref": "no-such-icon", "x": 0.5, "y": 0.5,
             "w": 0.2, "h": 0.2, "anchor": "mc", "z": 5, "rotation": 0.0,
             "opacity": 1.0, "fill": "#1746A2"}
    elements.draw_element(cv, layer, 400, 400)  # must not raise


from graphics_designer_agent.stage3_text import layout, text_overlay


def _base_png(w=400, h=400) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_element_position_resolution_independent():
    """An emoji at x=0.75 lands at the same fractional column at 400px and 800px —
    this is what guarantees the browser (fractional) canvas matches the final PNG."""
    run = {"brand_id": None, "config": {
        "tokens": {}, "aspect_ratio": "1:1",
        "elements": [{"kind": "emoji", "ref": "😀", "x": 0.75, "y": 0.5,
                      "w": 0.2, "h": 0.2, "anchor": "mc"}],
    }}
    layers = layout.resolve_layers(run)
    base_small = _base_png(400, 400)
    base_big = _base_png(800, 800)
    small = Image.open(BytesIO(text_overlay.render_layers(base_small, layers, 400, 400)))
    big = Image.open(BytesIO(text_overlay.render_layers(base_big, layers, 800, 800)))

    def emoji_centroid_x(img):
        gray = img.convert("L")
        px = gray.load()
        xs = [x for x in range(img.width) for y in range(img.height) if px[x, y] < 250]
        return (sum(xs) / len(xs)) / img.width if xs else 0.5

    assert abs(emoji_centroid_x(small) - emoji_centroid_x(big)) < 0.03
