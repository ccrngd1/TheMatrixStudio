# SPDX-License-Identifier: Apache-2.0
"""Tests for Phase 5 document ingestion (extraction, normalisation, chunking)."""

import pytest

from matrix_studio.documents import (
    DEFAULT_CHUNK_CHARS,
    ExtractionError,
    chunk_text,
    ingest_file,
    ingest_text,
    normalise_text,
)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def test_normalise_preserves_paragraphs_but_flattens_hard_wraps():
    """PDF extraction hard-wraps lines; those are not paragraph breaks."""
    raw = "First line\nsecond line of same para.\n\nA new paragraph."
    out = normalise_text(raw)
    assert out == "First line second line of same para.\n\nA new paragraph."


def test_normalise_dehyphenates_across_line_breaks():
    assert "retrieval" in normalise_text("we discuss re-\ntrieval here")


def test_normalise_collapses_exotic_whitespace():
    raw = "spend here\ttoo   many   spaces"
    out = normalise_text(raw)
    assert " " not in out
    assert "  " not in out


def test_normalise_drops_empty_paragraphs():
    assert normalise_text("a\n\n\n\n\nb") == "a\n\nb"


def test_normalise_empty_input():
    assert normalise_text("") == ""
    assert normalise_text("   \n\n  ") == ""


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_chunk_short_text_is_one_chunk():
    chunks = chunk_text("Just a short note.")
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].content == "Just a short note."


def test_chunk_respects_size_limit():
    para = "word " * 100  # ~500 chars
    text = "\n\n".join([para.strip()] * 6)
    chunks = chunk_text(text, chunk_chars=600, overlap=0)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 600


def test_chunk_ordinals_are_sequential():
    text = "\n\n".join(f"Paragraph number {i} with some body text." for i in range(40))
    chunks = chunk_text(text, chunk_chars=200, overlap=20)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunk_overlap_carries_trailing_context():
    """A passage on a boundary must be findable from the following chunk too."""
    a = "alpha " * 40
    b = "bravo " * 40
    chunks = chunk_text(f"{a.strip()}\n\n{b.strip()}", chunk_chars=300, overlap=100)
    assert len(chunks) >= 2
    assert "alpha" in chunks[1].content, "overlap did not carry prior context forward"


def test_chunk_zero_overlap_has_no_carryover():
    a = "alpha " * 40
    b = "bravo " * 40
    chunks = chunk_text(f"{a.strip()}\n\n{b.strip()}", chunk_chars=300, overlap=0)
    assert "alpha" not in chunks[1].content


def test_chunk_splits_oversized_paragraph_on_sentences():
    para = " ".join(f"Sentence number {i} about retrieval." for i in range(80))
    chunks = chunk_text(para, chunk_chars=300, overlap=0)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 300


def test_chunk_handles_pathological_single_long_sentence():
    """A table dumped as one unbroken line must not produce an oversized chunk."""
    text = "x" * 5000
    chunks = chunk_text(text, chunk_chars=400, overlap=0)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 400


def test_chunk_empty_text_is_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("    ") == []


def test_chunk_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_chars=0)


def test_chunk_overlap_cannot_exceed_half_chunk():
    """A huge overlap must not cause runaway duplication or an infinite loop.

    overlap is clamped to chunk_chars // 2, and the documented hard bound on a
    chunk is chunk_chars + overlap + 2.
    """
    text = "\n\n".join(f"Para {i} body text here." for i in range(30))
    chunks = chunk_text(text, chunk_chars=100, overlap=10_000)
    assert chunks
    for c in chunks:
        assert len(c.content) <= 100 + 50 + 2


def test_chunk_bound_includes_overlap():
    """Overlap is additive: the bound is chunk_chars + overlap + 2."""
    text = "\n\n".join("word " * 50 for _ in range(20))
    chunk_chars, overlap = 400, 120
    chunks = chunk_text(text, chunk_chars=chunk_chars, overlap=overlap)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= chunk_chars + overlap + 2


# --------------------------------------------------------------------------
# File ingestion
# --------------------------------------------------------------------------


def test_ingest_txt_file(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("Egress costs matter.\n\nSo does auditability.")
    doc = ingest_file(p)
    assert doc.title == "notes.txt"
    assert doc.media_type == "txt"
    assert doc.chunks
    assert "Egress" in doc.text
    assert doc.char_count == len(doc.text)
    assert doc.source_path == str(p)


def test_ingest_markdown_file(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Title\n\nSome body text about retrieval.")
    doc = ingest_file(p)
    assert doc.media_type == "md"
    assert "retrieval" in doc.text


def test_ingest_custom_title(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("content here")
    assert ingest_file(p, title="Nice Name").title == "Nice Name"


def test_ingest_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(ExtractionError, match="not found"):
        ingest_file(tmp_path / "nope.txt")


def test_ingest_directory_raises(tmp_path):
    with pytest.raises(ExtractionError, match="Not a file"):
        ingest_file(tmp_path)


def test_ingest_unsupported_type_names_supported_ones(tmp_path):
    p = tmp_path / "thing.xlsx"
    p.write_text("nope")
    with pytest.raises(ExtractionError) as exc:
        ingest_file(p)
    assert ".pdf" in str(exc.value) and ".txt" in str(exc.value)


def test_ingest_empty_file_raises_rather_than_indexing_nothing(tmp_path):
    p = tmp_path / "blank.txt"
    p.write_text("   \n\n  ")
    with pytest.raises(ExtractionError, match="No extractable text"):
        ingest_file(p)


def test_ingest_latin1_fallback(tmp_path):
    p = tmp_path / "legacy.txt"
    p.write_bytes("café costs".encode("latin-1"))
    assert "costs" in ingest_file(p).text


def test_missing_pypdf_gives_actionable_message(tmp_path, monkeypatch):
    """A missing optional extractor must name the package to install."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ExtractionError) as exc:
        ingest_file(p)
    assert "pypdf" in str(exc.value)
    assert "pip install" in str(exc.value)


def test_missing_python_docx_gives_actionable_message(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("no docx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "doc.docx"
    p.write_bytes(b"PK fake")
    with pytest.raises(ExtractionError) as exc:
        ingest_file(p)
    assert "python-docx" in str(exc.value)


# --------------------------------------------------------------------------
# Direct text ingestion (API upload / paste path)
# --------------------------------------------------------------------------


def test_ingest_text_basic():
    doc = ingest_text("Some background about cost.", title="pasted")
    assert doc.title == "pasted"
    assert doc.media_type == "txt"
    assert len(doc.chunks) == 1


def test_ingest_text_empty_raises():
    with pytest.raises(ExtractionError, match="No text content"):
        ingest_text("   ", title="empty")


def test_ingest_text_long_document_chunks():
    body = "\n\n".join(f"Paragraph {i} discussing retrieval design." for i in range(200))
    doc = ingest_text(body, title="long")
    assert len(doc.chunks) > 1
    for c in doc.chunks:
        assert len(c.content) <= DEFAULT_CHUNK_CHARS
