"""OpenRouter integration.

OpenRouter is used for BOTH:
- the agent's reasoning LLM (via LangChain's ChatOpenAI, OpenAI-compatible), and
- image generation (via the chat-completions endpoint with image output
  modality), returning base64 data-URL images.
"""

from __future__ import annotations

import base64

import httpx
from langchain_openai import ChatOpenAI

from app.config import settings


def _default_headers() -> dict[str, str]:
    # OpenRouter uses these for attribution/ranking; harmless if unset.
    return {"HTTP-Referer": settings.app_public_url, "X-Title": settings.app_title}


def get_llm(temperature: float = 0.4, *, fast: bool = False) -> ChatOpenAI:
    """LangChain chat model backed by OpenRouter (used inside the LangGraph agent).

    `fast=True` selects the cheap parsing model; the default is the high-end
    reasoning model that pieces the creative together (persona, art direction,
    master prompt).
    """
    model = settings.openrouter_fast_model if fast else settings.openrouter_model
    return ChatOpenAI(
        model=model,
        api_key=settings.require("openrouter_api_key"),
        base_url=settings.openrouter_base_url,
        default_headers=_default_headers(),
        temperature=temperature,
        timeout=120,
        max_retries=2,
    )


def _image_modalities(model: str) -> list[str]:
    """Correct `modalities` for an OpenRouter image model.

    Text+image models (Gemini, GPT image) use ["image","text"]; image-only
    models (Flux, Recraft, etc.) require ["image"] or OpenRouter rejects them.
    """
    text_and_image = ("gemini", "gpt-5-image", "gpt-4o", "gpt-image")
    if any(token in model.lower() for token in text_and_image):
        return ["image", "text"]
    return ["image"]


def _parse_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a `data:<mime>;base64,<payload>` URL into (bytes, mime_type)."""
    if not data_url.startswith("data:"):
        raise RuntimeError("OpenRouter returned a non-data-URL image reference")
    header, _, payload = data_url.partition(",")
    mime = header[len("data:") :].split(";")[0] or "image/png"
    return base64.b64decode(payload), mime


def vision_extract_text(image_bytes: bytes, mime_type: str) -> str:
    """OCR + read an image via an OpenRouter vision model.

    Returns extracted text plus a short description of key visual content, used
    to enrich the user's creative brief (Workflow C).
    """
    api_key = settings.require("openrouter_api_key")
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}
    data_url = f"data:{mime_type or 'image/png'};base64,{base64.b64encode(image_bytes).decode()}"
    body = {
        "model": settings.openrouter_vision_model,
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
) -> str:
    """Analyze one or more images with a vision-capable chat model.

    Used to reverse-engineer a brand's visual design system from its website
    imagery. Defaults to the reasoning model (multimodal); callers may pass
    `settings.openrouter_vision_model` as a cheaper fallback.
    """
    api_key = settings.require("openrouter_api_key")
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_bytes, mime in images:
        data_url = (
            f"data:{mime or 'image/png'};base64,{base64.b64encode(img_bytes).decode()}"
        )
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    body = {
        "model": model or settings.openrouter_model,
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
) -> tuple[bytes, str]:
    """Render a single image through an OpenRouter image-output model.

    If `reference_images` (list of (bytes, mime)) is provided, they are sent
    alongside the prompt so the model can composite them (e.g. the exact brand
    logo) rather than inventing them. Returns the image bytes and MIME type.
    """
    api_key = settings.require("openrouter_api_key")
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_default_headers()}

    if reference_images:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img_bytes, img_mime in reference_images:
            data_url = f"data:{img_mime};base64,{base64.b64encode(img_bytes).decode()}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        messages: list[dict] = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": prompt}]

    image_model = model or settings.openrouter_image_model
    body = {
        "model": image_model,
        "messages": messages,
        "modalities": _image_modalities(image_model),
    }

    try:
        response = httpx.post(url, json=body, headers=headers, timeout=180)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter image generation failed ({response.status_code}): {response.text}"
        )

    data = response.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {data}") from exc

    images = message.get("images") or []
    if not images:
        raise RuntimeError(
            "OpenRouter returned no image. Ensure OPENROUTER_IMAGE_MODEL supports "
            "image output (e.g. google/gemini-2.5-flash-image)."
        )
    return _parse_data_url(images[0]["image_url"]["url"])
