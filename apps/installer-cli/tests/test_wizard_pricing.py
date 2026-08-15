"""Installer coverage for the billing-grade pricing default."""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from maverick_installer import wizard


def test_pick_budget_defaults_to_strict_pricing(monkeypatch):
    prompts: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        wizard,
        "_q_text",
        lambda _message, *, default="": default,
    )

    def confirm(message: str, default: bool = True) -> bool:
        prompts.append((message, default))
        return default

    monkeypatch.setattr(wizard, "_q_confirm", confirm)

    budget = wizard.pick_budget()

    assert budget["strict_pricing"] is True
    assert prompts == [
        (
            "  Strict pricing? Fail closed if a model has no verified price "
            "(recommended; choose No only for estimate-only custom gateways)",
            True,
        )
    ]


def test_pick_budget_preserves_explicit_legacy_estimate_opt_out(monkeypatch):
    monkeypatch.setattr(
        wizard,
        "_q_text",
        lambda _message, *, default="": default,
    )
    monkeypatch.setattr(wizard, "_q_confirm", lambda *_args, **_kwargs: False)

    budget = wizard.pick_budget()

    assert budget["strict_pricing"] is False


def test_generated_config_persists_strict_default(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".env")

    wizard.write_config(
        providers=[],
        role_models={},
        channels={},
        safety={"profile": "balanced"},
        budget={
            "max_dollars": 5.0,
            "max_wall_seconds": 3600.0,
            "max_tool_calls": 500,
            "strict_pricing": True,
        },
        sandbox={"backend": "local", "workdir": "~/maverick-workspace"},
        keys={},
        capabilities={},
    )

    generated = tomllib.loads((tmp_path / "config.toml").read_text())
    assert generated["budget"]["strict_pricing"] is True
