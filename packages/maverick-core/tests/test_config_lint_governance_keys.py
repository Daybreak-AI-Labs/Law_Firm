"""config-lint must recognize the flagship governance config keys.

Client-journey finding (round 5): a regulated client configured the two
headline governance features straight from the docs --

    [audit]
    sign = true
    [capabilities]
    enforce = true

-- and `maverick config-lint` flagged [audit] as an unknown section ("did
you mean auth?") and capabilities.enforce as an unknown key, telling them
their flagship signed-audit + capability-enforcement config looked like
typos. Both are real keys the runtime reads (audit/writer.py:_resolve_signing,
capability.py:capability_enforced).
"""
from __future__ import annotations

from maverick.config_lint import lint_config


def _unknown(findings, section):
    return [f for f in findings if f.section == section
            and ("unknown" in f.message.lower())]


def test_audit_sign_is_recognized():
    findings = lint_config({"audit": {"sign": True}})
    assert not _unknown(findings, "audit"), [f.message for f in findings]


def test_capabilities_enforce_is_recognized():
    findings = lint_config({"capabilities": {"enforce": True}})
    assert not [f for f in findings if "enforce" in (f.key or "")], \
        [f.message for f in findings]


def test_full_governed_config_lints_clean():
    cfg = {
        "providers": {"vllm": {"base_url": "http://localhost:8000/v1"}},
        "budget": {"max_dollars": 2.0},
        "safety": {"profile": "strict"},
        "audit": {"sign": True},
        "capabilities": {"enforce": True},
        "roles": {"ap_clerk": {"max_risk": "low"}},
    }
    errors = [f for f in lint_config(cfg) if f.severity == "error"]
    unknown = [f for f in lint_config(cfg) if "unknown" in f.message.lower()]
    assert not errors, [f.message for f in errors]
    assert not unknown, [f.message for f in unknown]


def test_a_real_typo_is_still_caught():
    # The fix must not blunt the lint: a genuine unknown key still warns.
    findings = lint_config({"capabilities": {"enforcee": True}})
    assert any("enforcee" in (f.key or "") for f in findings), findings


def test_evidence_graph_is_closed_and_boolean():
    assert lint_config({"evidence_graph": {"enable": True}}) == []

    unknown = lint_config({"evidence_graph": {"enabel": True}})
    assert any(
        finding.section == "evidence_graph"
        and finding.key == "enabel"
        and "unknown" in finding.message.lower()
        for finding in unknown
    )

    wrong_type = lint_config({"evidence_graph": {"enable": "true"}})
    assert any(
        finding.section == "evidence_graph"
        and finding.key == "enable"
        and finding.severity == "error"
        for finding in wrong_type
    )


def test_firm_egress_allowlists_are_closed_schema():
    assert lint_config({
        "firm": {
            "approved_providers": ["openai"],
            "approved_hosts": ["api.openai.com"],
            "approved_local_hosts": ["model.firm.internal"],
        }
    }) == []

    findings = lint_config({"firm": {"approved_host": ["api.openai.com"]}})
    assert any(
        finding.section == "firm"
        and finding.key == "approved_host"
        and "unknown" in finding.message.lower()
        for finding in findings
    )
