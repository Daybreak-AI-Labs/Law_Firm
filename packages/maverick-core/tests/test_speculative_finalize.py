"""Speculative post-FINAL finalization in run_goal.

The conversation-turn write is run as a
background thread (via the speculative primitive) so it overlaps with
skill distillation, then joined before run_goal returns. This test proves
the side effects still happen on the success path — both with the overlap
on (default) and off (MAVERICK_SPECULATIVE_FINALIZE=0).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from maverick.budget import Budget
from maverick.matter_context import (
    matter_context_scope,
    resolve_goal_matter_context,
)
from maverick.orchestrator import run_goal
from maverick.sandbox import LocalBackend
from maverick.world_model import WorldModel
from maverick_knowledge import SqliteVectorStore, matter_collection


def _scripted(make_llm_response):
    """FINAL + verifier-accept + distill-terminal, the standard success
    script used by the orchestrator tests."""
    return [
        make_llm_response(text="FINAL: the answer is 42"),
        make_llm_response(
            text='{"confidence": 0.95, "accepts": true, "critique": "ok", "issues": []}',
        ),
        make_llm_response(text="FINAL: (no skill)"),
    ]


@pytest.mark.parametrize("overlap", ["1", "0"])
@pytest.mark.asyncio
async def test_conversation_turn_written_either_way(
    tmp_path: Path, fake_llm, make_llm_response, monkeypatch, overlap,
):
    monkeypatch.setenv("MAVERICK_SPECULATIVE_FINALIZE", overlap)
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", (b"s" * 32).hex())
    fake_llm.scripted = _scripted(make_llm_response)

    knowledge_path = tmp_path / "knowledge.db"
    monkeypatch.setattr(
        "maverick.config.get_knowledge",
        lambda: {
            "enable": True,
            "embedder": "deterministic",
            "store": "sqlite",
            "path": str(knowledge_path),
        },
    )
    world = WorldModel(path=tmp_path / "world.db")
    principal = "user:alice"
    domain = "legal_intake"
    matter_id = world.create_client_matter(
        "Speculative finalization matter",
        principal=principal,
        domain=domain,
        matter_number="2026-SPEC",
        jurisdiction="Tennessee",
        client_name="Speculative finalization client",
    )
    store = SqliteVectorStore(knowledge_path)
    store.add(
        matter_collection(matter_id, "legal"),
        [("context", "authenticated matter context", [1.0] + [0.0] * 255, {})],
    )
    store.close()
    conversation = world.get_or_create_matter_conversation(
        "cli", "sess-1", matter_id, principal=principal,
    )
    assert conversation is not None
    conv_id = conversation.id
    gid = world.create_matter_goal(
        "compute the answer",
        "trivial",
        principal=principal,
        domain=domain,
        project_id=matter_id,
    )
    assert gid is not None

    def resolve():
        return resolve_goal_matter_context(
            world,
            gid,
            principal=principal,
            source="speculative-finalize-test",
        )

    with matter_context_scope(resolve(), authority_resolver=resolve):
        out = await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
            conversation_id=conv_id,
        )
    assert "DONE." in out

    # The assistant turn was persisted by the speculative side effect
    # (and the run joined it before returning).
    turns = world.recent_matter_turns(
        conv_id,
        project_id=matter_id,
        principal=principal,
        limit=10,
    )
    assert any(
        t.role == "assistant" and "the answer is 42" in t.content
        for t in turns
    )


@pytest.mark.asyncio
async def test_goal_marked_done_with_overlap(
    tmp_path: Path, fake_llm, make_llm_response, monkeypatch,
):
    monkeypatch.setenv("MAVERICK_SPECULATIVE_FINALIZE", "1")
    fake_llm.scripted = _scripted(make_llm_response)
    world = WorldModel(path=tmp_path / "world.db")
    gid = world.create_goal("compute the answer", "trivial")

    await run_goal(
        llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
        goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
    )
    goal = world.get_goal(gid)
    assert goal.status == "done"
    # Episode closed successfully despite the backgrounded side effects.
    eps = world.list_episodes(goal_id=gid)
    assert eps and eps[0].outcome == "success"
