"""Extract plain text from uploaded documents, dispatched by extension.

text / markdown / HTML need no extra deps (HTML via stdlib ``html.parser``).
PDF and DOCX use the ``parsers`` extra (pypdf / python-docx), imported lazily so
the package imports clean without them.
"""
from __future__ import annotations

import io
import os
import zipfile
from html.parser import HTMLParser
from pathlib import Path

# Cap the on-disk size of a document read into memory. Without it a single huge
# file (a multi-GB .txt/.pdf) OOMs the process during extract_text -- mirroring
# the byte cap image.py already enforces before decoding an image. 0 disables.
DEFAULT_MAX_DOC_BYTES = 25 * 1024 * 1024
# Cap the DECOMPRESSED size of a zip-based document (DOCX). A small .docx can
# inflate to gigabytes (a zip bomb); python-docx applies no such limit, and the
# on-disk cap above bounds only the compressed input, not the expansion. 0 off.
DEFAULT_MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
# Cap the page count of a PDF. PDF content streams are Flate-compressed, so a
# small on-disk file can decompress to gigabytes and an attacker can pack
# millions of pages via object streams; the on-disk cap above bounds only the
# compressed input, not that expansion. Mirrors the DOCX zip-bomb guard. 0 off.
DEFAULT_MAX_PDF_PAGES = 5000
ISOLATED_TRANSPORT_MAX_BYTES = 64 * 1024 * 1024
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _check_on_disk_size(path: Path) -> None:
    cap = _int_env("MAVERICK_KNOWLEDGE_MAX_DOC_BYTES", DEFAULT_MAX_DOC_BYTES)
    if cap <= 0:
        return
    size = path.stat().st_size
    if size > cap:
        raise ValueError(
            f"document too large to ingest ({size} bytes > {cap} bytes); raise "
            "MAVERICK_KNOWLEDGE_MAX_DOC_BYTES to allow it"
        )


def _read_document_bytes(path: Path) -> bytes:
    """Read an upload once with a hard bound before sending it to the child."""
    configured = _int_env("MAVERICK_KNOWLEDGE_MAX_DOC_BYTES", DEFAULT_MAX_DOC_BYTES)
    cap = (
        min(configured, ISOLATED_TRANSPORT_MAX_BYTES)
        if configured > 0
        else ISOLATED_TRANSPORT_MAX_BYTES
    )
    with path.open("rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError(
            f"document too large to ingest ({len(data)} bytes > {cap} bytes)"
        )
    return data


def _check_docx_uncompressed_size(source, *, cap: int | None = None) -> None:
    """Reject a DOCX whose declared uncompressed size exceeds the cap, BEFORE
    handing it to python-docx (which would expand a zip bomb into memory)."""
    if cap is None:
        cap = _int_env("MAVERICK_KNOWLEDGE_MAX_DOCX_UNCOMPRESSED_BYTES",
                       DEFAULT_MAX_DOCX_UNCOMPRESSED_BYTES)
    if cap <= 0:
        return
    try:
        with zipfile.ZipFile(source) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as e:
        raise ValueError("not a valid .docx (corrupt zip container)") from e
    if total > cap:
        raise ValueError(
            f"docx decompresses too large ({total} bytes > {cap} bytes); "
            "refusing to expand a possible zip bomb"
        )


def _check_pdf_page_count(reader, *, cap: int | None = None) -> None:
    """Reject a PDF whose page count exceeds the cap, BEFORE iterating pages and
    decompressing their (Flate-compressed) content streams into memory."""
    if cap is None:
        cap = _int_env("MAVERICK_KNOWLEDGE_MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES)
    if cap <= 0:
        return
    pages = len(reader.pages)
    if pages > cap:
        raise ValueError(
            f"pdf has too many pages to ingest ({pages} > {cap}); raise "
            "MAVERICK_KNOWLEDGE_MAX_PDF_PAGES to allow it"
        )


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw)
    return " ".join(p.parts)


IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
})


def is_image(path: str | Path) -> bool:
    """True if ``path`` looks like an image (by extension). Images can't be read
    as UTF-8 -- they need an image_describer (OCR or a vision model) to become
    text, which the KnowledgeBase routes them through."""
    return Path(path).suffix.lower() in IMAGE_EXTS


def _extract_pdf_bytes(data: bytes, *, max_pages: int | None = None) -> str:
    """Child entry point for PDF parsing; direct use is trusted/test-only."""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "PDF parsing needs the 'parsers' extra: "
            "From the reviewed checkout run: python -m pip install -e "
            "'./packages/maverick-knowledge[parsers]'"
        ) from e
    reader = PdfReader(io.BytesIO(data))
    _check_pdf_page_count(reader, cap=max_pages)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx_bytes(
    data: bytes,
    *,
    max_uncompressed_bytes: int | None = None,
) -> str:
    """Child entry point for DOCX parsing; direct use is trusted/test-only."""
    _check_docx_uncompressed_size(
        io.BytesIO(data),
        cap=max_uncompressed_bytes,
    )
    try:
        import docx  # python-docx
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "DOCX parsing needs the 'parsers' extra: "
            "From the reviewed checkout run: python -m pip install -e "
            "'./packages/maverick-knowledge[parsers]'"
        ) from e
    return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)


def _trusted_inprocess_requested() -> bool:
    return os.environ.get(
        "MAVERICK_TRUSTED_IN_PROCESS_PARSERS", ""
    ).strip().lower() in _TRUE_VALUES


def _parse_rich_document(
    parser_name: str,
    label: str,
    data: bytes,
    trusted_parser,
    **kwargs,
) -> str:
    """Isolate an untrusted rich document, with no failure fallback."""
    try:
        from maverick.parser_isolation import parse_isolated, should_isolate
    except (ImportError, ModuleNotFoundError) as exc:
        if _trusted_inprocess_requested():
            return trusted_parser(data, **kwargs)
        raise RuntimeError(
            f"isolated {label} parser runtime is unavailable; refusing "
            "in-process parsing. For trusted tests only, set "
            "MAVERICK_TRUSTED_IN_PROCESS_PARSERS=1"
        ) from exc

    if not should_isolate():
        return trusted_parser(data, **kwargs)
    try:
        result = parse_isolated(parser_name, data, **kwargs)
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"isolated {label} parsing failed; in-process fallback is disabled: {exc}"
        ) from exc
    if not isinstance(result, str):
        raise RuntimeError(f"isolated {label} parser returned a non-text result")
    return result


def extract_text(path: str | Path) -> str:
    """Return document text, isolating untrusted PDF/DOCX parsing by default."""
    path = Path(path)
    suffix = path.suffix.lower()
    # Bound the on-disk size before any read/parse loads the file into memory.
    if not is_image(path):
        _check_on_disk_size(path)
    if suffix in (".html", ".htm"):
        return _html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".pdf":
        data = _read_document_bytes(path)
        return _parse_rich_document(
            "knowledge_pdf_text",
            "PDF",
            data,
            _extract_pdf_bytes,
            max_pages=_int_env(
                "MAVERICK_KNOWLEDGE_MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES
            ),
        )
    if suffix == ".docx":
        data = _read_document_bytes(path)
        return _parse_rich_document(
            "knowledge_docx_text",
            "DOCX",
            data,
            _extract_docx_bytes,
            max_uncompressed_bytes=_int_env(
                "MAVERICK_KNOWLEDGE_MAX_DOCX_UNCOMPRESSED_BYTES",
                DEFAULT_MAX_DOCX_UNCOMPRESSED_BYTES,
            ),
        )
    if is_image(path):
        raise RuntimeError(
            "images need an image_describer (OCR or a vision model); the "
            "KnowledgeBase routes them there before extract_text"
        )
    # text / markdown / unknown -> read as UTF-8 text.
    return path.read_text(encoding="utf-8", errors="replace")
