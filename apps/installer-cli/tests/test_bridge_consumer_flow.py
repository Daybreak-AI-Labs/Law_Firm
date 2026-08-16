"""Desktop installer sidecar (bridge.py) drives the 5-question consumer
flow and writes the SAME config the CLI's run_consumer produces.

The bridge speaks a line-delimited JSON protocol over stdin/stdout.
These tests drive it in-process by monkeypatching _recv/_send so we
don't need a real subprocess.
"""
from __future__ import annotations

try:
    import tomllib  # 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def _drive(monkeypatch, tmp_path, answers, *, key="sk-ant-test"):
    """Run bridge.run() feeding `answers` in order; capture emitted steps."""
    from maverick_installer import bridge, wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path / ".maverick")
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / ".maverick" / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".maverick" / ".env")
    monkeypatch.setattr(wizard, "_docker_available", lambda: False)

    sent = []
    recv_iter = iter(answers)

    monkeypatch.setattr(bridge, "_send", lambda step: sent.append(step))
    monkeypatch.setattr(bridge, "_recv", lambda: next(recv_iter, ""))
    bridge.run()
    return sent


def test_bridge_asks_five_questions_in_order(monkeypatch, tmp_path):
    sent = _drive(monkeypatch, tmp_path, [
        "",            # initial invoke (no answer)
        "Alex",        # name
        "essentials",  # governance level
        "sk-ant-xyz",  # api key
        str(tmp_path / "ws"),  # workdir
        "$5",          # budget
    ])
    ids = [s["id"] for s in sent]
    # name → governance → api_key → workdir → budget → __done__
    assert ids == ["name", "governance", "api_key", "workdir", "budget", "__done__"]
    # No jargon questions (no "deployment", "providers", "channels", "safety").
    for forbidden in ("deployment", "providers", "channels", "safety"):
        assert forbidden not in ids


def test_bridge_writes_consumer_safe_defaults(monkeypatch, tmp_path):
    _drive(monkeypatch, tmp_path, [
        "", "Alex", "essentials", "sk-ant-xyz", str(tmp_path / "ws"), "$5",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    # Identical safe defaults to the CLI consumer flow.
    assert config["safety"]["profile"] == "strict"
    assert config["sandbox"]["backend"] == "local"
    assert "computer" in config["security"]["denied_tools"]
    assert "browser" in config["security"]["denied_tools"]
    assert config["retention"]["audit_days"] == 30
    assert config["persona"]["user_name"] == "Alex"
    assert config["budget"]["max_dollars"] == 5.0
    # No channels / mcp / plugins.
    assert "channels" not in config
    assert "mcp_servers" not in config


def test_bridge_api_key_written_to_env(monkeypatch, tmp_path):
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "essentials", "sk-ant-secret", str(tmp_path / "ws"), "$1",
    ])
    env = (tmp_path / ".maverick" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-secret" in env
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["budget"]["max_dollars"] == 1.0


def test_bridge_blank_key_skips_env(monkeypatch, tmp_path):
    sent = _drive(monkeypatch, tmp_path, [
        "", "Sam", "essentials", "", str(tmp_path / "ws"), "$20",
    ])
    # No .env file when no key.
    assert not (tmp_path / ".maverick" / ".env").exists()
    # Done message acknowledges the skip.
    done = next(s for s in sent if s["id"] == "__done__")
    assert "Add an API key later" in done["question"]


def test_bridge_budget_fallback_on_garbage(monkeypatch, tmp_path):
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "essentials", "sk-ant-x", str(tmp_path / "ws"), "not-a-number",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["budget"]["max_dollars"] == 5.0  # fallback


def test_bridge_default_workdir_when_blank(monkeypatch, tmp_path):
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "essentials", "sk-ant-x", "", "$5",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    # Falls back to ~/Documents/Maverick.
    assert config["sandbox"]["workdir"].endswith("Maverick")


def test_bridge_steps_carry_kind_for_ui(monkeypatch, tmp_path):
    """The Svelte UI needs `kind` to render text vs secret vs choice."""
    sent = _drive(monkeypatch, tmp_path, [
        "", "Alex", "essentials", "sk-ant-x", str(tmp_path / "ws"), "$5",
    ])
    by_id = {s["id"]: s for s in sent}
    assert by_id["name"]["kind"] == "text"
    assert by_id["api_key"]["kind"] == "secret"
    assert by_id["workdir"]["kind"] == "text"
    assert by_id["budget"]["kind"] == "choice"
    assert by_id["governance"]["kind"] == "choice"


def test_bridge_governance_level_applies(monkeypatch, tmp_path):
    """Picking a level in the desktop flow lands the same preset as the CLI."""
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "regulated - bank / clinic / government (audit everything)",
        "sk-ant-x", str(tmp_path / "ws"), "$5",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["governance"]["profile"] == "regulated"
    assert config["audit"]["sign"] is True
    assert config["quotas"]["enforce"] is True
    assert config["self_learning"]["enable"] is True


def test_bridge_governance_blank_answer_takes_default(monkeypatch, tmp_path):
    """A UI that SKIPS the step (blank answer) still writes a valid config."""
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "", "sk-ant-x", str(tmp_path / "ws"), "$5",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["governance"]["profile"] == "essentials"
    assert "audit" not in config


def test_bridge_governance_garbage_fails_loudly(monkeypatch, tmp_path):
    """A NON-blank answer naming no known level is a UI protocol bug and must
    error, never silently downgrade a bank to the least-governed tier."""
    import pytest
    with pytest.raises(ValueError, match="unrecognized governance level"):
        _drive(monkeypatch, tmp_path, [
            "", "Sam", "whatever", "sk-ant-x", str(tmp_path / "ws"), "$5",
        ])
    # Nothing half-written.
    assert not (tmp_path / ".maverick" / "config.toml").exists()


def test_bridge_governance_substring_answer_resolves(monkeypatch, tmp_path):
    """A UI echoing a reworded label still resolves if it names the level."""
    _drive(monkeypatch, tmp_path, [
        "", "Sam", "The Regulated option please", "sk-ant-x",
        str(tmp_path / "ws"), "$5",
    ])
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["governance"]["profile"] == "regulated"


def test_bridge_governance_menu_is_the_wizard_menu(monkeypatch, tmp_path):
    """Rule 6: the wizard is the UX source of truth -- the desktop menu must
    be the SAME list object contents the CLI shows, so they cannot drift."""
    from maverick_installer import wizard
    sent = _drive(monkeypatch, tmp_path, [
        "", "Sam", "essentials", "sk-ant-x", str(tmp_path / "ws"), "$5",
    ])
    gov = next(s for s in sent if s["id"] == "governance")
    assert gov["choices"] == list(wizard.GOVERNANCE_CHOICES)
