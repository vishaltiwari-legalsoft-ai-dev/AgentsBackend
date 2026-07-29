"""Per-generation prompt remix: honest labeling, axis rotation, brief-first
asks, and the byte-identity law (remix off = canonical bytes)."""
import json
from types import SimpleNamespace

import pytest

from graphics_designer_agent import pipeline, registry, remix
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.stage1_gradient.prompting import STAGE1_AR_ANCHOR


def test_axis_rotates_per_attempt_and_is_deterministic():
    first = remix.axis_for("run-a", 0)
    assert remix.axis_for("run-a", 0) == first
    keys = {remix.axis_for("run-a", n)[0] for n in range(len(remix.AXES))}
    assert len(keys) == len(remix.AXES)  # consecutive attempts sweep every axis


def test_ask_carries_the_brief_as_rule_number_one():
    pack = registry.get_pack(None)
    ask = remix._remix_ask("base prompt", stage=1,
                           brief="Diwali offer 20% off contract review",
                           axis_directive="vary the lighting", pack=pack)
    assert "Diwali offer 20% off contract review" in ask
    assert "#1 hard rule" in ask


def _fake_llm(replies):
    it = iter(replies)
    return SimpleNamespace(invoke=lambda _q: SimpleNamespace(content=next(it)))


def test_valid_rewrite_is_labeled_ai(monkeypatch):
    run = create_run("remix-user")
    pack = registry.get_pack(None)
    base = pack.load_prompt(pack.stage1_variant("A")["prompt_file"])
    rewritten = base.replace("immersive", "immersive, softly grained")
    monkeypatch.setattr(remix, "_get_llm",
                        lambda **_k: _fake_llm([json.dumps({"prompt": rewritten})]))
    rr = remix.remix_prompt(run, 1, base, pack=pack)
    assert rr.meta["ai"] is True and rr.meta["axis"]
    assert rr.text == rewritten


def test_invalid_rewrite_falls_back_honestly(monkeypatch):
    run = create_run("remix-user-2")
    pack = registry.get_pack(None)
    base = pack.load_prompt(pack.stage1_variant("A")["prompt_file"])
    # Off-brand hex twice -> validation rejects twice -> deterministic fallback.
    bad = '{"prompt": "Create a 16:9 aspect ratio immersive abstract background gradient in #FF0000. no text."}'
    monkeypatch.setattr(remix, "_get_llm", lambda **_k: _fake_llm([bad, bad]))
    rr = remix.remix_prompt(run, 1, base, pack=pack)
    assert rr.meta["ai"] is False and rr.meta["fallback_reason"]
    assert rr.text.startswith(base)          # base preserved
    assert STAGE1_AR_ANCHOR in rr.text       # still valid for substitution


def test_llm_unavailable_falls_back_honestly(monkeypatch):
    def boom(**_k):
        raise RuntimeError("no app layer")
    monkeypatch.setattr(remix, "_get_llm", boom)
    run = create_run("remix-user-3")
    pack = registry.get_pack(None)
    base = pack.load_prompt(pack.stage1_variant("A")["prompt_file"])
    rr = remix.remix_prompt(run, 1, base, pack=pack)
    assert rr.meta["ai"] is False and "unavailable" in rr.meta["fallback_reason"]


def test_pipeline_off_by_default_is_byte_identical():
    run = create_run("remix-user-4")
    attempt = pipeline.generate(run, 1, "A")
    assert "remix" not in attempt
    assert attempt["prompt"] == pipeline.build_prompt(run, 1, "A")["text"]


def test_pipeline_remix_enabled_varies_the_prompt(monkeypatch):
    def boom(**_k):
        raise RuntimeError("offline")  # deterministic fallback path
    monkeypatch.setattr(remix, "_get_llm", boom)
    run = create_run("remix-user-5")
    run["config"]["remix_enabled"] = True
    attempt = pipeline.generate(run, 1, "A")
    assert attempt["remix"]["ai"] is False
    assert attempt["prompt"] != pipeline.build_prompt(run, 1, "A")["text"]
    # AI/UPLOAD variants are never remixed.
    run2 = create_run("remix-user-6")
    run2["config"]["remix_enabled"] = True
    run2["config"]["custom_gradient"] = {"id": "AI", "prompt": pipeline.build_prompt(run, 1, "A")["text"]}
    a2 = pipeline.generate(run2, 1, "AI")
    assert "remix" not in a2


# ── attached-subject awareness: remix must never write the subject out ───────
def test_ask_carries_attached_subject_rule_for_stage2():
    pack = registry.get_pack(None)
    ask = remix._remix_ask("desk scene", stage=2, brief="CEO on sofa",
                           axis_directive="vary the mood", pack=pack,
                           attached_subject=True)
    assert "ATTACHED SUBJECT" in ask and "no human presence" in ask
    ask1 = remix._remix_ask("gradient", stage=1, brief="CEO on sofa",
                            axis_directive="vary the mood", pack=pack,
                            attached_subject=True)
    assert "ATTACHED SUBJECT" not in ask1  # stage-1 backgrounds unaffected


def test_subjectless_rewrite_rejected_then_retry_succeeds(monkeypatch):
    run = create_run("remix-subj")
    run["config"]["prompt_image_refs"] = ["gd/x/stage-1-promptref-t.png"]
    pack = registry.get_pack(None)
    base = pack.stage2_variant("A")["subject"]
    bad = json.dumps({"prompt": (
        "A vacant law office desk shown in stark side light — the glow rakes across "
        "the surface from a single unseen source, casting long hard shadows from a "
        "closed laptop, stacked manila folders, and a dark leather chair pushed "
        "slightly back. No human presence. The mood is 11:47 PM — quiet and still."
    )})
    good_text = base + " Lit by a warm evening lamp beside the subject."
    good = json.dumps({"prompt": good_text})
    monkeypatch.setattr(remix, "_get_llm", lambda **_k: _fake_llm([bad, good]))
    rr = remix.remix_prompt(run, 2, base, pack=pack)
    assert rr.meta["ai"] is True and rr.text == good_text


def test_subjectless_rewrite_twice_falls_back_honestly(monkeypatch):
    run = create_run("remix-subj-2")
    run["config"]["prompt_image_refs"] = ["gd/x/stage-1-promptref-t.png"]
    pack = registry.get_pack(None)
    base = pack.stage2_variant("A")["subject"]
    bad = json.dumps({"prompt": (
        "An unoccupied corner office at midnight — moonlight across a wide wooden "
        "desk, a brass lamp switched off, rows of case folders squared into neat "
        "stacks, a leather chair angled away as if just left. No people anywhere."
    )})
    monkeypatch.setattr(remix, "_get_llm", lambda **_k: _fake_llm([bad, bad]))
    rr = remix.remix_prompt(run, 2, base, pack=pack)
    assert rr.meta["ai"] is False
    assert rr.text.startswith(base)


def test_subjectless_phrases_allowed_without_attached_image(monkeypatch):
    run = create_run("remix-subj-3")  # no prompt_image_refs
    pack = registry.get_pack(None)
    base = pack.stage2_variant("A")["subject"]
    text = (
        "A moody unoccupied law office at midnight — long shadows across a broad "
        "desk, stacked case folders catching a thin band of streetlight, a heavy "
        "leather chair sitting empty at the centre of the quiet scene."
    )
    monkeypatch.setattr(remix, "_get_llm",
                        lambda **_k: _fake_llm([json.dumps({"prompt": text})]))
    rr = remix.remix_prompt(run, 2, base, pack=pack)
    assert rr.meta["ai"] is True and rr.text == text
