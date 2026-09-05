# SPDX-License-Identifier: Apache-2.0
"""Phase 5f tests — embeddings, vector storage, KNN and hybrid fusion.

All embedding calls are mocked; no test makes a live billable call. The
properties locked here are the ones that make vector retrieval safe to ship:

- positional alignment (a failed text yields None in place, never a shift);
- a total failure raises, a partial failure does not;
- dimension mismatch is REFUSED rather than silently corrupting the index;
- vector scoping matches lexical scoping (a persona cannot cross slices);
- fusion is by rank, because BM25 and distance have opposite polarity;
- every failure path degrades to lexical retrieval instead of ending a run.
"""

import struct
from unittest.mock import patch

import pytest

from matrix_studio.embeddings import (
    EmbeddingError,
    EmbeddingResult,
    deserialise,
    embed_query,
    embed_texts,
    serialise,
)
from matrix_studio.retrieval import (
    embed_pending_chunks,
    reciprocal_rank_fusion,
    retrieve_for_turn,
)
from matrix_studio.storage import Database


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def test_serialise_roundtrip():
    vec = [0.5, -1.5, 2.25, 0.0]
    assert deserialise(serialise(vec)) == pytest.approx(vec)


def test_serialise_is_little_endian_float32():
    """sqlite-vec expects exactly this layout; a mismatch yields silent nonsense."""
    assert serialise([1.0]) == struct.pack("<1f", 1.0)
    assert len(serialise([0.0] * 1024)) == 4096


# --------------------------------------------------------------------------
# embed_texts
# --------------------------------------------------------------------------


class _Emb:
    """Minimal stand-in for a litellm embedding response."""

    def __init__(self, vector, tokens=7):
        self.data = [{"embedding": list(vector)}]
        self.usage = type("U", (), {"prompt_tokens": tokens})()


def _fake_embedding(vector=(0.1, 0.2, 0.3), tokens=7, fail_on=()):
    async def fake(model=None, input=None, **kwargs):
        text = input[0] if isinstance(input, list) else input
        if any(marker in text for marker in fail_on):
            raise RuntimeError("provider blew up")
        return _Emb(vector, tokens)

    return fake


async def test_embed_texts_returns_one_vector_per_input():
    with patch("litellm.aembedding", side_effect=_fake_embedding()), \
         patch("litellm.completion_cost", return_value=0.000001):
        result = await embed_texts(["a", "b", "c"], model="m")
    assert len(result.vectors) == 3
    assert all(v == [0.1, 0.2, 0.3] for v in result.vectors)
    assert result.dim == 3
    assert result.ok_count == 3
    assert result.tokens == 21
    assert result.cost_usd == pytest.approx(0.000003)
    assert result.model == "m"


async def test_partial_failure_keeps_positional_alignment():
    """A failed text must yield None IN PLACE, never shift later results."""
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("BAD",))), \
         patch("litellm.completion_cost", return_value=0.0):
        result = await embed_texts(["ok1", "BAD", "ok2"], model="m")
    assert result.vectors[0] is not None
    assert result.vectors[1] is None
    assert result.vectors[2] is not None
    assert result.ok_count == 2


async def test_total_failure_raises():
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("",))):
        with pytest.raises(EmbeddingError, match="All 2 embedding"):
            await embed_texts(["x", "y"], model="m")


async def test_empty_input_is_not_an_error():
    result = await embed_texts([], model="m")
    assert result.vectors == [] and result.dim == 0


async def test_blank_strings_are_skipped_without_a_call():
    calls = []

    async def counting(model=None, input=None, **kwargs):
        calls.append(input)
        return _Emb((1.0, 2.0))

    with patch("litellm.aembedding", side_effect=counting), \
         patch("litellm.completion_cost", return_value=0.0):
        result = await embed_texts(["real", "   ", ""], model="m")
    assert len(calls) == 1, "spent a call on whitespace"
    assert result.vectors[1] is None and result.vectors[2] is None


async def test_malformed_response_is_treated_as_a_failure():
    async def malformed(model=None, input=None, **kwargs):
        return type("R", (), {"data": [{}], "usage": None})()

    with patch("litellm.aembedding", side_effect=malformed):
        with pytest.raises(EmbeddingError):
            await embed_texts(["x"], model="m")


async def test_cost_reporting_failure_is_tolerated():
    """A provider that cannot price a call counts as zero, per cost-cap semantics."""
    with patch("litellm.aembedding", side_effect=_fake_embedding()), \
         patch("litellm.completion_cost", side_effect=RuntimeError("no pricing")):
        result = await embed_texts(["x"], model="m")
    assert result.ok_count == 1 and result.cost_usd == 0.0


async def test_embed_query_returns_none_instead_of_raising():
    """Query embedding is on the per-turn hot path; it must never raise."""
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("",))):
        assert await embed_query("anything", model="m") is None


async def test_embed_query_success():
    with patch("litellm.aembedding", side_effect=_fake_embedding()), \
         patch("litellm.completion_cost", return_value=0.0):
        result = await embed_query("egress", model="m")
    assert result and result.vectors[0] == [0.1, 0.2, 0.3]


# --------------------------------------------------------------------------
# Reciprocal Rank Fusion
# --------------------------------------------------------------------------


def _row(chunk_id, score=-1.0):
    return {
        "chunk_id": chunk_id, "document_id": "d1", "title": "t.md",
        "ordinal": chunk_id, "content": f"c{chunk_id}", "score": score,
    }


def test_fusion_rewards_agreement_between_retrievers():
    lexical = [_row(1), _row(2), _row(3)]
    semantic = [_row(3), _row(1), _row(9)]
    fused = reciprocal_rank_fusion([lexical, semantic], rrf_k=60)
    ids = [r["chunk_id"] for r in fused]
    # 1 and 3 appear in both lists, so they must outrank single-list results.
    assert set(ids[:2]) == {1, 3}
    assert set(ids[2:]) == {2, 9}


def test_fusion_uses_rank_not_score_polarity():
    """BM25 is negative-better, distance is positive-smaller-better. Fusion by
    rank must be indifferent to the magnitudes entirely."""
    lexical = [_row(1, score=-99.0), _row(2, score=-98.0)]
    semantic = [_row(1, score=0.01), _row(2, score=0.02)]
    fused = reciprocal_rank_fusion([lexical, semantic])
    assert [r["chunk_id"] for r in fused] == [1, 2]


def test_fusion_score_is_higher_is_better():
    fused = reciprocal_rank_fusion([[_row(1), _row(2)]])
    assert fused[0]["score"] > fused[1]["score"]


def test_fusion_records_its_sources_for_audit():
    fused = reciprocal_rank_fusion([[_row(1)], [_row(1)]])
    assert set(fused[0]["fusion_sources"]) == {"0", "1"}
    assert fused[0]["fusion_sources"]["0"]["rank"] == 1


def test_fusion_weights_shift_the_ordering():
    lexical = [_row(1)]
    semantic = [_row(2)]
    assert reciprocal_rank_fusion(
        [lexical, semantic], weights=[1.0, 5.0]
    )[0]["chunk_id"] == 2


def test_fusion_of_empty_and_single_lists():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
    assert [r["chunk_id"] for r in reciprocal_rank_fusion([[_row(4)]])] == [4]


# --------------------------------------------------------------------------
# Vector storage + KNN (skipped when sqlite-vec is unavailable)
# --------------------------------------------------------------------------


@pytest.fixture
async def vdb(tmp_path):
    db = Database(str(tmp_path / "vec.db"))
    await db.connect()
    await db.create_run(run_id="r1", topic="t", cast=[])
    yield db
    await db.close()


requires_vec = pytest.mark.skipif(
    not __import__("importlib").util.find_spec("sqlite_vec"),
    reason="sqlite-vec not installed",
)


@requires_vec
async def test_store_and_knn_search(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md", chunks=["alpha text", "beta text"],
        persona_name="Dana",
    )
    ids = [c["chunk_id"] for c in await vdb.chunks_missing_vectors("r1")]
    assert len(ids) == 2
    await vdb.store_chunk_vectors(
        "r1", [(ids[0], [1.0, 0.0, 0.0]), (ids[1], [0.0, 1.0, 0.0])], "m"
    )
    assert await vdb.count_chunk_vectors("r1") == 2
    rows = await vdb.vector_search("r1", [0.95, 0.05, 0.0], "Dana", k=2)
    assert [r["chunk_id"] for r in rows] == [ids[0], ids[1]]
    assert rows[0]["score"] < rows[1]["score"], "distance must be nearest-first"
    assert rows[0]["title"] == "a.md"


@requires_vec
async def test_vector_scoping_matches_lexical_scoping(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(run_id="r1", title="dana.md", chunks=["x"], persona_name="Dana")
    await vdb.add_document(run_id="r1", title="marcus.md", chunks=["y"], persona_name="Marcus")
    pending = await vdb.chunks_missing_vectors("r1")
    await vdb.store_chunk_vectors(
        "r1", [(pending[0]["chunk_id"], [1.0, 0.0]), (pending[1]["chunk_id"], [1.0, 0.0])], "m"
    )
    dana = await vdb.vector_search("r1", [1.0, 0.0], "Dana", k=5)
    assert {r["title"] for r in dana} == {"dana.md"}, "vector search crossed slices"


@requires_vec
async def test_dimension_mismatch_is_refused(vdb):
    """vec0 is fixed-width; mixing dimensions would yield meaningless distances."""
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x", "y"], persona_name="A")
    pending = await vdb.chunks_missing_vectors("r1")
    await vdb.store_chunk_vectors("r1", [(pending[0]["chunk_id"], [1.0, 0.0])], "m1")
    with pytest.raises(ValueError, match="fixed-width"):
        await vdb.store_chunk_vectors("r1", [(pending[1]["chunk_id"], [1.0, 0.0, 0.0])], "m2")


@requires_vec
async def test_query_vector_of_wrong_dimension_returns_nothing(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    pending = await vdb.chunks_missing_vectors("r1")
    await vdb.store_chunk_vectors("r1", [(pending[0]["chunk_id"], [1.0, 0.0])], "m")
    assert await vdb.vector_search("r1", [1.0, 0.0, 0.0], "A", k=5) == []


@requires_vec
async def test_embedding_model_recorded_and_reembedding_is_idempotent(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    cid = (await vdb.chunks_missing_vectors("r1"))[0]["chunk_id"]
    await vdb.store_chunk_vectors("r1", [(cid, [1.0, 0.0])], "m")
    await vdb.store_chunk_vectors("r1", [(cid, [0.0, 1.0])], "m")
    assert await vdb.embedding_model() == "m"
    assert await vdb.count_chunk_vectors("r1") == 1, "re-embedding duplicated a vector"
    assert await vdb.chunks_missing_vectors("r1") == []


# --------------------------------------------------------------------------
# embed_pending_chunks + graceful degradation
# --------------------------------------------------------------------------


@requires_vec
async def test_embed_pending_chunks_is_resumable(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md", chunks=["one", "two", "three"], persona_name="A"
    )
    with patch("litellm.aembedding", side_effect=_fake_embedding((1.0, 0.0))), \
         patch("litellm.completion_cost", return_value=0.0):
        first = await embed_pending_chunks(vdb, "r1", batch=2)
        assert first["embedded"] == 2
        second = await embed_pending_chunks(vdb, "r1")
        assert second["embedded"] == 1, "did not resume from where it stopped"
        third = await embed_pending_chunks(vdb, "r1")
        assert third["embedded"] == 0, "re-embedded already-embedded chunks"


async def test_embed_pending_chunks_reports_error_when_vec_unavailable(vdb, monkeypatch):
    monkeypatch.setattr(vdb, "_vec_available", False)
    stats = await embed_pending_chunks(vdb, "r1")
    assert stats["embedded"] == 0
    assert "sqlite-vec" in stats["error"]


@requires_vec
async def test_embed_pending_chunks_reports_provider_failure(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("",))):
        stats = await embed_pending_chunks(vdb, "r1")
    assert stats["embedded"] == 0 and "error" in stats


@requires_vec
async def test_vector_mode_falls_back_to_lexical_when_embedding_fails(vdb):
    """A provider outage must degrade the turn, not blank the retrieval."""
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("",))):
        passages, query = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="vector",
        )
    assert passages, "vector-mode failure returned nothing instead of falling back"
    assert "egress" in query


async def test_vector_mode_without_the_extension_uses_lexical(vdb, monkeypatch):
    monkeypatch.setattr(vdb, "_vec_available", False)
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    passages, _ = await retrieve_for_turn(
        vdb, "r1", "A", "egress inspection evidence", conversation=[],
        k=3, max_chars=900, mode="hybrid",
    )
    assert passages, "hybrid without sqlite-vec should still retrieve lexically"


@requires_vec
async def test_hybrid_mode_returns_fused_results(vdb):
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=[
            "egress inspection provides auditable evidence",
            "token accounting measures the delta",
        ],
        persona_name="A",
    )
    with patch("litellm.aembedding", side_effect=_fake_embedding((1.0, 0.0))), \
         patch("litellm.completion_cost", return_value=0.0):
        await embed_pending_chunks(vdb, "r1")
        passages, query = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="hybrid",
        )
    assert passages
    # Fused scores are higher-is-better, so they are positive RRF values.
    assert all(p.score > 0 for p in passages)


async def test_embedding_result_dim_and_ok_count_with_all_failures():
    result = EmbeddingResult(vectors=[None, None])
    assert result.dim == 0 and result.ok_count == 0


@requires_vec
async def test_empty_embedding_model_resolves_to_the_default(vdb):
    """Regression: "" means "use the default", not "pass an empty model name".

    Caught by a live run — passing "" to litellm raises "LLM Provider NOT
    provided", and because the failure path falls back to lexical, vector mode
    was silently inert while looking like it worked.
    """
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    from matrix_studio.embeddings import DEFAULT_EMBEDDING_MODEL

    seen = []

    async def capture(model=None, input=None, **kwargs):
        seen.append(model)
        return _Emb((1.0, 0.0))

    await vdb.add_document(
        run_id="r1", title="a.md", chunks=["egress inspection evidence"],
        persona_name="A",
    )
    with patch("litellm.aembedding", side_effect=capture), \
         patch("litellm.completion_cost", return_value=0.0):
        await embed_pending_chunks(vdb, "r1", embedding_model="")
        await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection", conversation=[],
            k=2, max_chars=900, mode="vector", embedding_model="",
        )
    assert seen, "no embedding call was made"
    assert all(m == DEFAULT_EMBEDDING_MODEL for m in seen), seen
    assert "" not in seen
