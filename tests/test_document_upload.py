# SPDX-License-Identifier: Apache-2.0
"""
Uploading a file as a persona's knowledge base: /api/documents/formats and
/api/documents/extract.

The endpoint extracts and returns text, storing nothing. That shape is what lets a
knowledge-base file be authored BEFORE the run exists — the engine ingests cast
documents ahead of turn 1, so attaching after creation is too late — and it means an
uploaded file becomes an ordinary `document_texts` entry rather than a second ingest
path to keep in step.

PDF and DOCX are exercised with real files built by pypdf/python-docx rather than
mocks, because the thing most likely to break is extraction against a real container
format, which a mocked extractor cannot tell us anything about.
"""

import io

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.documents import format_support


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "upload.db"))
    with TestClient(app) as c:
        yield c


def _post(client, name, data, title=None):
    files = {"file": (name, data if isinstance(data, bytes) else data.encode())}
    return client.post("/api/documents/extract", files=files,
                       data={"title": title} if title else None)


# --------------------------- format advertisement --------------------------- #

def test_formats_lists_every_supported_type_with_availability(client):
    """The picker needs to know what will work here BEFORE a file is chosen."""
    body = client.get("/api/documents/formats").json()
    by_suffix = {f["suffix"]: f for f in body["formats"]}
    assert {".txt", ".md", ".pdf", ".docx"} <= set(by_suffix)
    # Plain text never needs an extra, so it must always report available.
    assert by_suffix[".txt"]["available"] is True
    assert by_suffix[".txt"]["needs"] is None
    # Limits are advertised so the form can state them rather than guess.
    assert body["max_upload_bytes"] > 0
    assert body["max_document_chars"] > 0


def test_formats_names_the_missing_package_when_a_type_is_unavailable(monkeypatch):
    """
    An unavailable format reports what to install.

    PDF/Word support is an optional extra, so "the code supports it" and "this install
    can do it" differ. Without the package name the operator gets a dead end.
    """
    import matrix_studio.documents as docs_mod

    monkeypatch.setattr(docs_mod, "SUPPORTED_SUFFIXES",
                        {".txt": ("txt", None), ".pdf": ("pdf", "pypdf")})
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    by_suffix = {f.suffix: f for f in format_support()}
    assert by_suffix[".pdf"].available is False
    assert by_suffix[".pdf"].needs == "pypdf"
    # A format needing nothing stays available even when imports are unavailable.
    assert by_suffix[".txt"].available is True


def test_unavailable_format_is_refused_with_install_instructions(client, monkeypatch):
    import matrix_studio.api.app as app_mod

    from matrix_studio.documents import FormatSupport

    monkeypatch.setattr(app_mod, "format_support", lambda: [
        FormatSupport(suffix=".pdf", media_type="pdf", available=False, needs="pypdf"),
    ])
    r = _post(client, "brief.pdf", b"%PDF-1.4 whatever")
    assert r.status_code == 422
    assert "pypdf" in r.json()["detail"]
    assert "matrix-sim-studio[documents]" in r.json()["detail"]


# ------------------------------- extraction -------------------------------- #

def test_txt_upload_returns_editable_text(client):
    r = _post(client, "constraints.txt", "No feature may add an external service.\n")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "constraints.txt"
    assert body["media_type"] == "txt"
    assert "No feature may add an external service." in body["text"]
    assert body["char_count"] > 0
    assert body["chunk_count"] >= 1
    # Nothing was persisted: the text is the whole artefact the caller now owns.
    assert body["stored"] is False


def test_docx_upload_extracts_paragraphs_and_tables(client):
    """
    Word tables carry real content in briefs and specs.

    Built with python-docx so this exercises the actual container format; a mocked
    extractor would pass whether or not the table branch works.
    """
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("The migration must be reversible.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Region"
    table.cell(0, 1).text = "Cutover date"
    table.cell(1, 0).text = "eu-west-1"
    table.cell(1, 1).text = "March"
    buf = io.BytesIO()
    document.save(buf)

    r = _post(client, "plan.docx", buf.getvalue())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["media_type"] == "docx"
    assert "The migration must be reversible." in body["text"]
    # Table content, flattened row-wise.
    assert "eu-west-1" in body["text"]
    assert "Cutover date" in body["text"]


def _text_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF carrying a real text content stream.

    Hand-built rather than produced with reportlab: needing a PDF *writer* only to
    test the reader would mean either a new dependency or a skipped test, and a
    skipped test on the format most likely to give trouble proves nothing. pypdf's
    own `add_blank_page` has no text layer, so it cannot stand in either.
    """
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\n")
    out.write(b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n")
    return out.getvalue()


def test_pdf_upload_extracts_page_text(client):
    pytest.importorskip("pypdf")
    r = _post(client, "requirements.pdf", _text_pdf("Rollback is a hard requirement."))
    assert r.status_code == 200, r.text
    assert r.json()["media_type"] == "pdf"
    assert "Rollback is a hard requirement." in r.json()["text"]


def test_scanned_pdf_with_no_text_layer_says_so(client):
    """
    The failure a user will actually hit, and cannot diagnose from a blank box.

    A scanned PDF is a valid file that yields no text. Returning success with an empty
    string would put a silently useless document into a persona's knowledge base.
    """
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    r = _post(client, "scan.pdf", buf.getvalue())
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "No extractable text" in detail
    assert "scanned" in detail.lower()
    # Names the file the OPERATOR chose. Extraction runs against a temp file, and
    # reporting that name ("No extractable text in tmp7_gez8qn.pdf") identifies
    # nothing — which matters most in a multi-file upload, where it is the only clue
    # to which file failed.
    assert "scan.pdf" in detail
    assert "tmp" not in detail


def test_title_can_be_overridden(client):
    r = _post(client, "untitled-1.txt", "Some background.", title="Distribution constraints")
    assert r.status_code == 200
    assert r.json()["title"] == "Distribution constraints"


# ------------------------------ input guards ------------------------------- #

def test_unsupported_extension_is_refused_and_lists_what_works(client):
    r = _post(client, "notes.rtf", b"{\\rtf1}")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert ".rtf" in detail
    assert ".txt" in detail and ".pdf" in detail


def test_a_filename_with_no_extension_is_refused(client):
    # Extractor dispatch is by suffix, so there is nothing to dispatch on.
    r = _post(client, "README", b"some text")
    assert r.status_code == 422
    assert "(none)" in r.json()["detail"]


def test_path_components_in_the_filename_are_ignored(client):
    """
    Only the base name is used.

    The client-supplied filename reaches both the extractor dispatch and the default
    title. A traversal-shaped name must not be treated as a path.
    """
    r = _post(client, "../../../etc/passwd.txt", "harmless text")
    assert r.status_code == 200
    assert r.json()["title"] == "passwd.txt"
    assert "/" not in r.json()["title"]


def test_empty_file_is_refused(client):
    r = _post(client, "empty.txt", b"")
    assert r.status_code == 422
    assert "empty" in r.json()["detail"].lower()


def test_oversized_upload_is_refused_by_byte_count(client, monkeypatch):
    """
    The byte cap is enforced server-side and while streaming.

    A browser-side check is advice; this endpoint accepts arbitrary bytes from
    anything that can post a form.
    """
    from matrix_studio.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_bytes", 2048, raising=False)
    r = _post(client, "big.txt", b"x" * 5000)
    assert r.status_code == 413
    assert "larger than" in r.json()["detail"]


def test_a_file_within_the_byte_cap_still_succeeds(client, monkeypatch):
    """Guard against the cap test passing because everything is refused."""
    from matrix_studio.settings import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 2048, raising=False)
    r = _post(client, "small.txt", b"y" * 1000)
    assert r.status_code == 200


def test_text_over_the_character_cap_is_refused_with_advice(client, monkeypatch):
    """
    A file can be small on disk and huge as text, so the two caps are independent.

    Refusing beats truncating: a silently half-loaded knowledge base looks like a
    complete one, and the persona would cite material that stops mid-document.
    """
    from matrix_studio.settings import get_settings

    monkeypatch.setattr(get_settings(), "max_document_chars", 500, raising=False)
    r = _post(client, "long.txt", "word " * 500)
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert "characters" in detail
    assert "separately" in detail  # tells the operator what to do about it


def test_extraction_stores_nothing_and_leaves_no_temp_file(client, tmp_path, monkeypatch):
    """The uploaded file is transient; a leaked temp file per upload would accumulate."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", str(scratch), raising=False)

    assert _post(client, "a.txt", "content one").status_code == 200
    assert _post(client, "b.txt", "content two").status_code == 200
    assert list(scratch.iterdir()) == []


def test_upload_needs_no_run_to_exist(client):
    """
    Deliberately run-agnostic.

    A persona's knowledge base is authored before the run is created, and the same
    endpoint serves adding a file to a run that already exists.
    """
    assert client.get("/api/runs").json()["runs"] == []
    assert _post(client, "background.txt", "Something to know.").status_code == 200
