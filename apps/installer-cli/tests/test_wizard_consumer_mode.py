"""Council round-2: consumer-mode flow + first-screen picker."""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover -- Py 3.10 CI matrix
    import tomli as tomllib  # type: ignore[no-redef]


# ---------- pick_mode ----------

def test_pick_mode_default_is_consumer(monkeypatch):
    from maverick_installer import wizard
    captured = {}
    def fake_select(message, choices, default=None):
        captured["default"] = default
        return default
    monkeypatch.setattr(wizard, "_q_select", fake_select)
    mode = wizard.pick_mode()
    assert mode == "consumer"
    assert captured["default"].startswith("consumer")


def test_pick_mode_advanced_selectable(monkeypatch):
    from maverick_installer import wizard
    monkeypatch.setattr(wizard, "_q_select",
                        lambda *a, **kw: "advanced - let me configure everything")
    assert wizard.pick_mode() == "advanced"


def test_pick_mode_express_selectable(monkeypatch):
    from maverick_installer import wizard
    monkeypatch.setattr(wizard, "_q_select",
                        lambda *a, **kw: "express  - turn on all features, few questions")
    assert wizard.pick_mode() == "express"


# ---------- run_express: everything-on shortcut ----------

def test_run_express_enables_all_safe_features(monkeypatch, tmp_path: Path):
    """Express writes a single valid config that turns the safe single-user
    features ON and keeps host-dangerous ones OFF."""
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    rc = wizard.run_express()
    assert rc == 0

    # A duplicate table would raise here -- the whole config must parse.
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())

    # Product surfaces + the self-improvement lifecycle are ON.
    assert config["flows"]["enable"] is True
    assert config["connections"]["enable"] is True
    assert config["self_learning"]["enable"] is True
    assert config["durable"]["enabled"] is True
    assert config["reflexion"]["enable"] is True
    assert config["dreaming"]["enable"] is True
    assert config["data_engine"]["enable"] is True
    assert config["capabilities"]["web_search"] is True

    # Retired host-dangerous capabilities are absent from the firm config; the
    # fixed runtime registry is the enforcement boundary.
    assert not ({"computer_use", "browser", "code_exec"} & set(config["capabilities"]))
    assert "security" not in config
    # Self-learning is local and provider egress remains off.
    assert config["self_learning"]["allow_provider_egress"] is False
    assert config["self_learning"]["distill_local"] is True
    # DGM is a separate, explicit production decision even in express mode.
    assert "self_modify" not in config

    # The smoke-test canary: sandbox survives the round-trip (no [flows] dup).
    assert config["sandbox"]["backend"] == "local"
    assert config["models"] == {"default": "anthropic:claude-sonnet-4-6"}
    assert "routing" not in config


def test_run_express_never_guesses_a_mutable_docker_image(
    monkeypatch, tmp_path: Path,
):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_docker_available", lambda: True)

    rc = wizard.run_express()

    assert rc == 0
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["sandbox"]["backend"] == "local"
    assert "security" not in config
    assert not ({"computer_use", "browser", "code_exec"} & set(config["capabilities"]))


def test_run_express_routed_via_pick_mode(monkeypatch, tmp_path: Path):
    """Non-resume run() forks to run_express() when express is picked."""
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "pick_mode", lambda: "express")
    monkeypatch.setattr(wizard, "welcome", lambda: None)
    rc = wizard.run(fast=False, resume=False)
    assert rc == 0
    assert (tmp_path / ".maverick" / "config.toml").exists()


# ---------- run_consumer happy path ----------

def _stub_wizard_io(monkeypatch, tmp_path: Path, key: str = "sk-ant-test"):
    """Wire all the IO primitives the consumer flow touches."""
    from maverick_installer import wizard

    # The consumer flow is interactive; present an interactive stdin so run()'s
    # non-TTY guard doesn't short-circuit the prompt flow under pytest.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    answers = iter([
        # user_name
        "Alex",
        # workdir
        str(tmp_path / "workspace"),
    ])
    # Each _q_text call pops the next answer.
    monkeypatch.setattr(wizard, "_q_text", lambda *a, **kw: next(answers))
    monkeypatch.setattr(wizard, "_q_confirm", lambda *a, **kw: True)
    monkeypatch.setattr(wizard, "_q_select", lambda *a, **kw: "$5")
    monkeypatch.setattr(wizard, "pick_providers", lambda: ["anthropic"])
    monkeypatch.setattr(
        wizard,
        "pick_run_model",
        lambda providers: "anthropic:claude-sonnet-4-6",
    )
    monkeypatch.setattr(
        wizard,
        "collect_api_keys",
        lambda providers, extra_envs: ({"ANTHROPIC_API_KEY": key} if key else {}),
    )

    # Fix the config dir + skip the real preflight (uses console output).
    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path / ".maverick")
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / ".maverick" / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".maverick" / ".env")
    monkeypatch.setattr(
        wizard, "VALIDATION_CACHE_PATH",
        tmp_path / ".maverick" / "validation-cache.json",
    )
    monkeypatch.setattr(wizard, "PARTIAL_STATE_PATH",
                        tmp_path / ".maverick" / "wizard-partial.json")
    monkeypatch.setattr(wizard, "preflight", lambda: True)
    # Pretend Docker is unavailable so the test is hermetic.
    monkeypatch.setattr(wizard, "_docker_available", lambda: False)
    return wizard


def test_run_consumer_writes_safe_defaults(monkeypatch, tmp_path: Path):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    rc = wizard.run_consumer()
    assert rc == 0

    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    # Safe defaults per the safety seat.
    assert config["safety"]["profile"] == "strict"
    assert config["safety"]["block_threshold"] == "medium"
    assert config["sandbox"]["backend"] == "local"          # no docker
    assert "security" not in config
    assert not ({"computer_use", "browser", "code_exec"} & set(config["capabilities"]))
    assert config["retention"]["audit_days"] == 30
    assert config["rate_limits"]["web_search"] == "5/60"
    assert config["persona"]["user_name"] == "Alex"
    assert config["budget"]["max_dollars"] == 5.0
    assert config["capabilities"]["web_search"] is True
    assert config["self_learning"]["enable"] is True
    assert config["self_learning"]["allow_provider_egress"] is False
    assert config["self_learning"]["distill_local"] is True
    assert config["models"] == {"default": "anthropic:claude-sonnet-4-6"}
    assert "self_modify" not in config
    # No legacy channel configuration.
    assert "channels" not in config

    # API key landed in .env at chmod 600.
    env = (tmp_path / ".maverick" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env
    import os
    import stat
    mode = stat.S_IMODE((tmp_path / ".maverick" / ".env").stat().st_mode)
    if os.name != "nt":  # NTFS reports 0o666 regardless of the chmod
        assert mode == 0o600


def test_run_consumer_skip_key_succeeds(monkeypatch, tmp_path: Path):
    """Empty key → wizard saves config without secrets, no crash."""
    wizard = _stub_wizard_io(monkeypatch, tmp_path, key="")
    rc = wizard.run_consumer()
    assert rc == 0
    assert (tmp_path / ".maverick" / "config.toml").exists()
    # No .env created when no keys.
    assert not (tmp_path / ".maverick" / ".env").exists()


def test_run_consumer_never_guesses_a_mutable_docker_image(monkeypatch, tmp_path: Path):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_docker_available", lambda: True)
    wizard.run_consumer()
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert config["sandbox"]["backend"] == "local"


def test_run_consumer_with_docker_available_does_not_regrow_host_tools(
    monkeypatch, tmp_path: Path,
):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_docker_available", lambda: True)
    wizard.run_consumer()
    config = tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())
    assert "security" not in config
    assert not ({"computer_use", "browser", "code_exec"} & set(config["capabilities"]))


def test_run_consumer_demo_panel_points_at_dashboard(monkeypatch, tmp_path: Path, capsys):
    """The closing panel points at the dashboard with the curated starter goal
    (the `maverick start` demo command went with the CLI reduction)."""
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    wizard.run_consumer()
    out = capsys.readouterr().out
    # The closing panel prints the next step + the curated demo prompt.
    assert "maverick dashboard" in out
    assert "source-cited research memo" in out


def test_run_consumer_creates_workdir(monkeypatch, tmp_path: Path):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    wizard.run_consumer()
    assert (tmp_path / "workspace").exists()


def test_run_consumer_install_failure_renders_branded_panel(monkeypatch, tmp_path: Path, capsys):
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    def _boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(wizard, "write_config", _boom)
    rc = wizard.run_consumer()
    assert rc == 1
    out = capsys.readouterr().out
    assert "Setup hit a problem" in out
    assert "disk full" in out


# ---------- run() forks on mode ----------

def test_run_consumer_routed_via_pick_mode(monkeypatch, tmp_path: Path):
    """Non-resume run() asks for mode and forks to run_consumer() on default."""
    wizard = _stub_wizard_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "pick_mode", lambda: "consumer")
    monkeypatch.setattr(wizard, "welcome", lambda: None)
    rc = wizard.run(fast=False, resume=False)
    assert rc == 0
    assert (tmp_path / ".maverick" / "config.toml").exists()


def test_run_resume_skips_mode_picker(monkeypatch, tmp_path: Path):
    """--resume implies an in-progress advanced flow; don't re-pick mode."""
    from maverick_installer import wizard
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    called = {"pick_mode": False, "run_consumer": False}
    monkeypatch.setattr(wizard, "pick_mode", lambda: (called.__setitem__("pick_mode", True) or "consumer"))
    monkeypatch.setattr(wizard, "run_consumer", lambda: (called.__setitem__("run_consumer", True) or 0))
    monkeypatch.setattr(wizard, "welcome", lambda: None)
    monkeypatch.setattr(wizard, "preflight", lambda: False)  # short-circuit
    wizard.run(fast=False, resume=True)
    assert called["pick_mode"] is False
    assert called["run_consumer"] is False


def test_run_aborts_with_guidance_when_not_a_tty(monkeypatch):
    """Non-interactive stdin (CI / Docker / `... | maverick init`) must yield
    actionable guidance and a clean exit, not questionary's terse 'Aborted!'."""
    import io

    from maverick_installer import wizard
    from rich.console import Console

    monkeypatch.setattr(
        wizard, "console",
        Console(file=io.StringIO(), force_terminal=False, no_color=True),
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = wizard.run(fast=False, resume=False)
    assert rc == 1
    out = wizard.console.file.getvalue()
    assert "interactive terminal" in out
    assert "--fast" in out
