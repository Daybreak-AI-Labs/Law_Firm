"""Matter-scoped conversation history never crosses an ethical wall."""
from __future__ import annotations

import sqlite3

from maverick.world_model import SCHEMA_VERSION, WorldModel


def _matter(world: WorldModel, number: str, principal: str = "user:alice") -> int:
    return world.create_client_matter(
        f"Matter {number}",
        principal=principal,
        domain="legal",
        matter_number=number,
        jurisdiction="Tennessee",
        client_name=f"Client {number}",
    )


def test_same_user_and_channel_are_isolated_by_matter(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    first = _matter(world, "2026-101")
    second = _matter(world, "2026-102")

    one = world.get_or_create_matter_conversation(
        "dashboard", "alice", first, principal="user:alice"
    )
    two = world.get_or_create_matter_conversation(
        "dashboard", "alice", second, principal="user:alice"
    )
    assert one is not None and two is not None
    assert one.id != two.id
    assert (one.project_id, two.project_id) == (first, second)
    assert world.append_matter_turn(
        one.id,
        project_id=first,
        principal="user:alice",
        role="user",
        content="first matter secret",
    )
    assert world.append_matter_turn(
        two.id,
        project_id=second,
        principal="user:alice",
        role="user",
        content="second matter secret",
    )

    assert [t.content for t in world.recent_matter_turns(
        one.id, project_id=first, principal="user:alice"
    )] == ["first matter secret"]
    assert [t.content for t in world.recent_matter_turns(
        two.id, project_id=second, principal="user:alice"
    )] == ["second matter secret"]
    assert world.recent_matter_turns(
        one.id, project_id=second, principal="user:alice"
    ) == []


def test_revocation_blocks_create_append_and_read_without_fallback(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    matter_id = _matter(world, "2026-201")
    world.add_project_member(
        matter_id, "user:bob", "attorney", added_by="user:alice"
    )
    conversation = world.get_or_create_matter_conversation(
        "dashboard", "bob", matter_id, principal="user:bob"
    )
    assert conversation is not None
    assert world.append_matter_turn(
        conversation.id,
        project_id=matter_id,
        principal="user:bob",
        role="user",
        content="privileged",
    )
    assert world.deactivate_project_member(matter_id, "user:bob")

    assert world.get_or_create_matter_conversation(
        "dashboard", "bob", matter_id, principal="user:bob"
    ) is None
    assert world.append_matter_turn(
        conversation.id,
        project_id=matter_id,
        principal="user:bob",
        role="assistant",
        content="must not persist",
    ) is None
    assert world.recent_matter_turns(
        conversation.id, project_id=matter_id, principal="user:bob"
    ) == []
    stored = world.conn.execute(
        "SELECT content FROM matter_turns WHERE conversation_id = ?",
        (conversation.id,),
    ).fetchall()
    assert len(stored) == 1


def test_v36_migrates_legacy_history_to_unfiled_only(tmp_path):
    path = tmp_path / "world.db"
    world = WorldModel(path)
    legacy = world.get_or_create_conversation("dashboard", "alice")
    world.append_turn(legacy.id, "user", "legacy history")
    world.close()

    raw = sqlite3.connect(path)
    legacy_count = raw.execute(
        "SELECT COUNT(*) FROM conversations WHERE id = ?", (legacy.id,)
    ).fetchone()[0]
    secure_count = raw.execute(
        "SELECT COUNT(*) FROM matter_conversations"
    ).fetchone()[0]
    version = raw.execute("SELECT version FROM schema_version").fetchone()[0]
    raw.close()
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 36
    assert legacy_count == 1
    assert secure_count == 0

    reopened = WorldModel(path)
    matter_id = _matter(reopened, "2026-301")
    secure = reopened.get_or_create_matter_conversation(
        "dashboard", "alice", matter_id, principal="user:alice"
    )
    assert secure is not None
    assert secure.project_id == matter_id
    assert reopened.recent_matter_turns(
        secure.id, project_id=matter_id, principal="user:alice"
    ) == []
