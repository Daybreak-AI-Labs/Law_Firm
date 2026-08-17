"""After a regulated-posture setup, the wizard surfaces the compliance and
GDPR/EU AI Act documentation commands so a non-technical operator can find them
(the rule-6 integrity check: a capability the operator turned on must be
reachable from the wizard)."""
from __future__ import annotations

from maverick_installer import wizard


def test_regulated_deployment_detects_each_sensitive_control():
    assert wizard._regulated_deployment({"enterprise": True})
    assert wizard._regulated_deployment({"encrypt_at_rest": True})
    assert wizard._regulated_deployment({"audit_sign": True})
    assert wizard._regulated_deployment({"security_autofix": True})
    # A non-sensitive toggle (or nothing) does not trigger the panel.
    assert not wizard._regulated_deployment({"tenant_by_user": True})
    assert not wizard._regulated_deployment({})
    assert not wizard._regulated_deployment(None)


def test_compliance_command_set_is_the_full_surface():
    # Dashboard pages + the kept audit CLI: the enterprise CLI group
    # (compliance / assess / hunt / remediate / ropa / dpia / ai-act /
    # enterprise verify) was removed in the CLI reduction.
    assert [cmd for cmd, _ in wizard._COMPLIANCE_COMMANDS] == [
        "/compliance",
        "/safety",
        "/assessments",
        "/audit/binder",
        "maverick audit verify",
    ]


def test_panel_prints_only_for_a_regulated_deployment(capsys):
    # Not regulated -> silent (no noise for a personal install).
    wizard.show_compliance_commands({})
    assert capsys.readouterr().out == ""

    # Regulated -> the documentation commands are shown.
    wizard.show_compliance_commands({"enterprise": True})
    out = capsys.readouterr().out
    assert "/compliance" in out
    assert "/assessments" in out
    assert "Compliance" in out
