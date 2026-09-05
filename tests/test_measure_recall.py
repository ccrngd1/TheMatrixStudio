# SPDX-License-Identifier: Apache-2.0
"""Tests for the retrieval-recall measurement script (Phase 5 step 5b-4).

The script produces the number that decides whether FTS5 stays or embeddings get
added, so its scoring logic is tested rather than trusted. These cover the pure
functions only — no model calls.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "measure_retrieval_recall.py"
)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("measure_retrieval_recall", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _row(chunk_id, document_id="d1", ordinal=0):
    return {"chunk_id": chunk_id, "document_id": document_id, "ordinal": ordinal}


# --------------------------------------------------------------------------
# rank_of_gold
# --------------------------------------------------------------------------


def test_rank_is_one_indexed(mod):
    rows = [_row(7, "d1", 3), _row(8, "d1", 4)]
    assert mod.rank_of_gold(rows, 7, "d1", 3)["strict"] == 1


def test_rank_finds_gold_further_down(mod):
    rows = [_row(1, "d1", 0), _row(2, "d1", 1), _row(9, "d1", 5)]
    assert mod.rank_of_gold(rows, 9, "d1", 5)["strict"] == 3


def test_missing_gold_is_none(mod):
    rows = [_row(1, "d1", 0), _row(2, "d1", 1)]
    assert mod.rank_of_gold(rows, 99, "d1", 42)["strict"] is None


def test_empty_rows_is_none(mod):
    assert mod.rank_of_gold([], 1, "d1", 0)["strict"] is None


def test_lenient_accepts_an_adjacent_ordinal(mod):
    """Chunks overlap by design, so a neighbour genuinely shares gold text."""
    rows = [_row(50, "d1", 4)]  # gold is ordinal 5, this is 4
    got = mod.rank_of_gold(rows, 51, "d1", 5)
    assert got["strict"] is None
    assert got["lenient"] == 1


def test_lenient_rejects_a_distant_ordinal(mod):
    rows = [_row(50, "d1", 1)]
    assert mod.rank_of_gold(rows, 51, "d1", 5)["lenient"] is None


def test_lenient_rejects_a_neighbour_in_a_different_document(mod):
    """Ordinal adjacency only means anything within the same document."""
    rows = [_row(50, "OTHER", 4)]
    assert mod.rank_of_gold(rows, 51, "d1", 5)["lenient"] is None


def test_strict_hit_also_counts_as_lenient(mod):
    rows = [_row(51, "d1", 5)]
    got = mod.rank_of_gold(rows, 51, "d1", 5)
    assert got["strict"] == 1 and got["lenient"] == 1


# --------------------------------------------------------------------------
# lexical_overlap — the difficulty dial for the experiment
# --------------------------------------------------------------------------


def test_overlap_is_one_when_query_reuses_passage_words(mod):
    assert mod.lexical_overlap(
        "egress inspection evidence", "Egress inspection provides evidence."
    ) == 1.0


def test_overlap_is_zero_with_no_shared_vocabulary(mod):
    assert mod.lexical_overlap(
        "how much money will this burn", "Egress inspection provides evidence."
    ) == 0.0


def test_overlap_is_fractional_when_partially_shared(mod):
    got = mod.lexical_overlap("egress spend", "Egress inspection provides evidence.")
    assert 0.0 < got < 1.0


def test_overlap_ignores_stopwords_in_the_query(mod):
    """Stopwords must not inflate or deflate the difficulty measure."""
    assert mod.lexical_overlap(
        "what is the egress inspection", "Egress inspection provides evidence."
    ) == 1.0


def test_overlap_of_empty_query_is_zero(mod):
    assert mod.lexical_overlap("", "some passage") == 0.0
    assert mod.lexical_overlap("the a of to", "some passage") == 0.0


# --------------------------------------------------------------------------
# summarise
# --------------------------------------------------------------------------


def _result(arm, strict, lenient=None, matched=1, overlap=0.5):
    return {
        "arm": arm,
        "matched": matched,
        "overlap": overlap,
        "rank": {"strict": strict, "lenient": lenient if lenient is not None else strict},
    }


def test_summarise_recall_and_mrr(mod):
    results = [
        _result("natural", 1),
        _result("natural", 3),
        _result("natural", None, matched=0),
        _result("natural", 5),
    ]
    got = mod.summarise(results, "natural", [1, 3, 5])
    assert got["n"] == 4
    assert got["recall@1_strict"] == 0.25
    assert got["recall@3_strict"] == 0.5
    assert got["recall@5_strict"] == 0.75
    # MRR is averaged over ALL queries, misses included: (1 + 1/3 + 0 + 1/5)/4
    assert got["mrr_strict"] == pytest.approx((1 + 1 / 3 + 0 + 1 / 5) / 4, abs=1e-4)
    assert got["zero_result_rate"] == 0.25


def test_summarise_separates_arms(mod):
    results = [_result("natural", 1), _result("paraphrased", None, matched=0)]
    assert mod.summarise(results, "natural", [1])["recall@1_strict"] == 1.0
    assert mod.summarise(results, "paraphrased", [1])["recall@1_strict"] == 0.0


def test_summarise_lenient_can_exceed_strict(mod):
    results = [_result("natural", None, lenient=2)]
    got = mod.summarise(results, "natural", [3])
    assert got["recall@3_strict"] == 0.0
    assert got["recall@3_lenient"] == 1.0


def test_summarise_reports_mean_overlap(mod):
    results = [_result("natural", 1, overlap=0.2), _result("natural", 1, overlap=0.8)]
    assert mod.summarise(results, "natural", [1])["mean_lexical_overlap"] == 0.5


def test_summarise_empty_arm(mod):
    assert mod.summarise([], "natural", [1]) == {"n": 0}


# --------------------------------------------------------------------------
# collect_files
# --------------------------------------------------------------------------


def test_collect_files_expands_directories(mod, tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "skip.png").write_bytes(b"x")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "c.md").write_text("x")
    got = {p.name for p in mod.collect_files([str(tmp_path)])}
    assert got == {"a.md", "b.txt", "c.md"}


def test_collect_files_accepts_explicit_files(mod, tmp_path):
    f = tmp_path / "one.md"
    f.write_text("x")
    assert mod.collect_files([str(f)]) == [f]


def test_collect_files_skips_missing_targets(mod, tmp_path, capsys):
    assert mod.collect_files([str(tmp_path / "nope.md")]) == []
    assert "skipping" in capsys.readouterr().err
