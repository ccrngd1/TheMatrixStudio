# SPDX-License-Identifier: Apache-2.0
"""Tests for settings module - verifying configuration precedence."""

import os
import tempfile
from pathlib import Path

import pytest

from matrix_studio.settings import Settings


def test_settings_defaults():
    """Test that settings have sensible defaults."""
    # Ignore any local .env so we assert the true code defaults.
    settings = Settings(_env_file=None)
    assert settings.litellm_model == "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert settings.litellm_temperature == 0.7
    assert settings.max_messages == 20
    assert settings.aws_region == "us-east-1"
    assert settings.enable_avatars is True


def test_settings_from_env_vars(monkeypatch):
    """Test that environment variables override defaults."""
    monkeypatch.setenv("LITELLM_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("LITELLM_TEMPERATURE", "0.9")
    monkeypatch.setenv("MAX_MESSAGES", "10")
    monkeypatch.setenv("ENABLE_AVATARS", "false")

    settings = Settings()
    assert settings.litellm_model == "openai/gpt-4o"
    assert settings.litellm_temperature == 0.9
    assert settings.max_messages == 10
    assert settings.enable_avatars is False


def test_settings_precedence_env_over_dotenv(monkeypatch, tmp_path):
    """Test that environment variables take precedence over .env file."""
    # Create a .env file
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_MODEL=anthropic/claude-3-opus\nMAX_MESSAGES=5\n")

    # Set env var that should override .env
    monkeypatch.setenv("LITELLM_MODEL", "openai/gpt-4o")
    monkeypatch.chdir(tmp_path)

    settings = Settings()
    # Env var should win
    assert settings.litellm_model == "openai/gpt-4o"
    # .env value should be used for non-overridden keys
    assert settings.max_messages == 5


def test_settings_optional_aws_credentials():
    """Test that AWS credentials can be None."""
    settings = Settings()
    # Should not error when AWS credentials are None
    assert settings.aws_access_key_id is None
    assert settings.aws_secret_access_key is None


def test_settings_validation():
    """Test that settings validation works."""
    # Valid temperature range
    settings = Settings(litellm_temperature=0.0)
    assert settings.litellm_temperature == 0.0

    settings = Settings(litellm_temperature=2.0)
    assert settings.litellm_temperature == 2.0

    # Invalid temperature should fail
    with pytest.raises(Exception):
        Settings(litellm_temperature=-0.1)

    with pytest.raises(Exception):
        Settings(litellm_temperature=2.1)


def test_settings_multiple_providers(monkeypatch):
    """Test that settings work with different LLM providers."""
    test_cases = [
        "openai/gpt-4o",
        "anthropic/claude-3-5-sonnet-20241022",
        "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "ollama/llama2",
        "openrouter/anthropic/claude-3.5-sonnet",
    ]

    for model in test_cases:
        monkeypatch.setenv("LITELLM_MODEL", model)
        settings = Settings()
        assert settings.litellm_model == model
