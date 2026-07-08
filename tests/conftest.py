# SPDX-License-Identifier: Apache-2.0
"""Pytest configuration and shared fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def reset_settings_singleton():
    """Reset the settings singleton between tests."""
    import matrix_studio.settings
    matrix_studio.settings._settings = None
    yield
    matrix_studio.settings._settings = None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clean environment variables that might affect tests."""
    # Remove any API keys from environment
    env_vars_to_remove = [
        "LITELLM_MODEL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for var in env_vars_to_remove:
        monkeypatch.delenv(var, raising=False)
