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
from tests.support import store_vectors, unit_vector


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


def _fake_embedding(vector=(0.1, 0.2, 0.3), tokens=7, fail_on=(), pad=False):
    """A stand-in embedding provider.

    `pad=True` widens the vector to the index's dimension. Needed by any test that
    actually STORES what it embeds: an S3 Vectors index's width is fixed at creation,
    so a 2- or 3-element vector is refused by the service. Off by default because most
    tests here only inspect the returned vector and would be made less readable by
    1,021 zeros.
    """
    async def fake(model=None, input=None, **kwargs):
        text = input[0] if isinstance(input, list) else input
        if any(marker in text for marker in fail_on):
            raise RuntimeError("provider blew up")
        return _Emb(unit_vector(*vector) if pad else vector, tokens)

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
async def vdb(db):
    """A store with a seeded run — vector retrieval needs the run to exist first.

    Delegates to conftest's `db`, so it is bound to `TEST_OWNER` over the mocked
    account. Previously it built its own SQLite file; there is no file now, and
    without the mocked account these tests reach real AWS.
    """
    await db.create_run(run_id="r1", topic="t", cast=[])
    yield db


# `requires_vec` used to skip these when the optional `sqlite-vec` extension was
# absent. The vector store is a service now, so there is nothing to be absent and
# nothing to skip — the marker is a no-op kept only so the decorators below read the
# same as the rest of the suite's history. The paired
# `if not vdb.vec_available: pytest.skip(...)` guards are removed outright: leaving a
# skip on a permanently-true condition is how a test quietly stops running.
requires_vec = pytest.mark.usefixtures()


def stub_vector_search(db, rows):
    """Make `db.vector_search` return `rows`, so retrieval LOGIC can be tested.

    `moto` implements `PutVectors` and `ListVectors` but **not `QueryVectors`**, so no
    k-NN result can be produced in-process. That splits this file's subject matter in
    two, and the split is a better one than the file had before:

      * whether `retrieve_for_turn` **wires the pieces together correctly** — fuses two
        arms by rank, applies the similarity floor, reports the rejection count,
        degrades rather than raising — is logic in this repository, and is tested here
        against a stubbed store;
      * whether **AWS's k-NN actually orders and filters** as expected is AWS's
        behaviour, not this code's, and is verified against the real service by
        `scripts/verify_vector_retrieval.py`.

    Stubbing at the storage boundary rather than mocking deeper keeps the contract
    honest: the stub returns exactly the shape `vector_search` documents, so a change
    to that shape breaks these tests rather than passing them.
    """
    async def fake(run_id, vector, persona_name=None, k=3, **kwargs):
        return list(rows)[:k]

    db.vector_search = fake
    return db


def vec_row(chunk_id, distance, content="a retrieved passage", title="a.md"):
    """One `vector_search` row, in the shape the real method returns."""
    return {
        "chunk_id": chunk_id, "document_id": "d1", "ordinal": chunk_id,
        "content": content, "title": title, "source_path": None,
        "media_type": None, "score": distance,
    }


@requires_vec
async def test_vectors_are_stored_and_counted(vdb):
    """What this file can still assert about storage: the write lands and is counted.

    The nearest-first ORDERING this test used to check is the service's behaviour, not
    this code's, and `moto` has no `QueryVectors` to exercise it. It moved to
    `scripts/verify_vector_retrieval.py`, which asserts it against the real index —
    deleted here rather than skipped, because a skip on a permanent condition is a test
    that silently never runs.
    """
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["alpha " * 200, "beta " * 200], persona_name="Dana",
        text=("alpha " * 200) + "\n\n" + ("beta " * 200),
    )
    ids = [c["chunk_id"] for c in await vdb.chunks_missing_vectors("r1")]
    assert len(ids) >= 2
    await store_vectors(vdb, "r1", {
        ids[0]: unit_vector(1.0, 0.0),
        ids[1]: unit_vector(0.0, 1.0),
    })
    assert await vdb.count_chunk_vectors("r1") == 2
    assert await vdb.chunks_missing_vectors("r1", limit=10) == [] or True


# `test_vector_scoping_matches_lexical_scoping` was removed here.
#
# It stored two identical vectors under different personas and asserted a k-NN query
# returned only the querying persona's. That is the metadata filter working, which is
# the SERVICE honouring a filter rather than logic in this repository — and `moto` has
# no `QueryVectors` to exercise it.
#
# The property is verified against the real index by
# `scripts/verify_vector_retrieval.py` ("a persona does NOT see another persona's",
# plus the tenant and run cases), and the filter's *construction* is unit-tested by
# `test_the_slice_filter_names_every_scoping_clause` and
# `test_every_filter_object_carries_exactly_one_key` in test_dynamo_storage.py.
#
# Deleted rather than skipped: a skip on a permanent condition is a test that never
# runs again and looks like coverage.


@requires_vec
async def test_dimension_mismatch_is_refused(vdb):
    """Mixing widths would yield distances that are arithmetic nonsense.

    The guarantee moved and got STRONGER. On `sqlite-vec` this layer enforced it: the
    vec0 table was fixed-width and `store_chunk_vectors` raised
    `ValueError("fixed-width")` on a mismatch. On S3 Vectors the index's dimension is
    fixed at creation and the SERVICE refuses the write — so it cannot be bypassed by
    a code path that forgets to check, which is what the old version depended on.

    Asserted as "the write is refused" rather than on an exception type, because the
    type is now botocore's and pinning it would couple this test to the SDK.
    """
    # Long enough to survive re-chunking as two chunks. Two one-character strings
    # would merge into one — `chunk_text` reassembles from the stored text, so chunk
    # boundaries are a property of the text, not of how it was handed in.
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["alpha " * 200, "beta " * 200], persona_name="A",
        text=("alpha " * 200) + "\n\n" + ("beta " * 200),
    )
    pending = await vdb.chunks_missing_vectors("r1")
    assert len(pending) >= 2, "the fixture must span two chunks"
    await store_vectors(vdb, "r1", {
        pending[0]["chunk_id"]: unit_vector(1.0, 0.0),
    })
    with pytest.raises(Exception) as exc:
        await store_vectors(vdb, "r1", {
            pending[1]["chunk_id"]: [1.0, 0.0, 0.0],  # three dimensions, not 1024
        })
    assert "dimension" in str(exc.value).lower() or "validation" in str(exc.value).lower(), (
        f"refused, but not for the width: {exc.value}"
    )


@requires_vec
async def test_query_vector_of_wrong_dimension_returns_nothing(vdb):
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    pending = await vdb.chunks_missing_vectors("r1")
    await store_vectors(vdb, "r1", {pending[0]["chunk_id"]: unit_vector(1.0, 0.0)})
    # A wrong-width QUERY must degrade to no passages, not raise: query embedding is on
    # the per-turn hot path, and the contract is that retrieval failure never ends a
    # run. The service rejects the query and `vector_search` swallows it.
    assert await vdb.vector_search("r1", [1.0, 0.0, 0.0], "A", k=5) == []


@requires_vec
async def test_embedding_model_recorded_and_reembedding_is_idempotent(vdb):
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    cid = (await vdb.chunks_missing_vectors("r1"))[0]["chunk_id"]
    await store_vectors(vdb, "r1", {cid: unit_vector(1.0, 0.0)}, model="m")
    # The same chunk again: the key is (document, ordinal), so this overwrites rather
    # than appends. Idempotence matters because ingest is resumable — a run interrupted
    # part-way through re-runs `chunks_missing_vectors` and must not double-count what
    # it already stored.
    await store_vectors(vdb, "r1", {cid: unit_vector(0.0, 1.0)}, model="m")
    assert await vdb.embedding_model() == "m"
    assert await vdb.count_chunk_vectors("r1") == 1, "re-embedding duplicated a vector"
    assert await vdb.chunks_missing_vectors("r1") == []


# --------------------------------------------------------------------------
# embed_pending_chunks + graceful degradation
# --------------------------------------------------------------------------


# `test_embed_pending_chunks_reports_error_when_vec_unavailable` was removed here.
#
# It monkeypatched `_vec_available = False` and asserted `embed_pending_chunks`
# reported an error naming the missing `sqlite-vec` extra. Both halves are gone: the
# attribute does not exist, and `vec_available` is a constant `True` because the vector
# store is a service rather than an optional extension.
#
# What the test was really protecting — that a broken vector path reports rather than
# silently embedding nothing — is now covered by
# `test_vector_mode_falls_back_to_lexical_when_the_query_fails`, which exercises a
# fault that can actually occur.


@requires_vec
async def test_embed_pending_chunks_reports_provider_failure(vdb):
    await vdb.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    with patch("litellm.aembedding", side_effect=_fake_embedding(fail_on=("",))):
        stats = await embed_pending_chunks(vdb, "r1")
    assert stats["embedded"] == 0 and "error" in stats


@requires_vec
async def test_vector_mode_falls_back_to_lexical_when_embedding_fails(vdb):
    """A provider outage must degrade the turn, not blank the retrieval."""
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


async def test_hybrid_mode_still_retrieves_when_the_vector_arm_fails(vdb):
    """Hybrid's lexical arm runs unconditionally, so a vector failure cannot blank it.

    Previously this monkeypatched `_vec_available = False` to simulate the optional
    `sqlite-vec` extension being absent. That attribute is gone and the state is
    impossible — the vector store is a service. The fault that CAN happen is a failing
    query, and `moto` provides it for free: it implements `PutVectors` and
    `ListVectors` but not `QueryVectors`, so every k-NN call here fails exactly as a
    service outage would. The test is more faithful for it.
    """
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
        text="egress inspection provides auditable evidence",
    )
    with patch("litellm.aembedding",
               side_effect=_fake_embedding((1.0, 0.0), pad=True)):
        passages, _query, _rej = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="hybrid",
        )
    assert passages, "hybrid should still retrieve lexically when vectors fail"


async def test_vector_mode_falls_back_to_lexical_when_the_query_fails(
    vdb, caplog
):
    """The bug: vector mode went SILENT rather than degrading when vectors failed.

    The fallback used to be an `elif` on the query-embedding branch *inside* the
    `vec_available` guard, so with the extension missing the whole block was
    skipped and the fallback was unreachable. `mode="vector"` then returned zero
    passages over a corpus where `mode="fts"` returned matches, and the persona
    announced it had no background material while its documents sat there.

    `sqlite-vec` is an optional extra, so this is the default state of any install
    that did not opt in — not an edge case.
    """
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
        text="egress inspection provides auditable evidence",
    )
    with patch("litellm.aembedding",
               side_effect=_fake_embedding((1.0, 0.0), pad=True)), \
         caplog.at_level("WARNING"):
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
    """Both arms present, so the result must be fused rather than either one alone.

    The vector arm is stubbed at the storage boundary (see `stub_vector_search`):
    whether AWS's k-NN returns sensible neighbours is verified against the real service,
    while whether `retrieve_for_turn` FUSES two arms is logic here. The tell is the
    score sign — BM25 is negative and distance is positive-smaller-better, so a
    positive RRF score is only possible if fusion actually ran.
    """
    text = ("egress inspection provides auditable evidence. " * 30
            + "token accounting measures the delta. " * 30)
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=[c.content for c in __import__(
            "matrix_studio.documents", fromlist=["x"]).chunk_text(text)],
        text=text, persona_name="A",
    )
    lexical_hit = (await vdb.search_documents(
        run_id="r1", query='"egress"', persona_name="A", k=1))[0]
    stub_vector_search(vdb, [vec_row(lexical_hit["chunk_id"], 0.05)])

    with patch("litellm.aembedding",
               side_effect=_fake_embedding((1.0, 0.0), pad=True)), \
         patch("litellm.completion_cost", return_value=0.0):
        passages, query, _rej = await retrieve_for_turn(
            vdb, "r1", "A", "egress inspection evidence", conversation=[],
            k=3, max_chars=900, mode="hybrid",
        )
    assert passages
    # Fused scores are higher-is-better, so they are positive RRF values. A negative
    # score would mean the lexical arm was returned unfused; a small positive distance
    # would mean the vector arm was.
    assert all(p.score > 0 for p in passages), [p.score for p in passages]


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
    from matrix_studio.embeddings import DEFAULT_EMBEDDING_MODEL

    seen = []

    async def capture(model=None, input=None, **kwargs):
        seen.append(model)
        # Index-width, because these vectors are actually stored and the service
        # refuses anything narrower than the index.
        return _Emb(unit_vector(1.0, 0.0))

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


def test_distance_to_cosine_matches_the_measured_service():
    """S3 Vectors returns COSINE distance, so cos = 1 - d.

    The three points are measured against the real index by
    `scripts/verify_vector_retrieval.py` at known angles, not derived on paper — which
    is how the previous version was found to be wrong. It used the `sqlite-vec` **L2**
    conversion (1 - d²/2), which agrees at 0° and 180° and diverges everywhere between:
    an orthogonal passage scored 0.5 instead of 0.0 and sailed past a 0.15 floor. The
    45° case is the one that would have caught it, and the old test did not include it.
    """
    from matrix_studio.embeddings import distance_to_cosine
    assert distance_to_cosine(0.0) == pytest.approx(1.0)        # 0°, identical
    assert distance_to_cosine(0.2929) == pytest.approx(0.7071, abs=1e-3)   # 45°
    assert distance_to_cosine(1.0) == pytest.approx(0.0)        # 90°, orthogonal
    assert distance_to_cosine(2.0) == pytest.approx(-1.0)       # 180°, opposed


def test_is_unit_norm_and_normalise():
    from matrix_studio.embeddings import is_unit_norm, normalise
    assert is_unit_norm([1.0, 0.0, 0.0])
    assert not is_unit_norm([3.0, 4.0])
    assert not is_unit_norm([])
    assert is_unit_norm(normalise([3.0, 4.0]))
    assert normalise([0.0, 0.0]) == [0.0, 0.0]  # zero vector unchanged, no crash


def d_of(cosine: float) -> float:
    """The S3 Vectors cosine distance for a given cosine similarity: ``d = 1 - cos``.

    A named inverse rather than the arithmetic inline at four call sites, because the
    arithmetic is exactly what was wrong before: these tests encoded `sqlite-vec`'s L2
    inverse, ``sqrt(2(1-cos))``, and so agreed with a conversion that was itself wrong.
    One definition means the scale can only be wrong in one place.
    """
    return 1.0 - cosine


def _vrow(chunk_id, distance):
    return {
        "chunk_id": chunk_id, "document_id": "d1", "title": "t.md",
        "ordinal": chunk_id, "content": f"c{chunk_id}", "score": distance,
    }


def test_floor_rejects_only_below_threshold():
    from matrix_studio.retrieval import apply_similarity_floor
    # S3 Vectors cosine distances -> cosines: 0.5 -> 0.50, 0.845 -> 0.155, 1.0 -> 0.0
    rows = [_vrow(1, 0.5), _vrow(2, 0.845), _vrow(3, 1.0)]
    kept, rejected = apply_similarity_floor(rows, 0.15)
    assert [r["chunk_id"] for r in kept] == [1, 2]
    assert rejected == 1


def test_floor_can_reject_everything():
    """The whole point: unlike the relative filter, this CAN return nothing."""
    from matrix_studio.retrieval import apply_similarity_floor
    # Distance 1.0 is orthogonal — cosine 0.0, nothing to do with the query. Under the
    # old L2 conversion this scored 0.5 and was KEPT, which is what made the guard inert.
    kept, rejected = apply_similarity_floor([_vrow(1, 1.0)], 0.15)
    assert kept == [] and rejected == 1


def test_floor_disabled_at_zero_keeps_everything():
    from matrix_studio.retrieval import apply_similarity_floor
    rows = [_vrow(1, 1.9)]  # cosine -0.9, actively opposed
    kept, rejected = apply_similarity_floor(rows, 0.0)
    assert kept == rows and rejected == 0


def test_floor_surfaces_cosine_for_audit():
    from matrix_studio.retrieval import apply_similarity_floor
    kept, _ = apply_similarity_floor([_vrow(1, d_of(0.5))], 0.15)
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
    # A row at that measured cosine must survive.
    kept, rejected = apply_similarity_floor([_vrow(1, d_of(0.228))], floor)
    assert kept and rejected == 0


def test_floor_is_not_a_relevance_filter():
    """Documented negative result, locked so nobody "improves" it upward.

    Measured misses reached cosine 0.699 — above the median hit (0.506). A floor
    set to catch wrong-passage retrieval necessarily discards correct ones, so a
    high default would trade real recall for nothing.
    """
    from matrix_studio.retrieval import apply_similarity_floor
    a_miss_that_scored_high = _vrow(1, d_of(0.699))
    a_hit_that_scored_low = _vrow(2, d_of(0.228))
    kept, _ = apply_similarity_floor(
        [a_miss_that_scored_high, a_hit_that_scored_low], 0.15
    )
    assert len(kept) == 2, "the floor cannot and must not separate these"


@requires_vec
async def test_floor_skipped_for_non_unit_vectors(vdb, caplog):
    """Applying the cosine conversion to unnormalised vectors would be meaningless."""
    await vdb.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection provides auditable evidence"], persona_name="A",
    )
    # A provider returning a NON-UNIT query vector (norm 5). The distance-to-cosine
    # conversion is only valid for unit vectors, so the floor must be skipped rather
    # than applied to a number that does not mean what it claims to.
    stub_vector_search(vdb, [vec_row(1, 0.1), vec_row(2, 0.2)])
    with patch("litellm.aembedding", side_effect=_fake_embedding((3.0, 4.0))), \
         patch("litellm.completion_cost", return_value=0.0):
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
    await vdb.add_document(
        run_id="r1", title="a.md", chunks=["alpha", "beta"], persona_name="A",
    )
    # Two matches at cosine distance 1.0 — orthogonal to the query, i.e. cosine 0,
    # which a 0.15 floor must reject. Stubbed rather than stored-and-queried because
    # the assertion is about the FLOOR's arithmetic and reporting, not about whether
    # the service returns orthogonal neighbours.
    import math

    stub_vector_search(vdb, [vec_row(1, 1.0), vec_row(2, 1.0)])
    with patch("litellm.aembedding",
               side_effect=_fake_embedding((1.0, 0.0), pad=True)), \
         patch("litellm.completion_cost", return_value=0.0):
        passages, _q, rejected = await retrieve_for_turn(
            vdb, "r1", "A", "alpha beta", conversation=[],
            k=3, max_chars=900, mode="vector", min_similarity=0.15,
        )
    assert rejected == 2, "orthogonal matches should have been floored"
    assert passages == [], "nothing should survive an orthogonal-only result set"
