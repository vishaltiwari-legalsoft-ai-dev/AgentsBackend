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
