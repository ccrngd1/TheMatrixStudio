# SPDX-License-Identifier: Apache-2.0
"""Phase 5: document ingestion — text extraction and chunking.

Turns an operator-supplied file (PDF, Word, text, Markdown) into a list of
chunks suitable for FTS5 indexing, so background material can be attached to a
specific persona without occupying prompt context on every call.

Design notes:

- **Extraction is pure and synchronous.** No LLM, no network, no database. That
  keeps it trivially testable and keeps ingestion cost at zero.
- **PDF and Word support are optional extras.** ``.txt`` and ``.md`` need nothing
  installed. A missing extractor raises ``ExtractionError`` naming the package to
  install, rather than failing obscurely deep inside a third-party import — the
  base install stays light, which is the constraint ``PROJECT-SPEC.md`` §7 pins.
- **Chunking is paragraph-aware with overlap.** Splitting mid-sentence produces
  chunks that retrieve badly; overlap stops a passage that straddles a boundary
  from being unfindable from either side.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Target chunk size in characters. Chosen so a retrieved chunk is a substantial
# passage (a few paragraphs) while several still fit inside a modest per-call
# character budget — the budget is the point of the feature.
DEFAULT_CHUNK_CHARS = 900
DEFAULT_CHUNK_OVERLAP = 150

# Extension -> (media_type, optional package needed)
SUPPORTED_SUFFIXES = {
    ".txt": ("txt", None),
    ".text": ("txt", None),
    ".md": ("md", None),
    ".markdown": ("md", None),
    ".pdf": ("pdf", "pypdf"),
    ".docx": ("docx", "python-docx"),
}


# Import name of each optional extractor, keyed by the package to install. Needed
# because the two differ for python-docx, and a capability check has to probe the
# import name while an error message has to name the install one.
_EXTRACTOR_IMPORTS = {"pypdf": "pypdf", "python-docx": "docx"}


@dataclass(frozen=True)
class FormatSupport:
    """Whether one document format can be read by this install."""

    suffix: str
    media_type: str
    available: bool
    #: Package to install when unavailable; None when it works already.
    needs: Optional[str]


def format_support() -> List[FormatSupport]:
    """Which document formats this install can actually read, and what is missing.

    PDF and Word extraction are optional extras (the base install is a pinned
    five-minute quickstart), so "supported by the code" and "usable right now" are
    different questions. A UI that offers a .pdf picker on an install without pypdf
    produces an error the operator cannot act on from the file dialog, so the answer
    has to be available BEFORE the upload.
    """
    from importlib.util import find_spec

    out: List[FormatSupport] = []
    for suffix, (media_type, package) in sorted(SUPPORTED_SUFFIXES.items()):
        if package is None:
            available, needs = True, None
        else:
            available = find_spec(_EXTRACTOR_IMPORTS[package]) is not None
            needs = None if available else package
        out.append(FormatSupport(suffix=suffix, media_type=media_type,
                                 available=available, needs=needs))
    return out


class ExtractionError(RuntimeError):
    """Raised when a document's text cannot be extracted.

    Carries a message naming the missing package where that is the cause, so the
    operator gets an actionable error instead of a traceback from a library they
    did not know was involved.
    """


@dataclass(frozen=True)
class Chunk:
    """One indexed passage of a document."""

    ordinal: int
    content: str


@dataclass(frozen=True)
class ExtractedDocument:
    """The result of ingesting one file."""

    title: str
    media_type: str
    text: str
    chunks: List[Chunk]
    source_path: Optional[str] = None

    @property
    def char_count(self) -> int:
        return len(self.text)


def normalise_text(raw: str) -> str:
    """Collapse extraction noise without destroying paragraph structure.

    PDF extraction in particular yields hard-wrapped lines, ligatures and
    non-breaking spaces. Paragraph breaks are preserved because chunking uses
    them; everything else is flattened so BM25 tokenisation sees clean words.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # De-hyphenate words broken across a line break ("re-\ntrieval" -> "retrieval").
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Protect paragraph breaks, flatten single newlines into spaces, restore.
    text = re.sub(r"\n{2,}", "\x00", text)
    # Markdown structure lines — list items, headings, table rows — are their own
    # units, not continuations of the line above. Without this a bullet list
    # collapses into one enormous paragraph and gets split mid-item, producing
    # chunks that open on a fragment ("...never rewritten in place - **Pending
    # Threads**..."). Same failure mode as an unaligned overlap tail.
    text = re.sub(r"\n(?=[ \t]*(?:[-*+][ \t]|\d+[.)][ \t]|#{1,6}[ \t]|\|))", "\x00", text)
    text = text.replace("\n", " ")
    text = text.replace("\x00", "\n\n")
    # Collapse runs of horizontal whitespace. The NFKC pass above has already
    # folded non-breaking and other exotic spaces down to plain spaces.
    text = re.sub(r"[ \t]+", " ", text)
    # Trim each paragraph and drop empties.
    paragraphs = [p.strip() for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def _sentence_aligned_tail(text: str, overlap: int) -> str:
    """The trailing ``overlap`` characters of ``text``, snapped to a sentence start.

    A naive ``text[-overlap:]`` cuts mid-sentence, which produced a real failure:
    a chunk that began ``". This is a correctness requirement, not hardening."``
    had lost the antecedent of "This", and a persona quoted it verbatim and
    inferred the OPPOSITE of what the source meant. Measured on one real document,
    90% of chunks began mid-sentence.

    So the tail starts after the first sentence boundary inside the window. If the
    window contains no boundary, NO overlap is carried: a fragment that cannot be
    read on its own is worse than a missing one, because retrieval surfaces it as
    quotable evidence.
    """
    if overlap <= 0 or not text:
        return ""
    window = text[-overlap:]
    match = re.search(r"(?<=[.!?])\s+", window)
    if match:
        return window[match.end():].lstrip()
    return ""


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Chunk]:
    """Split normalised text into overlapping, paragraph-aligned chunks.

    Paragraphs are packed until adding the next would exceed ``chunk_chars``. A
    paragraph longer than ``chunk_chars`` on its own is split on sentence
    boundaries, and only split mid-sentence as a last resort.

    Every chunk after the first is prefixed with roughly ``overlap`` characters of
    the previous chunk's tail, so a passage spanning a boundary stays retrievable
    from either side. The overlap is *additive*, which makes the hard upper bound
    on chunk length ``chunk_chars + overlap + 2``, not ``chunk_chars``. Charging
    overlap against the packing budget instead would silently drop it whenever a
    single paragraph nearly filled a chunk — exactly the boundary case overlap
    exists to cover. ``overlap`` is clamped to half of ``chunk_chars`` so a chunk
    can never be mostly duplicated context.
    """
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be positive")
    overlap = max(0, min(overlap, chunk_chars // 2))

    text = text.strip()
    if not text:
        return []

    # Split into units no larger than chunk_chars.
    units: List[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_chars:
            units.append(para)
            continue
        # Oversized paragraph: break on sentence boundaries.
        sentences = re.split(r"(?<=[.!?])\s+", para)
        buf = ""
        for sentence in sentences:
            while len(sentence) > chunk_chars:
                # Pathological single sentence (e.g. a table dumped as one line).
                if buf:
                    units.append(buf)
                    buf = ""
                units.append(sentence[:chunk_chars])
                sentence = sentence[chunk_chars:]
            if not buf:
                buf = sentence
            elif len(buf) + 1 + len(sentence) <= chunk_chars:
                buf = f"{buf} {sentence}"
            else:
                units.append(buf)
                buf = sentence
        if buf:
            units.append(buf)

    # Pack units into chunks.
    chunks: List[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 2 + len(unit) <= chunk_chars:
            current = f"{current}\n\n{unit}"
        else:
            chunks.append(current)
            # Carry the previous chunk's tail forward. See the docstring: overlap
            # is additive to chunk_chars precisely so that a near-chunk-sized
            # paragraph still gets boundary context.
            tail = _sentence_aligned_tail(current, overlap)
            current = f"{tail}\n\n{unit}" if tail else unit
    if current:
        chunks.append(current)

    return [Chunk(ordinal=i, content=c) for i, c in enumerate(chunks)]


def _new_part(so_far: str, chunk: str) -> str:
    """The part of ``chunk`` that is not the tail ``chunk_text`` carried into it.

    ``chunk_text`` builds every chunk after the first as ``tail + "\\n\\n" + unit``,
    where ``tail`` is a suffix of the previous chunk. So the carried region is
    exactly the longest prefix of ``chunk`` that (a) is a suffix of the text
    assembled so far and (b) is followed by the ``"\\n\\n"`` it was joined with.
    Requiring the separator is what keeps this from matching an accidental one- or
    two-character coincidence; candidates are the paragraph breaks only, which also
    keeps the scan cheap.

    If nothing matches — no overlap was carried, or these chunks came from
    somewhere other than ``chunk_text`` — the whole chunk is new. Erring that way
    can only duplicate text, never lose it.
    """
    breaks = [m.start() for m in re.finditer(r"\n\n", chunk)]
    for k in reversed(breaks):
        if k and so_far.endswith(chunk[:k]):
            return chunk[k + 2:]
    return chunk


def join_chunks(chunks: List[str]) -> str:
    """Reassemble ``chunk_text`` output back into the document text.

    Chunks deliberately overlap, so plain concatenation repeats every boundary
    region — on a document chunked at the defaults that is ~150 duplicated
    characters per boundary, which would come back as visibly stuttering text if
    a stored document were ever shown to a human or re-submitted as a setup.

    Exact inverse of ``chunk_text`` for any ``chunk_chars``/``overlap``: the
    overlap is discovered per boundary rather than assumed, so it does not need to
    know which settings produced the chunks.
    """
    out = ""
    for chunk in chunks:
        if not out:
            out = chunk
            continue
        new = _new_part(out, chunk)
        if new:
            out = f"{out}\n\n{new}"
    return out


def _extract_pdf(path: Path, shown: Optional[str] = None) -> str:
    shown = shown or path.name
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ExtractionError(
            f"Reading {shown} needs the 'pypdf' package. "
            "Install it with: pip install 'matrix-sim-studio[documents]'"
        ) from exc
    try:
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Could not read PDF {shown}: {exc}") from exc


def _extract_docx(path: Path, shown: Optional[str] = None) -> str:
    shown = shown or path.name
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ExtractionError(
            f"Reading {shown} needs the 'python-docx' package. "
            "Install it with: pip install 'matrix-sim-studio[documents]'"
        ) from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"Could not read Word file {shown}: {exc}") from exc
    parts = [p.text for p in document.paragraphs]
    # Tables carry real content in specs and briefs; flatten them row-wise.
    for table in getattr(document, "tables", []):
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def _extract_plain(path: Path, shown: Optional[str] = None) -> str:
    shown = shown or path.name
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise ExtractionError(f"Could not read {shown}: {exc}") from exc
    raise ExtractionError(f"Could not decode {shown} as text")


def extract_text(path: Path, *, display_name: Optional[str] = None) -> tuple[str, str]:
    """Extract raw text from a supported file. Returns ``(text, media_type)``.

    ``display_name`` overrides the name used in error messages; see ``ingest_file``.
    """
    shown = display_name or path.name
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ExtractionError(
            f"Unsupported document type {suffix or '(none)'} for {shown}. "
            f"Supported: {supported}"
        )
    media_type, _package = SUPPORTED_SUFFIXES[suffix]
    if media_type == "pdf":
        return _extract_pdf(path, shown), media_type
    if media_type == "docx":
        return _extract_docx(path, shown), media_type
    return _extract_plain(path, shown), media_type


def ingest_file(
    path: str | Path,
    *,
    title: Optional[str] = None,
    display_name: Optional[str] = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> ExtractedDocument:
    """Read, normalise and chunk a file on disk.

    ``display_name`` is the name used in error messages. It exists because an upload
    is extracted from a temporary file: without it, "No extractable text in
    tmp7_gez8qn.pdf" is what the operator sees, which names nothing they chose and is
    useless for identifying the offending file in a multi-file upload.
    """
    p = Path(path)
    shown = display_name or p.name
    if not p.exists():
        raise ExtractionError(f"Document not found: {p}")
    if not p.is_file():
        raise ExtractionError(f"Not a file: {p}")
    raw, media_type = extract_text(p, display_name=shown)
    text = normalise_text(raw)
    if not text:
        raise ExtractionError(
            f"No extractable text in {shown} "
            "(a scanned PDF with no text layer would do this)"
        )
    return ExtractedDocument(
        title=title or p.name,
        media_type=media_type,
        text=text,
        chunks=chunk_text(text, chunk_chars, overlap),
        source_path=str(p),
    )


def ingest_text(
    text: str,
    *,
    title: str,
    media_type: str = "txt",
    source_path: Optional[str] = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> ExtractedDocument:
    """Ingest text supplied directly (API upload, paste, or a test)."""
    normalised = normalise_text(text)
    if not normalised:
        raise ExtractionError(f"No text content supplied for {title!r}")
    return ExtractedDocument(
        title=title,
        media_type=media_type,
        text=normalised,
        chunks=chunk_text(normalised, chunk_chars, overlap),
        source_path=source_path,
    )
