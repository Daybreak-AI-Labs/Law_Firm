from __future__ import annotations

import inspect
import sys
import types
from types import SimpleNamespace

import pytest
from maverick import orchestrator as orch
from maverick.matter_context import MatterContext, matter_context_scope


def _context(*, matter_id: int = 7, principal: str = "user:alice") -> MatterContext:
    return MatterContext(
        matter_id=matter_id,
        client_id=3,
        principal=principal,
        membership_role="responsible_attorney",
        domain="legal_contract_review",
        jurisdiction="Tennessee",
        purpose="goal-execution",
        source="test",
        egress_mode="local_only",
    )


class _AllowShield:
    def scan_input(self, _text):
        return SimpleNamespace(allowed=True, reasons=[])

    def scan_output(self, _text):
        return SimpleNamespace(allowed=True, reasons=[])


class _ThrowingShield:
    def scan_input(self, _text):
        raise RuntimeError("scanner offline")

    def scan_output(self, _text):
        raise RuntimeError("scanner offline")


def test_secure_shield_absence_and_scan_errors_fail_closed(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")

    assert orch._shield_input_block_reason(None, "client text") == "Shield is unavailable"
    assert orch._shield_output_block_reason(None, "client text") == "Shield is unavailable"
    assert "scan failed" in orch._shield_input_block_reason(
        _ThrowingShield(), "client text"
    )
    assert "scan failed" in orch._shield_output_block_reason(
        _ThrowingShield(), "client text"
    )
    assert "RAW-CLIENT-TEXT" not in orch._format_tree_of_thought_plan(
        "RAW-CLIENT-TEXT", shield=_ThrowingShield()
    )
    assert orch._sanitize_persisted_prompt_text(
        "RAW-CLIENT-TEXT",
        shield=_ThrowingShield(),
        max_chars=100,
    ) == orch._SHIELD_WITHHELD


def test_legacy_shield_fail_open_requires_explicit_secure_defaults_off(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")

    assert orch._shield_input_block_reason(None, "legacy") is None
    assert orch._shield_input_block_reason(_ThrowingShield(), "legacy") is None
    assert orch._sanitize_persisted_prompt_text(
        "legacy", shield=None, max_chars=100
    ) == "legacy"


def test_shield_configuration_error_yields_no_scanner(monkeypatch):
    class _BrokenShield:
        @classmethod
        def from_config(cls):
            raise ValueError("bad config")

    module = types.ModuleType("maverick_shield")
    module.Shield = _BrokenShield
    monkeypatch.setitem(sys.modules, "maverick_shield", module)

    assert orch._build_shield() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scanner_mode", ["deny", "error"])
async def test_persisted_history_is_matter_filtered_and_never_raw_on_scan_failure(
    monkeypatch,
    scanner_mode,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_COMPACT_HISTORY", "0")
    secret = "DISTINCTIVE-PERSISTED-MALICIOUS-TURN"
    calls: list[tuple[int, int, str]] = []

    class _World:
        def recent_matter_turns(
            self, conversation_id, *, project_id, principal, limit,
        ):
            calls.append((conversation_id, project_id, principal))
            return [SimpleNamespace(role="user", content=secret)]

        def all_questions(self, _goal_id):
            return []

    class _Shield(_AllowShield):
        def scan_input(self, _text):
            if scanner_mode == "error":
                raise RuntimeError("scanner offline")
            return SimpleNamespace(allowed=False, reasons=["injection"])

    async def _no_enrich(brief, **_kwargs):
        return brief, "default"

    monkeypatch.setattr(orch, "_apply_brief_enrichments", _no_enrich)
    goal = SimpleNamespace(
        title="Research controlling law",
        description="Use only binding authority",
        project_id=7,
        owner="user:alice",
        domain="legal_contract_review",
    )
    with matter_context_scope(_context()):
        brief, _mode = await orch._build_orchestrator_brief(
            llm=object(),
            world=_World(),
            budget=object(),
            blackboard=object(),
            goal=goal,
            goal_id=11,
            conversation_id=23,
            channel="dashboard",
            user_id="alice",
            domain=goal.domain,
            shield=_Shield(),
        )

    assert calls == [(23, 7, "user:alice")]
    assert secret not in brief
    assert (
        "[redacted by Shield]" in brief
        if scanner_mode == "deny"
        else orch._SHIELD_WITHHELD in brief
    )
    assert "Binding matter jurisdiction: Tennessee" in brief
    assert "Every legal workflow, authority search, citation" in brief


@pytest.mark.asyncio
async def test_cross_matter_conversation_id_has_no_global_history_fallback(monkeypatch):
    monkeypatch.setenv("MAVERICK_COMPACT_HISTORY", "0")

    class _World:
        def recent_matter_turns(self, *_args, **_kwargs):
            raise AssertionError("cross-matter history read")

        def recent_turns(self, *_args, **_kwargs):
            raise AssertionError("legacy global history fallback")

        def all_questions(self, _goal_id):
            return []

    async def _no_enrich(brief, **_kwargs):
        return brief, "default"

    monkeypatch.setattr(orch, "_apply_brief_enrichments", _no_enrich)
    goal = SimpleNamespace(
        title="Matter seven",
        description="",
        project_id=7,
        owner="user:alice",
        domain="legal_contract_review",
    )
    with matter_context_scope(_context(matter_id=8)):
        brief, _mode = await orch._build_orchestrator_brief(
            llm=object(),
            world=_World(),
            budget=object(),
            blackboard=object(),
            goal=goal,
            goal_id=11,
            conversation_id=23,
            channel="dashboard",
            user_id="alice",
            domain=goal.domain,
            shield=_AllowShield(),
        )

    assert "Prior conversation" not in brief
    assert "Binding matter jurisdiction" not in brief


def test_runtime_acquisition_mcp_and_webhook_paths_are_absent():
    source = inspect.getsource(orch)

    for forbidden in (
        "learn_capability",
        "self_learning.preflight",
        "start_mcp_clients",
        "stop_mcp_clients",
        "_fire_webhook",
        "_finished_webhook_payload",
    ):
        assert forbidden not in source
