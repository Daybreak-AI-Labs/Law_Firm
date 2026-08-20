"""`maverick --model <id>` must actually override the model for every agent.

The agents resolve their model via model_for_role(), not the LLM facade's
default, so the global flag was silently ignored -- the orchestrator/workers
used config/defaults regardless of --model. The flag now sets a run-wide
override env that model_for_role honors for the whole run.
"""
from __future__ import annotations

from click.testing import CliRunner
from maverick import llm
from maverick.cli import main


def test_global_override_beats_config(monkeypatch):
    # Compatibility-mode precedence is separate from the secure pin contract
    # asserted below.
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    monkeypatch.delenv("MAVERICK_MODEL_OVERRIDE_ORCHESTRATOR", raising=False)
    monkeypatch.setattr("maverick.config.get_role_model", lambda role: "config:model")

    monkeypatch.delenv("MAVERICK_MODEL_OVERRIDE", raising=False)
    assert llm.model_for_role("orchestrator") == "config:model"

    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "flag:model")
    assert llm.model_for_role("orchestrator") == "flag:model"


def test_secure_run_ignores_per_role_override(monkeypatch):
    monkeypatch.setattr(llm, "_secure_model_policy_enabled", lambda: True)
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "flag:model")
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE_CODER", "perrole:model")
    monkeypatch.setattr(llm, "_allowed_model_specs", lambda: set())
    assert llm.model_for_role("coder") == "flag:model"


def test_cli_model_flag_sets_global_override(monkeypatch):
    # Register the var with monkeypatch so its teardown restores it: main()
    # sets os.environ directly, and a bare delenv on an absent var records no
    # undo -- which would leak the override into later tests.
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "")
    # The group callback runs even for a trivial subcommand.
    result = CliRunner().invoke(main, ["--model", "prov:custom", "version"])
    assert result.exit_code == 0
    import os
    assert os.environ.get("MAVERICK_MODEL_OVERRIDE") == "prov:custom"
