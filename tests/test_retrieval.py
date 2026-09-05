# SPDX-License-Identifier: Apache-2.0
"""Tests for Phase 5 document retrieval: FTS5 storage, scoping, budget, rebuild.

The properties locked here are the ones the design rests on:

- a persona can only retrieve from its own slice (SQL-enforced, not prompt-asked);
- the character budget is a hard ceiling, because keeping a forty-page document
  out of per-call context is the entire point of the feature;
- raw conversation text never reaches FTS5 unsanitised (it contains operators);
- the index is rebuildable from ``doc_chunks``, the source of truth.
"""

import pytest

from matrix_studio.retrieval import (
    apply_budget,
    build_fts_query,
    build_turn_query,
    extract_terms,
    filter_by_score,
    format_documents_block,
    retrieve_for_turn,
    select_discriminative_terms,
)
from matrix_studio.state import RetrievalConfig
from matrix_studio.storage import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def run_db(db):
    await db.create_run(run_id="r1", topic="Retrieval design", cast=[])
    return db


# --------------------------------------------------------------------------
# Query sanitisation — a correctness requirement, not hardening
# --------------------------------------------------------------------------


def test_extract_terms_drops_stopwords_and_short_tokens():
    terms = extract_terms("We should think about the cost of retrieval")
    assert "cost" in terms and "retrieval" in terms
    assert "the" not in terms and "we" not in terms


def test_extract_terms_deduplicates_preserving_order():
    assert extract_terms("cost cost budget cost") == ["cost", "budget"]


def test_extract_terms_respects_limit():
    assert len(extract_terms(" ".join(f"term{i}" for i in range(50)), limit=5)) == 5


def test_build_fts_query_quotes_every_term():
    q = build_fts_query("egress inspection")
    assert q == '"egress" OR "inspection"'


def test_fts_operators_in_user_text_are_neutralised():
    """AND/OR/NOT/NEAR are FTS5 barewords; they must not survive as operators."""
    q = build_fts_query("cost AND budget OR NEAR latency NOT spend")
    assert " AND " not in q.replace('"AND"', "")
    assert "NEAR" not in q
    assert '"cost"' in q and '"budget"' in q


def test_punctuation_and_quotes_cannot_break_the_query():
    q = build_fts_query('the "index" is stale -- rebuild: it* now^')
    assert '"' in q  # our own quoting
    for bad in ("*", "^", ":", "-", "--"):
        assert bad not in q.replace(" OR ", " ")


def test_build_fts_query_empty_when_nothing_useful():
    assert build_fts_query("") == ""
    assert build_fts_query("the a of to and") == ""


def test_build_turn_query_prefers_recent_conversation(monkeypatch):
    conv = [
        {"speaker": "A", "content": "ancient irrelevant chatter"},
        {"speaker": "B", "content": "egress inspection choke point"},
    ]
    q = build_turn_query("topic about latency", conv, recent_turns=1, limit=4)
    assert "egress" in q
    assert "ancient" not in q


async def test_malformed_query_never_raises(run_db):
    """A bad MATCH must degrade to no results, never take a run down."""
    await run_db.add_document(
        run_id="r1", title="d.txt", chunks=["some content"], persona_name="Ana"
    )
    assert await run_db.search_documents("r1", 'unbalanced "quote', "Ana", 3) == []


# --------------------------------------------------------------------------
# Storage + scoping
# --------------------------------------------------------------------------


async def test_add_and_search_document(run_db):
    await run_db.add_document(
        run_id="r1",
        title="egress.md",
        chunks=[
            "Deep packet inspection gives auditable egress evidence.",
            "Latency is the cost of a single choke point.",
        ],
        persona_name="Dana",
        media_type="md",
        char_count=100,
    )
    rows = await run_db.search_documents("r1", build_fts_query("egress evidence"), "Dana", 3)
    assert rows
    assert "auditable egress" in rows[0]["content"]
    assert rows[0]["title"] == "egress.md"


async def test_persona_cannot_retrieve_another_personas_documents(run_db):
    await run_db.add_document(
        run_id="r1", title="dana.md", chunks=["egress inspection evidence"],
        persona_name="Dana",
    )
    await run_db.add_document(
        run_id="r1", title="marcus.md", chunks=["token cost measurement"],
        persona_name="Marcus",
    )
    q = build_fts_query("egress inspection token cost")
    dana = await run_db.search_documents("r1", q, "Dana", 5)
    marcus = await run_db.search_documents("r1", q, "Marcus", 5)
    assert {r["title"] for r in dana} == {"dana.md"}
    assert {r["title"] for r in marcus} == {"marcus.md"}


async def test_cast_wide_document_is_visible_to_everyone(run_db):
    await run_db.add_document(
        run_id="r1", title="shared.md", chunks=["shared briefing material"],
        persona_name=None,
    )
    for who in ("Dana", "Marcus"):
        rows = await run_db.search_documents("r1", build_fts_query("briefing"), who, 5)
        assert [r["title"] for r in rows] == ["shared.md"]


async def test_documents_are_scoped_to_their_run(run_db):
    await run_db.create_run(run_id="r2", topic="other", cast=[])
    await run_db.add_document(
        run_id="r1", title="r1.md", chunks=["retrieval design notes"], persona_name="A"
    )
    q = build_fts_query("retrieval design")
    assert await run_db.search_documents("r1", q, "A", 5)
    assert await run_db.search_documents("r2", q, "A", 5) == []


async def test_list_documents_scoped_by_persona(run_db):
    await run_db.add_document(run_id="r1", title="a.md", chunks=["x"], persona_name="A")
    await run_db.add_document(run_id="r1", title="b.md", chunks=["y"], persona_name="B")
    await run_db.add_document(run_id="r1", title="s.md", chunks=["z"], persona_name=None)
    assert len(await run_db.list_documents("r1")) == 3
    assert {d["title"] for d in await run_db.list_documents("r1", "A")} == {"a.md", "s.md"}


async def test_document_metadata_recorded(run_db):
    doc_id = await run_db.add_document(
        run_id="r1", title="spec.pdf", chunks=["one", "two", "three"],
        persona_name="A", source_path="/tmp/spec.pdf", media_type="pdf",
        char_count=4242,
    )
    doc = (await run_db.list_documents("r1"))[0]
    assert doc["id"] == doc_id
    assert doc["chunk_count"] == 3
    assert doc["char_count"] == 4242
    assert doc["media_type"] == "pdf"
    assert doc["source_path"] == "/tmp/spec.pdf"


async def test_delete_document_removes_it_from_the_index(run_db):
    doc_id = await run_db.add_document(
        run_id="r1", title="gone.md", chunks=["retrieval design notes"], persona_name="A"
    )
    q = build_fts_query("retrieval design")
    assert await run_db.search_documents("r1", q, "A", 5)
    assert await run_db.delete_document(doc_id) is True
    assert await run_db.search_documents("r1", q, "A", 5) == []
    assert await run_db.list_documents("r1") == []


async def test_delete_unknown_document_returns_false(run_db):
    assert await run_db.delete_document("nope") is False


async def test_count_documents(run_db):
    assert await run_db.count_documents("r1") == 0
    await run_db.add_document(run_id="r1", title="a", chunks=["x"], persona_name="A")
    assert await run_db.count_documents("r1") == 1


# --------------------------------------------------------------------------
# The rebuild path — the panel's hard prerequisite
# --------------------------------------------------------------------------


async def test_reindex_rebuilds_search_from_source_of_truth(run_db):
    await run_db.add_document(
        run_id="r1", title="spec.md",
        chunks=["egress inspection evidence", "latency and cost tradeoffs"],
        persona_name="A",
    )
    q = build_fts_query("egress inspection")
    assert await run_db.search_documents("r1", q, "A", 5)

    # Simulate a corrupt/stale index by emptying it behind the retrieval layer.
    await run_db._conn.execute("INSERT INTO doc_chunks_fts(doc_chunks_fts) VALUES('delete-all')")
    await run_db._conn.commit()
    assert await run_db.search_documents("r1", q, "A", 5) == []

    indexed = await run_db.reindex_documents()
    assert indexed == 2
    assert await run_db.search_documents("r1", q, "A", 5), "rebuild did not restore search"


async def test_reindex_is_idempotent(run_db):
    await run_db.add_document(
        run_id="r1", title="a.md", chunks=["retrieval design"], persona_name="A"
    )
    await run_db.reindex_documents()
    await run_db.reindex_documents()
    rows = await run_db.search_documents("r1", build_fts_query("retrieval"), "A", 5)
    assert len(rows) == 1, "rebuild duplicated index entries"


async def test_branch_inherits_parent_documents(run_db):
    await run_db.add_document(
        run_id="r1", title="bg.md", chunks=["egress inspection evidence"],
        persona_name="Dana",
    )
    await run_db.create_run(run_id="r1-branch", topic="t", cast=[])
    copied = await run_db.copy_documents_to_run("r1", "r1-branch")
    assert copied == 1
    rows = await run_db.search_documents(
        "r1-branch", build_fts_query("egress inspection"), "Dana", 5
    )
    assert rows and rows[0]["title"] == "bg.md"
    # The copy owns its own rows: deleting the parent's must not affect the branch.
    parent_doc = [d for d in await run_db.list_documents("r1")][0]
    await run_db.delete_document(parent_doc["id"])
    assert await run_db.search_documents(
        "r1-branch", build_fts_query("egress inspection"), "Dana", 5
    )


# --------------------------------------------------------------------------
# The character budget — the whole point of the feature
# --------------------------------------------------------------------------


def _row(chunk_id, content, score=-1.0):
    return {
        "chunk_id": chunk_id, "document_id": "d1", "title": "t.md",
        "ordinal": chunk_id, "content": content, "score": score,
    }


def test_budget_stops_before_exceeding_max_chars():
    rows = [_row(i, "x" * 400) for i in range(10)]
    passages = apply_budget(rows, max_chars=1000)
    assert sum(len(p.content) for p in passages) <= 1000
    assert len(passages) == 2


def test_budget_keeps_whole_passages_not_fragments():
    """Half a passage would be a corrupted quote a persona could 'cite'."""
    rows = [_row(0, "a" * 300), _row(1, "b" * 300)]
    passages = apply_budget(rows, max_chars=500)
    assert len(passages) == 1
    assert passages[0].content == "a" * 300


def test_budget_truncates_a_single_oversized_first_passage():
    """max_chars is a HARD ceiling — the ellipsis is charged against it."""
    rows = [_row(0, "word " * 500)]
    passages = apply_budget(rows, max_chars=100)
    assert len(passages) == 1
    assert len(passages[0].content) <= 100
    assert passages[0].content.endswith("…")


def test_budget_ceiling_holds_for_awkward_sizes():
    rows = [_row(0, "word " * 500)]
    for max_chars in (1, 2, 3, 4, 17, 99, 100, 301):
        passages = apply_budget(rows, max_chars=max_chars)
        assert sum(len(p.content) for p in passages) <= max_chars, max_chars


def test_budget_zero_returns_nothing():
    assert apply_budget([_row(0, "content")], max_chars=0) == []


def test_budget_empty_rows():
    assert apply_budget([], max_chars=1000) == []


async def test_retrieve_for_turn_respects_k_and_budget(run_db):
    await run_db.add_document(
        run_id="r1",
        title="big.md",
        chunks=[f"retrieval design passage number {i} " + "filler " * 40 for i in range(20)],
        persona_name="A",
    )
    passages, query = await retrieve_for_turn(
        run_db, "r1", "A", "retrieval design",
        conversation=[{"speaker": "B", "content": "tell me about retrieval design"}],
        k=3, max_chars=600,
    )
    assert query
    assert len(passages) <= 3
    assert sum(len(p.content) for p in passages) <= 600


async def test_retrieve_for_turn_disabled_by_zero_k(run_db):
    passages, query = await retrieve_for_turn(
        run_db, "r1", "A", "topic", conversation=[], k=0, max_chars=1000
    )
    assert passages == [] and query == ""


async def test_retrieve_for_turn_no_documents_is_empty_not_an_error(run_db):
    passages, _ = await retrieve_for_turn(
        run_db, "r1", "A", "retrieval design", conversation=[], k=3, max_chars=900
    )
    assert passages == []


# --------------------------------------------------------------------------
# Prompt block
# --------------------------------------------------------------------------


def test_format_block_empty_when_no_passages():
    assert format_documents_block([]) == ""


def test_format_block_carries_citations():
    passages = apply_budget([_row(0, "Egress evidence matters.")], max_chars=900)
    block = format_documents_block(passages)
    assert "t.md #0" in block
    assert "Egress evidence matters." in block
    assert "background material" in block


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_retrieval_config_defaults_to_off():
    cfg = RetrievalConfig.from_config(None)
    assert cfg.enabled is False


def test_retrieval_config_ignores_non_dict():
    assert RetrievalConfig.from_config({"retrieval": "yes"}).enabled is False


def test_retrieval_config_parses_and_ignores_unknown_keys():
    cfg = RetrievalConfig.from_config(
        {"retrieval": {"enabled": True, "k": 5, "max_chars": 800, "bogus": 1}}
    )
    assert cfg.enabled is True and cfg.k == 5 and cfg.max_chars == 800


def test_contractions_do_not_pollute_the_query():
    """Observed in a live run: "doesn't" and "it's" were reaching FTS5."""
    q = build_fts_query("it's clear that doesn't blow up the install story")
    assert "doesn" not in q and "it's" not in q
    assert '"install"' in q and '"story"' in q


def test_possessive_is_stripped_to_the_stem():
    assert extract_terms("the auditor's evidence") == ["auditor", "evidence"]


def test_conversational_filler_is_dropped():
    terms = extract_terms("Look, we need to make the call on egress inspection")
    assert "look" not in terms and "make" not in terms and "call" not in terms
    assert "egress" in terms and "inspection" in terms


def test_unlisted_contraction_falls_back_to_stem():
    """An unlisted contraction contributes its stem, not the raw contraction.

    For the common cases the stem is itself a stopword and so disappears
    ("we'll" -> "we"). A stem that survives is harmless: it simply matches
    nothing in the corpus.
    """
    terms = extract_terms("we'll ship egress")
    assert "we'll" not in terms and "we" not in terms
    assert terms == ["ship", "egress"]


# --------------------------------------------------------------------------
# Experimental query/ranking knobs (MEASURED HARMFUL, default off).
# Tested because they remain callable and the harness uses them.
# --------------------------------------------------------------------------


def test_select_discriminative_drops_absent_and_ubiquitous_terms():
    terms = ["absent", "rare", "common"]
    df = {"absent": 0, "rare": 2, "common": 90}
    got = select_discriminative_terms(terms, df, total_chunks=100, limit=8, max_df_ratio=0.5)
    assert got == ["rare"]


def test_select_discriminative_ranks_rarest_first():
    terms = ["a1", "b2", "c3"]
    df = {"a1": 9, "b2": 1, "c3": 5}
    assert select_discriminative_terms(terms, df, 100, limit=3) == ["b2", "c3", "a1"]


def test_select_discriminative_respects_limit():
    terms = [f"t{i}" for i in range(20)]
    df = {t: i + 1 for i, t in enumerate(terms)}
    assert len(select_discriminative_terms(terms, df, 100, limit=5)) == 5


def test_select_discriminative_falls_back_rather_than_returning_nothing():
    """An over-aggressive filter must never produce an empty query."""
    terms = ["only", "common"]
    df = {"only": 99, "common": 98}
    got = select_discriminative_terms(terms, df, 100, limit=8, max_df_ratio=0.1)
    assert got, "filter emptied the query instead of falling back"
    assert set(got) <= set(terms)


def test_select_discriminative_falls_back_when_all_absent():
    terms = ["x1", "y2"]
    assert select_discriminative_terms(terms, {"x1": 0, "y2": 0}, 100) == terms


def test_select_discriminative_without_frequencies_is_a_passthrough():
    terms = ["a1", "b2", "c3"]
    assert select_discriminative_terms(terms, {}, 0, limit=2) == ["a1", "b2"]


def test_select_discriminative_empty_input():
    assert select_discriminative_terms([], {"a": 1}, 10) == []


def test_filter_by_score_keeps_strong_matches_only():
    rows = [_row(0, "a", score=-10.0), _row(1, "b", score=-8.0), _row(2, "c", score=-1.0)]
    kept = filter_by_score(rows, score_ratio=0.5)
    assert [r["chunk_id"] for r in kept] == [0, 1]


def test_filter_by_score_always_keeps_the_best_row():
    """Which is exactly why it cannot produce an empty result — see the docstring."""
    rows = [_row(0, "a", score=-2.0)]
    assert filter_by_score(rows, score_ratio=0.99) == rows


def test_filter_by_score_disabled_at_zero():
    rows = [_row(0, "a", score=-10.0), _row(1, "b", score=-0.001)]
    assert filter_by_score(rows, score_ratio=0.0) == rows


def test_filter_by_score_handles_empty_and_all_zero_scores():
    assert filter_by_score([], 0.5) == []
    rows = [_row(0, "a", score=0.0), _row(1, "b", score=0.0)]
    assert filter_by_score(rows, 0.5) == rows


def test_retrieval_config_experimental_knobs_default_off():
    """They were measured harmful; the defaults must not enable them."""
    cfg = RetrievalConfig.from_config({"retrieval": {"enabled": True}})
    assert cfg.term_limit == 0
    assert cfg.score_ratio == 0.0


async def test_retrieve_for_turn_defaults_do_not_apply_the_knobs(run_db):
    """Default retrieval must behave as the measured-best baseline."""
    await run_db.add_document(
        run_id="r1", title="a.md",
        chunks=[f"retrieval design passage {i} with egress inspection" for i in range(6)],
        persona_name="A",
    )
    passages, query = await retrieve_for_turn(
        run_db, "r1", "A", "retrieval design egress inspection",
        conversation=[], k=5, max_chars=5000,
    )
    # All terms retained (no discriminative narrowing) and no tail trimming.
    assert query.count(" OR ") >= 3
    assert len(passages) == 5


async def test_term_document_frequencies_counts_within_scope(run_db):
    await run_db.add_document(
        run_id="r1", title="a.md",
        chunks=["egress inspection", "egress evidence", "unrelated text"],
        persona_name="A",
    )
    await run_db.add_document(
        run_id="r1", title="b.md", chunks=["egress elsewhere"], persona_name="B",
    )
    df = await run_db.term_document_frequencies("r1", ["egress", "inspection", "absent"], "A")
    assert df["egress"] == 2, "counted outside the persona's slice"
    assert df["inspection"] == 1
    assert df["absent"] == 0


async def test_chunk_count_is_scoped(run_db):
    await run_db.add_document(run_id="r1", title="a", chunks=["x", "y"], persona_name="A")
    await run_db.add_document(run_id="r1", title="b", chunks=["z"], persona_name="B")
    await run_db.add_document(run_id="r1", title="s", chunks=["w"], persona_name=None)
    assert await run_db.chunk_count("r1") == 4
    assert await run_db.chunk_count("r1", "A") == 3  # own + cast-wide
