"""OpenRouter integration.

OpenRouter is used for BOTH:
- the agent's reasoning LLM (via LangChain's ChatOpenAI, OpenAI-compatible), and
- image generation, through one of two OpenRouter surfaces chosen by model:
  the chat-completions endpoint with image output modality (Gemini image,
  GPT-5.x Image, Flux...) or the dedicated Images API (``POST /images``) for
  the OpenAI ``gpt-image-*`` family, which chat-completions rejects with a 404.
"""

from __future__ import annotations

import base64
import logging
import math
import random
import re
import time
from io import BytesIO

import httpx
from langchain_openai import ChatOpenAI

from app.config import settings
from app.services import runtime_config

logger = logging.getLogger("agentos.openrouter")


def _default_headers() -> dict[str, str]:
    # OpenRouter uses these for attribution/ranking; harmless if unset.
    return {"HTTP-Referer": settings.app_public_url, "X-Title": settings.app_title}


def get_llm(
    temperature: float = 0.4,
    *,
    fast: bool = False,
    model: str | None = None,
    agent_id: str | None = None,
    timeout: float = 120,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """LangChain chat model backed by OpenRouter.

    ``model`` pins an explicit model id (e.g. the GD planner); otherwise
    ``fast=True`` selects the cheap parsing model and the default is the
    high-end reasoning model. ``agent_id`` resolves the creator's per-agent
    model override first (agent → global → env); without it the global
    default applies.

    ``timeout`` and ``max_tokens`` exist for callers that make MANY small
    calls inside one budgeted request (Inbox Triage classifies one email per
    call, ~70 tokens back, inside a cron fire): the 120s default is right for
    a long reasoning turn and wrong for a classifier, where two hung calls
    would eat the whole fire. Both keep the previous behaviour when omitted.
    """
    resolved = model or runtime_config.get_for_agent(
        agent_id, "openrouter_fast_model" if fast else "openrouter_model"
    )
    return ChatOpenAI(
        model=resolved,
        api_key=runtime_config.require("openrouter_api_key"),
        base_url=settings.openrouter_base_url,
        default_headers=_default_headers(),
        temperature=temperature,
        timeout=timeout,
        max_retries=2,
        max_tokens=max_tokens,
    )


# Models known to accept a "4K" image_size. Everything else tops out at 2K —
# requesting 4K from e.g. an OpenAI image model returns a 400. Keep this
# permissive (substring match) so new Gemini 3 Pro Image revisions still qualify.
_FOUR_K_CAPABLE = ("gemini-3-pro-image", "gemini-3-pro")


def _clamp_image_size(model: str, size: str | None) -> str | None:
    """Downgrade an unsupported 4K request to 2K for non-4K-capable models, so
    an admin-selected image model never fails OpenRouter's image_size validation.
    """
    if size and size.upper() == "4K":
        m = model.lower()
        if not any(tok in m for tok in _FOUR_K_CAPABLE):
            return "2K"
    return size


def _image_modalities(model: str) -> list[str]:
    """Correct `modalities` for an OpenRouter image model.

    Text+image models (Gemini image, the GPT-5.x Image chat models such as
    ``openai/gpt-5-image`` and ``openai/gpt-5.4-image-2``) use ["image","text"];
    image-only models (Flux, Recraft, etc.) require ["image"] or OpenRouter
    rejects them. The plain ``gpt-image-*`` family never reaches this function —
    it is served by the Images API (see :func:`_uses_images_api`).
    """
    m = model.lower()
    if "gemini" in m or "gpt-4o" in m or _GPT_CHAT_IMAGE.search(m):
        return ["image", "text"]
    return ["image"]


# GPT-5.x "Image" chat models: openai/gpt-5-image, gpt-5-image-mini, gpt-5.4-image-2.
# The old substring "gpt-5-image" missed gpt-5.4-image-2 (the "." breaks it), so
# that model was sent ["image"] only although its output is image+text.
_GPT_CHAT_IMAGE = re.compile(r"gpt-[0-9][\w.]*-image")


# --------------------------------------------------------------------------- #
# OpenAI GPT Image family via OpenRouter's Images API.
#
# Verified live 2026-10-08: chat-completions answers 404 "is an image generation
# model and cannot be used with the chat/completions endpoint" for
# openai/gpt-image-*; the Images API accepts them. Their ``aspect_ratio`` enum
# has no 4:5 (1:1, 3:2, 2:3, 4:3, 3:4, 16:9, 9:16, 21:9) and there is no
# ``resolution`` tier — but an explicit ``size`` "WxH" passes straight through
# to OpenAI, which renders ANY exact size whose sides are multiples of 16 within
# a pixel budget (3840x2160 accepted; 3072x3840 rejected "exceeds the current
# pixel budget"). So every AR — 4:5 included — is rendered natively at the exact
# ratio; nothing is cropped or stretched.
# --------------------------------------------------------------------------- #

_IMAGES_API_MODEL = re.compile(r"^openai/gpt-image-")
_GPT_PIXEL_BUDGET = 3840 * 2160
_GPT_MAX_SIDE = 3840
_GPT_SIDE_MULTIPLE = 16
# Minimum pixel AREA per tier, mirroring what Gemini's 1K/2K/4K tiers deliver
# (~1 MP / ~4 MP / as large as allowed), and never a short side below the
# 1080-px social canvas so a later stage never has to upscale.
_GPT_TIER_AREA = {"1K": 1024 * 1024, "2K": 2048 * 2048, "4K": _GPT_PIXEL_BUDGET}
_MIN_SHORT_SIDE = 1080
# Rendering quality for the GPT Image family. "high" measured live: ~$0.05 at
# ~1.5 MP and ~$0.07-0.12 at ~4 MP per image, 25-50 s.
GPT_IMAGE_QUALITY = "high"


def _uses_images_api(model: str) -> bool:
    return bool(_IMAGES_API_MODEL.match((model or "").lower()))


def gpt_image_size(aspect_ratio: str | None, tier: str | None) -> str:
    """Exact-ratio "WxH" for a GPT Image request.

    Sides are multiples of 16 at precisely ``aspect_ratio``; the smallest such
    size meeting the tier's area (and the 1080-px short side) is chosen, capped
    at the provider pixel budget / 3840-px side. Raises ``ValueError`` for a
    malformed ratio — the caller must never guess a shape.
    """
    try:
        a, b = (int(x) for x in (aspect_ratio or "1:1").split(":"))
        if a <= 0 or b <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"unsupported aspect ratio {aspect_ratio!r}") from exc
    g = math.gcd(a, b)
    a, b = a // g, b // g
    step = 1
    while (a * step) % _GPT_SIDE_MULTIPLE or (b * step) % _GPT_SIDE_MULTIPLE:
        step += 1
    want = _GPT_TIER_AREA.get((tier or "1K").upper(), _GPT_TIER_AREA["1K"])
    best: tuple[int, int] | None = None
    k = step
    while True:
        w, h = a * k, b * k
        if w * h > _GPT_PIXEL_BUDGET or max(w, h) > _GPT_MAX_SIDE:
            break
        best = (w, h)
        if w * h >= want and min(w, h) >= _MIN_SHORT_SIDE:
            break
        k += step
    if best is None:
        raise ValueError(f"aspect ratio {aspect_ratio!r} cannot be rendered exactly")
    return f"{best[0]}x{best[1]}"


# --------------------------------------------------------------------------- #
# Honest, typed failures + bounded retry.
# --------------------------------------------------------------------------- #

class ImageProviderError(RuntimeError):
    """An image-model call failed. Subclasses ``RuntimeError`` so every existing
    ``except RuntimeError``/``Exception`` caller keeps working, while the HTTP
    layer can map it to an honest status instead of a flat 500.

    ``status`` is the upstream HTTP status (None for timeouts / network errors /
    malformed responses); ``rate_limited`` marks a 429 so the caller can answer
    429 with ``retry_after`` seconds. The message names the model and the real
    cause and never contains a secret.
    """

    def __init__(self, message: str, *, model: str, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.status = status
        self.retry_after = retry_after

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


# 408/425/429 + gateway/provider overload. OpenRouter also answers 503 "Catalog
# snapshot read timed out" transiently (seen live 2026-10-08).
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})
# One retry (two attempts in all): enough to ride out a burst-time 429 / a blip,
# bounded so Stage-3's fan-out (3 parallel polish calls x QA retry) stays well
# inside Cloud Run's 900 s request timeout.
IMAGE_MAX_ATTEMPTS = 2
_RETRY_AFTER_CAP_S = 30.0
# Per-attempt HTTP timeout. Measured worst case 2026-10-08: GPT-5.4 Image 2
# 115 s, GPT Image 2.5 Sunburst 50 s, Gemini 3 Pro Image 28 s.
IMAGE_TIMEOUT = httpx.Timeout(150.0, connect=10.0)


def _sleep(seconds: float) -> None:  # seam for tests
    time.sleep(seconds)


def _retry_after_s(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _upstream_reason(response: httpx.Response) -> str:
    """The upstream ``error.message`` only (OpenRouter bodies also carry account
    ids), trimmed — this text reaches the user through the GD router."""
    try:
        message = (response.json().get("error") or {}).get("message")
    except Exception:  # noqa: BLE001 - non-JSON error body
        message = None
    return str(message or response.text or response.reason_phrase)[:300]


def _post_image(url: str, body: dict, headers: dict, model: str) -> dict:
    """POST with a bounded, jittered retry. Returns the parsed JSON body or
    raises :class:`ImageProviderError` naming the model and the real cause."""
    last: ImageProviderError | None = None
    for attempt in range(1, IMAGE_MAX_ATTEMPTS + 1):
        response: httpx.Response | None = None
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=IMAGE_TIMEOUT)
        except httpx.TimeoutException as exc:
            last = ImageProviderError(
                f"image model {model} timed out ({type(exc).__name__})", model=model)
        except httpx.HTTPError as exc:
            last = ImageProviderError(
                f"image model {model} request failed: {exc}", model=model)
        else:
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ImageProviderError(
                        f"image model {model} returned a non-JSON body", model=model,
                        status=response.status_code) from exc
            last = ImageProviderError(
                f"image model {model} failed ({response.status_code}): "
                f"{_upstream_reason(response)}",
                model=model, status=response.status_code,
                retry_after=_retry_after_s(response))
            if response.status_code not in _RETRYABLE_STATUS:
                raise last
        if attempt < IMAGE_MAX_ATTEMPTS:
            wait = last.retry_after if last.retry_after is not None else 2.0 * attempt
            wait = min(_RETRY_AFTER_CAP_S, wait) + random.uniform(0, 1.5)
            logger.warning("image call retry %d/%d for %s in %.1fs: %s",
                           attempt, IMAGE_MAX_ATTEMPTS - 1, model, wait, last)
            _sleep(wait)
    assert last is not None
    raise last


def _check_shape(png: bytes, model: str, aspect_ratio: str | None) -> None:
    """Refuse an image whose ratio is off the requested one. Stage 3 resizes the
    base to the canvas, so a wrong-ratio image would ship stretched; Gemini's
    native sizes are within ~0.8% (928x1152 for 4:5), so 2% is the bound."""
    if not aspect_ratio:
        return
    try:
        from PIL import Image

        w, h = Image.open(BytesIO(png)).size
        a, b = (int(x) for x in aspect_ratio.split(":"))
    except Exception:  # noqa: BLE001 - unreadable image / odd ratio string
        raise ImageProviderError(
            f"image model {model} returned an unreadable image", model=model) from None
    if abs((w / h) / (a / b) - 1) > 0.02:
        raise ImageProviderError(
            f"image model {model} returned {w}x{h}, not the requested {aspect_ratio}",
            model=model)


def _parse_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a `data:<mime>;base64,<payload>` URL into (bytes, mime_type)."""
    if not data_url.startswith("data:"):
        raise RuntimeError("OpenRouter returned a non-data-URL image reference")
    header, _, payload = data_url.partition(",")
    mime = header[len("data:") :].split(";")[0] or "image/png"
    return base64.b64decode(payload), mime


def vision_extract_text(
    image_bytes: bytes, mime_type: str, *, agent_id: str | None = None
) -> str:
    """OCR + read an image via an OpenRouter vision model.

    Returns extracted text plus a short description of key visual content, used
    to enrich the user's creative brief (Workflow C).
    """
    api_key = runtime_config.require("openrouter_api_key")
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}
    data_url = f"data:{mime_type or 'image/png'};base64,{base64.b64encode(image_bytes).decode()}"
    body = {
        "model": runtime_config.get_for_agent(agent_id, "openrouter_vision_model"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract ALL readable text from this image verbatim "
                            "(OCR). Then add one short line describing the key "
                            "visual content. Keep it concise."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }

    try:
        response = httpx.post(url, json=body, headers=headers, timeout=120)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenRouter vision request failed: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter OCR failed ({response.status_code}): {response.text}"
        )

    payload = response.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {payload}") from exc
    # Some providers return content as a list of parts; normalize to text.
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content).strip()


def analyze_images(
    prompt: str,
    images: list[tuple[bytes, str]],
    model: str | None = None,
    *,
    agent_id: str | None = None,
) -> str:
    """Analyze one or more images with a vision-capable chat model.

    Used to reverse-engineer a brand's visual design system from its website
    imagery. Defaults to the reasoning model (multimodal); callers may pass
    `settings.openrouter_vision_model` as a cheaper fallback.
    """
    api_key = runtime_config.require("openrouter_api_key")
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_bytes, mime in images:
        data_url = (
            f"data:{mime or 'image/png'};base64,{base64.b64encode(img_bytes).decode()}"
        )
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    body = {
        "model": model or runtime_config.get_for_agent(agent_id, "openrouter_model"),
        "messages": [{"role": "user", "content": content}],
    }
    response = httpx.post(url, json=body, headers=headers, timeout=180)
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter image analysis failed ({response.status_code}): {response.text}"
        )
    payload = response.json()
    try:
        result = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {payload}") from exc
    if isinstance(result, list):
        result = " ".join(
            part.get("text", "") for part in result if isinstance(part, dict)
        )
    return str(result).strip()


def generate_image(
    prompt: str,
    reference_images: list[tuple[bytes, str]] | None = None,
    model: str | None = None,
    *,
    aspect_ratio: str | None = None,
    image_size: str | None = None,
    agent_id: str | None = None,
) -> tuple[bytes, str]:
    """Render a single image through an OpenRouter image-output model.

    If `reference_images` (list of (bytes, mime)) is provided, they are sent
    alongside the prompt so the model can composite them (e.g. the exact brand
    logo) rather than inventing them. Returns the image bytes and MIME type.

    `aspect_ratio` (e.g. "4:5", "16:9") and `image_size` ("1K"/"2K"/"4K") set the
    output shape and resolution: through `image_config` on the chat surface, and
    as an exact "WxH" (:func:`gpt_image_size`) on the Images API. Without them a
    model only infers the shape from the prompt and emits a low-resolution image.

    Failures raise :class:`ImageProviderError` (after one bounded retry for
    429/5xx/timeouts) naming the model and the real cause. There is no fallback
    to another model: the model asked for is the model that answers, or the
    call fails loudly.
    """
    api_key = runtime_config.require("openrouter_api_key")
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}
    image_model = model or runtime_config.get_for_agent(agent_id, "openrouter_image_model")
    started = time.monotonic()

    if _uses_images_api(image_model):
        try:
            size = gpt_image_size(aspect_ratio, image_size)
        except ValueError as exc:
            raise ImageProviderError(str(exc), model=image_model) from exc
        body: dict = {
            "model": image_model,
            "prompt": prompt,
            "size": size,
            "quality": GPT_IMAGE_QUALITY,
            "n": 1,
            "output_format": "png",
        }
        if reference_images:
            body["input_references"] = [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}}
                for raw, mime in reference_images
            ]
        data = _post_image(f"{settings.openrouter_base_url}/images", body, headers, image_model)
        try:
            item = data["data"][0]
            raw = base64.b64decode(item["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ImageProviderError(
                f"image model {image_model} returned no image", model=image_model) from exc
        mime = item.get("media_type") or "image/png"
        endpoint = "images"
    else:
        if reference_images:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for img_bytes, img_mime in reference_images:
                data_url = f"data:{img_mime};base64,{base64.b64encode(img_bytes).decode()}"
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            messages: list[dict] = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": prompt}]
        body = {
            "model": image_model,
            "messages": messages,
            "modalities": _image_modalities(image_model),
        }
        image_config: dict[str, str] = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        size = _clamp_image_size(image_model, image_size)
        if size:
            image_config["image_size"] = size
        if image_config:
            body["image_config"] = image_config
        data = _post_image(
            f"{settings.openrouter_base_url}/chat/completions", body, headers, image_model)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ImageProviderError(
                f"image model {image_model} returned an unexpected response shape",
                model=image_model) from exc
        images = message.get("images") or []
        if not images:
            raise ImageProviderError(
                f"image model {image_model} returned no image (does it support "
                "image output?)", model=image_model)
        raw, mime = _parse_data_url(images[0]["image_url"]["url"])
        endpoint = "chat"

    _check_shape(raw, image_model, aspect_ratio)
    usage = data.get("usage") or {}
    logger.info(
        "image generated model=%s endpoint=%s ar=%s size=%s latency_s=%.1f cost_usd=%s id=%s",
        image_model, endpoint, aspect_ratio, body.get("size") or image_size,
        time.monotonic() - started, usage.get("cost"), data.get("id"),
    )
    return raw, mime
