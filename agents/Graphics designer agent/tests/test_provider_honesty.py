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
