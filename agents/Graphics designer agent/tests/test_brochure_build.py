# tests/test_brochure_build.py
import io
from PIL import Image
from graphics_designer_agent.creative import document_builder as db
from graphics_designer_agent.creative import planner
from graphics_designer_agent import registry


def _pack():
    return registry.get_pack(None)


def _plan():
    return planner.plan("brochure", "virtual legal staff", brand_name="Legal Soft", use_llm=False)


def test_build_brochure_returns_one_pdf_artifact():
    arts = db.build_brochure_pdf(_plan(), _pack())
    assert len(arts) == 1
    name, data, mime = arts[0]
    assert name.endswith(".pdf") and mime == "application/pdf" and data[:4] == b"%PDF"


def test_designed_brochure_returns_page_rasters():
    (name, data, mime), rasters = db._designed_brochure_pdf(_plan(), _pack())
    assert mime == "application/pdf"
    assert len(rasters) >= 2                       # cover + pages
    assert Image.open(io.BytesIO(rasters[0])).size == db.brochure_render._BROCHURE_PAGE


def test_text_brochure_fallback_is_gone():
    assert not hasattr(db, "_text_brochure_pdf")


def test_build_brochure_does_not_fall_back_to_text_on_error(monkeypatch):
    # Force the designed path to blow up; the builder must raise, never ship text.
    monkeypatch.setattr(db, "_designed_brochure_pdf",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        db.build_brochure_pdf(_plan(), _pack())
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


class _FakeProvider:
    def __init__(self, fail_on: set[int] | None = None):
        self.calls = []
        self._fail_on = fail_on or set()

    def generate(self, prompt, **kw):
        self.calls.append(prompt)
        if len(self.calls) in self._fail_on:
            raise RuntimeError("boom")
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 80), (120, 90, 200)).save(buf, format="PNG")
        return buf.getvalue(), "image/png"


def _pages():
    return [
        {"template": "cover", "heading": "T", "bg": "cover scene"},
        {"template": "card_grid", "heading": "G", "cards": [], "bg": "grid scene"},
    ]


def test_render_backgrounds_one_png_per_page(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(db.providers, "get_provider", lambda **kw: fake)
    out = db._render_brochure_backgrounds(_pages(), _pack(), "house style note")
    assert len(out) == 2 and all(isinstance(b, bytes) for b in out)
    assert len(fake.calls) == 2
    assert "cover scene" in fake.calls[0] or "cover scene" in fake.calls[1]
    assert all("no text" in c for c in fake.calls)          # guard present
    assert all("house style note" in c for c in fake.calls)  # brand study grounding


def test_render_backgrounds_no_provider_all_none(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("no key")
    monkeypatch.setattr(db.providers, "get_provider", _boom)
    out = db._render_brochure_backgrounds(_pages(), _pack(), "")
    assert out == [None, None]


def test_render_backgrounds_single_failure_isolated(monkeypatch):
    fake = _FakeProvider(fail_on={1})
    monkeypatch.setattr(db.providers, "get_provider", lambda **kw: fake)
    out = db._render_brochure_backgrounds(_pages(), _pack(), "")
    assert len(out) == 2
    assert (out[0] is None) != (out[1] is None)  # exactly one fell back


def test_designed_brochure_pdf_still_builds_offline(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("no key")
    monkeypatch.setattr(db.providers, "get_provider", _boom)
    monkeypatch.setattr(db, "_study_brand_brochures", lambda brand_id: "")
    plan = {"cover": {"title": "Perks", "subtitle": "S"},
            "pages": [{"template": "steps", "heading": "How", "steps": []}]}
    (fname, pdf, mime), rasters = db._designed_brochure_pdf(plan, _pack())
    assert mime == "application/pdf" and pdf[:4] == b"%PDF"
    assert len(rasters) == 2
