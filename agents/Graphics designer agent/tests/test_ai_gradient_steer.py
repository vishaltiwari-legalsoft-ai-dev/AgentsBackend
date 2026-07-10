"""AI gradient must honour the user's steer as a HARD requirement (user feedback
2026-07-11: keyword steers were demoted to "mood" while a hardcoded composition
archetype list took over — outputs read as prerecorded).

Contract pinned here, mirroring the Stage-2 element path:
* a non-empty steer is the #1 requirement — it drives composition AND texture;
* no archetype is forced when a steer is present (only steer-less regenerates
  rotate through the archetype list, for variety);
* a validation-rejected LLM answer is retried once with the errors echoed back,
  instead of silently serving a curated pick;
* when the LLM path truly fails, the curated fallback says so honestly
  (``ai: False`` + ``fallback_reason``) so the UI can stop labelling it "AI".
"""

import json
from types import SimpleNamespace

from graphics_designer_agent import registry, suggestions

PACK = registry.get_pack(None)


# ── prompt builder: the steer is the #1 requirement ───────────────────────────
def test_prompt_puts_steer_first_as_hard_requirement():
    p = suggestions._gradient_llm_prompt("aurora mesh, grainy, flowing waves", PACK)
    assert 'THE USER ASKED FOR: "aurora mesh, grainy, flowing waves"' in p
    assert "MUST visibly deliver" in p


def test_prompt_with_steer_does_not_force_an_archetype():
    # "glow" used to pin the composition to a radial archetype via keyword map.
    p = suggestions._gradient_llm_prompt("soft glowing aurora", PACK)
    assert "Do NOT use any other composition" not in p
    assert "RADIAL bloom glowing from the centre" not in p
    # Texture asks (grain/mesh) must be allowed: only "no text" stays mandatory.
    assert 'end with "no text."' in p
    assert 'end with "no noise, no text."' not in p


def test_prompt_without_steer_keeps_rotating_archetype():
    p0 = suggestions._gradient_llm_prompt("", PACK, exclude=set())
    assert "COMPOSITION — the gradient MUST be" in p0
    # Rotation advances with the exclude count so "Regenerate" varies.
    p1 = suggestions._gradient_llm_prompt("", PACK, exclude={"x"})
    assert p0 != p1


def test_prompt_stays_brand_locked_and_demands_honesty():
    p = suggestions._gradient_llm_prompt("deep emerald green wave", PACK)
    assert "ONLY these brand hex colours" in p
    # Off-palette asks must be acknowledged in desc, never faked via the title.
    assert "HONESTY" in p


# ── LLM plumbing: retry on rejection, honest fallback ─────────────────────────
class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.replies.pop(0))


_VALID_PROMPT = (
    "Create a 16:9 aspect ratio immersive abstract background gradient flowing as "
    "layered aurora-like waves from luminous #FFFFFF through #A2C0E6 into deep "
    "#1746A2, with soft feathered crests and a fine subtle grain, no text."
)


def _reply(prompt_text, css="linear-gradient(90deg, #FFFFFF 0%, #1746A2 100%)"):
    return json.dumps(
        {"title": "Aurora Wave", "desc": "Layered waves.", "prompt": prompt_text, "css_gradient": css}
    )


def test_validation_failure_retries_with_errors_then_succeeds(monkeypatch):
    off_brand = _VALID_PROMPT.replace("#A2C0E6", "#FF0000")
    fake = _FakeLLM([_reply(off_brand), _reply(_VALID_PROMPT)])
    monkeypatch.setattr(suggestions, "_get_llm", lambda **kw: fake)

    out = suggestions.suggest_gradient(answers={}, steer="aurora waves")
    assert out["ai"] is True and out["source"] == "agent+llm"
    assert out["gradient"]["prompt"] == _VALID_PROMPT
    assert len(fake.prompts) == 2
    # The retry must tell the model WHY it was rejected.
    assert "REJECTED" in fake.prompts[1] and "#FF0000" in fake.prompts[1]


def test_double_rejection_falls_back_with_reason(monkeypatch):
    off_brand = _reply(_VALID_PROMPT.replace("#A2C0E6", "#FF0000"))
    fake = _FakeLLM([off_brand, off_brand])
    monkeypatch.setattr(suggestions, "_get_llm", lambda **kw: fake)

    out = suggestions.suggest_gradient(answers={}, steer="aurora waves")
    assert out["ai"] is False
    assert out.get("fallback_reason")


def test_llm_error_falls_back_with_reason(monkeypatch):
    def boom(**kw):
        raise RuntimeError("no key configured")

    monkeypatch.setattr(suggestions, "_get_llm", boom)
    out = suggestions.suggest_gradient(answers={}, steer="aurora waves")
    assert out["ai"] is False
    assert out.get("fallback_reason")


def test_off_brand_css_is_rejected_then_retried(monkeypatch):
    bad_css = _reply(_VALID_PROMPT, css="linear-gradient(90deg, #FF0000 0%, #1746A2 100%)")
    fake = _FakeLLM([bad_css, _reply(_VALID_PROMPT)])
    monkeypatch.setattr(suggestions, "_get_llm", lambda **kw: fake)

    out = suggestions.suggest_gradient(answers={}, steer="aurora waves")
    assert out["ai"] is True
    assert out["gradient"]["css_gradient"] == "linear-gradient(90deg, #FFFFFF 0%, #1746A2 100%)"
    assert len(fake.prompts) == 2
