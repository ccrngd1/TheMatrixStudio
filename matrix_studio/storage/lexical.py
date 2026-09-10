# SPDX-License-Identifier: Apache-2.0
"""
In-process BM25, replacing SQLite FTS5 for the operator-facing document search.

**Why this is small on purpose.** Lexical retrieval is not the retrieval path. The
project measured it: `docs/PHASE5-RETRIEVAL-MEASUREMENT.md` §5f puts lexical recall@1
at **0.017** against vector's **0.367** on the diluted queries a turn actually
produces — 22× worse. So the engine retrieves by embedding, and this exists for
`/documents/search`, the endpoint whose job is to let an operator *inspect* what a
query matches. Building a serious text-search stack for that would be spending the
Phase 3 budget on the arm that was measured not to work.

**Why not keep FTS5, or reach for OpenSearch.** FTS5 needs SQLite, which Phase 2
deletes. OpenSearch Serverless was rejected in §8 on cost grounds at this corpus size
(1–5 documents per persona, ~63 chunks), and nothing about an inspection endpoint
changes that. In-process is the right size: the whole corpus for one run is a handful
of S3 objects averaging 11 KB.

**Scores match FTS5's sign convention**: negative, and more negative is better. That
is not cosmetic. `retrieval.filter_by_score` computes strength as `abs(score)`, the
reciprocal-rank fusion in `hybrid` mode assumes an ordering, and the existing tests
assert it. Returning conventional positive BM25 scores would invert every ranking
while looking numerically reasonable.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Sequence, Tuple

# Okapi BM25's usual constants. `k1` controls term-frequency saturation and `b` how
# much document length normalises the score; these are the values FTS5 itself uses,
# so a score here is comparable in spirit to what the SQLite path produced.
K1 = 1.2
B = 0.75

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> List[str]:
    """Raw lowercase alphanumeric tokens, UNSTEMMED.

    Separate from `tokenize` because stemming has to happen exactly once. It did not
    at first: `extract_query_terms` stemmed, and `search` stemmed its argument again —
    so a query for "egress" became "egres" and then "egre", matched nothing, and
    lexical search silently returned no results for every query. Double stemming is
    invisible in the output; it just looks like a corpus that does not contain the
    word.
    """
    return _WORD.findall(text.lower())


def tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric tokens, lightly stemmed — the INDEXING path.

    FTS5 was configured with `porter unicode61`, so it stemmed properly. This handles
    only inflectional endings — `-ing`, `-edly`, `-ed`, `-es`, `-s` — which covers
    `audit`/`auditing`/`audited`/`costs` and **not** derivational pairs like
    `migrate`/`migration`, `allocate`/`allocation` or `produce`/`production`.

    That limit is a decision, and it was measured rather than assumed. Extending the
    rules to `-ion`/`-ation` was tried: it unified 1 of 11 derivational pairs and
    introduced 2 false collisions (`ration`/`rat`, `region`/`reg`), because unifying
    them correctly needs Porter's measure conditions rather than more suffixes. So the
    choice is a real Porter implementation or none, and none is right here:

    * this serves `/documents/search`, an INSPECTION endpoint. The engine retrieves by
      embedding, measured 22× better at recall@1 (0.367 vs 0.017,
      `PHASE5-RETRIEVAL-MEASUREMENT.md` §5f);
    * handling morphological variation is precisely what embeddings do, and the
      measurement that chose them over lexical was largely a measurement of that;
    * a hundred lines of Porter, or a dependency, to improve the arm that was measured
      not to work is effort spent in the wrong place.

    Stated here rather than left to be discovered, and pinned by
    `test_the_stemmer_handles_inflection_but_not_derivation`.
    """
    return [_stem(t) for t in words(text)]


def _stem(token: str) -> str:
    for suffix in ("ing", "edly", "ed", "es", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def extract_query_terms(query: str) -> List[str]:
    """Terms from a query, tolerating the FTS5 syntax callers still build.

    `retrieval.build_fts_query` produces `"term" OR "other"`, because the SQLite path
    needed valid FTS5. Rather than change every caller, the quoting and the `OR`/`AND`
    /`NOT` operators are stripped here — so the same query string works against both
    backends and the engine needs no branch on which storage it is talking to.
    """
    cleaned = query.replace('"', " ")
    # `words`, not `tokenize`: these terms are stemmed by whoever consumes them, and
    # stemming here as well is the double-stem bug described in `words`.
    terms = [
        t for t in words(cleaned)
        if t not in {"or", "and", "not", "near"}
    ]
    # De-duplicated, order preserved: a term repeated by the query builder should not
    # count twice toward a score.
    seen = set()
    out = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


class Bm25Index:
    """A BM25 index over one run's chunks, built per query and thrown away.

    Built fresh rather than cached, and that is the correct trade here for a reason
    the project already measured: BM25 statistics are drawn from whatever corpus the
    index holds, and a cached cross-run index is exactly the bug that was found and
    fixed in v0.6 — `bm25()` computed statistics over the *whole* SQLite index while
    `run_id` was only an outer filter, so a score depended on what other runs existed.
    Building per run makes a score a property of the run, which is what makes it
    reproducible.
    """

    def __init__(self, chunks: Sequence[Tuple[int, str]]) -> None:
        """`chunks` is `[(chunk_id, content), ...]`."""
        self.chunk_ids: List[int] = []
        self.tokens: List[List[str]] = []
        self.freqs: List[Dict[str, int]] = []
        self.doc_freq: Dict[str, int] = {}

        for chunk_id, content in chunks:
            toks = tokenize(content)
            self.chunk_ids.append(chunk_id)
            self.tokens.append(toks)
            counts: Dict[str, int] = {}
            for tok in toks:
                counts[tok] = counts.get(tok, 0) + 1
            self.freqs.append(counts)
            for tok in counts:
                self.doc_freq[tok] = self.doc_freq.get(tok, 0) + 1

        self.n = len(self.chunk_ids)
        self.avg_len = (
            sum(len(t) for t in self.tokens) / self.n if self.n else 0.0
        )

    def document_frequencies(self, terms: Iterable[str]) -> Dict[str, int]:
        """How many chunks contain each term — the input to term selection.

        Keyed by the caller's raw term, not the stemmed form. `term_document_frequencies`
        promises counts for the terms it was handed, and `select_discriminative_terms`
        looks them up by those strings; returning stems would silently produce zeros
        for every term and disable discriminative selection without failing.
        """
        return {term: self.doc_freq.get(_stem(term.lower()), 0) for term in terms}

    def search(self, terms: Sequence[str], k: int) -> List[Tuple[int, float]]:
        """`[(chunk_id, score), ...]`, best first, score negative as FTS5 returns.

        A term absent from the corpus is skipped rather than scored as zero: with
        `idf` computed from a corpus that does not contain it, the standard formula
        can go negative and *penalise* a chunk for matching nothing, which inverts the
        ranking for queries built from conversational text (most of whose terms are
        absent).
        """
        if not self.n or not terms:
            return []
        stems = [_stem(t.lower()) for t in terms]
        scored: List[Tuple[int, float]] = []
        for i, chunk_id in enumerate(self.chunk_ids):
            counts = self.freqs[i]
            length = len(self.tokens[i]) or 1
            total = 0.0
            for stem in stems:
                df = self.doc_freq.get(stem, 0)
                if not df:
                    continue
                tf = counts.get(stem, 0)
                if not tf:
                    continue
                # The +0.5 smoothing keeps idf positive even for a term in every
                # chunk, which is what stops a ubiquitous term from subtracting.
                idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                total += idf * (
                    tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.avg_len))
                )
            if total > 0:
                # Negated to match FTS5: more negative is a better match.
                scored.append((chunk_id, -total))
        scored.sort(key=lambda pair: pair[1])
        return scored[:k]
