"""Prompt-image attach: user images reach Stage-1/2 generation with guidance."""

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run, save_artifact


class _RecordingProvider:
    name = "fake"
    supports_negative = False

    def __init__(self):
        self.calls = []

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350,
                 negative_prompt=None, label="", aspect_ratio=None, image_size=None):
        self.calls.append({"prompt": prompt, "refs": len(reference_images or [])})
        buf = BytesIO()
        Image.new("RGB", (8, 10), (7, 7, 7)).save(buf, format="PNG")
        return buf.getvalue(), "image/png"


def _attach(run, color, token):
    buf = BytesIO()
    Image.new("RGBA", (6, 6), color).save(buf, format="PNG")
    ref = save_artifact(run["id"], 1, "promptref", token, buf.getvalue())
    refs = list(run["config"].get("prompt_image_refs") or [])
    refs.append(ref)
    run["config"]["prompt_image_refs"] = refs
    return ref


def test_prompt_images_reach_stage1_and_stage2():
    run = create_run("u-pi-1")
    _attach(run, (255, 0, 0, 255), "t1")
    _attach(run, (0, 255, 0, 255), "t2")
    p = _RecordingProvider()
    pipeline.generate(run, 1, variant="A", provider=p)
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, variant="A", provider=p)
    assert p.calls[0]["refs"] == 2      # Stage 1: just the 2 user images
    assert p.calls[1]["refs"] == 3      # Stage 2: approved base + 2 user images


def test_guidance_block_present_iff_refs():
    run = create_run("u-pi-2")
    assert "user-attached" not in pipeline.build_prompt(run, 1, "A")["text"].lower()
    _attach(run, (255, 0, 0, 255), "t1")
    assert "user-attached" in pipeline.build_prompt(run, 1, "A")["text"].lower()
    assert "user-attached" in pipeline.build_prompt(run, 2, "A")["text"].lower()


def test_guidance_lands_in_provider_prompt():
    run = create_run("u-pi-3")
    _attach(run, (255, 0, 0, 255), "t1")
    p = _RecordingProvider()
    pipeline.generate(run, 1, variant="A", provider=p)
    assert "user-attached" in p.calls[0]["prompt"].lower()


def test_unreadable_ref_warns_but_generates():
    run = create_run("u-pi-4")
    run["config"]["prompt_image_refs"] = ["gd/nope/stage-1-promptref-x.png"]
    p = _RecordingProvider()
    attempt = pipeline.generate(run, 1, variant="A", provider=p)
    assert p.calls[0]["refs"] == 0
    assert any("attached image" in w.lower() for w in attempt["warnings"])


# ── never-paint-logo clause: brief text must not make the model paint text ───
def test_no_painted_logo_clause_gated_on_brief_or_refs():
    run = create_run("u-npl-1")
    assert "NEVER PAINT TEXT OR LOGOS" not in pipeline.build_prompt(run, 1, "A")["text"]
    run["config"]["creative_brief"] = {"goal": "Legal Soft logo bottom-left"}
    assert "NEVER PAINT TEXT OR LOGOS" in pipeline.build_prompt(run, 1, "A")["text"]
    assert "NEVER PAINT TEXT OR LOGOS" in pipeline.build_prompt(run, 2, "A")["text"]


def test_no_painted_logo_clause_present_with_prompt_images():
    run = create_run("u-npl-2")
    run["config"]["prompt_image_refs"] = ["gd/x/stage-1-promptref-t.png"]
    assert "NEVER PAINT TEXT OR LOGOS" in pipeline.build_prompt(run, 1, "A")["text"]


def test_stage2_attached_block_overrides_subjectless_scene():
    run = create_run("u-pi-5")
    run["config"]["prompt_image_refs"] = ["gd/x/stage-1-promptref-t.png"]
    p2 = pipeline.build_prompt(run, 2, "A")["text"]
    assert "HIGHEST PRIORITY" in p2 and "IGNORE that conflict" in p2
    p1 = pipeline.build_prompt(run, 1, "A")["text"]
    assert "HIGHEST PRIORITY" not in p1  # stage-1 keeps the visual-source wording
    assert "user-attached" in p1.lower()
