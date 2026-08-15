"""ocr extract_url must stream the HTTP body with a hard byte ceiling, not
buffer the whole thing into memory.

Regression: the fetch used `safe_get`, which materializes `resp.content`
(the entire body) inside the client -- so a model-supplied URL to a multi-GB
or endless resource was fully loaded into memory with no cap at all. It now
streams via `safe_client(...).stream` with a content-length precheck and an
incremental 50 MiB ceiling, mirroring view_image / pdf_reader.
"""
from __future__ import annotations

from pathlib import Path

from maverick.tools import ocr as ocr_mod


class _FakeStreamResp:
    def __init__(self, chunks, headers, status_code=200):
        self._chunks = chunks
        self.headers = headers
        self.status_code = status_code

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, method, url):
        return self._resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _SB:
    def __init__(self, workdir):
        self.workdir = workdir


def _patch(monkeypatch, resp):
    import maverick.tools._ssrf as ssrf
    monkeypatch.setattr(ssrf, "safe_client", lambda url, **k: _FakeClient(resp))


def test_extract_url_caps_without_reading_whole_body(monkeypatch):
    consumed = {"chunks": 0}

    def chunks():
        for _ in range(200):  # 200 x 1 MiB available; cap is 50 MiB
            consumed["chunks"] += 1
            yield b"x" * (1024 * 1024)

    _patch(monkeypatch, _FakeStreamResp(chunks(), {"content-type": "image/png"}))
    out = ocr_mod._op_extract_url(
        "https://example.com/big.png", "eng", "tesseract", "", None)
    assert out.startswith("ERROR") and "cap" in out
    # Stopped shortly after crossing 50 MiB -- did NOT read all 200 chunks.
    assert consumed["chunks"] <= 52


def test_extract_url_rejects_on_content_length_without_streaming(monkeypatch):
    pulled = {"n": 0}

    def chunks():
        pulled["n"] += 1
        yield b"x"

    headers = {"content-type": "image/png",
               "content-length": str(200 * 1024 * 1024)}
    _patch(monkeypatch, _FakeStreamResp(chunks(), headers))
    out = ocr_mod._op_extract_url(
        "https://example.com/huge.png", "eng", "tesseract", "", None)
    assert out.startswith("ERROR") and "cap" in out
    assert pulled["n"] == 0  # rejected on content-length, never streamed


def test_extract_url_normal_body_reaches_ocr(monkeypatch, tmp_path):
    seen = {}

    def fake_extract(path, lang, backend, hf_model, sandbox):
        p = Path(path)
        seen["blob"] = p.read_bytes()
        seen["suffix"] = p.suffix
        return "OCRTEXT"

    monkeypatch.setattr(ocr_mod, "_op_extract", fake_extract)
    _patch(monkeypatch,
           _FakeStreamResp([b"\x89PNG", b"img"], {"content-type": "image/png"}))
    out = ocr_mod._op_extract_url(
        "https://example.com/ok.png", "eng", "tesseract", "", _SB(tmp_path))
    assert out == "OCRTEXT"
    assert seen["blob"] == b"\x89PNGimg"
    assert seen["suffix"] == ".png"


def test_extract_url_http_error_status(monkeypatch):
    _patch(monkeypatch,
           _FakeStreamResp([], {"content-type": "image/png"}, status_code=404))
    out = ocr_mod._op_extract_url(
        "https://example.com/gone.png", "eng", "tesseract", "", None)
    assert out.startswith("ERROR: image fetch 404")
