# SPDX-License-Identifier: Apache-2.0
"""Shared test constants.

Its own module rather than living in `conftest.py`, because a test that needs one of
these has to *import* it, and importing `conftest` directly is both discouraged and
unreliable (it is loaded by pytest as a plugin, not as a module on the path).
"""

TEST_TABLE_PREFIX = "matrix-studio-test"
TEST_DATA_BUCKET = "matrix-studio-test-data"
TEST_VECTOR_BUCKET = "matrix-studio-test-vectors"
TEST_VECTOR_INDEX = "matrix-studio-test-chunks"

#: Dimension of the test vector index. Matches production (1024, measured — see
#: docs/EMBEDDING-DIMENSION-MEASUREMENT.md), because an index's dimension is immutable
#: after creation and a test fixture that disagreed with the real one would let a
#: width bug through.
TEST_VECTOR_DIM = 1024

#: The owner every storage fixture is bound to.
#:
#: A real-looking sub rather than `LOCAL_USER_SUB`, deliberately: if a test passes only
#: because the storage layer fell back to the local single-user identity, that is a
#: hole the suite should not paper over.
TEST_OWNER = "sub-test-0000-1111"


def unit_vector(*components: float) -> list:
    """A unit-length vector of `TEST_VECTOR_DIM`, from its leading components.

    Two reasons this exists rather than tests writing `[1.0, 0.0]`:

    **Width.** An S3 Vectors index's dimension is fixed at creation, so a 2- or
    3-element vector is refused outright by the service. Every test vector has to be
    the index's width, and padding by hand at each call site is noise that hides what
    the test is actually about — which is usually the *direction*.

    **Unit length.** The index is cosine, and `retrieval.apply_similarity_floor`
    converts distance to cosine similarity only for unit vectors (`is_unit_norm`
    guards it and skips the floor otherwise). A non-unit fixture would silently take
    the "floor not applicable" branch, so a test meaning to exercise the floor would
    exercise its bypass instead.
    """
    import math

    vec = [0.0] * TEST_VECTOR_DIM
    for i, value in enumerate(components):
        vec[i] = float(value)
    norm = math.sqrt(sum(v * v for v in vec))
    if not norm:
        raise ValueError("a zero vector has no direction and cannot be normalised")
    return [v / norm for v in vec]


async def store_vectors(db, run_id: str, vectors: dict, model: str = "test-model"):
    """Store embeddings for a run, supplying the metadata the store requires.

    `vectors` maps chunk_id to the vector. The metadata — document, ordinal, text,
    persona — comes from `chunks_missing_vectors`, which is where the application gets
    it too.

    A helper rather than a `chunks=` argument spelled out per test, because the store
    REFUSES a vector with no `owner_sub`/`run_id` (an unscoped vector is returned to
    every tenant by a filtered query that cannot exclude what it cannot see). Making
    that easy to do correctly is better than making every test remember it.
    """
    pending = {c["chunk_id"]: c for c in await db.chunks_missing_vectors(run_id)}
    # Include already-embedded chunks too, so re-embedding an existing chunk works.
    for chunk_id in vectors:
        if chunk_id not in pending:
            for doc in await db.list_documents(run_id):
                from matrix_studio.documents import chunk_text

                text = await db.document_text(str(doc["id"]))
                for chunk in chunk_text(text):
                    if db.chunk_id_for(str(doc["id"]), chunk.ordinal) == chunk_id:
                        pending[chunk_id] = {
                            "chunk_id": chunk_id,
                            "document_id": str(doc["id"]),
                            "ordinal": chunk.ordinal,
                            "content": chunk.content,
                            "persona_name": doc.get("persona_name"),
                        }
    meta = {cid: pending[cid] for cid in vectors if cid in pending}
    return await db.store_chunk_vectors(
        run_id, list(vectors.items()), model, chunks=meta
    )
