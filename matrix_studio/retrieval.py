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
    query = build_turn_query(topic, conversation, recent_turns=recent_turns, limit=24)
    if not query:
        return [], ""
    # Over-fetch a little: the budget may drop trailing rows, so asking for
    # exactly k risks returning fewer passages than the budget could afford.
    rows = await db.search_documents(
        run_id=run_id, query=query, persona_name=persona_name, k=max(k * 2, k)
    )
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
