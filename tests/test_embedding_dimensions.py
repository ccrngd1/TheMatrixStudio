# SPDX-License-Identifier: Apache-2.0
"""
Explicit embedding width, and the guard that stops a silent mismatch.

Why this matters more than it looks: on the AWS target an S3 Vectors index's dimension is
**fixed at creation**, so a vector produced at the wrong width is not a slow path — it is a
value that can never be stored in the index it was made for, and changing the index means
rebuilding it. The AWS Well-Architected Generative AI Lens raises choosing the width twice
(GENCOST04-BP01 "Reduce vector length on embedded tokens", GENPERF04-BP02 "Optimize vector
sizes").

The specific failure guarded here: `litellm.drop_params` is set globally by the engine, so
an unsupported `dimensions` parameter would be **silently discarded** and the provider would
return its default width. Every vector would then be quietly wrong. Asserting the returned
length is what turns that into a loud failure.
"""

from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    TITAN_V2_DIMENSIONS,
    EmbeddingError,
    embed_query,
    embed_texts,
)


def _resp(dim: int):
    r = MagicMock()
    r.data = [{"embedding": [0.1] * dim}]
    r.usage = MagicMock(prompt_tokens=5)
    return r


def test_titan_widths_are_named_not_guessed():
    """256/512/1024 are what the model offers; a caller should not have to know that."""
    assert TITAN_V2_DIMENSIONS == (256, 512, 1024)


@pytest.mark.asyncio
async def test_dimensions_is_forwarded_to_the_provider():
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return _resp(kwargs.get("dimensions", 1024))

    with patch("litellm.aembedding", side_effect=fake):
        result = await embed_texts(["hello"], dimensions=256)

    assert calls[0]["dimensions"] == 256
    assert len(result.vectors[0]) == 256


@pytest.mark.asyncio
async def test_dimensions_is_omitted_when_not_asked_for():
    """
    A provider that does not accept the parameter must be unaffected.

    Sending `dimensions=None` explicitly would be a different request from not sending it,
    and some providers reject unknown keys outright.
    """
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return _resp(1024)

    with patch("litellm.aembedding", side_effect=fake):
        await embed_texts(["hello"])

    assert "dimensions" not in calls[0]


@pytest.mark.asyncio
async def test_a_provider_ignoring_dimensions_fails_loudly():
    """
    The guard that matters. `litellm.drop_params = True` is set globally by the engine,
    so an unsupported parameter is dropped rather than refused — the request succeeds at
    the WRONG width. Without this check every vector would be silently unusable for the
    index it was destined for, discovered only when the index rejected it.
    """
    async def fake(**kwargs):
        return _resp(1024)  # ignored the request for 256

    with patch("litellm.aembedding", side_effect=fake):
        with pytest.raises(EmbeddingError, match="asked for 256 dimensions, got 1024"):
            await embed_texts(["hello"], dimensions=256)


@pytest.mark.asyncio
async def test_query_and_chunk_paths_take_the_same_width():
    """
    Both sides must agree or the vectors are incomparable.

    A query embedded at one width and chunks at another is not a degraded search, it is a
    meaningless one — so the parameter has to reach both entry points.
    """
    async def fake(**kwargs):
        return _resp(kwargs.get("dimensions", 1024))

    with patch("litellm.aembedding", side_effect=fake):
        q = await embed_query("a question", dimensions=512)
        c = await embed_texts(["a chunk"], dimensions=512)

    assert len(q.vectors[0]) == 512
    assert len(c.vectors[0]) == 512


@pytest.mark.asyncio
async def test_query_embedding_still_degrades_rather_than_raising():
    """
    Query embedding is on the per-turn hot path, so a width mismatch there must degrade
    that turn to lexical rather than fail the run — the same contract as any other
    provider blip.
    """
    async def fake(**kwargs):
        return _resp(1024)

    with patch("litellm.aembedding", side_effect=fake):
        assert await embed_query("a question", dimensions=256) is None


@pytest.mark.asyncio
async def test_the_default_model_is_unchanged():
    """Titan v2 is what Phase 5f measured, so its numbers are the ones that transfer."""
    assert DEFAULT_EMBEDDING_MODEL == "bedrock/amazon.titan-embed-text-v2:0"
