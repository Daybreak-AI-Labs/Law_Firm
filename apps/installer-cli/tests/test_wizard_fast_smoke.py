"""`maverick init --fast` must not report success over a failed smoke test.

run_fast() called smoke_test() and discarded its bool, so the one command whose
whole promise is "trust this without answering questions" printed "Fast setup
finished" and exited 0 on a broken install. An installer that cannot fail is an
installer whose success carries no information.
"""
from __future__ import annotations

from pathlib import Path


def _stub(monkeypatch, tmp_path: Path, *, smoke: bool):
    from maverick_installer import wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path / ".maverick")
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / ".maverick" / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".maverick" / ".env")
    monkeypatch.setattr(wizard, "preflight", lambda: True)
    monkeypatch.setattr(wizard, "welcome", lambda: None)
    monkeypatch.setattr(wizard, "_docker_available", lambda: False)
    monkeypatch.setattr(wizard, "smoke_test", lambda: smoke)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "anthropic:claude-sonnet-4-6")
    return wizard


def test_fast_setup_fails_when_the_smoke_test_fails(monkeypatch, tmp_path):
    wizard = _stub(monkeypatch, tmp_path, smoke=False)
    assert wizard.run_fast() == 1


def test_fast_setup_succeeds_when_the_smoke_test_passes(monkeypatch, tmp_path):
    """Control: rejection only means something if acceptance also works."""
    wizard = _stub(monkeypatch, tmp_path, smoke=True)
    assert wizard.run_fast() == 0


def test_the_config_is_still_written_before_the_smoke_test_verdict(
        monkeypatch, tmp_path):
    """A failed smoke test must not leave the user with nothing to fix.

    write_config runs first deliberately: the operator needs the file on disk to
    inspect and correct. The failure is in the exit code and the message, not in
    withholding the artifact.
    """
    wizard = _stub(monkeypatch, tmp_path, smoke=False)
    assert wizard.run_fast() == 1
    assert (tmp_path / ".maverick" / "config.toml").is_file()


def test_fast_setup_rejects_missing_model_pin_before_write(monkeypatch, tmp_path):
    wizard = _stub(monkeypatch, tmp_path, smoke=True)
    monkeypatch.delenv("MAVERICK_MODEL_OVERRIDE")

    assert wizard.run_fast() == 1
    assert not (tmp_path / ".maverick" / "config.toml").exists()


def test_fast_setup_derives_provider_from_exact_model_pin(monkeypatch, tmp_path):
    wizard = _stub(monkeypatch, tmp_path, smoke=True)
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "openai:gpt-5.4")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-selected-key")

    assert wizard.run_fast() == 0
    config = (tmp_path / ".maverick" / "config.toml").read_text()
    assert '[providers.openai]' in config
    assert 'default = "openai:gpt-5.4"' in config
    assert '[providers.anthropic]' not in config
