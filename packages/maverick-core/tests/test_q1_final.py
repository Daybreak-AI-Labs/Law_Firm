"""Final Q1 2026 tests for prompt caching, world indexes, and wizard resume."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

# ---------- openai_provider prompt-caching extraction ----------

def test_openai_provider_records_cache_read_tokens():
    """When usage.prompt_tokens_details.cached_tokens is present, it
    flows into budget as cache_read_tok and billable input = full - cached.
    """
    try:
        from openai import OpenAI  # noqa: F401
    except ImportError:
        pytest.skip("openai SDK not installed")
    from maverick.budget import Budget
    from maverick.providers.openai_provider import OpenAIClient

    # Build a fake response shape matching the SDK's pydantic model.
    fake_choice = MagicMock()
    fake_choice.message.content = "hi"
    fake_choice.message.tool_calls = None
    fake_choice.finish_reason = "stop"

    fake_usage = MagicMock()
    fake_usage.prompt_tokens = 1000
    fake_usage.completion_tokens = 50
    # OpenAI-style cached tokens.
    fake_usage.prompt_tokens_details.cached_tokens = 400
    # Make sure the DeepSeek-shaped attr isn't preferred when OpenAI's is set.
    fake_usage.prompt_cache_hit_tokens = 999

    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = fake_usage

    budget = Budget(max_dollars=10.0)
    OpenAIClient._from_response(fake_resp, budget, model="gpt-5.4")

    # full=1000, cached=400 -> billable=600 to input_tokens; cached -> cache_read_tokens.
    assert budget.input_tokens == 600
    assert budget.cache_read_tokens == 400
    assert budget.output_tokens == 50


def test_openai_provider_records_deepseek_cache_hit_tokens():
    """DeepSeek puts cached count under prompt_cache_hit_tokens."""
    try:
        from openai import OpenAI  # noqa: F401
    except ImportError:
        pytest.skip("openai SDK not installed")
    from maverick.budget import Budget
    from maverick.providers.openai_provider import OpenAIClient

    fake_choice = MagicMock()
    fake_choice.message.content = "hi"
    fake_choice.message.tool_calls = None
    fake_choice.finish_reason = "stop"

    fake_usage = MagicMock()
    fake_usage.prompt_tokens = 2000
    fake_usage.completion_tokens = 100
    # DeepSeek shape: OpenAI-style details absent, fall through to *_hit_tokens.
    fake_usage.prompt_tokens_details = None
    fake_usage.prompt_cache_hit_tokens = 800

    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = fake_usage

    budget = Budget(max_dollars=10.0)
    OpenAIClient._from_response(
        fake_resp,
        budget,
        model="deepseek-v4-flash",
    )

    assert budget.input_tokens == 1200
    assert budget.cache_read_tokens == 800
    assert budget.output_tokens == 100


def test_openai_provider_no_cache_data_records_full_input():
    """When no cache fields, full prompt_tokens count as billable."""
    try:
        from openai import OpenAI  # noqa: F401
    except ImportError:
        pytest.skip("openai SDK not installed")
    from maverick.budget import Budget
    from maverick.providers.openai_provider import OpenAIClient

    fake_choice = MagicMock()
    fake_choice.message.content = "hi"
    fake_choice.message.tool_calls = None
    fake_choice.finish_reason = "stop"

    fake_usage = MagicMock()
    fake_usage.prompt_tokens = 500
    fake_usage.completion_tokens = 50
    fake_usage.prompt_tokens_details = None
    # Explicitly delete the DeepSeek attr so getattr returns the default.
    del fake_usage.prompt_cache_hit_tokens

    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = fake_usage

    budget = Budget(max_dollars=10.0)
    OpenAIClient._from_response(fake_resp, budget, model="gpt-5.4")

    assert budget.input_tokens == 500
    assert budget.cache_read_tokens == 0
    assert budget.output_tokens == 50


# ---------- world-model indexes ----------

def test_world_model_v8_indices_present(tmp_path):
    """A fresh world model should have all v8 indices."""
    from maverick.world_model import WorldModel
    wm = WorldModel(tmp_path / "wm.sqlite")
    try:
        names = [
            row["name"] for row in wm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        ]
    finally:
        wm.close()
    for expected in (
        "idx_episodes_goal_started",
        "idx_episodes_started",
        "idx_goals_status_updated",
        "idx_goals_parent",
    ):
        assert expected in names, f"missing index: {expected}"

# ---------- wizard --resume ----------

def test_wizard_resume_loads_partial_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_installer import wizard

    cfg_dir = tmp_path / ".maverick"
    cfg_dir.mkdir()
    wizard.CONFIG_DIR = cfg_dir
    wizard.PARTIAL_STATE_PATH = cfg_dir / "wizard-partial.json"

    # Pre-populate a partial state.
    pre = {
        "deployment": "local",
        "providers": ["anthropic"],
        "run_model": "anthropic:claude-sonnet-4-6",
        "safety": {"profile": "balanced", "block_threshold": "high",
                   "scan_input": True, "scan_tool_calls": True, "scan_output": True},
        "budget": {"max_dollars": 5.0, "max_wall_seconds": 3600.0, "max_tool_calls": 500},
        "sandbox": {"backend": "local", "workdir": "/tmp/ws", "timeout": 60},
        "capabilities": {"computer_use": False, "browser": False},
    }
    wizard.PARTIAL_STATE_PATH.write_text(json.dumps(pre))

    loaded = wizard._load_partial()
    assert loaded == pre


def test_wizard_run_accepts_resume_flag():
    import inspect

    from maverick_installer.wizard import run
    sig = inspect.signature(run)
    assert "resume" in sig.parameters
    assert sig.parameters["resume"].default is False


def test_wizard_partial_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_installer import wizard

    cfg_dir = tmp_path / ".maverick"
    wizard.CONFIG_DIR = cfg_dir
    wizard.PARTIAL_STATE_PATH = cfg_dir / "wizard-partial.json"

    wizard._save_partial({"deployment": "docker", "providers": ["anthropic", "openai"]})
    loaded = wizard._load_partial()
    assert loaded == {"deployment": "docker", "providers": ["anthropic", "openai"]}
    wizard._clear_partial()
    assert wizard._load_partial() is None
