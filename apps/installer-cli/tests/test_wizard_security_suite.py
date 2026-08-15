"""Installer coverage for the Security/GRC and defensive-hunter controls."""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def test_pick_security_suite_safe_defaults(monkeypatch):
    from maverick_installer import wizard

    answers = iter([True, False, False, False, False, False, False])
    monkeypatch.setattr(
        wizard, "_q_confirm", lambda *args, **kwargs: next(answers)
    )

    assert wizard.pick_security_suite() == {
        "security_ops": True,
        "evidence_graph": False,
        "model_risk_assurance": False,
        "evidence_gateway": False,
        "model_improvement": False,
        "allow_hosted_training": False,
        "threat_hunt": False,
        "env_hunt": False,
        "response_execution": False,
    }


def test_pick_security_suite_response_is_separate_opt_in(monkeypatch):
    from maverick_installer import wizard

    answers = iter([True, True, True, True, True, True, True, True, True])
    monkeypatch.setattr(
        wizard, "_q_confirm", lambda *args, **kwargs: next(answers)
    )
    checkbox_answers = iter([
        ["AWS CloudTrail", "Microsoft Entra ID"],
        ["AWS CloudTrail"],
        ["Microsoft Entra ID"],
    ])
    monkeypatch.setattr(wizard, "_q_checkbox", lambda *args, **kwargs: next(checkbox_answers))
    text_answers = iter(["virustotal, abuseipdb", "120"])
    monkeypatch.setattr(wizard, "_q_text", lambda *args, **kwargs: next(text_answers))

    picked = wizard.pick_security_suite()
    assert picked["evidence_gateway"] is True
    assert picked["model_improvement"] is True
    assert picked["allow_hosted_training"] is True
    assert picked["env_hunt"] is True
    assert picked["response_execution"] is True
    assert picked["connectors"]["cloudtrail"] == {
        "enable": True, "push_enable": True, "pivot_enable": False,
    }
    assert picked["connectors"]["entra"] == {
        "enable": True, "push_enable": False, "pivot_enable": True,
    }
    assert picked["connectors"]["edr"] == {
        "enable": False, "push_enable": False, "pivot_enable": False,
    }
    assert picked["enrichment_sources"] == ["virustotal", "abuseipdb"]
    assert picked["poll_seconds"] == 120


def test_write_config_emits_independent_security_knobs(tmp_path, monkeypatch):
    from maverick_installer import wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".env")
    wizard.write_config(
        providers=["anthropic"],
        role_models={},
        channels={},
        safety={"profile": "balanced"},
        budget={"max_dollars": 5.0, "max_wall_seconds": 600,
                "max_tool_calls": 30},
        sandbox={"backend": "local", "workdir": "~/ws"},
        keys={},
        capabilities={},
        security_suite={
            "security_ops": True,
            "evidence_graph": True,
            "model_risk_assurance": True,
            "evidence_gateway": True,
            "model_improvement": True,
            "allow_hosted_training": False,
            "threat_hunt": True,
            "env_hunt": True,
            "response_execution": False,
        },
    )

    parsed = tomllib.loads((tmp_path / "config.toml").read_text())
    assert parsed["security_ops"]["enable"] is True
    assert parsed["governed_records"] == {"backend": "auto"}
    assert parsed["evidence_graph"]["enable"] is True
    assert parsed["model_risk_assurance"] == {
        "enable": True,
        "gate_promotions": True,
    }
    assert parsed["evidence_gateway"] == {"enable": True}
    assert parsed["model_improvement"] == {
        "enable": True,
        "allow_hosted": False,
        "allow_cross_tenant": False,
        "require_signed_receipt": True,
        "minimum_train_families": 20,
        "minimum_holdout_families": 20,
    }
    assert parsed["threat_hunt"]["enable"] is True
    assert parsed["env_hunt"] == {
        "enable": True,
        "response_execution": False,
        "poll_seconds": 300,
        "connectors": {
            "cloudtrail": {"enable": False, "push_enable": False, "pivot_enable": False},
            "guardduty": {"enable": False, "push_enable": False, "pivot_enable": False},
            "syslog": {"enable": False, "push_enable": False, "pivot_enable": False},
            "edr": {"enable": False, "push_enable": False, "pivot_enable": False},
            "splunk": {"enable": False, "push_enable": False, "pivot_enable": False},
            "elastic": {"enable": False, "push_enable": False, "pivot_enable": False},
            "sentinel": {"enable": False, "push_enable": False, "pivot_enable": False},
            "kubernetes_audit": {"enable": False, "push_enable": False, "pivot_enable": False},
            "okta": {"enable": False, "push_enable": False, "pivot_enable": False},
            "entra": {"enable": False, "push_enable": False, "pivot_enable": False},
        },
    }


def test_evidence_gateway_enables_assurance_dependencies(monkeypatch):
    from maverick_installer import wizard

    answers = iter([True, False, False, True, False, False, False])
    monkeypatch.setattr(
        wizard, "_q_confirm", lambda *args, **kwargs: next(answers)
    )

    picked = wizard.pick_security_suite()

    assert picked["evidence_gateway"] is True
    assert picked["evidence_graph"] is True
    assert picked["model_risk_assurance"] is True


def test_security_connector_knobs_are_explicit_and_independent():
    from maverick_installer import wizard

    lines = wizard._cfg_security_suite({
        "security_ops": True,
        "env_hunt": True,
        "connectors": {
            "cloudtrail": {"enable": True, "push_enable": True},
            "entra": {"enable": True, "pivot_enable": True},
        },
    })
    parsed = tomllib.loads("\n".join(lines))
    assert parsed["env_hunt"]["connectors"]["cloudtrail"]["enable"] is True
    assert parsed["env_hunt"]["connectors"]["cloudtrail"]["push_enable"] is True
    assert parsed["env_hunt"]["connectors"]["cloudtrail"]["pivot_enable"] is False
    assert parsed["env_hunt"]["connectors"]["entra"]["enable"] is True
    assert parsed["env_hunt"]["connectors"]["entra"]["pivot_enable"] is True
    assert parsed["env_hunt"]["connectors"]["edr"]["enable"] is False


def test_security_enrichment_choices_render_as_runtime_enable_tables():
    from maverick_installer import wizard

    lines = wizard._cfg_security_suite({
        "security_ops": True,
        "env_hunt": True,
        "enrichment_sources": ["VirusTotal", "abuseipdb", "virustotal"],
    })
    parsed = tomllib.loads("\n".join(lines))

    assert parsed["env_hunt"]["enrichment_sources"] == {
        "virustotal": {"enable": True},
        "abuseipdb": {"enable": True},
    }
