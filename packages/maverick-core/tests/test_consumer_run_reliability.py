"""Reliability fixes surfaced by dogfooding the consumer run path.

- A config-only provider (local / OpenAI-compatible model, key in config not a
  well-known env var) must not be wrongly blocked by the key gate.
- A run that errors must mark its goal failed, not leave a ghost stuck
  'active' forever.
- The "shield SDK missing" and "skill distill disabled" advisories must not
  spam every run / chat turn.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

# ---------- A: provider gate validates the selected route --------------------

def test_require_llm_key_accepts_selected_route(monkeypatch):
    from maverick import cli
    monkeypatch.setattr(
        cli,
        "_model_route_configuration_missing",
        lambda model=None: ("ollama", ()),
    )
    assert cli._require_llm_key("ollama:qwen") == "config"


def test_require_llm_key_blocks_when_selected_route_is_incomplete(monkeypatch):
    from maverick import cli
    monkeypatch.setattr(
        cli,
        "_model_route_configuration_missing",
        lambda model=None: ("anthropic", ("api_key",)),
    )
    with pytest.raises(SystemExit):
        cli._require_llm_key("anthropic:test")


@pytest.mark.parametrize("provider", ["ollama", "tgi", "vllm"])
def test_keyless_local_route_is_not_rejected(provider, monkeypatch):
    from maverick import cli

    monkeypatch.setattr("maverick.config.load_config", dict)
    assert cli._require_llm_key(f"{provider}:test") == "config"


def test_codex_login_is_not_rejected(monkeypatch):
    from maverick import cli

    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.setattr(
        "maverick.providers.codex_cli_provider._auth_file_present",
        lambda: True,
    )
    assert cli._require_llm_key("codex_cli:gpt-5") == "config"


def test_codex_config_table_token_is_not_rejected(monkeypatch):
    from maverick import cli

    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {
            "providers": {"codex_cli": {"api_key": "configured-token"}}  # pragma: allowlist secret
        },
    )
    monkeypatch.setattr(
        "maverick.providers.codex_cli_provider._auth_file_present",
        lambda: False,
    )
    assert cli._require_llm_key("codex_cli:gpt-5") == "config"


def test_unrelated_provider_key_does_not_unblock_selected_route(monkeypatch):
    from maverick import cli

    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    with pytest.raises(SystemExit):
        cli._require_llm_key("anthropic:test")


@pytest.mark.parametrize(
    "provider",
    [
        "anthropic",
        "openai",
        "openrouter",
        "ollama",
        "gemini",
        "moonshot",
        "deepseek",
        "xai",
        "tgi",
        "vllm",
        "azure",
        "bedrock",
        "openai_compatible",
        "codex_cli",
    ],
)
def test_cli_selected_route_contract_matches_operator_preflight(
    provider, monkeypatch,
):
    from maverick import cli, config
    from maverick.operator_preflight import _route_configuration_missing

    for name in (
        config.PROVIDER_CREDENTIAL_ENV_VARS
        + config.PROVIDER_BASE_URL_ENV_VARS
        + (
            "AZURE_OPENAI_AUTH",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AWS_REGION",
        )
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(
        "maverick.providers.codex_cli_provider._auth_file_present",
        lambda: False,
    )

    selected, missing = cli._model_route_configuration_missing(
        f"{provider}:test"
    )
    assert selected == provider
    assert missing == _route_configuration_missing(provider, {})


# ---------- B: an erroring run marks its goal failed (no ghost) ----------

@pytest.mark.asyncio
async def test_run_goal_marks_goal_failed_on_unexpected_error(tmp_path: Path):
    from maverick.budget import Budget
    from maverick.orchestrator import run_goal
    from maverick.sandbox import LocalBackend
    from maverick.world_model import WorldModel

    class BoomLLM:
        model = "fake:boom"

        async def complete_async(self, **kwargs):
            raise RuntimeError("simulated provider failure")

        def complete(self, **kwargs):
            raise RuntimeError("simulated provider failure")

    world = WorldModel(tmp_path / "world.db")
    gid = world.create_goal("do a thing", "")
    with pytest.raises(RuntimeError):
        await run_goal(
            BoomLLM(), world, Budget(max_dollars=1.0), gid,
            sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
        )
    # The goal must NOT be left 'active' -- it is marked terminal.
    assert world.get_goal(gid).status == "blocked"


# ---------- C: advisories warn at most once per process ----------

def test_shield_sdk_missing_warns_once(caplog):
    from maverick_shield import guard

    if guard._HAVE_SDK:
        pytest.skip("agent-shield SDK installed; advisory not emitted")
    guard._WARNED_SDK_MISSING = False
    with caplog.at_level(logging.WARNING, logger="maverick_shield.guard"):
        guard.Shield(warn_if_missing=True)
        guard.Shield(warn_if_missing=True)
        guard.Shield(warn_if_missing=True)
    hits = [r for r in caplog.records if "agent-shield SDK not installed" in r.getMessage()]
    assert len(hits) == 1


# ---------- chat threads conversation memory across turns ----------



