"""The admin DB viewer must never render a secret in full.

/admin/db/* is require_admin while the Secrets panel is require_creator, so an
unmasked key here is a way around the Creator boundary. The mask set used to be
hand-written and drifted behind runtime_config (the GEO engine keys landed in
app_config/global and were readable); these tests pin the derivation instead of
the four strings, so the next key added is covered without a code change here.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path

from app.routers import admin
from app.services import runtime_config

SECRET = "sk-live-0123456789abcdefghijklmnop"


def test_every_runtime_secret_override_is_masked():
    secret_overrides = [
        f
        for f in runtime_config.OVERRIDE_FIELDS
        if f not in runtime_config.AGENT_OVERRIDE_FIELDS
    ]
    # Guards the derivation itself: if OVERRIDE_FIELDS ever became all-models
    # this test would pass vacuously.
    assert "openrouter_api_key" in secret_overrides
    assert {"perplexity_api_key", "gemini_api_key", "openai_api_key"} <= set(
        secret_overrides
    )
    for field in secret_overrides:
        assert admin._is_sensitive_key(field), f"{field} is rendered in full"


def test_model_ids_stay_readable():
    for field in runtime_config.AGENT_OVERRIDE_FIELDS:
        assert not admin._is_sensitive_key(field)


def test_keys_written_outside_runtime_config_are_masked():
    # Agent code writes these straight into app_config/global.
    for field in ("seo_serper_api_key", "dataforseo_password", "canva_client_secret",
                  "refresh_token", "google_sub", "jwt_secret"):
        assert admin._is_sensitive_key(field), f"{field} is rendered in full"


def test_sanitize_masks_nested_secrets_in_a_document():
    row = admin._sanitize(
        {
            "id": "global",
            "openrouter_model": "anthropic/claude-opus-4.6",
            "perplexity_api_key": SECRET,
            "seo_serper_api_key": SECRET,
            "agents": {"a10": {"gemini_api_key": SECRET}},
        }
    )
    assert row["openrouter_model"] == "anthropic/claude-opus-4.6"
    for masked in (
        row["perplexity_api_key"],
        row["seo_serper_api_key"],
        row["agents"]["a10"]["gemini_api_key"],
    ):
        assert SECRET not in masked
        assert masked == admin._mask(SECRET)
