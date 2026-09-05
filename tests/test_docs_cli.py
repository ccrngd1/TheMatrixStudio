# SPDX-License-Identifier: Apache-2.0
"""Phase 5b-3 tests — the ``matrix-studio docs`` CLI.

The CLI talks to the database directly (no running server), which is the same
choice the ``run`` subcommand makes. ``docs search`` is the terminal-side
measurement tool, so its honest-empty behaviour is covered too: an empty result
is the signal that shows the lexical limitation.
"""

import asyncio

import pytest

from matrix_studio.__main__ import build_parser
from matrix_studio.storage import Database


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point settings.data_dir at a temp dir and seed a run with documents."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # get_settings() is cached via a module singleton; conftest resets it between
    # tests, but reset here too so DATA_DIR is picked up for this call.
    import matrix_studio.settings as s
    s._settings = None

    async def seed():
        db = Database(str(tmp_path / "matrix_studio.db"))
        await db.connect()
        await db.create_run(
            run_id="run-1",
            topic="egress",
            cast=[{"name": "Dana", "persona": "p", "goals": []}],
            name="trusted-robot",
        )
        await db.close()

    asyncio.run(seed())
    yield tmp_path
    s._settings = None


def _run_cli(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_attach_then_list(data_dir, capsys, tmp_path):
    doc = tmp_path / "bg.md"
    doc.write_text("Egress inspection provides auditable evidence for the auditor.")

    rc = _run_cli(["docs", "run-1", "attach", str(doc), "-p", "Dana"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Attached bg.md to Dana" in out
    assert "chunks" in out

    rc = _run_cli(["docs", "run-1", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bg.md" in out and "Dana" in out


def test_attach_resolves_run_by_name(data_dir, tmp_path, capsys):
    doc = tmp_path / "bg.md"
    doc.write_text("Egress inspection evidence.")
    assert _run_cli(["docs", "trusted-robot", "attach", str(doc)]) == 0
    assert "Attached" in capsys.readouterr().out


def test_attach_cast_wide_when_no_persona(data_dir, tmp_path, capsys):
    doc = tmp_path / "shared.md"
    doc.write_text("Shared briefing material for the whole cast.")
    _run_cli(["docs", "run-1", "attach", str(doc)])
    assert "(whole cast)" in capsys.readouterr().out
    _run_cli(["docs", "run-1", "list"])
    assert "(all)" in capsys.readouterr().out


def test_attach_custom_title(data_dir, tmp_path, capsys):
    doc = tmp_path / "raw.md"
    doc.write_text("Egress inspection evidence.")
    _run_cli(["docs", "run-1", "attach", str(doc), "-t", "Nice Title"])
    assert "Nice Title" in capsys.readouterr().out


def test_unknown_run_is_an_error(data_dir, capsys, tmp_path):
    doc = tmp_path / "bg.md"
    doc.write_text("content here")
    assert _run_cli(["docs", "nope", "attach", str(doc)]) == 1
    assert "run not found" in capsys.readouterr().err


def test_missing_file_is_an_error_naming_the_cause(data_dir, capsys, tmp_path):
    rc = _run_cli(["docs", "run-1", "attach", str(tmp_path / "absent.pdf")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_unsupported_type_is_an_error(data_dir, capsys, tmp_path):
    bad = tmp_path / "sheet.xlsx"
    bad.write_text("nope")
    assert _run_cli(["docs", "run-1", "attach", str(bad)]) == 1
    assert "Unsupported" in capsys.readouterr().err


def test_list_with_no_documents(data_dir, capsys):
    assert _run_cli(["docs", "run-1", "list"]) == 0
    assert "No documents attached" in capsys.readouterr().out


def test_search_shows_terms_query_and_passages(data_dir, tmp_path, capsys):
    doc = tmp_path / "bg.md"
    doc.write_text("Egress inspection provides auditable evidence for the auditor.")
    _run_cli(["docs", "run-1", "attach", str(doc), "-p", "Dana"])
    capsys.readouterr()

    assert _run_cli(["docs", "run-1", "search", "egress", "inspection"]) == 0
    out = capsys.readouterr().out
    assert "terms :" in out
    assert '"egress" OR "inspection"' in out
    assert "bg.md #0" in out
    assert "auditable evidence" in out


def test_search_empty_result_is_honest(data_dir, tmp_path, capsys):
    """An empty result is the useful signal — it is how the lexical gap shows."""
    doc = tmp_path / "bg.md"
    doc.write_text("Egress inspection provides auditable evidence.")
    _run_cli(["docs", "run-1", "attach", str(doc), "-p", "Dana"])
    capsys.readouterr()

    assert _run_cli(["docs", "run-1", "search", "how", "much", "money", "burns"]) == 0
    out = capsys.readouterr().out
    assert "No passages matched" in out


def test_search_with_only_stopwords_says_so(data_dir, capsys):
    assert _run_cli(["docs", "run-1", "search", "the", "a", "of"]) == 0
    assert "No searchable terms" in capsys.readouterr().out


def test_search_respects_max_chars(data_dir, tmp_path, capsys):
    doc = tmp_path / "big.md"
    doc.write_text(
        "\n\n".join(f"Paragraph {i} about egress inspection evidence." for i in range(80))
    )
    _run_cli(["docs", "run-1", "attach", str(doc), "-p", "Dana"])
    capsys.readouterr()
    _run_cli(["docs", "run-1", "search", "egress", "inspection",
              "--max-chars", "200", "-k", "5"])
    out = capsys.readouterr().out
    reported = int(out.split("passage(s), ")[1].split(" chars")[0])
    assert reported <= 200


def test_search_scoped_to_a_persona(data_dir, tmp_path, capsys):
    dana = tmp_path / "dana.md"
    dana.write_text("Egress inspection auditable evidence.")
    other = tmp_path / "other.md"
    other.write_text("Token accounting measured delta.")
    _run_cli(["docs", "run-1", "attach", str(dana), "-p", "Dana"])
    _run_cli(["docs", "run-1", "attach", str(other), "-p", "Marcus"])
    capsys.readouterr()
    _run_cli(["docs", "run-1", "search", "egress", "token", "-p", "Dana"])
    out = capsys.readouterr().out
    assert "dana.md" in out and "other.md" not in out


def test_reindex_reports_chunk_count(data_dir, tmp_path, capsys):
    doc = tmp_path / "bg.md"
    doc.write_text("Egress inspection evidence.")
    _run_cli(["docs", "run-1", "attach", str(doc), "-p", "Dana"])
    capsys.readouterr()
    assert _run_cli(["docs", "run-1", "reindex"]) == 0
    assert "Rebuilt the document index" in capsys.readouterr().out


def test_docs_requires_an_action():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["docs", "run-1"])
