"""The dashboard's browser-voice fallback goes through the natural-voice
layer (maverick-voice.js), never the bare default utterance."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})
_TPL = Path(__file__).resolve().parents[1] / "maverick_dashboard"


def test_voice_module_served_with_ranking_and_humanizer():
    r = client.get("/static/maverick-voice.js")
    assert r.status_code == 200
    for marker in ("lwVoice", "voiceschanged", "MAX_CHUNK",
                   "Article ", "G D P R"):
        assert marker in r.text


def test_base_loads_module_and_chat_fallback_uses_it():
    base = (_TPL / "templates" / "base.html").read_text(encoding="utf-8")
    assert "/static/maverick-voice.js" in base
    chat = (_TPL / "templates" / "chat_goal.html").read_text(encoding="utf-8")
    assert "window.lwVoice" in chat
    # The bare utterance survives only as the last-resort fallback branch.
    assert chat.index("window.lwVoice") < chat.index(
        "new SpeechSynthesisUtterance")
