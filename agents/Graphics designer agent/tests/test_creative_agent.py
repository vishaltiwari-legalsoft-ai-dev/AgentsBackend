"""Creative Agent rail — offline tests.

Covers routing/taxonomy, the planner's reviewable-plan shapes, the run lifecycle
+ decision log, the autonomous acknowledgement gate + one-click override, the
always-on Pillow builders (carousel/blog), the optional engines (PDF/PPTX, gated
on the deps being installed), and document (PDF) ingestion into the reference
library (gated on PyMuPDF).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from graphics_designer_agent import reference_library as rl
from graphics_designer_agent import registry
from graphics_designer_agent.creative import document_builder as db
from graphics_designer_agent.creative import pipeline
from graphics_designer_agent.creative import planner
from graphics_designer_agent.creative import runs as cruns
from graphics_designer_agent.creative import types as ctypes


@pytest.fixture(autouse=True)
def _sandbox_runs(monkeypatch, tmp_path):
    """Isolate creative-run storage per test."""
    monkeypatch.setattr(cruns, "CREATIVE_RUNS_ROOT", tmp_path / "creative_runs")


PACK = registry.get_pack("legalsoft")


# --------------------------------------------------------------------------- #
# Taxonomy + routing
# --------------------------------------------------------------------------- #

def test_routing_splits_social_from_creative_agent():
    # Standard social posts stay on the studio editor.
    assert rl.routes_to_creative_agent("social_story") is False
    assert ctypes.is_creative_agent_type("social_story") is False
    # The four new jobs route to the Creative Agent.
    for t in ("carousel", "brochure", "presentation", "blog"):
        assert rl.routes_to_creative_agent(t) is True
        assert ctypes.is_creative_agent_type(t) is True


def test_output_formats():
    assert rl.output_format_for("brochure") == "pdf"
    assert rl.output_format_for("presentation") == "pptx"
    assert rl.output_format_for("carousel") == "image_set"
    assert rl.output_format_for("blog") == "image_set"


def test_agent_types_listing_and_steps():
    keys = {t["key"] for t in ctypes.creative_agent_types()}
    assert keys == {"carousel", "brochure", "presentation", "blog"}
    assert ctypes.STEP_KEYS == ["intent", "strategy", "layout", "output"]
    assert "autonomous" in ctypes.AUTONOMOUS_WARNING.lower()


def test_require_known_rejects_social_and_unknown():
    with pytest.raises(ValueError):
        ctypes.require_known("social_story")
    with pytest.raises(ValueError):
        ctypes.require_known("nope")
    ctypes.require_known("carousel")  # no raise


# --------------------------------------------------------------------------- #
# Planner — reviewable plan shapes (deterministic, offline)
# --------------------------------------------------------------------------- #

def test_carousel_plan_shape():
    p = planner.plan("carousel", "remote hiring drive", brand_name="Legal Soft",
                     count=5, use_llm=False)
    assert p["creative_type"] == "carousel" and p["count"] == 5
    frames = p["frames"]
    assert len(frames) == 5
    assert frames[0]["role"] == "hook" and frames[-1]["role"] == "cta"
    assert all({"index", "role", "headline", "body", "visual"} <= set(f) for f in frames)
    assert p["decisions"]  # decision rationale recorded


def test_presentation_and_brochure_and_blog_shapes():
    deck = planner.plan("presentation", "q3 results", brand_name="Legal Soft",
                        count=4, use_llm=False)
    assert len(deck["slides"]) == 4 and deck["slides"][0]["title"]
    assert all("bullets" in s for s in deck["slides"])

    br = planner.plan("brochure", "practice areas", brand_name="Legal Soft",
                      count=3, use_llm=False)
    assert br["cover"]["title"] and len(br["sections"]) == 3
    assert all({"heading", "body", "bullets"} <= set(s) for s in br["sections"])

    blog = planner.plan("blog", "legal tech trends", brand_name="Legal Soft",
                        count=3, use_llm=False)
    assert blog["cover"]["title"]
    assert len(blog["inline"]) == 2  # cover + 2 inline = 3


def test_plan_count_is_clamped_to_type_bounds():
    p = planner.plan("carousel", "x", brand_name="B", count=99, use_llm=False)
    assert p["count"] == ctypes.PLAN_HINTS["carousel"]["max"]


# --------------------------------------------------------------------------- #
# Run lifecycle + decision log
# --------------------------------------------------------------------------- #

def test_manual_lifecycle_and_decision_log():
    run = cruns.create_run("u1", "carousel", brand_id="legalsoft", brief="hiring")
    assert run["state"] == "INTENT" and run["output_format"] == "image_set"

    pipeline.gather_intent(run, brief="hiring drive")
    assert run["state"] == "STRATEGY"

    pipeline.make_plan(run, count=3, use_llm=False)
    assert run["state"] == "LAYOUT" and run["plan"] and run["plan_approved"] is False

    # Cannot generate before approval.
    with pytest.raises(ValueError):
        pipeline.produce(run)

    pipeline.approve_plan(run)
    assert run["state"] == "OUTPUT" and run["plan_approved"] is True

    pipeline.produce(run)
    assert run["state"] == "DONE"
    names = {a["name"] for a in run["artifacts"]}
    assert any(n.endswith(".png") for n in names)
    assert any(n.endswith(".zip") for n in names)  # multi-file → zipped

    # Decision log captured every step, with sources.
    steps = {d["step"] for d in run["decision_log"]}
    assert {"intent", "strategy", "layout", "output"} <= steps
    assert any(d["source"] == "user" for d in run["decision_log"])

    # Persisted + reloadable.
    assert cruns.get_run(run["id"])["state"] == "DONE"


def test_grounding_retrieved_before_planning(monkeypatch, tmp_path):
    # Empty reference dir → graceful "no precedent" grounding, still plans.
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))
    run = cruns.create_run("u1", "blog", brand_id="legalsoft", brief="tips")
    pipeline.make_plan(run, use_llm=False)
    assert "grounding" in run and run["plan"]
    assert any(d["step"] == "strategy" for d in run["decision_log"])


# --------------------------------------------------------------------------- #
# Autonomous mode — acknowledgement gate + override
# --------------------------------------------------------------------------- #

def test_autonomous_requires_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))
    run = cruns.create_run("u1", "carousel", brand_id="legalsoft",
                           brief="promo", autonomous=True)
    assert run["autonomous"] is True and run["autonomous_ack"] is False
    with pytest.raises(pipeline.AutonomyError):
        pipeline.run_autonomous(run, use_llm=False)

    pipeline.acknowledge(run)
    assert run["autonomous_ack"] is True
    pipeline.run_autonomous(run, use_llm=False)
    assert run["state"] == "DONE" and run["plan_approved"] is True
    assert run["artifacts"]
    # Agent logged its own decisions across all steps.
    assert any(d["source"] == "agent" for d in run["decision_log"])


def test_take_manual_control_is_one_click(monkeypatch, tmp_path):
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))
    run = cruns.create_run("u1", "brochure", brand_id="legalsoft", autonomous=True)
    pipeline.take_manual_control(run)
    assert run["autonomous"] is False
    assert any(d["decision"] == "Manual control taken" for d in run["decision_log"])


# --------------------------------------------------------------------------- #
# Document builders
# --------------------------------------------------------------------------- #

def test_carousel_and_blog_builders_produce_pngs():
    cp = planner.plan("carousel", "hiring", brand_name=PACK.name, use_llm=False)
    arts = db.build("carousel", cp, PACK)
    assert len(arts) == cp["count"]
    assert all(mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"
               for _n, data, mime in arts)

    bp = planner.plan("blog", "trends", brand_name=PACK.name, use_llm=False)
    barts = db.build("blog", bp, PACK)
    assert barts[0][0] == "cover.png"

    bundle = db.zip_artifacts(arts, "legalsoft-carousel")
    assert bundle[0].endswith(".zip") and bundle[2] == "application/zip"


def test_engine_status_reports_pillow_always():
    status = db.engine_status()
    assert status["carousel"] is True and status["blog"] is True
    assert set(status) == {"carousel", "blog", "brochure", "presentation"}


def test_brochure_pdf_builder():
    pytest.importorskip("reportlab")
    p = planner.plan("brochure", "practice areas", brand_name=PACK.name, use_llm=False)
    arts = db.build("brochure", p, PACK)
    assert len(arts) == 1
    name, data, mime = arts[0]
    assert name.endswith(".pdf") and mime == "application/pdf"
    assert data[:5] == b"%PDF-"


def test_presentation_pptx_builder():
    pytest.importorskip("pptx")
    p = planner.plan("presentation", "q3", brand_name=PACK.name, use_llm=False)
    arts = db.build("presentation", p, PACK)
    assert len(arts) == 1
    name, data, mime = arts[0]
    assert name.endswith(".pptx")
    assert data[:2] == b"PK"  # zip-based OOXML container


# --------------------------------------------------------------------------- #
# Document ingestion into the reference library
# --------------------------------------------------------------------------- #

def test_pdf_reference_ingestion():
    fitz = pytest.importorskip("fitz")  # PyMuPDF
    base = Path(tempfile.mkdtemp(prefix="cre_refs_"))
    brochure_dir = base / "Legal Soft" / "brochure"
    brochure_dir.mkdir(parents=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # portrait
    page.draw_rect(fitz.Rect(0, 0, 595, 300), color=(0.09, 0.27, 0.64),
                   fill=(0.09, 0.27, 0.64))
    doc.save(str(brochure_dir / "services_overview.pdf"))
    doc.close()

    records = rl.ingest_all(base)
    assert len(records) == 1
    rec = records[0]
    assert rec.creative_type == "brochure"
    assert rec.extra.get("is_document") is True and rec.extra.get("kind") == "pdf"
    assert rec.extra.get("pages") == 1
    assert rec.orientation == "portrait" and rec.palette  # rendered → real palette
    assert "document" in rec.tags
