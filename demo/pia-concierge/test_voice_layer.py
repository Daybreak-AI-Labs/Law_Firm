"""The natural-voice layer: the concierge must never speak through the bare
default utterance — ranked voices, humanized text, sentence delivery, and a
script that doesn't repeat itself like a machine."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

HERE = Path(__file__).resolve().parent

_SHARED_MODULES = ("backend", "capabilities", "license_kit", "mailsink",
                   "value_ledger", "store", "pia_engine", "dsar_engine",
                   "ot_mock", "onetrust_client", "notice_check",
                   "contract_guard", "serve_standalone")


@pytest.fixture(scope="module")
def client():
    for m in _SHARED_MODULES:
        sys.modules.pop(m, None)
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_voice_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return TestClient(module.app)
    finally:
        sys.path.remove(str(HERE))


def test_voice_module_is_served_and_complete(client):
    r = client.get("/static/concierge-voice.js")
    assert r.status_code == 200
    js = r.text
    # The three legs of sounding human: voice ranking, text humanizing,
    # sentence-chunked delivery (which also dodges Chrome's cutoff).
    for marker in ("lwVoice", "voiceschanged", "natural|neural|premium",
                   "MAX_CHUNK", "u.rate"):
        assert marker in js, f"voice module lost {marker!r}"
    # Spoken forms: citations, ids, and acronyms never read out raw.
    for spoken in ("Article ", "the OneTrust record", "dee-sar",
                   "G D P R", "the link on screen"):
        assert spoken in js, f"humanizer lost the {spoken!r} form"
    # Novelty/robotic voices are never eligible.
    assert "espeak" in js and "zarvox" in js


def test_intake_speaks_through_the_voice_layer(client):
    import store as demo_store
    case = demo_store.Case(
        id="LW-PIA-V001", ticket_number="PRV-V-1", subject="Voice Test Co",
        requester="Sam", requester_email="sam@example.test",
        data_types="contact data")
    demo_store.STORE.cases[case.id] = case
    page = client.get(f"/intake/{case.id}").text
    assert '/static/concierge-voice.js' in page
    assert "lwVoice.speak(text)" in page
    # The picker is a labelled control, and the ack phrasing rotates —
    # a person doesn't say the same sentence forty times.
    assert "Choose the reading voice" in page
    assert page.count("(l) =>") >= 4
    assert "Your own words stay on the record" in page
