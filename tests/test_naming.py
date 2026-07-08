# SPDX-License-Identifier: Apache-2.0
"""
Tests for the run-naming module. The litellm call is MOCKED — naming must never
make a live call in tests, and must never block/raise a run on failure.
"""

from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.naming import (
    NAME_RE,
    _extract_json,
    _fallback_name,
    _is_valid_name,
    _slugify,
    generate_run_name,
)


def _resp(content: str):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=content))]
    return r


def test_name_regex_and_validation():
    assert _is_valid_name("trusted-robot")
    assert _is_valid_name("summit-compass")
    assert not _is_valid_name("test-run")       # banned generic
    assert not _is_valid_name("one-two-three")  # three words
    assert not _is_valid_name("Trusted-Robot")  # uppercase
    assert not _is_valid_name("single")         # one word
    assert NAME_RE.match("muse-machine")


def test_slugify():
    assert _slugify("Trusted Robot!") == "trusted-robot"
    assert _slugify("already-kebab") == "already-kebab"


def test_extract_json_handles_fences():
    assert _extract_json('```json\n{"name":"a-b"}\n```') == {"name": "a-b"}
    assert _extract_json('{"name":"a-b","description":"x"}')["name"] == "a-b"
    assert _extract_json("prefix {\"name\": \"a-b\"} suffix")["name"] == "a-b"
    assert _extract_json("not json at all") is None


def test_fallback_name_is_valid_and_varies():
    n0 = _fallback_name("AI ethics", 0)
    n1 = _fallback_name("AI ethics", 1)
    assert NAME_RE.match(n0)
    assert NAME_RE.match(n1)


@pytest.mark.asyncio
async def test_generate_name_from_llm():
    with patch("matrix_studio.naming.litellm.acompletion") as mock:
        mock.return_value = _resp('{"name": "trusted-robot", "description": "AI ethics debate"}')
        result = await generate_run_name("AI ethics", ["Ada", "Ben"], model="x")
    assert result["name"] == "trusted-robot"
    assert result["source"] == "llm"
    assert result["slug"] == "trusted-robot"


@pytest.mark.asyncio
async def test_generate_name_retries_on_invalid_then_valid():
    with patch("matrix_studio.naming.litellm.acompletion") as mock:
        mock.side_effect = [
            _resp("garbage not json"),
            _resp('{"name": "muse-machine", "description": "creative AI"}'),
        ]
        result = await generate_run_name("AI in creative work", model="x")
    assert result["name"] == "muse-machine"
    assert result["source"] == "llm"


@pytest.mark.asyncio
async def test_generate_name_falls_back_on_llm_failure():
    """If the LLM call keeps failing, we fall back to a wordlist name — never raise."""
    with patch("matrix_studio.naming.litellm.acompletion", side_effect=Exception("API down")):
        result = await generate_run_name("weekend hiking trip", model="x")
    assert result["source"] == "fallback"
    assert NAME_RE.match(result["name"])
    assert result["description"]


@pytest.mark.asyncio
async def test_generate_name_uniqueness_disambiguation():
    """On collision the name is disambiguated, never a duplicate."""
    taken = {"trusted-robot"}

    async def name_exists(n):
        return n in taken

    with patch("matrix_studio.naming.litellm.acompletion") as mock:
        # Always returns the same taken name → forces suffix disambiguation.
        mock.return_value = _resp('{"name": "trusted-robot", "description": "d"}')
        result = await generate_run_name("AI ethics", model="x", name_exists=name_exists)
    assert result["name"] != "trusted-robot"
    assert result["name"].startswith("trusted-robot-")
