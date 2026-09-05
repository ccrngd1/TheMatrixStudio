# SPDX-License-Identifier: Apache-2.0
"""Phase 5: document retrieval — query building and the per-call context budget.

This module is the layer between the conversation and the FTS5 index. It does two
jobs, both of which are correctness requirements rather than conveniences:

1. **Query sanitisation.** Raw conversation text is not a valid FTS5 query. It
   contains quotes, hyphens, colons and bare words that FTS5 reads as operators
   (``AND``, ``OR``, ``NOT``, ``NEAR``, ``*``, ``^``, ``-``, ``:``). Passing it
   through unmodified either raises a syntax error or silently changes what was
   asked for. Terms are therefore extracted, filtered and quoted individually.

2. **The character budget.** Enforcing a hard ceiling on retrieved text is the
   entire point of the feature: a forty-page document must contribute at most
   ``max_chars`` to any single call. Without this, retrieval just relocates the
   context-cost problem instead of solving it.

Pure functions except for the one DB read, so retrieval behaviour is testable
without a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# FTS5 bareword operators, plus conversational filler that adds no retrieval
# signal. Kept deliberately small: over-filtering throws away real query terms.
_FTS_OPERATORS = {"and", "or", "not", "near"}

_STOPWORDS = _FTS_OPERATORS | {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "for", "with", "from", "by", "as", "that",
    "this", "these", "those", "it", "its", "we", "you", "they", "i", "he",
    "she", "them", "us", "our", "your", "their", "my", "me", "him", "her",
    "do", "does", "did", "have", "has", "had", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "so", "if", "then",
    "than", "but", "about", "into", "over", "under", "just", "very", "really",
    "what", "which", "who", "when", "where", "why", "how", "all", "any",
    "both", "each", "more", "most", "some", "such", "only", "own", "same",
    "too", "here", "there", "now", "also", "because", "while", "not",
    "think", "know", "going", "need", "want", "like", "get", "got", "say",
    "said", "one", "two", "thing", "things", "lot", "much", "many",
    # Conversational filler verbs. These dominate BM25 queries built from live
    # dialogue and carry no retrieval signal, which dilutes the real terms.
    "look", "looks", "make", "makes", "made", "call", "calls", "let", "lets",
    "come", "comes", "put", "take", "takes", "give", "gives", "sure", "right",
    "actually", "basically", "mean", "means", "point", "way", "something",
    "anything", "everything", "nothing", "someone", "anyone",
}

# Contractions survive tokenisation as single words (``doesn't``, ``it's``) and so
# slip past the stopword set unless listed explicitly. Observed polluting real
# queries in a live run, where "doesn't" and "it's" were being sent to FTS5.
_CONTRACTIONS = {
    "i'm", "i've", "i'd", "i'll", "it's", "that's", "there's", "here's",
    "what's", "let's", "we're", "we've", "we'd", "we'll", "you're", "you've",
    "you'd", "you'll", "they're", "they've", "they'd", "they'll", "he's",
    "she's", "who's", "isn't", "aren't", "wasn't", "weren't", "don't",
    "doesn't", "didn't", "won't", "wouldn't", "can't", "couldn't", "shouldn't",
    "haven't", "hasn't", "hadn't", "ain't", "y'all",
}

_STOPWORDS |= _CONTRACTIONS

# Below this length a token is noise for BM25 purposes.
_MIN_TERM_CHARS = 3


@dataclass(frozen=True)
class RetrievedPassage:
    """One document passage selected for a turn's prompt."""

    chunk_id: int
    document_id: str
    title: str
    ordinal: int
    content: str
    score: float

    @property
    def citation(self) -> str:
        """Short human-readable source label, e.g. ``spec.pdf #3``."""
        return f"{self.title} #{self.ordinal}"


def extract_terms(text: str, limit: int = 24) -> List[str]:
    """Reduce free text to distinct, ranked query terms.

    Terms are de-duplicated with first-occurrence order preserved, which keeps
    the most recent conversational content at the front when the caller passes
    recency-ordered text.
    """
    if not text:
        return []
    # Keep alphanumerics and internal apostrophes; everything else is a separator,
    # which also strips every FTS5 operator character.
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", text)
    seen: set = set()
    terms: List[str] = []
    for token in raw:
        term = token.strip("'").lower()
        if term in _STOPWORDS:
            continue
        # Drop the possessive so "auditor's" and "auditor" are the same key.
        if term.endswith("'s"):
            term = term[:-2]
        # Any remaining apostrophe means an unlisted contraction; keep the stem,
        # which is the part that carries meaning ("we'll" -> "we", a stopword).
        if "'" in term:
            term = term.split("'", 1)[0]
        if len(term) < _MIN_TERM_CHARS or term in _STOPWORDS:
            continue
        # A bare number is rarely a useful retrieval key on its own.
        if term.isdigit() and len(term) < 4:
            continue
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def build_fts_query(text: str, limit: int = 24) -> str:
    """Build a safe FTS5 ``MATCH`` expression that ORs the extracted terms.

    Each term is wrapped in double quotes so FTS5 treats it as a literal string
    rather than a bareword that might be an operator or a column filter. OR is
    used because a turn should surface passages matching *any* salient term;
    requiring all of them would almost always return nothing.

    Returns an empty string when there is nothing worth querying, which callers
    treat as "no retrieval this turn" rather than as an error.
    """
    terms = extract_terms(text, limit=limit)
    if not terms:
        return ""
    # Internal quotes cannot survive inside a quoted FTS5 string literal.
    return " OR ".join(f'"{t}"' for t in terms if '"' not in t)


def build_turn_query(
    topic: str,
    conversation: Sequence[Dict[str, Any]],
    recent_turns: int = 3,
    limit: int = 24,
) -> str:
    """Compose the retrieval query for a turn: recent conversation, then topic.

    Recent messages come first so their terms survive the ``limit`` cut ahead of
    the (static, already well-represented) topic text. On a cold start the topic
    is all there is, which is the correct query for an opening turn.
    """
    recent = list(conversation)[-recent_turns:] if conversation else []
    parts = [str(m.get("content", "")) for m in recent]
    parts.append(topic or "")
    return build_fts_query("\n".join(parts), limit=limit)


def select_discriminative_terms(
    terms: Sequence[str],
    doc_freq: Dict[str, int],
    total_chunks: int,
    limit: int = 8,
    max_df_ratio: float = 0.5,
) -> List[str]:
    """Narrow a query to its rarest in-corpus terms.

    **MEASURED HARMFUL — off by default. Do not enable without re-measuring.**
    The hypothesis was that real turn-queries are diluted (an unweighted OR over
    ~24 conversational terms, only ~39% of which appear in the corpus) and that
    keeping the rarest would sharpen ranking. The A/B in
    ``docs/PHASE5-RETRIEVAL-MEASUREMENT.md`` refuted it: recall fell on all three
    arms, worst on the "diluted" arm this was designed for (recall@5 0.339 vs
    0.509 baseline).

    The reason is structural: document frequency measures rarity, not relevance.
    When a query mixes a real question with conversational filler, the filler
    often supplies the *rarest* terms, so rarity-ranking actively promotes noise
    and can discard the common-but-relevant terms that were matching.

    Retained only so the harness can re-evaluate it against a future corpus or a
    smarter ranking signal.

    Two filters, in order:

    - **drop df == 0** — a term absent from the corpus can never match, so it can
      only add noise to the query string.
    - **drop df > max_df_ratio * total_chunks** — a term in most chunks carries
      almost no ranking signal (the IDF intuition, applied as a hard cut because
      FTS5's OR has no term weighting we can set).

    Survivors are ranked rarest-first and truncated to ``limit``, because a short
    query of rare terms targets far better than a long one of common terms.

    Falls back deliberately: if the filters would leave nothing, the original
    terms are returned rather than an empty query. Retrieving something imperfect
    beats retrieving nothing because a heuristic was too aggressive.
    """
    if not terms:
        return []
    if not doc_freq or total_chunks <= 0:
        return list(terms)[:limit]

    ceiling = max(1, int(total_chunks * max_df_ratio))
    kept = [
        t for t in terms
        if 0 < doc_freq.get(t, 0) <= ceiling
    ]
    if not kept:
        # Nothing survived: fall back to any term that at least exists, then to
        # the raw terms. Never return an empty query from a non-empty one.
        kept = [t for t in terms if doc_freq.get(t, 0) > 0] or list(terms)
        return kept[:limit]

    kept.sort(key=lambda t: doc_freq.get(t, 0))
    return kept[:limit]


def filter_by_score(
    rows: Sequence[Dict[str, Any]], score_ratio: float
) -> List[Dict[str, Any]]:
    """Drop matches far weaker than the best one.

    FTS5 BM25 scores are negative and more-negative is better, so strength is
    ``abs(score)``. A row is kept when its strength is at least ``score_ratio`` of
    the best row's strength.

    **MEASURED HARMFUL — off by default. Do not enable without re-measuring.**
    It was intended to address the 0.000 zero-result rate (FTS5 essentially always
    returns something, so on a hard query a persona is handed a confidently
    irrelevant passage). It fails at that AND costs recall:

    - It cannot produce an empty result, because the filter is *relative* and the
      best row always clears its own threshold. The zero-result rate stayed 0.000
      with it enabled, so the stated motivation went unmet.
    - The gold passage is often ranked below the top match, so trimming the tail
      trims real hits: recall@5 fell in every measured arm.

    Making "no supporting passage found" reachable needs an ABSOLUTE score floor,
    which requires calibration this measurement has not done.

    ``score_ratio <= 0`` disables the filter. The top-ranked row is always kept.
    """
    if not rows or score_ratio <= 0:
        return list(rows)
    strengths = [abs(float(r["score"])) for r in rows]
    best = max(strengths)
    if best <= 0:
        return list(rows)
    floor = best * score_ratio
    return [r for r, s in zip(rows, strengths) if s >= floor]


def reciprocal_rank_fusion(
    lists: Sequence[Sequence[Dict[str, Any]]],
    rrf_k: int = 60,
    weights: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    """Fuse ranked result lists by Reciprocal Rank Fusion.

    Fusion is done by **rank, not score**, and that is the whole point: BM25
    returns negative numbers where more-negative is better, while vector search
    returns distances where smaller is better. The two are not on a comparable
    scale and no normalisation of them is principled, but their *orderings* are
    directly comparable.

    Each document scores ``sum(weight_i / (rrf_k + rank_i))`` over the lists it
    appears in, so a passage both retrievers like outranks one that only a single
    retriever liked. ``rrf_k=60`` is the value from the original RRF paper and is
    deliberately not tuned here — tuning it without a held-out set would be
    fitting the measurement.

    The returned rows keep their original fields, with ``score`` replaced by the
    fused score (HIGHER is better, unlike either input) and the pre-fusion values
    preserved as ``fusion_sources`` for auditability.
    """
    if not lists:
        return []
    if weights is None:
        weights = [1.0] * len(lists)

    fused: Dict[int, Dict[str, Any]] = {}
    for list_index, (rows, weight) in enumerate(zip(lists, weights)):
        for rank, row in enumerate(rows, start=1):
            chunk_id = int(row["chunk_id"])
            entry = fused.get(chunk_id)
            if entry is None:
                entry = dict(row)
                entry["fusion_score"] = 0.0
                entry["fusion_sources"] = {}
                fused[chunk_id] = entry
            entry["fusion_score"] += weight / (rrf_k + rank)
            entry["fusion_sources"][str(list_index)] = {
                "rank": rank,
                "score": float(row.get("score", 0.0)),
            }

    out = sorted(fused.values(), key=lambda r: -r["fusion_score"])
    for row in out:
        row["score"] = row.pop("fusion_score")
    return out


def apply_budget(
    rows: Sequence[Dict[str, Any]], max_chars: int
) -> List[RetrievedPassage]:
    """Convert search rows to passages, stopping at the character budget.

    Whole passages are kept or dropped rather than truncated mid-way: half a
    passage reads as a corrupted quote, and a persona citing it would be citing
    something the document does not say. The single exception is a first passage
    that alone exceeds the budget — it is truncated on a word boundary with an
    ellipsis, because returning nothing would be worse.
    """
    if max_chars <= 0:
        # A zero budget means "no document text this turn". Without this guard the
        # oversized-first-passage branch below would emit an empty ellipsis.
        return []

    passages: List[RetrievedPassage] = []
    used = 0
    for row in rows:
        content = str(row["content"])
        if used + len(content) > max_chars:
            if passages:
                break
            # First passage over budget: truncate rather than return nothing.
            # The ellipsis is charged against the budget, because max_chars is
            # documented as a HARD ceiling — returning max_chars + 2 would make
            # the one guarantee this function offers untrue.
            ellipsis = " …"
            if max_chars <= len(ellipsis):
                content = content[:max_chars]
            else:
                room = max_chars - len(ellipsis)
                clipped = content[:room].rsplit(" ", 1)[0].rstrip()
                if not clipped:
                    clipped = content[:room]
                content = clipped + ellipsis
        passages.append(
            RetrievedPassage(
                chunk_id=int(row["chunk_id"]),
                document_id=str(row["document_id"]),
                title=str(row["title"]),
                ordinal=int(row["ordinal"]),
                content=content,
                score=float(row["score"]),
            )
        )
        used += len(content)
        if used >= max_chars:
            break
    return passages


async def retrieve_for_turn(
    db: Any,
    run_id: str,
    persona_name: str,
    topic: str,
    conversation: Sequence[Dict[str, Any]],
    k: int,
    max_chars: int,
    recent_turns: int = 3,
    term_limit: int = 0,
    max_df_ratio: float = 0.5,
    score_ratio: float = 0.0,
    mode: str = "fts",
    embedding_model: str = "",
    rrf_k: int = 60,
) -> tuple[List[RetrievedPassage], str]:
    """Retrieve a persona's supporting passages for one turn.

    Returns ``(passages, query)``. The query is returned so the emitted
    ``document.retrieved`` event can record exactly what was asked — retrieval
    that cannot be inspected cannot be debugged or measured.

    Never raises: any failure degrades to no passages, because a retrieval
    problem must not end a run.
    """
    if k <= 0 or max_chars <= 0:
        return [], ""

    candidates = extract_terms(
        "\n".join(
            [str(m.get("content", "")) for m in list(conversation)[-recent_turns:]]
            + [topic or ""]
        ),
        limit=24,
    )
    if not candidates:
        return [], ""

    # Narrow to the terms that actually discriminate within this persona's slice.
    # Skipped when term_limit is 0, which reproduces the pre-measurement behavior.
    terms = candidates
    if term_limit and hasattr(db, "term_document_frequencies"):
        try:
            doc_freq = await db.term_document_frequencies(
                run_id, candidates, persona_name=persona_name
            )
            total = await db.chunk_count(run_id, persona_name=persona_name)
            terms = select_discriminative_terms(
                candidates, doc_freq, total, limit=term_limit,
                max_df_ratio=max_df_ratio,
            )
        except Exception:  # noqa: BLE001
            # Term selection is an optimisation; a failure must not stop retrieval.
            terms = candidates

    query = " OR ".join(f'"{t}"' for t in terms if '"' not in t)
    if not query:
        return [], ""

    # Over-fetch a little: the score filter and budget may drop trailing rows, so
    # asking for exactly k risks returning fewer passages than the budget affords.
    fetch_k = max(k * 2, k)
    lexical: List[Dict[str, Any]] = []
    if mode in ("fts", "hybrid"):
        lexical = await db.search_documents(
            run_id=run_id, query=query, persona_name=persona_name, k=fetch_k
        )

    # Phase 5f: vector arm. The query embedded is the raw conversational text, not
    # the sanitised OR-expression — the whole advantage of embeddings is that they
    # read meaning, so stripping the sentence to keywords first would discard it.
    semantic: List[Dict[str, Any]] = []
    if mode in ("vector", "hybrid") and getattr(db, "vec_available", False):
        from matrix_studio.embeddings import DEFAULT_EMBEDDING_MODEL, embed_query

        # An empty embedding_model means "use the module default" (that is what
        # RetrievalConfig documents). Resolving it here is required: passing "" to
        # litellm raises "LLM Provider NOT provided", which the fallback would
        # then silently swallow, leaving vector mode permanently inert.
        model = embedding_model or DEFAULT_EMBEDDING_MODEL
        query_text = "\n".join(
            [str(m.get("content", "")) for m in list(conversation)[-recent_turns:]]
            + [topic or ""]
        ).strip()
        result = await embed_query(query_text, model=model) if query_text else None
        if result and result.vectors and result.vectors[0]:
            semantic = await db.vector_search(
                run_id=run_id, vector=result.vectors[0],
                persona_name=persona_name, k=fetch_k,
            )
        elif mode == "vector":
            # Vector-only mode with no usable embedding: fall back to lexical
            # rather than returning nothing. Degrading beats going silent.
            lexical = await db.search_documents(
                run_id=run_id, query=query, persona_name=persona_name, k=fetch_k
            )

    if mode == "hybrid" and lexical and semantic:
        rows = reciprocal_rank_fusion([lexical, semantic], rrf_k=rrf_k)
    elif mode == "vector" and semantic:
        rows = semantic
    else:
        # Covers fts mode, and hybrid/vector where one arm produced nothing.
        rows = lexical or semantic

    rows = filter_by_score(rows, score_ratio)
    return apply_budget(rows[:k], max_chars), query


def format_documents_block(passages: Sequence[RetrievedPassage]) -> str:
    """Render retrieved passages for the system prompt.

    Each passage carries its citation so the persona can attribute a claim, and
    the framing states plainly that this is the persona's own background material
    — not conversation, and not something another participant said.
    """
    if not passages:
        return ""
    lines = "\n".join(
        f"- [{p.citation}] {p.content}" for p in passages
    )
    return (
        "\n\nFrom your own background material (quote or cite it by name when "
        f"it supports a claim; it is not part of the conversation):\n{lines}"
    )


async def embed_pending_chunks(
    db: Any,
    run_id: str,
    embedding_model: str = "",
    batch: Optional[int] = None,
) -> Dict[str, Any]:
    """Embed any of a run's chunks that do not yet have a vector.

    Idempotent and resumable: it only touches chunks with no stored embedding, so
    an interrupted ingest can be re-run without paying to re-embed what succeeded.

    Returns a dict with ``embedded``, ``skipped``, ``tokens``, ``cost_usd`` and
    ``model``, so ingest cost lands in the same visible accounting as generation.
    Never raises — a failure returns ``embedded: 0`` with an ``error`` key, because
    a run must proceed on lexical retrieval rather than die on an embedding
    provider.
    """
    from matrix_studio.embeddings import (
        DEFAULT_EMBEDDING_MODEL,
        EmbeddingError,
        embed_texts,
    )

    model = embedding_model or DEFAULT_EMBEDDING_MODEL
    out: Dict[str, Any] = {
        "embedded": 0, "skipped": 0, "tokens": 0, "cost_usd": 0.0, "model": model,
    }
    if not getattr(db, "vec_available", False):
        out["error"] = (
            "sqlite-vec is not available; install the 'vectors' extra "
            "(pip install 'matrix-sim-studio[vectors]')"
        )
        return out

    pending = await db.chunks_missing_vectors(run_id, limit=batch)
    if not pending:
        return out

    try:
        result = await embed_texts([c["content"] for c in pending], model=model)
    except EmbeddingError as exc:
        out["error"] = str(exc)
        return out

    pairs = [
        (int(chunk["chunk_id"]), vector)
        for chunk, vector in zip(pending, result.vectors)
        if vector
    ]
    try:
        stored = await db.store_chunk_vectors(run_id, pairs, result.model)
    except ValueError as exc:
        # Dimension mismatch against an existing index — refuse, do not corrupt.
        out["error"] = str(exc)
        return out

    out["embedded"] = stored
    out["skipped"] = len(pending) - stored
    out["tokens"] = result.tokens
    out["cost_usd"] = round(result.cost_usd, 8)
    out["model"] = result.model
    return out
