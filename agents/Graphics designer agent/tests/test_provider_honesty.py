"""The mock provider must never stand in for a real generation.

MockImageProvider returns brand-gradient bytes through the *same*
``(bytes, mime)`` contract as OpenRouterProvider, so anything that auto-selects
it saves a placeholder as the run's creative, uploads it to GCS and logs it as a
completed run. These tests pin the two halves of the fix:

1. a transient failure reading the admin key override reports "could not
   determine", never "no API key";
2. auto mode raises ``ImageProviderUnavailable`` (``ai=False`` + a populated
   ``fallback_reason``) rather than returning the mock. The mock stays reachable
   only through an explicit ``GD_IMAGE_PROVIDER=mock``.
"""

import pytest

from graphics_designer_agent import providers


# --- 1. key status is tri-state -------------------------------------------

def test_env_key_reports_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-value")
    assert providers._openrouter_key_status() == (providers.KEY_PRESENT, None)


def test_missing_key_reports_absent_with_no_detail(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_runtime_key", lambda: False)
    assert providers._openrouter_key_status() == (providers.KEY_ABSENT, None)


def test_config_read_failure_is_unknown_not_absent(monkeypatch):
    """A Firestore hiccup reading the admin override is not evidence the key is
    missing — the old ``except Exception: return False`` claimed it was."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _boom():
        raise TimeoutError("deadline exceeded")

    monkeypatch.setattr(providers, "_runtime_key", _boom)
    status, detail = providers._openrouter_key_status()
    assert status == providers.KEY_UNKNOWN
    assert detail and "TimeoutError" in detail
    assert "deadline exceeded" in detail


def test_unknown_status_is_not_reported_as_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")  # conftest pins "mock" suite-wide
    monkeypatch.setattr(
        providers, "_openrouter_key_status",
        lambda: (providers.KEY_UNKNOWN, "config read failed"))
    # The advisory boolean still says "don't try the LLM"...
    assert providers._openrouter_key_configured() is False
    # ...but provider selection must not silently downgrade on it.
    with pytest.raises(providers.ImageProviderUnavailable):
        providers.get_provider(agent_id="a1")


# --- 2. auto mode raises, explicit mock still works ------------------------

@pytest.mark.parametrize("factory", ["get_provider", "get_polish_provider"])
def test_auto_mode_raises_when_key_absent(monkeypatch, factory):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    monkeypatch.setattr(
        providers, "_openrouter_key_status", lambda: (providers.KEY_ABSENT, None))
    with pytest.raises(providers.ImageProviderUnavailable) as exc:
        getattr(providers, factory)(agent_id="a1")
    assert exc.value.ai is False
    assert exc.value.fallback_reason
    assert "GD_IMAGE_PROVIDER=mock" in exc.value.fallback_reason


@pytest.mark.parametrize("factory", ["get_provider", "get_polish_provider"])
def test_auto_mode_raises_and_names_the_cause_when_undeterminable(monkeypatch, factory):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    monkeypatch.setattr(
        providers, "_openrouter_key_status",
        lambda: (providers.KEY_UNKNOWN, "admin-override lookup failed (TimeoutError: x)"))
    with pytest.raises(providers.ImageProviderUnavailable) as exc:
        getattr(providers, factory)(agent_id="a1")
    assert exc.value.ai is False
    assert "TimeoutError" in exc.value.fallback_reason


@pytest.mark.parametrize("factory", ["get_provider", "get_polish_provider"])
def test_explicit_mock_still_selectable(monkeypatch, factory):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "mock")
    monkeypatch.setattr(
        providers, "_openrouter_key_status", lambda: (providers.KEY_ABSENT, None))
    assert getattr(providers, factory)(agent_id="a1").name == "mock"


@pytest.mark.parametrize("factory", ["get_provider", "get_polish_provider"])
def test_key_present_yields_real_provider(monkeypatch, factory):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    monkeypatch.setattr(
        providers, "_openrouter_key_status", lambda: (providers.KEY_PRESENT, None))
    assert getattr(providers, factory)(agent_id="a1").name == "openrouter"


def test_no_generation_path_can_return_mock_bytes_in_auto_mode(monkeypatch):
    """End-to-end shape of the bug: whatever get_provider hands back in auto mode
    must not be the gradient renderer."""
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    for status in (providers.KEY_ABSENT, providers.KEY_UNKNOWN):
        monkeypatch.setattr(
            providers, "_openrouter_key_status", lambda s=status: (s, "why"))
        with pytest.raises(providers.ImageProviderUnavailable):
            providers.get_provider()


# --- 3. the real model call: right surface, exact shape, honest failure ------
#
# 2026-10-08 switch to OpenAI GPT Image 2.5 (Stage 2+). Its OpenRouter surface is
# the Images API (chat-completions 404s it), it has no 4:5 enum and no
# resolution tier - so the exact WxH is computed, and a failure must surface as
# a typed ImageProviderError, never a substitute model or image.

import base64  # noqa: E402
from io import BytesIO  # noqa: E402

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.services import openrouter  # noqa: E402
from graphics_designer_agent import pipeline  # noqa: E402
from graphics_designer_agent.runs import create_run  # noqa: E402


def _png(w: int, h: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (23, 70, 162)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = httpx.Headers(headers or {})
        self.text = str(self._body)
        self.reason_phrase = "x"

    def json(self):
        return self._body


@pytest.fixture()
def wire(monkeypatch):
    """Capture every POST the image layer makes; answer from a queue."""
    calls: list[dict] = []
    queue: list = []

    def fake_post(url, *, json, headers, timeout):
        calls.append({"url": url, "body": json})
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(openrouter.runtime_config, "require", lambda field: "unit-test-key")
    monkeypatch.setattr(openrouter.httpx, "post", fake_post)
    monkeypatch.setattr(openrouter, "_sleep", lambda s: calls.append({"slept": s}))
    monkeypatch.setattr(openrouter.random, "uniform", lambda a, b: 0.0)
    return calls, queue


def _images_ok(w, h):
    return _Resp(200, {"data": [{"b64_json": base64.b64encode(_png(w, h)).decode(),
                                 "media_type": "image/png"}], "usage": {"cost": 0.09}})


def _chat_ok(w, h):
    url = "data:image/png;base64," + base64.b64encode(_png(w, h)).decode()
    return _Resp(200, {"id": "gen-1",
                       "choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]})


@pytest.mark.parametrize("ar", ["1:1", "4:5", "9:16", "16:9", "3:4"])
@pytest.mark.parametrize("tier", ["1K", "2K", "4K"])
def test_gpt_image_size_is_the_exact_ratio_within_provider_limits(ar, tier):
    w, h = (int(v) for v in openrouter.gpt_image_size(ar, tier).split("x"))
    a, b = (int(v) for v in ar.split(":"))
    assert w * b == h * a                      # exact ratio, no crop needed
    assert w % 16 == 0 and h % 16 == 0         # OpenAI: sides divisible by 16
    assert w * h <= 3840 * 2160 and max(w, h) <= 3840
    assert min(w, h) >= 1080                   # never below the social canvas


def test_gpt_image_size_maps_tiers_to_growing_resolution():
    assert openrouter.gpt_image_size("4:5", "1K") == "1088x1360"
    assert openrouter.gpt_image_size("4:5", "2K") == "1856x2320"
    assert openrouter.gpt_image_size("9:16", "4K") == "2160x3840"


def test_gpt_image_model_goes_to_the_images_api_with_exact_size(wire):
    calls, queue = wire
    queue.append(_images_ok(1856, 2320))
    png, mime = openrouter.generate_image(
        "blend", reference_images=[(_png(8, 10), "image/png")],
        model="openai/gpt-image-2.5-sunburst", aspect_ratio="4:5", image_size="2K")
    assert mime == "image/png" and Image.open(BytesIO(png)).size == (1856, 2320)
    (call,) = calls
    assert call["url"].endswith("/images")
    body = call["body"]
    assert body["model"] == "openai/gpt-image-2.5-sunburst"
    assert body["size"] == "1856x2320" and "aspect_ratio" not in body
    assert body["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "messages" not in body and "modalities" not in body


def test_chat_image_models_keep_the_chat_surface_and_image_config(wire):
    calls, queue = wire
    queue.append(_chat_ok(928, 1152))
    openrouter.generate_image("gradient", model="google/gemini-3-pro-image",
                              aspect_ratio="4:5", image_size="1K")
    body = calls[0]["body"]
    assert calls[0]["url"].endswith("/chat/completions")
    assert body["image_config"] == {"aspect_ratio": "4:5", "image_size": "1K"}
    assert body["modalities"] == ["image", "text"]


@pytest.mark.parametrize("model", ["openai/gpt-5.4-image-2", "openai/gpt-5-image",
                                   "openai/gpt-5-image-mini", "google/gemini-3-pro-image"])
def test_text_and_image_chat_models_get_both_modalities(model):
    """gpt-5.4-image-2 used to fall through to ["image"]: the "." in its id
    defeated the old "gpt-5-image" substring."""
    assert openrouter._image_modalities(model) == ["image", "text"]


def test_rate_limit_is_retried_once_honouring_retry_after(wire):
    calls, queue = wire
    queue += [_Resp(429, {"error": {"message": "slow down"}}, {"Retry-After": "7"}),
              _images_ok(1088, 1088)]
    openrouter.generate_image("x", model="openai/gpt-image-2.5-sunburst",
                              aspect_ratio="1:1", image_size="1K")
    assert [c for c in calls if "slept" in c] == [{"slept": 7.0}]
    assert sum(1 for c in calls if "url" in c) == 2


def test_persistent_upstream_failure_raises_typed_error_after_bounded_retry(wire):
    calls, queue = wire
    queue += [_Resp(503, {"error": {"message": "Catalog snapshot read timed out"}})] * 2
    with pytest.raises(openrouter.ImageProviderError) as exc:
        openrouter.generate_image("x", model="openai/gpt-image-2.5-sunburst",
                                  aspect_ratio="1:1", image_size="1K")
    assert exc.value.status == 503 and not exc.value.rate_limited
    assert "openai/gpt-image-2.5-sunburst" in str(exc.value)
    assert "Catalog snapshot read timed out" in str(exc.value)
    assert sum(1 for c in calls if "url" in c) == openrouter.IMAGE_MAX_ATTEMPTS == 2


def test_a_bad_request_is_not_retried(wire):
    calls, queue = wire
    queue.append(_Resp(400, {"error": {"message": "aspect_ratio: not supported"}}))
    with pytest.raises(openrouter.ImageProviderError) as exc:
        openrouter.generate_image("x", model="openai/gpt-image-2.5-sunburst",
                                  aspect_ratio="4:5", image_size="1K")
    assert exc.value.status == 400
    assert len(calls) == 1


def test_timeouts_are_retried_then_fail_loudly(wire):
    _calls, queue = wire
    queue += [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]
    with pytest.raises(openrouter.ImageProviderError, match="timed out"):
        openrouter.generate_image("x", model="google/gemini-3-pro-image",
                                  aspect_ratio="1:1", image_size="1K")


def test_an_off_ratio_image_is_refused_not_shipped(wire):
    """Stage 3 resizes the base to the canvas: a 1:1 image for a 4:5 run would
    ship stretched, so it must fail instead."""
    _calls, queue = wire
    queue.append(_chat_ok(1024, 1024))
    with pytest.raises(openrouter.ImageProviderError, match="not the requested 4:5"):
        openrouter.generate_image("x", model="openai/gpt-5.4-image-2",
                                  aspect_ratio="4:5", image_size="1K")


def test_attempts_record_the_model_that_made_them(monkeypatch):
    """Each stage's provider is resolved per stage and the model id lands on the
    attempt, so a run says which model produced every image."""
    seen: list = []

    class _Rec:
        name = "openrouter"
        supports_negative = False

        def __init__(self, model):
            self.model = model

        def generate(self, prompt, *, width=1080, height=1350, **_kw):
            return _png(width, height), "image/png"

    def fake_get_provider(name=None, *, agent_id=None, stage=None):
        model = "gemini/gradient" if stage == 1 else "openai/gpt-image-2.5-sunburst"
        seen.append(stage)
        return _Rec(model)

    monkeypatch.setattr(pipeline, "get_provider", fake_get_provider)
    run = create_run("model-stamp")
    a1 = pipeline.generate(run, 1)
    pipeline.approve(run, 1)
    a2 = pipeline.generate(run, 2)
    assert a1["model"] == "gemini/gradient"
    assert a2["model"] == "openai/gpt-image-2.5-sunburst"
    assert seen == [1, 2]
