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
        passages, query, _rej = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="vector",
        )
    assert passages, "vector-mode failure returned nothing instead of falling back"
    assert "egress" in query


async def test_hybrid_mode_without_the_extension_uses_lexical(vdb, monkeypatch):
    """Renamed: this only ever exercised HYBRID, despite being named for vector.

    Hybrid was never at risk — its lexical arm runs unconditionally. The name
    claiming vector coverage is part of why the vector-mode hole below survived:
    a reader scanning test names would conclude the case was tested.
    """
    monkeypatch.setattr(vdb, "_vec_available", False)
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    passages, _query, _rej = await retrieve_for_turn(
        vdb, "r1", "A", "egress inspection evidence", conversation=[],
        k=3, max_chars=900, mode="hybrid",
    )
    assert passages, "hybrid without sqlite-vec should still retrieve lexically"


async def test_vector_mode_without_the_extension_falls_back_to_lexical(
    vdb, monkeypatch, caplog
):
    """The bug: vector mode went SILENT, not degraded, with sqlite-vec absent.

    The fallback used to be an `elif` on the query-embedding branch *inside* the
    `vec_available` guard, so with the extension missing the whole block was
    skipped and the fallback was unreachable. `mode="vector"` then returned zero
    passages over a corpus where `mode="fts"` returned matches, and the persona
    announced it had no background material while its documents sat there.

    `sqlite-vec` is an optional extra, so this is the default state of any install
    that did not opt in — not an edge case.
    """
    monkeypatch.setattr(vdb, "_vec_available", False)
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    with caplog.at_level("WARNING"):
        passages, query, _rej = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="vector",
        )
    assert passages, "vector mode without sqlite-vec returned nothing at all"
    assert "egress" in query
    # Degrading quietly is how this went unnoticed; the log has to say so.
    assert any("falling back to lexical" in r.message for r in caplog.records)


@requires_vec
async def test_vector_mode_with_unembedded_chunks_falls_back_to_lexical(vdb, caplog):
    """The other unreachable case: documents attached but never embedded.

    `vector_search` swallows its own errors (here: no `chunk_vec` table, because
    nothing has ever been stored) and returns no rows, which the old code could not
    distinguish from a legitimately empty result — so it returned nothing. KNN
    applies no score threshold, so an empty vector result never means "your query
    matched nothing"; it means the arm could not run, and lexical beats silence.
    """
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    # Deliberately no embed_pending_chunks call — that is the whole scenario.
    with patch("litellm.aembedding", side_effect=_fake_embedding((1.0, 0.0))), \
         caplog.at_level("WARNING"):
        passages, _query, _rej = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="vector",
        )
    assert passages, "unembedded chunks made vector mode return nothing"
    assert any("falling back to lexical" in r.message for r in caplog.records)


@requires_vec
async def test_fts_mode_is_unaffected_by_the_vector_fallback(vdb):
    """The fallback must not change fts mode, which never had the defect.

    Stated as its own test because the fix touches shared code above the mode
    branch, and `fts` is what every existing run uses.
    """
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    passages, _query, _rej = await retrieve_for_turn(
        vdb, "r1", "A", "egress inspection evidence", conversation=[],
        k=3, max_chars=900, mode="fts",
    )
    assert len(passages) == 1


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
        passages, query, _rej = await retrieve_for_turn(
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


# --------------------------------------------------------------------------
# Phase 5h: absolute similarity floor (off-topic guard)
# --------------------------------------------------------------------------


def test_distance_to_cosine_endpoints():
    from matrix_studio.embeddings import distance_to_cosine
    assert distance_to_cosine(0.0) == pytest.approx(1.0)       # identical
    assert distance_to_cosine(2 ** 0.5) == pytest.approx(0.0)  # orthogonal
    assert distance_to_cosine(2.0) == pytest.approx(-1.0)      # opposed


def test_is_unit_norm_and_normalise():
    from matrix_studio.embeddings import is_unit_norm, normalise
    assert is_unit_norm([1.0, 0.0, 0.0])
    assert not is_unit_norm([3.0, 4.0])
    assert not is_unit_norm([])
    assert is_unit_norm(normalise([3.0, 4.0]))
    assert normalise([0.0, 0.0]) == [0.0, 0.0]  # zero vector unchanged, no crash


def _vrow(chunk_id, distance):
    return {
        "chunk_id": chunk_id, "document_id": "d1", "title": "t.md",
        "ordinal": chunk_id, "content": f"c{chunk_id}", "score": distance,
    }


def test_floor_rejects_only_below_threshold():
    from matrix_studio.retrieval import apply_similarity_floor
    # distances -> cosines: 1.0 -> 0.50, 1.3 -> 0.155, 1.414 -> ~0.0
    rows = [_vrow(1, 1.0), _vrow(2, 1.3), _vrow(3, 1.4142)]
    kept, rejected = apply_similarity_floor(rows, 0.15)
    assert [r["chunk_id"] for r in kept] == [1, 2]
    assert rejected == 1


def test_floor_can_reject_everything():
    """The whole point: unlike the relative filter, this CAN return nothing."""
    from matrix_studio.retrieval import apply_similarity_floor
    kept, rejected = apply_similarity_floor([_vrow(1, 1.4142)], 0.15)
    assert kept == [] and rejected == 1


def test_floor_disabled_at_zero_keeps_everything():
    from matrix_studio.retrieval import apply_similarity_floor
    rows = [_vrow(1, 1.9)]
    kept, rejected = apply_similarity_floor(rows, 0.0)
    assert kept == rows and rejected == 0


def test_floor_surfaces_cosine_for_audit():
    from matrix_studio.retrieval import apply_similarity_floor
    kept, _ = apply_similarity_floor([_vrow(1, 1.0)], 0.15)
    assert kept[0]["cosine"] == pytest.approx(0.5, abs=1e-3)


def test_floor_on_empty_rows():
    from matrix_studio.retrieval import apply_similarity_floor
    assert apply_similarity_floor([], 0.15) == ([], 0)


def test_default_floor_admits_the_lowest_measured_genuine_hit():
    """Calibration: the weakest correct retrieval measured scored cosine 0.228.

    The default floor must sit below it with margin, or the guard would start
    discarding real hits — which is what the calibration showed no threshold can
    do safely (hits 0.228-0.870 overlap misses 0.166-0.699).
    """
    from matrix_studio.retrieval import apply_similarity_floor
    from matrix_studio.state import RetrievalConfig

    floor = RetrievalConfig().min_similarity
    assert floor == 0.15
    assert floor < 0.228, "default floor would reject the weakest measured hit"
    # A row at that measured cosine must survive. cos=0.228 -> d=sqrt(2*(1-.228))
    d = (2 * (1 - 0.228)) ** 0.5
    kept, rejected = apply_similarity_floor([_vrow(1, d)], floor)
    assert kept and rejected == 0


def test_floor_is_not_a_relevance_filter():
    """Documented negative result, locked so nobody "improves" it upward.

    Measured misses reached cosine 0.699 — above the median hit (0.506). A floor
    set to catch wrong-passage retrieval necessarily discards correct ones, so a
    high default would trade real recall for nothing.
    """
    from matrix_studio.retrieval import apply_similarity_floor
    d_of = lambda cos: (2 * (1 - cos)) ** 0.5
    a_miss_that_scored_high = _vrow(1, d_of(0.699))
    a_hit_that_scored_low = _vrow(2, d_of(0.228))
    kept, _ = apply_similarity_floor(
        [a_miss_that_scored_high, a_hit_that_scored_low], 0.15
    )
    assert len(kept) == 2, "the floor cannot and must not separate these"


@requires_vec
async def test_floor_skipped_for_non_unit_vectors(vdb, caplog):
    """Applying the cosine conversion to unnormalised vectors would be meaningless."""
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    # A provider returning a non-unit vector (norm 5).
    with patch("litellm.aembedding", side_effect=_fake_embedding((3.0, 4.0))), \
         patch("litellm.completion_cost", return_value=0.0):
        await embed_pending_chunks(vdb, "r1")
        with caplog.at_level("WARNING"):
            passages, _q, rejected = await retrieve_for_turn(
                vdb, "r1", "A", "egress inspection", conversation=[],
                k=3, max_chars=900, mode="vector", min_similarity=0.9,
            )
    # A 0.9 floor would reject everything if wrongly applied; it must be skipped.
    assert rejected == 0
    assert any("non-unit" in r.message for r in caplog.records)


@requires_vec
async def test_floor_rejection_is_reported_to_the_caller(vdb):
    """Also the guard on the other side of the lexical fallback.

    The `passages == []` assertion is load-bearing beyond floor reporting: it
    proves the vector→lexical fallback does NOT fire when the floor rejected the
    matches. "Matched, but only below the floor" is a decision the operator
    configured; reaching past it to lexical would quietly defeat the floor. A
    fallback written as "vector mode returned nothing, so try lexical" would pass
    every other test in this file and fail here.
    """
    if not vdb.vec_available:
        pytest.skip("sqlite-vec could not be loaded in this environment")
    await vdb.add_document(
        run_id="r1", title="a.md", chunks=["alpha", "beta"], persona_name="A",
    )
    pending = await vdb.chunks_missing_vectors("r1")
    # Store unit vectors orthogonal to the query, i.e. cosine ~0.
    await vdb.store_chunk_vectors(
        "r1",
        [(pending[0]["chunk_id"], [0.0, 1.0]), (pending[1]["chunk_id"], [0.0, 1.0])],
        "m",
    )
    with patch("litellm.aembedding", side_effect=_fake_embedding((1.0, 0.0))), \
         patch("litellm.completion_cost", return_value=0.0):
        passages, _q, rejected = await retrieve_for_turn(
            vdb, "r1", "A", "alpha beta", conversation=[],
            k=3, max_chars=900, mode="vector", min_similarity=0.15,
        )
    assert rejected == 2, "orthogonal matches should have been floored"
    assert passages == [], "nothing should survive an orthogonal-only result set"
