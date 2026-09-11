# SPDX-License-Identifier: Apache-2.0
"""Phase 6 step 3: retrieval fanned out across per-KB indexes.

`docs/PHASE6-KB-DESIGN.md` §3. The claims tested here are that the fan-out loses no
recall, that merging is sound because the metric is cosine, that the similarity floor
applies once after the merge, and that a failed KB degrades visibly rather than silently.

**`moto` does not implement `QueryVectors`** — it has `CreateIndex`, `PutVectors` and
`ListVectors`, and a query escapes to the real service (which fails with an invalid
token, loudly, which is how this was noticed). So the fan-out tests stub `query_vectors`
on the CLIENT rather than going through moto. That is the right level anyway: everything
this step adds — the parallel fan-out, the cross-index merge, the trim, the partial-
failure reporting, the kb_id tagging — is exercised, while the service's own k-NN
behaviour is not something a mock could tell us about. That part is verified against the
real account by `scripts/verify_vector_retrieval.py`.

The `store_kb_vectors` tests below DO go through moto, because `PutVectors` and
`CreateIndex` are implemented.
"""

import pytest

from matrix_studio.storage import vectors as vecmod
from tests.support import TEST_OWNER, unit_vector

pytestmark = pytest.mark.asyncio


def _axis(i, n=8):
    v = [0.0] * n
    v[i] = 1.0
    return v


def _cosine_distance(a, b) -> float:
    """1 - cos, which is what S3 Vectors returns and what the merge sorts on."""
    dot = sum(x * y for x, y in zip(a, b))
    return 1.0 - dot


class FakeVectors:
    """A stand-in for the `s3vectors` client's query path.

    Ranks by real cosine distance over the vectors it was given, so the ORDERING the
    merge relies on is genuine rather than asserted into existence. Only `query_vectors`
    is faked; nothing else is called on this path.
    """

    def __init__(self):
        self.corpus: dict = {}
        self.broken: set = set()
        self.calls: list = []
        # Every filter the code sent. Recorded because the first version of this fake
        # IGNORED `filter` entirely — so the test for the phase's central mechanism
        # ("a grantee can read a shared KB") passed even with an `owner_sub` filter
        # added back, which would have broken sharing in production. The fake shared a
        # wrong premise with the code, which is the failure mode a mock is worst at.
        self.filters: list = []

    def add(self, index_name, doc_id, ordinal, text, vector, **metadata):
        entry = {
            "document_id": doc_id, "ordinal": ordinal, "text": text, "v": vector,
        }
        entry.update(metadata)
        self.corpus.setdefault(index_name, []).append(entry)

    @staticmethod
    def _matches(metadata, expr) -> bool:
        """Evaluate the subset of S3 Vectors filter syntax this code can emit.

        Flat equality and `$and`/`$or`, which is what `_slice_filter` builds. Enough to
        make an unwanted filter visibly exclude results rather than be ignored.
        """
        if not expr:
            return True
        if "$and" in expr:
            return all(FakeVectors._matches(metadata, e) for e in expr["$and"])
        if "$or" in expr:
            return any(FakeVectors._matches(metadata, e) for e in expr["$or"])
        return all(metadata.get(key) == value for key, value in expr.items())

    def query_vectors(self, **kwargs):
        index = kwargs["indexName"]
        self.calls.append(index)
        self.filters.append(kwargs.get("filter"))
        if index in self.broken:
            raise RuntimeError(f"index {index} is unavailable")
        if index not in self.corpus:
            raise RuntimeError(f"NotFoundException: no index {index}")
        query = kwargs["queryVector"]["float32"]
        hits = []
        for e in self.corpus[index]:
            metadata = {
                k: v for k, v in e.items() if k not in ("v",)
            }
            if not self._matches(metadata, kwargs.get("filter")):
                continue
            hits.append({
                "key": f"{e['document_id']}:{e['ordinal']}",
                "distance": _cosine_distance(query, e["v"]),
                "metadata": metadata,
            })
        hits.sort(key=lambda h: h["distance"])
        return {"vectors": hits[: kwargs["topK"]]}


async def _kb_with_chunks(db, fake, name, texts, *, owner=None, model="test-model"):
    """A KB whose (faked) index holds one vector per text, each along its own axis.

    Axis-aligned unit vectors make distances predictable: a query along axis i is at
    cosine 1 to text i and cosine 0 to every other, so ranking is decided by the test
    rather than by an embedder.
    """
    kb = await db.create_knowledge_base(name, owner_sub=owner or TEST_OWNER)
    index = vecmod.kb_index_name(kb["id"], db.table_prefix)
    for doc_id, ordinal, text, axis in texts:
        fake.add(index, doc_id, ordinal, text, unit_vector(*axis))
    _ = model  # the model is recorded by store_kb_vectors, tested separately
    return kb


@pytest.fixture
def fake(db, monkeypatch):
    """Route `query_vectors` to the fake, leaving every other call on the real client."""
    f = FakeVectors()
    real = db._vectors_client()

    class Routed:
        def __getattr__(self, name):
            if name == "query_vectors":
                return f.query_vectors
            return getattr(real, name)

    monkeypatch.setattr(db, "_vectors_client", lambda: Routed())
    return f


async def test_fan_out_returns_passages_from_every_bound_kb(db, fake):
    a = await _kb_with_chunks(db, fake, "a", [("doc-a", 0, "alpha text", _axis(0))])
    b = await _kb_with_chunks(db, fake, "b", [("doc-b", 0, "beta text", _axis(1))])

    # A query between the two axes is close to both.
    query = unit_vector(1.0, 1.0)
    rows, failed = await db.vector_search_kbs(query, [a["id"], b["id"]], k=5)
    assert failed == []
    assert {r["kb_id"] for r in rows} == {a["id"], b["id"]}
    assert {r["content"] for r in rows} == {"alpha text", "beta text"}


async def test_the_merge_returns_the_true_global_top_k(db, fake):
    """Querying each index for topK=k and merging is EXACT, not an approximation.

    Any vector in the global top-k is also in its own index's top-k, so the union
    contains it. Asserted by construction: the nearest passage lives in the SECOND KB, so
    a merge that favoured the first, or trimmed per index, would rank it wrong.
    """
    a = await _kb_with_chunks(db, fake, "a", [
        ("doc-a", 0, "far", _axis(5)),
        ("doc-a", 1, "farther", _axis(6)),
    ])
    b = await _kb_with_chunks(db, fake, "b", [("doc-b", 0, "nearest", _axis(0))])

    rows, _ = await db.vector_search_kbs(unit_vector(1.0), [a["id"], b["id"]], k=3)
    assert rows[0]["content"] == "nearest", [r["content"] for r in rows]
    assert rows[0]["kb_id"] == b["id"]
    # And distances are ordered, which is what makes a cross-index merge a plain sort.
    assert [r["score"] for r in rows] == sorted(r["score"] for r in rows)


async def test_the_merge_trims_to_k_across_indexes_not_per_index(db, fake):
    """k is a budget for the TURN, not per collection.

    Trimming per index would give a persona bound to three KBs three times the passages
    and three times the prompt cost — silently blowing the max_chars budget the whole
    feature exists to enforce.
    """
    a = await _kb_with_chunks(db, fake, "a", [
        ("doc-a", i, f"a{i}", _axis(i)) for i in range(3)
    ])
    b = await _kb_with_chunks(db, fake, "b", [
        ("doc-b", i, f"b{i}", _axis(i + 3)) for i in range(3)
    ])
    rows, _ = await db.vector_search_kbs(unit_vector(1.0), [a["id"], b["id"]], k=2)
    assert len(rows) == 2, f"expected 2 passages total, got {len(rows)}"


async def test_a_failing_kb_yields_partial_results_and_is_reported(db, fake, monkeypatch):
    """One KB being unavailable must not lose the others — but silence would be worse.

    The merged top-k is then drawn from a smaller pool, so a passage that would have
    ranked first is absent and something worse takes its place. That is the "confidently
    irrelevant passage" hazard, so the failure is RETURNED rather than only logged.
    """
    a = await _kb_with_chunks(db, fake, "a", [("doc-a", 0, "alpha", _axis(0))])
    rows, failed = await db.vector_search_kbs(
        unit_vector(1.0), [a["id"], "kb-that-has-no-index"], k=5
    )
    assert [r["content"] for r in rows] == ["alpha"]
    assert failed == ["kb-that-has-no-index"], (
        "a failed KB must be reported so the caller can put it in the event log"
    )


async def test_nothing_bound_means_no_query_at_all(db):
    rows, failed = await db.vector_search_kbs(unit_vector(1.0), [], k=3)
    assert rows == [] and failed == []


async def test_a_missing_vector_bucket_reports_every_kb_as_failed(db, fake, monkeypatch):
    """Not silently empty: an empty corpus and a misconfigured bucket look identical to
    a reader, and only one of them is the operator's problem."""
    a = await _kb_with_chunks(db, fake, "a", [("doc-a", 0, "alpha", _axis(0))])
    monkeypatch.setenv("VECTOR_BUCKET", "")
    rows, failed = await db.vector_search_kbs(unit_vector(1.0), [a["id"]], k=3)
    assert rows == []
    assert failed == [a["id"]]


async def test_titles_are_supplied_by_the_caller_not_looked_up_per_run(db, fake):
    """A document belongs to a KB now, not a run, so `list_documents(run_id)` is the
    wrong lookup. Passing the mapping keeps this method independent of how documents
    are scoped — which is what step 4 changes."""
    a = await _kb_with_chunks(db, fake, "a", [("doc-a", 0, "alpha", _axis(0))])
    rows, _ = await db.vector_search_kbs(
        unit_vector(1.0), [a["id"]], k=3, titles={"doc-a": "policy.md"}
    )
    assert rows[0]["title"] == "policy.md"
    # Absent a title, the document id is shown rather than an empty string — a citation
    # with no label is worse than an ugly one.
    rows, _ = await db.vector_search_kbs(unit_vector(1.0), [a["id"]], k=3)
    assert rows[0]["title"] == "doc-a"


async def test_no_owner_filter_so_a_grantee_can_read_a_shared_kb(db, fake):
    """THE test for the phase's central mechanism.

    A shared KB's vectors carry the KB OWNER's sub. `_slice_filter` pins queries to the
    caller's own `owner_sub`, which with one shared index IS the isolation boundary — and
    here would return nothing from every KB a user was granted. Authorisation happens in
    `searchable_kbs` instead.
    """
    shared = await _kb_with_chunks(
        db, fake, "theirs", [("doc-x", 0, "their passage", _axis(0))], owner="someone-else",
    )
    await db.grant_kb(shared["id"], user=TEST_OWNER)
    permitted = await db.searchable_kbs([shared["id"]], TEST_OWNER)
    assert permitted == [shared["id"]]

    rows, failed = await db.vector_search_kbs(unit_vector(1.0), permitted, k=3)
    assert failed == []
    assert [r["content"] for r in rows] == ["their passage"], (
        "a grantee read nothing from a KB they are permitted to read — the owner filter "
        "is still being applied"
    )
    # And asserted directly on what was SENT, because the behavioural assertion above is
    # only as good as the fake's filter evaluation. Two independent checks of the same
    # fact, which is what it takes when a mock is in the loop.
    assert fake.filters == [None], (
        f"no filter should be sent to a per-KB index; sent {fake.filters}"
    )


async def test_the_floor_is_applied_once_after_the_merge(db, fake):
    """Per index would apply a GLOBAL threshold to a LOCAL candidate set, so the answer
    would depend on how documents happened to be grouped into collections."""
    from matrix_studio.embeddings import distance_to_cosine
    from matrix_studio.retrieval import apply_similarity_floor

    near = await _kb_with_chunks(db, fake, "near", [("doc-n", 0, "on topic", _axis(0))])
    far = await _kb_with_chunks(db, fake, "far", [("doc-f", 0, "off topic", _axis(7))])

    rows, _ = await db.vector_search_kbs(
        unit_vector(1.0), [near["id"], far["id"]], k=5
    )
    assert len(rows) == 2
    # The far passage is orthogonal, so cosine 0 — below any positive floor.
    assert distance_to_cosine(rows[-1]["score"]) < 0.15
    kept, rejected = apply_similarity_floor(rows, 0.15)
    assert [r["content"] for r in kept] == ["on topic"]
    assert rejected == 1


# --------------------------------------------------------------------------- #
# store_kb_vectors — through moto, which DOES implement CreateIndex and PutVectors
# --------------------------------------------------------------------------- #


async def _stored(db, name, texts, *, model="test-model", owner=None, kb=None):
    """Really write vectors into a KB's index. No fake — this path works under moto."""
    kb = kb or await db.create_knowledge_base(name, owner_sub=owner or TEST_OWNER)
    pairs, chunks = [], {}
    for doc_id, ordinal, text, axis in texts:
        chunk_id = db.chunk_id_for(doc_id, ordinal)
        pairs.append((chunk_id, unit_vector(*axis)))
        chunks[chunk_id] = {
            "document_id": doc_id, "ordinal": ordinal, "content": text,
        }
    await db.store_kb_vectors(kb["id"], pairs, model, chunks)
    return kb


async def test_storing_creates_the_kb_index_through_the_factory(db):
    """An index is never created at a call site — its dimension and metric can never
    be changed, so one factory supplies them."""
    import boto3

    from tests.support import TEST_VECTOR_BUCKET

    kb = await _stored(db, "k", [("doc-a", 0, "text", _axis(0))])
    name = vecmod.kb_index_name(kb["id"], db.table_prefix)
    client = boto3.client("s3vectors", region_name="us-east-1")
    got = client.get_index(vectorBucketName=TEST_VECTOR_BUCKET, indexName=name)["index"]
    assert got["dimension"] == vecmod.EMBEDDING_DIMENSION
    assert got["distanceMetric"] == vecmod.DISTANCE_METRIC


async def test_a_second_write_to_the_same_kb_reuses_its_index(db):
    """The factory is on the INGEST path, so a second upload must not fail."""
    kb = await _stored(db, "k", [("doc-a", 0, "one", _axis(0))])
    await _stored(db, "k", [("doc-a", 1, "two", _axis(1))], kb=kb)


async def test_the_model_is_recorded_on_the_kb_at_first_write(db):
    kb = await db.create_knowledge_base("k")
    assert kb["embedding_model"] is None
    await _stored(db, "k", [("doc-z", 0, "t", _axis(0))], model="titan-v2", kb=kb)
    assert (await db.get_knowledge_base(kb["id"]))["embedding_model"] == "titan-v2"


async def test_a_model_swap_within_one_kb_is_refused(db):
    """Per KB, which is what the global marker could not be: two KBs may legitimately
    hold different models, and only a query crossing them is a problem."""
    from matrix_studio.storage import StorageError

    kb = await _stored(db, "k", [("doc-a", 0, "t", _axis(0))], model="model-a")
    with pytest.raises(StorageError, match="indexed with 'model-a'"):
        await _stored(db, "k", [("doc-a", 1, "t2", _axis(1))], model="model-b", kb=kb)


async def test_two_kbs_may_hold_different_models(db):
    """The whole reason the marker moved off the global singleton."""
    a = await _stored(db, "a", [("d1", 0, "t", _axis(0))], model="model-a")
    b = await _stored(db, "b", [("d2", 0, "t", _axis(1))], model="model-b")
    assert (await db.get_knowledge_base(a["id"]))["embedding_model"] == "model-a"
    assert (await db.get_knowledge_base(b["id"]))["embedding_model"] == "model-b"


async def test_storing_into_a_missing_kb_is_refused(db):
    """A vector in an index whose KB does not exist is unreachable and unauthorised —
    `searchable_kbs` can never return that id, so nothing could ever read it."""
    from matrix_studio.storage import StorageError

    chunk_id = db.chunk_id_for("d", 0)
    with pytest.raises(StorageError, match="does not exist"):
        await db.store_kb_vectors(
            "no-such-kb", [(chunk_id, unit_vector(1.0))], "m",
            {chunk_id: {"document_id": "d", "ordinal": 0, "content": "t"}},
        )


async def test_a_chunk_without_metadata_is_refused(db):
    """A vector with no document and ordinal cannot be rendered or cited."""
    from matrix_studio.storage import StorageError

    kb = await db.create_knowledge_base("k")
    with pytest.raises(StorageError, match="no metadata for chunk"):
        await db.store_kb_vectors(kb["id"], [(12345, unit_vector(1.0))], "m", {})


async def test_a_shared_kbs_vectors_carry_the_owners_sub_for_audit(db):
    """Recorded, but never filtered on. Filtering would break sharing — a grantee would
    match none of a shared KB's vectors, because they carry the KB owner's sub."""
    import boto3

    from tests.support import TEST_VECTOR_BUCKET

    kb = await _stored(db, "theirs", [("d", 0, "t", _axis(0))], owner="someone-else")
    name = vecmod.kb_index_name(kb["id"], db.table_prefix)
    client = boto3.client("s3vectors", region_name="us-east-1")
    got = client.list_vectors(
        vectorBucketName=TEST_VECTOR_BUCKET, indexName=name, returnMetadata=True
    )["vectors"]
    assert got and got[0]["metadata"]["owner_sub"] == "someone-else"
    assert got[0]["metadata"]["kb_id"] == kb["id"]
