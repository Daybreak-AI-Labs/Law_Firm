"""Document parsing size guards in extract_text.

Mirrors the byte cap image.py enforces before decoding: a document is bounded
on disk before it is read into memory, and a zip-based DOCX is bounded on its
DECOMPRESSED size before python-docx expands it (zip-bomb guard).
"""
from __future__ import annotations

import builtins
import zipfile

import pytest
from maverick_knowledge import parse as parse_module
from maverick_knowledge.parse import (
    _check_docx_uncompressed_size,
    _check_pdf_page_count,
    extract_text,
)


class _StubReader:
    """Minimal stand-in for pypdf.PdfReader: only ``.pages`` is consulted by the
    page-count guard, so we avoid needing the optional 'parsers' extra here."""

    def __init__(self, n_pages):
        self.pages = list(range(n_pages))


def test_small_text_file_extracts(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello world", encoding="utf-8")
    assert extract_text(p) == "hello world"


def test_oversized_file_rejected_before_read(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_DOC_BYTES", "8")
    p = tmp_path / "big.txt"
    p.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        extract_text(p)


def test_doc_size_cap_disabled_with_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_DOC_BYTES", "0")
    p = tmp_path / "big.txt"
    p.write_text("x" * 100, encoding="utf-8")
    assert extract_text(p) == "x" * 100


def test_html_strips_tags_and_scripts(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(
        "<html><body><script>evil()</script><p>keep this</p></body></html>",
        encoding="utf-8",
    )
    out = extract_text(p)
    assert "keep this" in out
    assert "evil" not in out


def test_docx_uncompressed_guard_rejects_bomb(tmp_path, monkeypatch):
    # A zip whose declared uncompressed size exceeds the cap is refused before
    # python-docx ever expands it (the guard reads only the central directory).
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_DOCX_UNCOMPRESSED_BYTES", "1024")
    z = tmp_path / "bomb.docx"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"A" * 100_000)  # >>1KB uncompressed
    with pytest.raises(ValueError, match="too large"):
        _check_docx_uncompressed_size(z)


def test_docx_uncompressed_guard_allows_normal(tmp_path):
    z = tmp_path / "ok.docx"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"<xml>small</xml>")
    _check_docx_uncompressed_size(z)  # no raise


def test_corrupt_zip_docx_rejected(tmp_path):
    z = tmp_path / "bad.docx"
    z.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="corrupt zip"):
        _check_docx_uncompressed_size(z)


def test_pdf_page_count_guard_rejects_too_many(monkeypatch):
    # A PDF whose page count exceeds the cap is refused BEFORE the per-page
    # extract loop decompresses each (Flate) content stream into memory.
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_PDF_PAGES", "10")
    with pytest.raises(ValueError, match="too many pages"):
        _check_pdf_page_count(_StubReader(11))


def test_pdf_page_count_guard_allows_normal(monkeypatch):
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_PDF_PAGES", "10")
    _check_pdf_page_count(_StubReader(10))  # no raise


def test_pdf_page_count_guard_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_PDF_PAGES", "0")
    _check_pdf_page_count(_StubReader(1_000_000))  # no raise


@pytest.mark.parametrize(
    ("suffix", "parser_name", "expected_kwarg"),
    [
        (".pdf", "knowledge_pdf_text", "max_pages"),
        (".docx", "knowledge_docx_text", "max_uncompressed_bytes"),
    ],
)
def test_rich_documents_route_through_isolation_by_default(
    monkeypatch, tmp_path, suffix, parser_name, expected_kwarg
):
    import maverick.parser_isolation as isolation

    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    source_bytes = b"hostile-parser-input"
    path = tmp_path / f"upload{suffix}"
    path.write_bytes(source_bytes)
    seen = {}

    def fake_isolated(name, data, **kwargs):
        seen.update(name=name, data=data, kwargs=kwargs)
        return "isolated text"

    monkeypatch.setattr(isolation, "parse_isolated", fake_isolated)

    assert extract_text(path) == "isolated text"
    assert seen["name"] == parser_name
    assert seen["data"] == source_bytes
    assert expected_kwarg in seen["kwargs"]


@pytest.mark.parametrize(("suffix", "label"), [(".pdf", "PDF"), (".docx", "DOCX")])
def test_isolation_failure_never_falls_back_in_process(
    monkeypatch, tmp_path, suffix, label
):
    import maverick.parser_isolation as isolation

    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    path = tmp_path / f"hostile{suffix}"
    path.write_bytes(b"malformed hostile bytes")
    monkeypatch.setattr(
        isolation,
        "parse_isolated",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("child crashed")),
    )
    monkeypatch.setattr(
        parse_module,
        "_extract_pdf_bytes",
        lambda *a, **k: pytest.fail("PDF fell back in process"),
    )
    monkeypatch.setattr(
        parse_module,
        "_extract_docx_bytes",
        lambda *a, **k: pytest.fail("DOCX fell back in process"),
    )

    with pytest.raises(
        RuntimeError,
        match=rf"isolated {label} parsing failed; in-process fallback is disabled",
    ):
        extract_text(path)


def test_missing_isolation_runtime_fails_closed(monkeypatch):
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    real_import = builtins.__import__

    def import_without_isolation(name, *args, **kwargs):
        if name == "maverick.parser_isolation":
            raise ImportError("isolation runtime unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_isolation)

    with pytest.raises(RuntimeError, match="refusing in-process parsing"):
        parse_module._parse_rich_document(
            "knowledge_pdf_text",
            "PDF",
            b"hostile",
            lambda *a, **k: pytest.fail("missing runtime fell back in process"),
        )


def test_trusted_inprocess_escape_hatch_is_explicit(monkeypatch, tmp_path):
    import maverick.parser_isolation as isolation

    monkeypatch.setenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", "1")
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    path = tmp_path / "trusted-fixture.pdf"
    path.write_bytes(b"trusted fixture bytes")
    monkeypatch.setattr(
        isolation,
        "parse_isolated",
        lambda *a, **k: pytest.fail("trusted bypass should not spawn a child"),
    )
    monkeypatch.setattr(
        parse_module,
        "_extract_pdf_bytes",
        lambda data, **kwargs: "trusted local text",
    )

    assert extract_text(path) == "trusted local text"


@pytest.mark.parametrize("configured_cap", ["0", "100"])
def test_rich_document_keeps_hard_transport_cap(
    monkeypatch, tmp_path, configured_cap
):
    import maverick.parser_isolation as isolation

    monkeypatch.setenv("MAVERICK_KNOWLEDGE_MAX_DOC_BYTES", configured_cap)
    monkeypatch.setattr(parse_module, "ISOLATED_TRANSPORT_MAX_BYTES", 8)
    monkeypatch.setattr(
        isolation,
        "parse_isolated",
        lambda *a, **k: pytest.fail("oversized bytes reached the child"),
    )
    path = tmp_path / "large.pdf"
    path.write_bytes(b"x" * 9)

    with pytest.raises(ValueError, match="too large"):
        extract_text(path)


def test_isolated_parser_must_return_text(monkeypatch, tmp_path):
    import maverick.parser_isolation as isolation

    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    path = tmp_path / "hostile.pdf"
    path.write_bytes(b"hostile")
    monkeypatch.setattr(isolation, "parse_isolated", lambda *a, **k: {"text": "no"})

    with pytest.raises(RuntimeError, match="non-text result"):
        extract_text(path)
