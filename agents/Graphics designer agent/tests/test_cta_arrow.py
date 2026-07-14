"""CTA arrow vs missing-glyph tofu (live-run fix 2026-07-14): brand fonts
without U+2192 (e.g. MedVirtual's Be Vietnam) must get a drawn vector arrow,
never the .notdef box the polish model keeps repainting as vertical gibberish."""

from pathlib import Path

from PIL import Image

from graphics_designer_agent.stage3_text import text_overlay

_MV_FONTS = (Path(__file__).resolve().parents[1] / "graphics_designer_agent"
             / "brands" / "medvirtual" / "fonts")


def _mv_theme():
    return text_overlay._Theme(
        dark=(15, 15, 15), white=(255, 255, 255),
        grad_text=((134, 175, 254), (38, 83, 171)),
        cta_grad=((255, 138, 61), (242, 106, 26)),
        fonts_dir=_MV_FONTS, font_file=lambda name: "BeVietnam-SemiBold.ttf",
    )


def test_glyph_available_detects_missing_arrow():
    causten = text_overlay._font("Causten Bold", 40, text_overlay._default_theme())
    bevietnam = text_overlay._font("any", 40, _mv_theme())
    assert text_overlay._glyph_available(causten, "→") is True
    assert text_overlay._glyph_available(bevietnam, "→") is False


def _render_cta(theme) -> bytes:
    canvas = Image.new("RGBA", (480, 600), (200, 210, 230, 255))
    text_overlay._draw_cta(
        canvas,
        {"text": "Book a Demo", "font": "any", "size_pct": 3.4,
         "placement": "bottom", "offset": (0, 0)},
        480, 600, theme,
    )
    return canvas.tobytes()


def test_missing_arrow_glyph_takes_the_vector_path(monkeypatch):
    vector = _render_cta(_mv_theme())  # real detection: Be Vietnam has no arrow
    monkeypatch.setattr(text_overlay, "_glyph_available", lambda f, c: True)
    tofu = _render_cta(_mv_theme())    # forced legacy path draws the notdef box
    assert vector != tofu


def test_fonts_with_the_glyph_keep_the_legacy_path(monkeypatch):
    calls = {"n": 0}
    real = text_overlay._glyph_available

    def spy(font, ch):
        calls["n"] += 1
        return real(font, ch)

    monkeypatch.setattr(text_overlay, "_glyph_available", spy)
    _render_cta(text_overlay._default_theme())  # Causten — has the glyph
    assert calls["n"] == 1
