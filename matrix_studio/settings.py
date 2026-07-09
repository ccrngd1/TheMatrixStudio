# SPDX-License-Identifier: Apache-2.0
"""
Configuration settings for TheMatrix Simulation Studio.

Settings precedence: environment variables > .env file > config.json defaults
"""

from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global settings for the simulation engine."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    # LiteLLM model configuration
    litellm_model: str = Field(
        default="bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="LiteLLM model string (e.g., openai/gpt-4o, anthropic/..., bedrock/...)"
    )
    litellm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    litellm_max_tokens: int = Field(default=2048, ge=1)

    # Selectable models offered in the UI (new-run form + in-thread analysis /
    # branch pickers). Comma-separated model strings; env AVAILABLE_MODELS. The
    # current ``litellm_model`` default is always included by ``/api/models``
    # even if omitted here. Keys stay server-side; this is just the allowlist of
    # model strings a user may pick.
    available_models: str = Field(
        default="",
        description="Comma-separated selectable model strings (AVAILABLE_MODELS).",
    )

    @property
    def available_model_list(self) -> list[str]:
        """Parsed, de-duplicated selectable models with the default first."""
        out: list[str] = [self.litellm_model]
        for m in self.available_models.split(","):
            m = m.strip()
            if m and m not in out:
                out.append(m)
        return out

    # AWS credentials (for Bedrock and Nova Canvas)
    aws_access_key_id: Optional[str] = Field(default=None)
    aws_secret_access_key: Optional[str] = Field(default=None)
    aws_region: str = Field(default="us-east-1")

    # OpenAI API key
    openai_api_key: Optional[str] = Field(default=None)

    # Anthropic API key
    anthropic_api_key: Optional[str] = Field(default=None)

    # Simulation defaults
    max_messages: int = Field(default=20, ge=1, description="Default max turns per simulation")

    # Storage
    data_dir: str = Field(default="./data", description="Directory for SQLite database")

    # Server settings (for Phase 1)
    matrix_port: int = Field(default=8000, ge=1, le=65535)
    matrix_host: str = Field(default="127.0.0.1")

    # Avatar generation
    enable_avatars: bool = Field(default=True, description="Enable avatar generation via Stability image model on Bedrock")
    avatar_model_id: str = Field(default="stability.sd3-5-large-v1:0")
    # Image models are not always available in the same region as the text model;
    # SD3.5 Large is served from us-west-2.
    avatar_region: str = Field(default="us-west-2")
    avatar_aspect_ratio: str = Field(default="1:1")
    # Avatar art style. Default is a NON-photorealistic illustration so avatars
    # read as clearly-synthetic characters, not photos of real people (avoids
    # misrepresentation / synthetic-media-labeling / accidental-likeness risk).
    # Options: 'illustration' (flat vector), 'anime', '3d' (stylized cartoon
    # render). Unknown values fall back to 'illustration'.
    avatar_style: str = Field(default="illustration", description="Avatar art style (env AVATAR_STYLE)")


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
