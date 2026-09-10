# SPDX-License-Identifier: Apache-2.0
"""Phase 5f: provider-agnostic text embeddings for document retrieval.

`docs/PHASE5-RETRIEVAL-MEASUREMENT.md` measured FTS5 at ~0.51 recall@5 under the
engine's real query shape and showed the two free query-side fixes made it worse.
That met the design doc's stated trigger for embeddings, so this module supplies
them.

Design constraints inherited from the project, not chosen here:

- **Provider-agnostic via LiteLLM**, exactly like generation. Any
  ``litellm.aembedding``-supported model works via config, so BYO-key holds and
  no new SDK enters the dependency tree.
- **No local model.** ``sentence-transformers`` would pull ``torch`` (hundreds of
  MB to GBs) and wreck the five-minute quickstart that ``PROJECT-SPEC.md`` §7
  pins down. Embeddings are an API call or they are nothing.
- **Never fatal.** An embedding failure degrades retrieval to FTS5 rather than
  ending a run. Retrieval is an enhancement; the simulation is the product.
- **Cost is visible.** Every call reports real token/cost so the spend shows up in
  the same meter as generation, which is what the cost gate demands.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# Titan Embed v2 emits 1024 floats by default but accepts 256 or 512. The width is worth
# choosing rather than defaulting to: it drives vector storage, query cost and latency, and
# on the AWS target an index's dimension is FIXED AT CREATION, so changing it later means
# rebuilding every index. The Well-Architected Generative AI Lens raises this twice
# (GENCOST04-BP01, GENPERF04-BP02). None = let the provider use its default.
TITAN_V2_DIMENSIONS = (256, 512, 1024)

# Amazon Titan Embed v2: 1024 dimensions and the cheapest of the Bedrock options
# measured on this project (~$1e-7 for a short input). Overridable per run.
DEFAULT_EMBEDDING_MODEL = "bedrock/amazon.titan-embed-text-v2:0"

# Bedrock's Titan embedding endpoint accepts a single string per call, so batching
# is done by issuing concurrent calls rather than by sending arrays. Kept modest
# to avoid throttling a shared account.
DEFAULT_BATCH_CONCURRENCY = 8


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be produced at all.

    Callers are expected to catch this and fall back to lexical retrieval, not to
    propagate it.
    """


@dataclass
class EmbeddingResult:
    """Vectors plus the real cost of producing them."""

    vectors: List[Optional[List[float]]] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""

    @property
    def dim(self) -> int:
        for v in self.vectors:
            if v:
                return len(v)
        return 0

    @property
    def ok_count(self) -> int:
        return sum(1 for v in self.vectors if v)


def is_unit_norm(vector: Sequence[float], tolerance: float = 0.01) -> bool:
    """Whether a vector is (near enough) unit length.

    This matters because ``distance_to_cosine`` below is only valid for unit
    vectors. Titan Embed v2 returns exactly unit-norm vectors (verified), but a
    different provider may not, and silently applying the conversion to
    non-normalised vectors would produce a meaningless similarity — and therefore
    a meaningless score floor.
    """
    if not vector:
        return False
    norm = sum(float(x) * float(x) for x in vector) ** 0.5
    return abs(norm - 1.0) <= tolerance


def normalise(vector: Sequence[float]) -> List[float]:
    """Scale a vector to unit length. A zero vector is returned unchanged."""
    norm = sum(float(x) * float(x) for x in vector) ** 0.5
    if norm <= 0:
        return [float(x) for x in vector]
    return [float(x) / norm for x in vector]


def distance_to_cosine(distance: float) -> float:
    """Convert a sqlite-vec L2 distance to cosine similarity.

    For UNIT vectors, ``|a-b|^2 = 2 - 2·cos``, so ``cos = 1 - d^2/2``. That gives
    an interpretable, provider-portable scale where 1.0 is identical, 0.0 is
    unrelated (orthogonal) and negative is actively opposed — which is what makes
    an absolute threshold meaningful, unlike a raw BM25 score.

    Only valid for unit-norm vectors; see ``is_unit_norm``.
    """
    return 1.0 - (distance * distance) / 2.0


def serialise(vector: Sequence[float]) -> bytes:
    """Pack a float vector into the little-endian float32 blob sqlite-vec expects."""
    return struct.pack(f"<{len(vector)}f", *(float(x) for x in vector))


def deserialise(blob: bytes) -> List[float]:
    """Unpack a sqlite-vec float32 blob back into a Python list."""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


async def embed_texts(
    texts: Sequence[str],
    model: str = DEFAULT_EMBEDDING_MODEL,
    concurrency: int = DEFAULT_BATCH_CONCURRENCY,
    dimensions: Optional[int] = None,
) -> EmbeddingResult:
    """Embed a list of texts, returning one vector per input (None where failed).

    Positional alignment with ``texts`` is guaranteed, so a caller can zip the
    result against its chunks without tracking which ones succeeded. A text that
    fails yields ``None`` in that slot rather than shifting everything after it.

    Raises ``EmbeddingError`` only if EVERY text failed, which is the signal that
    the provider or model is genuinely unusable rather than that one input was
    awkward.
    """
    import asyncio

    if not texts:
        return EmbeddingResult(vectors=[], model=model)

    import litellm

    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: List[Optional[List[float]]] = [None] * len(texts)
    tokens = 0
    cost = 0.0
    failures: List[str] = []

    async def one(index: int, text: str) -> None:
        nonlocal tokens, cost
        # An empty string is not embeddable and is not worth a call.
        if not text or not text.strip():
            return
        async with semaphore:
            try:
                # `dimensions` is only sent when asked for, so a provider that does
                # not accept it is unaffected — and litellm.drop_params would silently
                # discard it, which would look like the request worked at the wrong
                # width. Asserting the returned length below is what catches that.
                kwargs = {"dimensions": dimensions} if dimensions else {}
                resp = await litellm.aembedding(model=model, input=[text], **kwargs)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{type(exc).__name__}: {exc}")
                return
        try:
            vector = [float(x) for x in resp.data[0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            failures.append(f"malformed embedding response: {exc}")
            return
        # A provider that ignored `dimensions` would hand back its default width, and
        # every vector would then be silently wrong for the index it is destined for.
        # Fail loudly instead: an index's dimension cannot be changed after creation.
        if dimensions and len(vector) != dimensions:
            failures.append(
                f"asked for {dimensions} dimensions, got {len(vector)} — the provider "
                f"or model does not honour the parameter"
            )
            return
        results[index] = vector
        usage = getattr(resp, "usage", None)
        if usage is not None:
            tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        try:
            cost += litellm.completion_cost(resp) or 0.0
        except Exception:  # noqa: BLE001
            # Providers that do not report cost count as zero, matching the
            # engine's existing cost-cap semantics.
            pass

    await asyncio.gather(*(one(i, t) for i, t in enumerate(texts)))

    embedded = sum(1 for v in results if v)
    if embedded == 0:
        raise EmbeddingError(
            f"All {len(texts)} embedding call(s) failed with model {model!r}. "
            f"First error: {failures[0] if failures else 'unknown'}"
        )
    if failures:
        logger.warning(
            "Embedded %d/%d texts with %s; %d failed (first: %s)",
            embedded, len(texts), model, len(failures), failures[0],
        )
    return EmbeddingResult(
        vectors=results, tokens=tokens, cost_usd=cost, model=model
    )


async def embed_query(
    text: str,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: Optional[int] = None,
) -> Optional[EmbeddingResult]:
    """Embed a single query. Returns None on failure instead of raising.

    Query embedding sits on the per-turn hot path, so a provider blip must
    degrade that turn to lexical retrieval rather than surface an error.
    """
    try:
        result = await embed_texts(
            [text], model=model, concurrency=1, dimensions=dimensions
        )
    except EmbeddingError as exc:
        logger.warning("Query embedding failed, falling back to lexical: %s", exc)
        return None
    return result if result.vectors and result.vectors[0] else None
