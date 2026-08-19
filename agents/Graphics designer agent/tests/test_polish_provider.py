"""Polish-model seam — premium default, env/runtime override, mock semantics."""

import pytest

from graphics_designer_agent import providers


def test_mock_env_yields_mock(monkeypatch):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "mock")
    assert providers.get_polish_provider().name == "mock"


def test_openrouter_uses_premium_default(monkeypatch):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "openrouter")
    monkeypatch.delenv("GD_POLISH_IMAGE_MODEL", raising=False)
    p = providers.get_polish_provider()
    assert p.name == "openrouter"
    assert p.model == providers._DEFAULT_POLISH_MODEL == "google/gemini-3-pro-image"


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "openrouter")
    monkeypatch.setenv("GD_POLISH_IMAGE_MODEL", "openai/gpt-5.4-image-2")
    assert providers.get_polish_provider().model == "openai/gpt-5.4-image-2"


def test_no_key_auto_raises_instead_of_silently_using_mock(monkeypatch):
    """Auto mode must fail loudly. The mock renders a brand gradient through the
    same (bytes, mime) path as a real generation, so falling back to it here
    would ship a placeholder as a finished AI creative."""
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    monkeypatch.setattr(
        providers, "_openrouter_key_status", lambda: (providers.KEY_ABSENT, None))
    with pytest.raises(providers.ImageProviderUnavailable) as exc:
        providers.get_polish_provider()
    assert exc.value.ai is False
    assert "OPENROUTER_API_KEY" in exc.value.fallback_reason
