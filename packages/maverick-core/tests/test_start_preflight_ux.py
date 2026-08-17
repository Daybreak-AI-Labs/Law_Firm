"""`maverick start` preflight UX: refuse early, leave no residue, exit honestly.

Platform-test findings, round 2 fixes:
  - A halted `maverick start` printed the refusal but exited 0, so scripts
    could not tell "ran" from "refused"; it also created a goal row first.
    Now: killswitch is checked before goal creation -> exit 3, no row.
  - A missing provider SDK (e.g. the openai package for vllm:/ollama:
    routed roles) surfaced AFTER the goal row existed, orphaning a failed
    goal per attempt. Now: SDK availability is preflighted before goal
    creation -> exit 2, no row, same actionable message.
  - Unknown model ids on self-hosted providers (ollama:/vllm:/tgi:)
    billed at the Sonnet fallback rate, accruing phantom spend for free
    local models. Now priced at $0. The generic openai_compatible provider
    is not blanket-zeroed because it can target paid public gateways; an
    unknown hosted rate now fails closed instead of becoming a bill.
"""
from __future__ import annotations

import pytest
from maverick.budget import Budget, UnpricedModelError


def _goal_count(home) -> int:
    import sqlite3
    db = home / ".maverick" / "world.db"
    if not db.exists():
        return 0
    return sqlite3.connect(db).execute("select count(*) from goals").fetchone()[0]


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONFIG", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    # Bust the killswitch's 1s stat-throttle so each test sees ITS home's
    # HALT state, not the previous test's cached answer (established
    # pattern, see test_q1_2026.py).
    from maverick import killswitch as ks
    ks._last_file_check_ts = 0.0
    ks.clear()








def test_missing_sdks_helper_detects_absent_module(monkeypatch):
    import importlib.util

    from maverick import providers

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name == "openai":
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    msgs = providers.missing_sdks(["vllm:stub-1", "claude-opus-4-8"])
    assert any("openai" in m for m in msgs)
    # anthropic SDK is installed -> no complaint about it
    assert not any("anthropic" in m.lower() for m in msgs)


def test_missing_sdks_helper_quiet_when_all_present():
    from maverick import providers
    assert providers.missing_sdks(["claude-opus-4-8"]) == []


def test_unknown_local_models_priced_zero():
    for spec in ("ollama:my-local-llm", "vllm:stub-1", "tgi:custom"):
        b = Budget(max_dollars=1.0)
        b.record_tokens(1000, 1000, model=spec)
        assert b.dollars == 0.0, (spec, b.dollars)


def test_unknown_openai_compatible_model_fails_closed():
    b = Budget(max_dollars=10.0)
    with pytest.raises(UnpricedModelError):
        b.record_tokens(1_000_000, 0, model="openai_compatible:proxy-model")
    assert b.dollars == 0
    assert b.input_tokens == 0


def test_unknown_hosted_model_fails_closed():
    b = Budget(max_dollars=10.0)
    with pytest.raises(UnpricedModelError):
        b.record_tokens(1_000_000, 0, model="mystery-model")
    assert b.dollars == 0
    assert b.input_tokens == 0


def test_known_model_via_local_prefix_still_priced():
    # A REAL priced id behind a local prefix keeps its table rate
    # (prefix-stripping match has priority over the local-zero rule).
    b = Budget(max_dollars=10.0)
    b.record_tokens(1_000_000, 0, model="ollama:deepseek-v4-flash")
    assert b.dollars > 0


