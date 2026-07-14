"""Polish-model seam — premium default, env/runtime override, mock semantics."""

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


def test_no_key_auto_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "")
    monkeypatch.setattr(providers, "_openrouter_key_configured", lambda: False)
    assert providers.get_polish_provider().name == "mock"
