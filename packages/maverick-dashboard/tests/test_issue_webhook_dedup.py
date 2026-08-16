"""Issue-webhook replay dedup: in-process window + shared store under HA (audit H17)."""
from __future__ import annotations

import maverick_dashboard.app as app_mod


class _FakeSharedWorld:
    """First-writer-wins stand-in for mark_message_processed."""

    def __init__(self):
        self.seen: set[tuple[str, str]] = set()

    def mark_message_processed(self, channel, external_id, goal_id=None):
        key = (channel, external_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def test_in_process_window_is_default(monkeypatch):
    app_mod._issue_webhook_seen.clear()

    assert app_mod._issue_webhook_replay_seen("sig-1", 300) is False  # first delivery
    assert app_mod._issue_webhook_replay_seen("sig-1", 300) is True   # replay
    assert "sig-1" in app_mod._issue_webhook_seen


