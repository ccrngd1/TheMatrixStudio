# SPDX-License-Identifier: Apache-2.0
"""Tests for settings module - verifying configuration precedence."""

import os
import tempfile
from pathlib import Path

import pytest

from matrix_studio.settings import Settings, project_root


def test_settings_defaults():
    """Test that settings have sensible defaults.

    ``_env_file=None`` ignores any local ``.env``; the ``clean_env`` fixture in
    conftest clears the matching environment variables, which is what makes this
    assert real code defaults rather than local configuration.
    """
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

    # Explicitly pass _env_file to enable .env loading for this test
    # (test mode disables it globally)
    settings = Settings(_env_file=".env")
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


def test_project_root_is_the_checkout_containing_pyproject():
    """The anchor is the directory holding pyproject.toml, above the package."""
    root = project_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert (root / "matrix_studio").is_dir()


def test_relative_data_dir_ignores_the_working_directory(monkeypatch, tmp_path):
    """
    A relative data_dir resolves to the checkout, not to cwd.

    This is the regression test for a real incident: the server was started with
    cwd inside ``frontend/``, so ``./data`` became ``frontend/data`` and SQLite
    created a second empty database. The UI truthfully reported no previous
    conversations while 37 runs sat in the real file. Resolving against cwd made a
    cwd mistake indistinguishable from data loss.
    """
    settings = Settings(_env_file=None, data_dir="./data")
    from_root = settings.resolved_data_dir

    # Same setting, evaluated from two unrelated working directories.
    monkeypatch.chdir(tmp_path)
    assert settings.resolved_data_dir == from_root
    sub = tmp_path / "frontend"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert settings.resolved_data_dir == from_root

    # And it is the checkout's data dir, not the temp cwd's.
    assert from_root == project_root() / "data"
    assert tmp_path not in from_root.parents


def test_absolute_data_dir_is_honoured_as_given(monkeypatch, tmp_path):
    """An absolute DATA_DIR wins untouched — this is how the container passes /app/data."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "container-data"))
    settings = Settings()
    assert settings.resolved_data_dir == tmp_path / "container-data"
    assert settings.db_file == tmp_path / "container-data" / "matrix_studio.db"


def test_db_file_sits_under_the_resolved_data_dir():
    """db_file is the single source of the database path for every entry point."""
    settings = Settings(_env_file=None, data_dir="./data")
    assert settings.db_file == settings.resolved_data_dir / "matrix_studio.db"
    assert settings.db_file.is_absolute()


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
