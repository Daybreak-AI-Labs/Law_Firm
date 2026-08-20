"""Retained Wave 6 EU AI Act disclosure regressions."""
from __future__ import annotations


class TestArticle50Disclosure:
    def test_first_turn_returns_disclosure(self, tmp_path):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        wm = WorldModel(tmp_path / "w.db")
        msg = first_turn_disclosure(wm, "telegram", "user-42")
        assert msg is not None
        assert "Maverick" in msg
        assert "AI" in msg

    def test_after_assistant_turn_returns_none(self, tmp_path):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        wm = WorldModel(tmp_path / "w.db")
        conv = wm.get_or_create_conversation("telegram", "user-42")
        wm.append_turn(conv.id, "user", "hello")
        wm.append_turn(conv.id, "assistant", "hi back")
        assert first_turn_disclosure(wm, "telegram", "user-42") is None

    def test_only_user_turn_still_returns_disclosure(self, tmp_path):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        wm = WorldModel(tmp_path / "w.db")
        conv = wm.get_or_create_conversation("telegram", "user-42")
        wm.append_turn(conv.id, "user", "hello")
        assert first_turn_disclosure(wm, "telegram", "user-42") is not None

    def test_trailing_user_turn_after_assistant_returns_none(self, tmp_path):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        wm = WorldModel(tmp_path / "w.db")
        conv = wm.get_or_create_conversation("telegram", "user-42")
        wm.append_turn(conv.id, "user", "hello")
        wm.append_turn(conv.id, "assistant", "hi back")
        wm.append_turn(conv.id, "user", "second message (goal then failed)")
        assert first_turn_disclosure(wm, "telegram", "user-42") is None

    def test_empty_disclosure_text_opts_out(self, tmp_path, monkeypatch):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        monkeypatch.setenv("MAVERICK_AI_DISCLOSURE", "")
        wm = WorldModel(tmp_path / "w.db")
        assert first_turn_disclosure(wm, "telegram", "user-42") is None

    def test_custom_disclosure_text(self, tmp_path, monkeypatch):
        from maverick.compliance import first_turn_disclosure
        from maverick.world_model import WorldModel

        monkeypatch.setenv("MAVERICK_AI_DISCLOSURE", "Howdy, I am the AI.")
        wm = WorldModel(tmp_path / "w.db")
        msg = first_turn_disclosure(wm, "telegram", "user-42")
        assert msg == "Howdy, I am the AI."
