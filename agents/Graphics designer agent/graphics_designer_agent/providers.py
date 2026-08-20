"""Provider-agnostic image generation (spec §3).

One interface, ``ImageProvider.generate``, behind which any backend can plug in.
Two implementations ship:

* ``MockImageProvider`` — renders a brand-accurate placeholder locally (no API
  key, no network). Stage 1 draws the real brand gradient; later stages tint and
  annotate the chained reference image so the pipeline is fully demoable offline.
* ``OpenRouterProvider`` — delegates to the existing ``app.services.openrouter``
  image model used elsewhere in the backend.

Provider is chosen by the ``GD_IMAGE_PROVIDER`` env var. **The mock is reachable
only when it is explicitly selected** (``GD_IMAGE_PROVIDER=mock``): its output is
a brand gradient that travels the same ``(bytes, mime)`` return path as a real
generation, so auto-selecting it would save a placeholder as the run's creative,
upload it to GCS and log it as a completed run — a canned image badged as an AI
result. In auto mode a missing (or undeterminable) key raises
:class:`ImageProviderUnavailable` instead, carrying ``ai=False`` and a populated
``fallback_reason`` — the same honest-failure contract ``suggestions`` and
``text_optimizer`` already use.
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


# The Graphic Designer's agent id (matches app.services.agent_config.AGENTS).
# Every model lookup this package makes must carry it, or the creator's
# per-agent overrides in the Agent Configuration panel silently do nothing.
GD_AGENT_ID = "a1"


class ImageProviderUnavailable(RuntimeError):
    """No real image provider could be resolved in auto mode.

    Carries the honest-failure pair the rest of this agent already speaks
    (``suggestions.py``, ``text_optimizer.py``): ``ai`` is ``False`` and
    ``fallback_reason`` names the actual cause, so the HTTP layer can surface
    *why* nothing was generated instead of shipping a placeholder.
    """

    ai = False

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.fallback_reason = reason


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
        aspect_ratio: str | None = None,
        image_size: str | None = None,
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
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> tuple[bytes, str]:
        # The mock renders at the pixel dimensions it is given; aspect_ratio /
        # image_size are honoured by the real provider only (width/height already
        # encode the shape here).
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
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> tuple[bytes, str]:
        # Imported lazily so the package works without the backend app installed.
        from app.services import openrouter

        full = prompt
        if negative_prompt:
            full = f"{prompt}\n\nAVOID THE FOLLOWING:\n{negative_prompt}"
        return openrouter.generate_image(
            full,
            reference_images=reference_images,
            model=self.model,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        )


# Tri-state key resolution. "absent" and "unknown" are deliberately distinct:
# reading the admin override goes through Firestore, and a one-second hiccup
# there must never be reported as "no API key" when the key is set correctly.
KEY_PRESENT = "present"
KEY_ABSENT = "absent"
KEY_UNKNOWN = "unknown"


def _runtime_key() -> bool:
    """Whether the admin-set key override resolves to a value. Module-level seam
    so tests can drive the failure branch without a Firestore stub. Raises on any
    read failure — the caller turns that into ``KEY_UNKNOWN``, never ``absent``."""
    from app.services import runtime_config

    return bool(runtime_config.get("openrouter_api_key"))


def _openrouter_key_status() -> tuple[str, str | None]:
    """Resolve whether an OpenRouter key is configured, distinguishing
    "genuinely absent" from "could not determine".

    pydantic-settings loads ``.env`` into the settings object, not ``os.environ``,
    and the Super Admin may set the key from the UI (stored in Firestore), so we
    consult ``runtime_config`` which layers the override over the environment.
    That read can fail transiently — which is *not* evidence the key is missing,
    so it returns ``KEY_UNKNOWN`` plus the real cause rather than a bare False.

    Returns ``(status, detail)``; ``detail`` is only set for ``KEY_UNKNOWN`` and
    never contains a secret value — only env-var / setting names.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return KEY_PRESENT, None
    try:
        configured = _runtime_key()
    except Exception as exc:  # noqa: BLE001 - config/Firestore read failed
        return KEY_UNKNOWN, (
            "could not determine whether an image-model key is configured: the "
            f"admin-override lookup failed ({type(exc).__name__}: {exc})"
        )
    return (KEY_PRESENT, None) if configured else (KEY_ABSENT, None)


def _openrouter_key_configured() -> bool:
    """Boolean view of :func:`_openrouter_key_status` for the *advisory* callers
    (``placement_brain``, ``qa_brain``) that only ask "is the LLM worth trying?"
    and degrade to a heuristic either way. Provider selection must NOT use this —
    it collapses "absent" and "unknown" together; use ``_openrouter_key_status``.
    """
    return _openrouter_key_status()[0] == KEY_PRESENT


def _require_real_provider() -> None:
    """Gate for auto mode: return quietly when a real provider can be used, and
    raise a named, honest failure otherwise. Never downgrades to the mock."""
    status, detail = _openrouter_key_status()
    if status == KEY_PRESENT:
        return
    if status == KEY_UNKNOWN:
        raise ImageProviderUnavailable(
            (detail or "could not determine whether an image-model API key is configured")
            + " — refusing to substitute a placeholder image for a real generation"
        )
    raise ImageProviderUnavailable(
        "no image-model API key configured (set OPENROUTER_API_KEY, or the admin "
        "key override in the Secrets panel); to render brand-gradient placeholders "
        "on purpose, select them explicitly with GD_IMAGE_PROVIDER=mock"
    )


def _agent_image_model(agent_id: str | None) -> str | None:
    """The image model this agent should use (per-agent override → global → env).
    Returns None so the OpenRouter layer applies its own global fallback when we
    can't resolve one (e.g. backend app not importable in standalone tests)."""
    if not agent_id:
        return None
    try:
        from app.services import runtime_config

        return runtime_config.get_for_agent(agent_id, "openrouter_image_model") or None
    except Exception:
        return None


def get_provider(name: str | None = None, *, agent_id: str | None = None) -> ImageProvider:
    name = (name or os.environ.get("GD_IMAGE_PROVIDER") or "").strip().lower()
    if name == "mock":
        return MockImageProvider()
    model = _agent_image_model(agent_id)
    if name == "openrouter":
        return OpenRouterProvider(model=model)
    # Auto: the real model, or a loud failure. Never a silent placeholder.
    _require_real_provider()
    return OpenRouterProvider(model=model)


# The Stage-3 polish pass runs a PREMIUM image-edit model by default: collision
# fixes and text fidelity are exactly what Gemini 3 Pro Image (Nano Banana Pro)
# is strongest at, and the polish pass is where placement quality is decided.
# Stages 1–2 keep the cheaper default model — cost rises only where it pays.
# Standalone fallback only: reached when the backend app (and therefore
# Settings.gd_polish_image_model) is not importable. Keep the two in sync.
_DEFAULT_POLISH_MODEL = "google/gemini-3-pro-image"


def _polish_model(agent_id: str | None) -> str:
    """Model for the Text Optimizer polish fan-out:
    env ``GD_POLISH_IMAGE_MODEL`` → runtime config ``gd_polish_image_model``
    (per-agent override → global override → env) → the premium default.

    ``gd_polish_image_model`` is a real ``Settings`` field and a member of both
    ``runtime_config.OVERRIDE_FIELDS`` and ``AGENT_OVERRIDE_FIELDS``, so the
    admin path this docstring advertises actually resolves. It used to be
    neither, which made ``runtime_config.get`` return "" every time.
    """
    env = (os.environ.get("GD_POLISH_IMAGE_MODEL") or "").strip()
    if env:
        return env
    try:
        from app.services import runtime_config

        override = runtime_config.get_for_agent(agent_id, "gd_polish_image_model")
        if override:
            return str(override)
    except Exception:
        pass
    return _DEFAULT_POLISH_MODEL


def get_polish_provider(*, agent_id: str | None = None) -> ImageProvider:
    """Provider for the Stage-3 polish fan-out — same mock/auto semantics as
    ``get_provider``, but with the dedicated polish-model resolution above."""
    name = (os.environ.get("GD_IMAGE_PROVIDER") or "").strip().lower()
    if name == "mock":
        return MockImageProvider()
    if name != "openrouter":
        _require_real_provider()
    return OpenRouterProvider(model=_polish_model(agent_id or GD_AGENT_ID))
