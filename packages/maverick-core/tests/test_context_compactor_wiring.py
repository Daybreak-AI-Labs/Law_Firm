"""Context compaction, wired into the orchestrator's conversation history.

context_compactor.compact existed but nothing called it. The orchestrator
now optionally pulls a larger window of prior turns and compacts it to a
token budget (keeping the most relevant older turns) instead of always
including just the last 10. Opt-in via MAVERICK_COMPACT_HISTORY=1 or
[context] compact = true; off by default.
"""
from __future__ import annotations

from pathlib import Path

import maverick.config as cfg
import pytest
from maverick import context_compactor as cc
from maverick.budget import Budget
from maverick.llm import LLMResponse
from maverick.matter_context import (
    matter_context_scope,
    resolve_goal_matter_context,
)
from maverick.orchestrator import run_goal
from maverick.sandbox import LocalBackend
from maverick.world_model import WorldModel
from maverick_knowledge import SqliteVectorStore, matter_collection

# ---------- token counting ----------

class TestApproxTokens:
    def _reset(self, monkeypatch):
        # The resolved counter is module-global + cached; reset it per test.
        monkeypatch.setattr(cc, "_token_counter", None)

    def test_empty_is_zero(self, monkeypatch):
        self._reset(monkeypatch)
        assert cc._approx_tokens("") == 0

    def test_falls_back_to_heuristic_without_tokenizer(self, monkeypatch):
        # Force the heuristic path (env opt-out) -> char/4 rounding, positive.
        monkeypatch.setenv("MAVERICK_COMPACT_TIKTOKEN", "0")
        self._reset(monkeypatch)
        assert cc._approx_tokens("abcd") == 1
        assert cc._approx_tokens("a" * 40) == 10

    def test_uses_real_counter_when_available(self, monkeypatch):
        # Inject a fake BPE counter through the resolution seam and confirm
        # _approx_tokens prefers it over the char heuristic.
        monkeypatch.setenv("MAVERICK_COMPACT_TIKTOKEN", "1")
        monkeypatch.setattr(cc, "_token_counter", lambda s: 999)
        assert cc._approx_tokens("anything") == 999

    def test_counter_error_fails_open_to_heuristic(self, monkeypatch):
        def _boom(_s):
            raise RuntimeError("tokenizer blew up")

        monkeypatch.setattr(cc, "_token_counter", _boom)
        # 8 chars -> (8+3)//4 == 2 via the heuristic fallback.
        assert cc._approx_tokens("12345678") == 2


# ---------- config helpers ----------

class TestCompactorConfig:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_COMPACT_HISTORY", raising=False)
        monkeypatch.setattr(cfg, "load_config", dict)
        assert cc.enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_COMPACT_HISTORY", "1")
        assert cc.enabled() is True

    def test_enabled_via_config(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_COMPACT_HISTORY", raising=False)
        monkeypatch.setattr(cfg, "load_config", lambda: {"context": {"compact": True}})
        assert cc.enabled() is True

    def test_target_tokens_and_window(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_HISTORY_TOKENS", raising=False)
        monkeypatch.delenv("MAVERICK_HISTORY_WINDOW", raising=False)
        monkeypatch.setattr(cfg, "load_config", dict)
        assert cc.target_tokens() == 1500
        assert cc.window() == 50
        monkeypatch.setenv("MAVERICK_HISTORY_TOKENS", "200")
        monkeypatch.setenv("MAVERICK_HISTORY_WINDOW", "0")  # clamp to >= 1
        assert cc.target_tokens() == 200
        assert cc.window() == 50
        monkeypatch.delenv("MAVERICK_HISTORY_TOKENS", raising=False)
        monkeypatch.setattr(cfg, "load_config", lambda: {"context": {"history_tokens": 99}})
        assert cc.target_tokens() == 99


# ---------- orchestrator wiring ----------

def _prompt_blob(fake_llm) -> str:
    blob = ""
    for c in fake_llm.calls:
        blob += c.get("system") or ""
        for m in (c.get("messages") or []):
            blob += str(m.get("content", ""))
    return blob


_PRINCIPAL = "user:alice"
_DOMAIN = "legal_intake"


def _prepare_knowledge(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", (b"c" * 32).hex())
    path = tmp_path / "knowledge.db"
    monkeypatch.setattr(
        cfg,
        "get_knowledge",
        lambda: {
            "enable": True,
            "embedder": "deterministic",
            "store": "sqlite",
            "path": str(path),
        },
    )
    return path


def _seed_required_knowledge(path: Path, matter_id: int) -> None:
    store = SqliteVectorStore(path)
    store.add(
        matter_collection(matter_id, "legal"),
        [("context", "authenticated matter context", [1.0] + [0.0] * 255, {})],
    )
    store.close()


def _matter_run(world, knowledge_path: Path, *, title: str):
    matter_id = world.create_client_matter(
        "Compaction matter",
        principal=_PRINCIPAL,
        domain=_DOMAIN,
        matter_number="2026-COMPACT",
        jurisdiction="Tennessee",
        client_name="Compaction client",
    )
    _seed_required_knowledge(knowledge_path, matter_id)
    conv = world.get_or_create_matter_conversation(
        "tg", "u1", matter_id, principal=_PRINCIPAL,
    )
    assert conv is not None
    gid = world.create_matter_goal(
        title,
        "",
        principal=_PRINCIPAL,
        domain=_DOMAIN,
        project_id=matter_id,
    )
    assert gid is not None

    def resolve():
        return resolve_goal_matter_context(
            world,
            gid,
            principal=_PRINCIPAL,
            source="context-compactor-test",
        )

    return matter_id, conv, gid, resolve(), resolve


def _seed_conversation(world, knowledge_path: Path, n=30):
    matter_id, conv, gid, context, resolve = _matter_run(
        world, knowledge_path, title="Continue the deploy",
    )
    for i in range(n):
        assert world.append_matter_turn(
            conv.id,
            project_id=matter_id,
            principal=_PRINCIPAL,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message number {i} about deploying the parser service",
        )
    return conv, gid, context, resolve


@pytest.mark.asyncio
async def test_long_history_is_compacted_when_enabled(monkeypatch, tmp_path: Path, fake_llm):
    monkeypatch.setenv("MAVERICK_COMPACT_HISTORY", "1")
    monkeypatch.setenv("MAVERICK_HISTORY_TOKENS", "80")   # small -> forces a drop
    monkeypatch.setenv("MAVERICK_HISTORY_WINDOW", "40")

    knowledge_path = _prepare_knowledge(monkeypatch, tmp_path)
    world = WorldModel(path=tmp_path / "world.db")
    conv, gid, context, resolve = _seed_conversation(world, knowledge_path, n=30)
    fake_llm.scripted = [
        LLMResponse(text="FINAL: done", thinking=None, stop_reason="end_turn", tool_calls=[]),
    ]

    with matter_context_scope(context, authority_resolver=resolve):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
            conversation_id=conv.id,
        )
    assert "compacted to save context" in _prompt_blob(fake_llm)  # compaction ran


@pytest.mark.asyncio
async def test_history_uses_last_10_when_disabled(monkeypatch, tmp_path: Path, fake_llm):
    monkeypatch.delenv("MAVERICK_COMPACT_HISTORY", raising=False)
    # Pin the legacy fixed bounds: with model-scaled context (the default)
    # a 200k-window orchestrator keeps 50 turns, not 10.
    monkeypatch.setenv("MAVERICK_CONTEXT_MODEL_SCALED", "0")
    monkeypatch.setattr(cfg, "load_config", dict)

    knowledge_path = _prepare_knowledge(monkeypatch, tmp_path)
    world = WorldModel(path=tmp_path / "world.db")
    conv, gid, context, resolve = _seed_conversation(world, knowledge_path, n=30)
    fake_llm.scripted = [
        LLMResponse(text="FINAL: done", thinking=None, stop_reason="end_turn", tool_calls=[]),
    ]

    with matter_context_scope(context, authority_resolver=resolve):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
            conversation_id=conv.id,
        )
    blob = _prompt_blob(fake_llm)
    assert "compacted to save context" not in blob   # no compaction
    assert "message number 29" in blob               # last turn included
    assert "message number 0 about" not in blob       # turn 0 dropped (only last 10)


@pytest.mark.asyncio
async def test_history_scales_with_model_window_by_default(
        monkeypatch, tmp_path: Path, fake_llm):
    """Model-scaled context (default ON): a 200k-window orchestrator keeps
    50 turns, so all 30 seeded turns — including turn 0 — make the brief."""
    monkeypatch.delenv("MAVERICK_COMPACT_HISTORY", raising=False)
    monkeypatch.delenv("MAVERICK_CONTEXT_MODEL_SCALED", raising=False)
    monkeypatch.setattr(cfg, "load_config", dict)

    knowledge_path = _prepare_knowledge(monkeypatch, tmp_path)
    world = WorldModel(path=tmp_path / "world.db")
    conv, gid, context, resolve = _seed_conversation(world, knowledge_path, n=30)
    fake_llm.scripted = [
        LLMResponse(text="FINAL: done", thinking=None, stop_reason="end_turn", tool_calls=[]),
    ]

    with matter_context_scope(context, authority_resolver=resolve):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
            conversation_id=conv.id,
        )
    blob = _prompt_blob(fake_llm)
    assert "message number 29" in blob
    assert "message number 0 about" in blob

def test_compactor_trims_oversized_tail_to_target():
    old = [
        {"role": "user", "content": f"old turn {i} " + ("padding " * 40)}
        for i in range(12)
    ]
    oversized_tail = [
        {"role": "user", "content": "fresh user " + ("A" * 800)},
        {"role": "assistant", "content": "fresh assistant " + ("B" * 800)},
    ]

    out = cc.compact(old + oversized_tail, target_tokens=80, preserve_tail=2)
    blob = "\n".join(str(m.get("content", "")) for m in out.messages)

    assert out.tokens_after <= 80
    assert "compacted to save context" in blob
    assert "A" * 400 not in blob
    assert "B" * 400 not in blob


@pytest.mark.asyncio
async def test_compact_history_caps_each_persisted_turn(monkeypatch, tmp_path: Path, fake_llm):
    monkeypatch.setenv("MAVERICK_COMPACT_HISTORY", "1")
    monkeypatch.setenv("MAVERICK_HISTORY_TOKENS", "1500")
    monkeypatch.setenv("MAVERICK_HISTORY_WINDOW", "10")
    # Pin the legacy 300-char per-turn cap; model-scaled context (the
    # default) raises it with the orchestrator model's window.
    monkeypatch.setenv("MAVERICK_CONTEXT_MODEL_SCALED", "0")

    knowledge_path = _prepare_knowledge(monkeypatch, tmp_path)
    world = WorldModel(path=tmp_path / "world.db")
    matter_id, conv, gid, context, resolve = _matter_run(
        world, knowledge_path, title="Continue safely",
    )
    assert world.append_matter_turn(
        conv.id,
        project_id=matter_id,
        principal=_PRINCIPAL,
        role="user",
        content=("A" * 320) + "UNSAFE_AFTER_300",
    )
    fake_llm.scripted = [
        LLMResponse(text="FINAL: done", thinking=None, stop_reason="end_turn", tool_calls=[]),
    ]

    with matter_context_scope(context, authority_resolver=resolve):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
            conversation_id=conv.id,
        )

    blob = _prompt_blob(fake_llm)
    assert "A" * 300 in blob
    assert "UNSAFE_AFTER_300" not in blob
