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
