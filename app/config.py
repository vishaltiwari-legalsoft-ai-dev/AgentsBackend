"""Typed application configuration loaded from environment variables.

Service credentials are optional at boot so the server can start while they are
being filled in; each service raises a clear error when first used without its
required configuration (see `require`).
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Server
    app_env: str = "development"
    port: int = 8080
    # Comma-separated list of allowed browser origins (the Vercel frontend).
    cors_origins: str = "http://localhost:3000,http://localhost:3001,http://localhost:8000"

    # Auth
    jwt_secret: str = ""
    jwt_expires_minutes: int = 60 * 24 * 7
    # Google OAuth (Identity Services). The Web Client ID is used both in the
    # frontend button and as the audience when verifying ID tokens here.
    google_client_id: str = ""
    # Comma-separated emails granted Super Admin (analytics + user directory).
    admin_emails: str = ""

    # OpenRouter (agent LLM + image generation)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Reasoning model — the agent's "brain" for piecing everything together:
    # brand persona compilation, creative-type decision, and master-prompt
    # synthesis. A top-tier model is important for faithfully preserving brand
    # detail and making good art-direction calls.
    openrouter_model: str = "anthropic/claude-opus-4.6"
    # Fast/cheap model for trivial parsing (extracting aspect ratio/brief from
    # chat, guessing the official website URL). Quality is not critical here.
    openrouter_fast_model: str = "anthropic/claude-sonnet-4.5"
    # Image-output model. Nano Banana Pro (Gemini 3 Pro Image) is the default
    # because it accepts the real brand logo as a reference image AND follows the
    # detailed brand master prompt — giving on-brand, logo-accurate creatives.
    # Flux.2 (black-forest-labs/flux.2-pro|max) is excellent for pure backgrounds
    # but is image-only (can't composite the real logo), so it's reserved for the
    # future layered editor.
    openrouter_image_model: str = "google/gemini-3-pro-image-preview"
    # Optional alternative/background model toggle.
    openrouter_image_model_hero: str = "black-forest-labs/flux.2-max"
    # Vision-capable model used for OCR / reading uploaded images.
    openrouter_vision_model: str = "openai/gpt-4o-mini"
    # Sent as HTTP-Referer/X-Title to OpenRouter for attribution (optional).
    app_public_url: str = "http://localhost:3000"
    app_title: str = "AgentOS"

    # Google Cloud
    gcp_project_id: str = ""
    google_application_credentials: str = ""
    gcs_bucket_name: str = ""
    # Firestore database id. Use "(default)" for the default database, or set
    # the name of a custom database (e.g. "lsbrandkit").
    firestore_database: str = "(default)"

    # Canva Connect
    canva_client_id: str = ""
    canva_client_secret: str = ""
    canva_redirect_uri: str = "http://localhost:8080/api/canva/callback"

    # Ingestion source (the ONLY brand-asset directory the app reads).
    brand_kits_dir: str = ""

    # Brand-wise master prompt library CSV. Empty = use the bundled
    # app/data/Sample_master_prompt_brandwise - Sheet2.csv.
    master_prompts_csv: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    def require(self, field: str) -> str:
        """Return a config value, raising a descriptive error if it is empty."""
        value = getattr(self, field, "")
        if not value:
            raise RuntimeError(
                f'Missing required configuration "{field}". Set it in your .env '
                f"(see credentials.md for how to obtain it)."
            )
        return str(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Google's client libraries read GOOGLE_APPLICATION_CREDENTIALS from the OS
# environment (not from .env), so export the configured path for local dev. On
# Cloud Run the attached service account is used instead, so this is left unset.
if settings.google_application_credentials and not os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS"
):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(
        settings.google_application_credentials
    )
