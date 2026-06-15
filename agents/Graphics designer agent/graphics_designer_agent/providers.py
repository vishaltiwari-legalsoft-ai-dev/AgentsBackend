"""Provider-agnostic image generation (spec §3).

One interface, ``ImageProvider.generate``, behind which any backend can plug in.
Two implementations ship:

* ``MockImageProvider`` — renders a brand-accurate placeholder locally (no API
  key, no network). Stage 1 draws the real brand gradient; later stages tint and
  annotate the chained reference image so the pipeline is fully demoable offline.
* ``OpenRouterProvider`` — delegates to the existing ``app.services.openrouter``
  image model used elsewhere in the backend.

Provider is chosen by the ``GD_IMAGE_PROVIDER`` env var, defaulting to ``mock``
unless an OpenRouter key is configured.
"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Protocol

from PIL import Image, ImageDraw

# Brand palette (locked — §2.2)
_WHITE = (255, 255, 255)
_BDCFED = (189, 207, 237)
_A2C0E6 = (162, 192, 230)
_1746A2 = (23, 70, 162)
_INK = (15, 15, 15)


class ImageProvider(Protocol):
    name: str
    supports_negative: bool

    def generate(
        self,
        prompt: str,
        *,
        reference_images: list[tuple[bytes, str]] | None = None,
        width: int = 1080,
        height: int = 1350,
        negative_prompt: str | None = None,
        label: str = "",
    ) -> tuple[bytes, str]:
        ...


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _palette(t: float) -> tuple[int, int, int]:
    """White → light blue → sky blue → royal blue across t∈[0,1]."""
    stops = [(0.0, _WHITE), (0.35, _BDCFED), (0.6, _A2C0E6), (1.0, _1746A2)]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            span = (t1 - t0) or 1
            return _lerp(c0, c1, (t - t0) / span)
    return _1746A2


def _brand_gradient(width: int, height: int, *, vertical: bool = False) -> Image.Image:
    """Smooth diagonal (or inverted-vertical) brand gradient, upscaled from a
    small render for speed."""
    sw, sh = 96, 120
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    for y in range(sh):
        for x in range(sw):
            if vertical:
                t = 1.0 - (y / (sh - 1))
            else:
                t = (x / (sw - 1) + y / (sh - 1)) / 2
            px[x, y] = _palette(t)
    return small.resize((width, height), Image.BICUBIC)


def _label_band(img: Image.Image, text: str) -> None:
    if not text:
        return
    draw = ImageDraw.Draw(img, "RGBA")
    pad = max(8, img.width // 90)
    band_h = pad * 4
    draw.rectangle([0, img.height - band_h, img.width, img.height], fill=(15, 15, 15, 150))
    draw.text((pad, img.height - band_h + pad), text, fill=(255, 255, 255, 230))


class MockImageProvider:
    name = "mock"
    supports_negative = False

    def generate(
        self,
        prompt: str,
        *,
        reference_images: list[tuple[bytes, str]] | None = None,
        width: int = 1080,
        height: int = 1350,
        negative_prompt: str | None = None,
        label: str = "",
    ) -> tuple[bytes, str]:
        if reference_images:
            base = Image.open(BytesIO(reference_images[0][0])).convert("RGB")
            base = base.resize((width, height), Image.BICUBIC)
            # Faint scrim so chained stages read as "transformed", not identical.
            scrim = Image.new("RGBA", base.size, (23, 70, 162, 26))
            base = Image.alpha_composite(base.convert("RGBA"), scrim).convert("RGB")
        else:
            vertical = "inverted horizon" in prompt.lower()
            base = _brand_gradient(width, height, vertical=vertical)
        buf = BytesIO()
        base.save(buf, format="PNG")
        return buf.getvalue(), "image/png"


class OpenRouterProvider:
    name = "openrouter"
    supports_negative = False

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    def generate(
        self,
        prompt: str,
        *,
        reference_images: list[tuple[bytes, str]] | None = None,
        width: int = 1080,
        height: int = 1350,
        negative_prompt: str | None = None,
        label: str = "",
    ) -> tuple[bytes, str]:
        # Imported lazily so the package works without the backend app installed.
        from app.services import openrouter

        full = prompt
        if negative_prompt:
            full = f"{prompt}\n\nAVOID THE FOLLOWING:\n{negative_prompt}"
        return openrouter.generate_image(full, reference_images=reference_images, model=self.model)


def _openrouter_key_configured() -> bool:
    """True if an OpenRouter key is available via env OR the app's settings.

    pydantic-settings loads ``.env`` into the settings object, not ``os.environ``,
    so we must consult ``app.config`` to honour a key set there.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    try:
        from app.config import settings

        return bool(getattr(settings, "openrouter_api_key", ""))
    except Exception:
        return False


def get_provider(name: str | None = None) -> ImageProvider:
    name = (name or os.environ.get("GD_IMAGE_PROVIDER") or "").strip().lower()
    if name == "mock":
        return MockImageProvider()
    if name == "openrouter":
        return OpenRouterProvider()
    # Auto: use the real model when a key is configured, otherwise the mock.
    return OpenRouterProvider() if _openrouter_key_configured() else MockImageProvider()
