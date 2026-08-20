"""Installer coverage for the surviving security controls.

The GRC self-certification cluster (security records workspace, AI evidence
gateway, Model Risk officer, environment hunter and its connector fabric) was
deleted; the wizard now configures only the review-gated evidence graph and
the read-only platform threat hunter.
"""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def test_pick_security_suite_safe_defaults(monkeypatch):
    from maverick_installer import wizard

    answers = iter([False, False])
    monkeypatch.setattr(
        wizard, "_q_confirm", lambda *args, **kwargs: next(answers)
    )

    assert wizard.pick_security_suite() == {
        "evidence_graph": False,
        "threat_hunt": False,
    }


def test_pick_security_suite_each_surface_is_independent(monkeypatch):
    from maverick_installer import wizard

    answers = iter([True, False])
    monkeypatch.setattr(
        wizard, "_q_confirm", lambda *args, **kwargs: next(answers)
    )

    assert wizard.pick_security_suite() == {
        "evidence_graph": True,
        "threat_hunt": False,
    }


def test_write_config_emits_independent_security_knobs(tmp_path, monkeypatch):
    from maverick_installer import wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".env")
    wizard.write_config(
        providers=["anthropic"],
        run_model="anthropic:claude-sonnet-4-6",
        safety={"profile": "balanced"},
        budget={"max_dollars": 5.0, "max_wall_seconds": 600,
                "max_tool_calls": 30},
        sandbox={"backend": "local", "workdir": "~/ws"},
        keys={},
        capabilities={},
        security_suite={
            "evidence_graph": True,
            "threat_hunt": False,
        },
    )

    parsed = tomllib.loads((tmp_path / "config.toml").read_text())
    assert parsed["evidence_graph"]["enable"] is True
    assert parsed["threat_hunt"]["enable"] is False
    # The deleted GRC/self-certification knobs must never be re-emitted.
    for gone in (
        "security_ops",
        "evidence_gateway",
        "model_risk_assurance",
        "model_improvement",
        "env_hunt",
        "governed_records",
    ):
        assert gone not in parsed


def test_cfg_security_suite_emits_only_surviving_tables():
    from maverick_installer import wizard

    lines = wizard._cfg_security_suite({
        "evidence_graph": True,
        "threat_hunt": True,
    })
    parsed = tomllib.loads("\n".join(lines))
    assert parsed == {
        "evidence_graph": {"enable": True},
        "threat_hunt": {"enable": True},
    }
