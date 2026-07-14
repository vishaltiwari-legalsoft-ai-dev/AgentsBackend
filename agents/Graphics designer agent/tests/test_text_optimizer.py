"""Text Optimizer — font resolution from the brand's weight pool (family locked)."""

from graphics_designer_agent import registry
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.stage3_text import text_optimizer as to


def test_pick_variant_prefers_upright_closest_weight():
    pack = registry.get_pack(None)
    assert to.pick_variant(pack.font_variants, 800) == "Causten ExtraBold"
    assert to.pick_variant(pack.font_variants, 600) == "Causten SemiBold"
    assert to.pick_variant(pack.font_variants, 400) == "Causten Regular"


def test_resolve_fonts_only_touches_auto_elements():
    run = create_run("u-fonts")
    pack = registry.get_pack(None)
    run["config"]["element_styles"]["headline"]["font"] = to.AUTO_FONT
    run["config"]["subheadings"][0]["font"] = to.AUTO_FONT
    chosen = to.resolve_fonts(run, pack)
    assert chosen["headline"] == "Causten ExtraBold"
    assert chosen["subheading-0"] == "Causten Regular"
    assert "cta" not in chosen  # explicit font untouched


def test_busy_judgment_steps_headline_down():
    run = create_run("u-busy")
    pack = registry.get_pack(None)
    run["config"]["element_styles"]["headline"]["font"] = to.AUTO_FONT
    chosen = to.resolve_fonts(run, pack, judgment={"density": "busy"})
    assert chosen["headline"] == "Causten Bold"


def test_resolved_fonts_view_never_mutates_the_run():
    run = create_run("u-view")
    pack = registry.get_pack(None)
    run["config"]["element_styles"]["headline"]["font"] = to.AUTO_FONT
    view, chosen = to.resolved_fonts_view(run, pack)
    assert view["config"]["element_styles"]["headline"]["font"] == "Causten ExtraBold"
    assert run["config"]["element_styles"]["headline"]["font"] == to.AUTO_FONT
    assert chosen == {"headline": "Causten ExtraBold"}


def test_view_is_identity_when_nothing_is_auto():
    run = create_run("u-noauto")
    pack = registry.get_pack(None)
    view, chosen = to.resolved_fonts_view(run, pack)
    assert view is run and chosen == {}


# ── optimize: 3-style polish fan-out with QA gate + honest fallback ──────────
from graphics_designer_agent.stage3_text import polish_prompts, qa_brain  # noqa: E402


class _FakeProvider:
    name = "fake"
    supports_negative = False

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350,
                 negative_prompt=None, label="", aspect_ratio=None, image_size=None):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("model down")
        return b"POLISHED-" + str(len(self.calls)).encode(), "image/png"


_LAYERS = [{"type": "text", "id": "headline", "text": "Hi", "x": 0.1, "y": 0.1}]


def test_optimize_returns_one_result_per_style_qa_skipped(monkeypatch):
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: None)
    prov = _FakeProvider()
    results = to.optimize(composite_png=b"BASE", layers=_LAYERS, provider=prov,
                          width=480, height=600)
    assert [r["style"] for r in results] == polish_prompts.STYLE_KEYS
    assert all(r["ai"] and r["qa"] == "skipped" and r["png"].startswith(b"POLISHED")
               for r in results)
    assert len(prov.calls) == 3


def test_optimize_retry_then_fallback_on_qa_failure(monkeypatch):
    monkeypatch.setattr(qa_brain, "check",
                        lambda *a, **k: {"passed": False, "violations": ["font changed"]})
    prov = _FakeProvider()
    results = to.optimize(composite_png=b"BASE", layers=_LAYERS, provider=prov,
                          width=480, height=600)
    assert len(prov.calls) == 6  # 3 styles x (first try + one retry)
    assert all(not r["ai"] and r["png"] == b"BASE" and r["qa"] == "failed" for r in results)
    assert all("font changed" in r["fallback_reason"] for r in results)
    # the retry prompt fed the violations back
    assert any("font changed" in p for p in prov.calls)


def test_optimize_qa_pass_ships_polished(monkeypatch):
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    results = to.optimize(composite_png=b"BASE", layers=_LAYERS,
                          provider=_FakeProvider(), width=480, height=600)
    assert all(r["ai"] and r["qa"] == "passed" for r in results)


def test_optimize_provider_error_falls_back_honestly():
    results = to.optimize(composite_png=b"BASE", layers=_LAYERS,
                          provider=_FakeProvider(fail=True), width=480, height=600)
    assert all(not r["ai"] and r["png"] == b"BASE" and r["qa"] == "not_run" for r in results)
    assert all(r["fallback_reason"] == "image model call failed" for r in results)


# ── highlight contrast guard (live-run fix 2026-07-14) ────────────────────────
from io import BytesIO

from PIL import Image


def _base_png(color):
    buf = BytesIO()
    Image.new("RGB", (100, 125), color).save(buf, format="PNG")
    return buf.getvalue()


def _headline_layer():
    return {"type": "text", "id": "headline", "text": "Hi there", "highlight": "there",
            "highlight_color": "gradient", "x": 0.5, "y": 0.5, "w": 0.9, "anchor": "mc"}


def test_highlight_guard_darkens_on_light_background():
    pack = registry.get_pack(None)
    layers = [_headline_layer()]
    info = to.ensure_highlight_contrast(layers, _base_png((235, 240, 250)), pack)
    assert info is not None and info["to"] == pack.locked_colors["headline_highlight"]["to"]
    assert layers[0]["highlight_color"] == pack.locked_colors["headline_highlight"]["to"]


def test_highlight_guard_keeps_gradient_on_dark_background():
    pack = registry.get_pack(None)
    layers = [_headline_layer()]
    assert to.ensure_highlight_contrast(layers, _base_png((10, 20, 40)), pack) is None
    assert layers[0]["highlight_color"] == "gradient"


def test_highlight_guard_skips_without_gradient_or_highlight():
    pack = registry.get_pack(None)
    solid = {**_headline_layer(), "highlight_color": "#111111"}
    no_hl = {**_headline_layer(), "highlight": ""}
    assert to.ensure_highlight_contrast([solid], _base_png((235, 240, 250)), pack) is None
    assert to.ensure_highlight_contrast([no_hl], _base_png((235, 240, 250)), pack) is None
