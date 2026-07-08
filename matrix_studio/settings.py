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
        default="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        description="LiteLLM model string (e.g., openai/gpt-4o, anthropic/..., bedrock/...)"
    )
    litellm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    litellm_max_tokens: int = Field(default=2048, ge=1)

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
    enable_avatars: bool = Field(default=True, description="Enable avatar generation via Nova Canvas")
    avatar_model_id: str = Field(default="amazon.nova-canvas-v1:0")
    avatar_width: int = Field(default=512, ge=256, le=2048)
    avatar_height: int = Field(default=512, ge=256, le=2048)


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
