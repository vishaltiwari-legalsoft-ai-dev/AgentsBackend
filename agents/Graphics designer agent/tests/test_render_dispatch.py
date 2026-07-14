from io import BytesIO

from PIL import Image

from graphics_designer_agent.stage3_text import render, text_overlay


def _png(w: int, h: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), (136, 170, 204)).save(out, format="PNG")
    return out.getvalue()


def _layers():
    return [{"type": "text", "id": "headline", "text": "Hi", "highlight": "",
             "font": "Causten Bold", "size_pct": 8.0, "color": "dark",
             "highlight_color": "gradient", "placement": "left", "offset": (0, 0),
             "z": 10, "pinned": False, "x": 0.06, "y": 0.5, "w": 0.42, "anchor": "ml"}]


def test_default_engine_is_byte_identical_to_pillow(monkeypatch):
    monkeypatch.delenv("GD_RENDERER", raising=False)
    png = _png(64, 80)
    assert render.render_layers(png, _layers(), 64, 80) == \
        text_overlay.render_layers(png, _layers(), 64, 80)


def test_konva_engine_posts_contract_and_returns_service_bytes(monkeypatch):
    monkeypatch.setenv("GD_RENDERER", "konva")
    monkeypatch.setenv("GD_RENDERER_URL", "http://renderer.test")
    sent = {}

    def fake(req, url):
        sent["url"], sent["req"] = url, req
        return b"SERVICE-PNG"

    monkeypatch.setattr(render, "_service_render", fake)
    out = render.render_layers(_png(64, 80), _layers(), 64, 80)
    assert out == b"SERVICE-PNG"
    assert sent["url"] == "http://renderer.test"
    assert sent["req"]["base_w"] == 64 and sent["req"]["layers"][0]["font_file"]


def test_konva_failure_falls_back_to_pillow(monkeypatch):
    monkeypatch.setenv("GD_RENDERER", "konva")
    monkeypatch.setenv("GD_RENDERER_URL", "http://renderer.test")

    def boom(req, url):
        raise OSError("renderer down")

    monkeypatch.setattr(render, "_service_render", boom)
    png = _png(64, 80)
    assert render.render_layers(png, _layers(), 64, 80) == \
        text_overlay.render_layers(png, _layers(), 64, 80)


# ── landscape text normalization (live-run fix 2026-07-14) ────────────────────
def test_landscape_text_sizes_scale_down():
    layers = [{"type": "text", "id": "h", "size_pct": 8.0},
              {"type": "cta", "id": "c", "size_pct": 3.4},
              {"type": "shape", "id": "s", "w": 0.3}]
    out = render._normalize_text_sizes(layers, 1920, 1080)
    assert out[0]["size_pct"] == 5.4   # 8.0 * 1.2 * 1080/1920
    assert out[1]["size_pct"] == 2.295
    assert "size_pct" not in out[2]    # shapes stay fraction-of-canvas
    assert layers[0]["size_pct"] == 8.0  # input never mutated


def test_portrait_and_square_are_identity():
    layers = [{"type": "text", "id": "h", "size_pct": 8.0}]
    assert render._normalize_text_sizes(layers, 1080, 1350) is layers
    assert render._normalize_text_sizes(layers, 1080, 1080) is layers


def test_landscape_render_uses_smaller_text(monkeypatch):
    monkeypatch.delenv("GD_RENDERER", raising=False)
    png = _png(192, 108)  # 16:9
    naive = text_overlay.render_layers(png, _layers(), 192, 108)
    normalized = render.render_layers(png, _layers(), 192, 108)
    assert normalized != naive  # dispatch applied the aspect normalization
