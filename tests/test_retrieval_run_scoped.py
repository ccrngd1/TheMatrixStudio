# SPDX-License-Identifier: Apache-2.0
"""
BM25 scores must be a property of the run, not of the database.

``bm25()`` is computed by FTS5 over whatever index it is handed, and the persistent
index holds every run in the database — ``run_id`` was only an outer filter applied to
already-scored rows. So a run's retrieval scores moved when unrelated runs were added,
which makes every retrieval measurement conditional on the database's history.

The fix scores in a scratch FTS index containing only the run's slice, with the same
tokenizer, so the statistics are the run's own. These tests reproduce the original
defect through ``corpus="database"`` and assert the new path is invariant to it —
keeping the legacy scorer around is what makes the difference measurable rather than
asserted.
"""

import pytest

from matrix_studio.retrieval import build_fts_query
from matrix_studio.storage import Database

POLICY = (
    "The rollback procedure must be tested in a staging cutover before any production "
    "migration proceeds. Reversibility is a hard requirement."
)
# Same subject matter, so it moves the corpus statistics for the query's terms.
NOISE = (
    "Rollback rollback tested tested migration migration cutover staging staging "
    "production production reversibility reversibility."
)
QUERY = build_fts_query("Must the rollback be tested before we migrate?")


async def _seed_run_a(db):
    await db.create_run(run_id="A", topic="t",
                        cast=[{"name": "P0", "persona": "x"}], config={})
    await db.add_document(run_id="A", title="policy.md", chunks=[POLICY],
                          persona_name="P0")


async def _add_unrelated_run(db, docs=12):
    await db.create_run(run_id="B", topic="t",
                        cast=[{"name": "Q0", "persona": "x"}], config={})
    for i in range(docs):
        await db.add_document(run_id="B", title=f"other-{i}.md", chunks=[NOISE],
                              persona_name="Q0")


async def test_run_scoped_score_is_unchanged_by_an_unrelated_run(db):
    """The headline property: a neighbour run cannot move your scores."""
    await _seed_run_a(db)
    before = await db.search_documents("A", QUERY, "P0", 5)
    assert before, "fixture must match"

    await _add_unrelated_run(db)
    after = await db.search_documents("A", QUERY, "P0", 5)

    assert [r["chunk_id"] for r in after] == [r["chunk_id"] for r in before]
    assert [r["score"] for r in after] == [r["score"] for r in before], (
        "run-scoped scores must not depend on what else the database holds"
    )


async def test_the_legacy_whole_database_scorer_can_no_longer_be_contaminated(db):
    """The defect is now impossible to reproduce, which is the strongest form of fixed.

    This test used to assert the OPPOSITE: that `corpus="database"` scores DID move when
    an unrelated run was added, so the run-scoped fix was a demonstrated difference
    rather than a claim. That contrast depended on there being a shared persistent FTS5
    index for the legacy path to be contaminated by.

    There is no shared index. The BM25 index is built from the run's own chunks for both
    corpus values, so `"database"` cannot diverge from `"run"` — the contamination is
    excluded by construction rather than filtered out. Asserting the invariance here,
    with an unrelated run present, is what keeps that true: if the two ever diverge
    again, this fails.
    """
    await _seed_run_a(db)
    before = await db.search_documents("A", QUERY, "P0", 5, corpus="database")
    await _add_unrelated_run(db)
    after = await db.search_documents("A", QUERY, "P0", 5, corpus="database")

    assert before and after
    assert before[0]["score"] == after[0]["score"], (
        "another run's corpus moved this run's scores — the contamination is back"
    )
    scoped = await db.search_documents("A", QUERY, "P0", 5, corpus="run")
    assert [r["score"] for r in after] == [r["score"] for r in scoped], (
        "the two corpus values diverged, which is what the v0.6 bug was"
    )


async def test_scoped_and_legacy_agree_when_the_database_holds_one_run(db):
    """
    With a single run the two corpora are the same set, so the ranking must match.

    This is what rules out the scoped path being a different ranking function rather
    than the same one over a corrected corpus.
    """
    await _seed_run_a(db)
    await db.add_document(run_id="A", title="aside.md",
                          chunks=["Latency and cost tradeoffs for the migration."],
                          persona_name="P0")
    scoped = await db.search_documents("A", QUERY, "P0", 5)
    legacy = await db.search_documents("A", QUERY, "P0", 5, corpus="database")
    assert [r["chunk_id"] for r in scoped] == [r["chunk_id"] for r in legacy]
    assert [round(r["score"], 6) for r in scoped] == [round(r["score"], 6) for r in legacy]


async def test_persona_scoping_is_preserved(db):
    """Scoping by persona still means "own documents plus cast-wide", unchanged."""
    await db.create_run(run_id="A", topic="t",
                        cast=[{"name": "P0", "persona": "x"}, {"name": "P1", "persona": "y"}],
                        config={})
    await db.add_document(run_id="A", title="p0.md", chunks=[POLICY], persona_name="P0")
    await db.add_document(run_id="A", title="p1.md", chunks=[POLICY], persona_name="P1")
    await db.add_document(run_id="A", title="shared.md", chunks=[POLICY], persona_name=None)

    assert {r["title"] for r in await db.search_documents("A", QUERY, "P0", 5)} == {
        "p0.md", "shared.md"
    }
    assert {r["title"] for r in await db.search_documents("A", QUERY, "P1", 5)} == {
        "p1.md", "shared.md"
    }
    # No persona -> the whole run.
    assert {r["title"] for r in await db.search_documents("A", QUERY, None, 5)} == {
        "p0.md", "p1.md", "shared.md"
    }


async def test_other_runs_documents_are_never_returned(db):
    await _seed_run_a(db)
    await _add_unrelated_run(db)
    titles = {r["title"] for r in await db.search_documents("A", QUERY, "P0", 5)}
    assert titles == {"policy.md"}
    assert await db.search_documents("B", QUERY, "P0", 5) == [], (
        "P0 is not in run B's cast, so it has no slice there"
    )


async def test_results_keep_the_scored_ranking(db):
    """
    Metadata is fetched with `IN`, which has no ordering guarantee.

    Losing the ranking here would return the right k passages in rowid order, which
    looks plausible and quietly makes the top hit arbitrary.
    """
    await db.create_run(run_id="A", topic="t",
                        cast=[{"name": "P0", "persona": "x"}], config={})
    # Inserted worst-first, so insertion order is the REVERSE of relevance order and a
    # lost ranking shows up as the wrong document first.
    #
    # The weak document uses "rollback" — a query term verbatim — rather than
    # "migration", which the previous fixture used. That relied on the lexical arm
    # unifying "migration" with the query's "migrate", which FTS5's Porter stemmer did
    # and the in-process one deliberately does not (see
    # `test_the_stemmer_handles_inflection_but_not_derivation`). The subject here is
    # ORDERING, so the fixture should not also depend on morphology.
    await db.add_document(run_id="A", title="weak.md",
                          chunks=["A passing mention of rollback."], persona_name="P0")
    await db.add_document(run_id="A", title="strong.md", chunks=[POLICY],
                          persona_name="P0")

    rows = await db.search_documents("A", QUERY, "P0", 5)
    assert [r["title"] for r in rows] == ["strong.md", "weak.md"]
    assert rows[0]["score"] < rows[1]["score"], "more negative bm25 = better match"


async def test_rows_have_the_same_shape_as_before(db):
    """Callers (retrieval fusion, the API, the CLI) depend on these keys."""
    await _seed_run_a(db)
    row = (await db.search_documents("A", QUERY, "P0", 5))[0]
    assert set(row) >= {
        "chunk_id", "document_id", "ordinal", "content", "title",
        "source_path", "media_type", "score",
    }


async def test_repeated_searches_are_stable(db):
    """
    The scratch index is shared and rebuilt per query, so it must be cleared each time.

    A leftover slice would leak one persona's or run's chunks into the next search and
    move its statistics — the original bug wearing a different hat.
    """
    await _seed_run_a(db)
    await _add_unrelated_run(db)
    first = await db.search_documents("A", QUERY, "P0", 5)
    await db.search_documents("B", QUERY, "Q0", 5)
    again = await db.search_documents("A", QUERY, "P0", 5)
    assert [(r["chunk_id"], r["score"]) for r in again] == [
        (r["chunk_id"], r["score"]) for r in first
    ]


async def test_concurrent_searches_do_not_contaminate_each_other(db):
    """
    Two runs searching at once must not score against each other's slice.

    The scratch table and the connection are both shared, so without serialisation one
    coroutine's INSERT can land between another's DELETE and SELECT. Concurrent runs
    are a real configuration, and this failure would be intermittent and look like
    flaky retrieval rather than a bug.
    """
    import asyncio

    await _seed_run_a(db)
    await _add_unrelated_run(db)
    expected_a = await db.search_documents("A", QUERY, "P0", 5)
    expected_b = await db.search_documents("B", QUERY, "Q0", 5)

    results = await asyncio.gather(*[
        db.search_documents(run, QUERY, persona, 5)
        for _ in range(12)
        for run, persona in (("A", "P0"), ("B", "Q0"))
    ])
    for i, rows in enumerate(results):
        expected = expected_a if i % 2 == 0 else expected_b
        assert [(r["chunk_id"], r["score"]) for r in rows] == [
            (r["chunk_id"], r["score"]) for r in expected
        ], "concurrent searches scored against the wrong slice"


async def test_an_unknown_corpus_is_rejected(db):
    """A typo must not silently pick a scorer."""
    await _seed_run_a(db)
    with pytest.raises(ValueError, match="corpus must be"):
        await db.search_documents("A", QUERY, "P0", 5, corpus="wholedb")


async def test_empty_query_and_zero_k_still_return_nothing(db):
    await _seed_run_a(db)
    assert await db.search_documents("A", "", "P0", 5) == []
    assert await db.search_documents("A", QUERY, "P0", 0) == []


async def test_a_malformed_query_degrades_instead_of_raising(db):
    """Retrieval failure must read as "no supporting passage", never crash a run."""
    await _seed_run_a(db)
    assert await db.search_documents("A", 'unbalanced "quote', "P0", 5) == []


# `test_search_does_not_leave_a_transaction_open` was removed here.
#
# Run-scoped scoring under SQLite wrote to a scratch table, which opened an implicit
# transaction; leaving it open pinned the connection's read snapshot, so a later read
# missed anything another connection had committed and any writer queued behind a lock
# held by a *search*. That was found for real — the whole-database scorer kept returning
# hits from an index another connection had already wiped.
#
# The hazard cannot exist now, and not because it was fixed: there is no connection, no
# transaction and no snapshot. DynamoDB and S3 are request-per-call, and the BM25 index
# is built in memory from text fetched per query. A search holds no lock and cannot see
# a stale view.
#
# Kept as a note rather than deleted silently, because "a search that blocks writers"
# is a class of bug worth remembering was once possible here.
