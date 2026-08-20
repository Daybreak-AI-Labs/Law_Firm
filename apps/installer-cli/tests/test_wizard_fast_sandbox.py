"""`maverick init --fast` must not guess an unpinned Docker image.

Non-interactive setup has no reviewed image digest to record, so it uses local.
Advanced setup is the explicit Docker path and requires an immutable digest.
"""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def _stub_fast(monkeypatch, tmp_path: Path, *, docker: bool):
    from maverick_installer import wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path / ".maverick")
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / ".maverick" / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".maverick" / ".env")
    monkeypatch.setattr(wizard, "preflight", lambda: True)
    monkeypatch.setattr(wizard, "welcome", lambda: None)
    # Must return True, not None: run_fast now checks the result, so a falsy
    # stub would fail the install these tests are not about. The smoke-test
    # contract itself is covered in test_wizard_fast_smoke.py.
    monkeypatch.setattr(wizard, "smoke_test", lambda: True)
    monkeypatch.setattr(wizard, "_docker_available", lambda: docker)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "anthropic:claude-sonnet-4-6")
    return wizard


def _config(tmp_path: Path) -> dict:
    return tomllib.loads((tmp_path / ".maverick" / "config.toml").read_text())


def _backend(tmp_path: Path) -> str:
    return _config(tmp_path)["sandbox"]["backend"]


def test_fast_setup_falls_back_to_local_when_docker_down(monkeypatch, tmp_path):
    wizard = _stub_fast(monkeypatch, tmp_path, docker=False)
    assert wizard.run_fast() == 0
    assert _backend(tmp_path) == "local"
    cfg = _config(tmp_path)
    assert cfg["models"] == {"default": "anthropic:claude-sonnet-4-6"}
    assert "security" not in cfg
    assert not ({"computer_use", "browser", "code_exec"} & set(cfg.get("capabilities", {})))


def test_fast_setup_stays_local_when_daemon_up(monkeypatch, tmp_path):
    wizard = _stub_fast(monkeypatch, tmp_path, docker=True)
    assert wizard.run_fast() == 0
    assert _backend(tmp_path) == "local"
    cfg = _config(tmp_path)
    assert cfg["models"] == {"default": "anthropic:claude-sonnet-4-6"}
    assert "security" not in cfg
    assert not ({"computer_use", "browser", "code_exec"} & set(cfg.get("capabilities", {})))
