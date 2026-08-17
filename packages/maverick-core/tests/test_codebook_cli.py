"""`maverick codebook` learns the swarm's coordination shorthand from its real
messages -- and every learned code still decodes EXACTLY back to English (the
audit contract holds on whatever the corpus produces)."""
from __future__ import annotations

from maverick.world_model import open_world


class _World:
    """Stand-in world exposing the same read-only accessor the command uses."""

    def __init__(self, messages):
        self._messages = messages

    def recent_event_contents(self, limit: int = 5000) -> list[str]:
        return list(self._messages[:limit])


_CORPUS = (
    ["spawning sub-agent for research task"] * 4
    + ["verification passed with high confidence"] * 4
    + ["handing off to the reviewer agent"] * 3
)






def test_recent_event_contents_accessor(tmp_path):
    # The real corpus the command learns from: coordination bodies, newest first.
    w = open_world(tmp_path / "world.db")
    gid = w.create_goal("g", "d", owner="")
    w.append_event(gid, "planner", "note", "first message")
    w.append_event(gid, "worker", "note", "second message")
    contents = w.recent_event_contents(limit=10)
    assert "first message" in contents and "second message" in contents
    # Newest-first ordering.
    assert contents.index("second message") < contents.index("first message")
    # Honours the limit.
    assert w.recent_event_contents(limit=1) == ["second message"]


