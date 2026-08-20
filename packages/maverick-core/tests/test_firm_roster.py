from __future__ import annotations

from maverick.domain import available_domains, builtin_dir, suite_for
from maverick.skills import (
    builtin_skills_dir,
    load_builtin_skills,
    validate_skill_file,
)

EXPECTED_DOMAINS = {
    "legal", "legal_board", "legal_briefs", "legal_citation",
    "legal_conflicts", "legal_contract_drafting", "legal_contract_intake",
    "legal_contract_review", "legal_data_breach_legal",
    "legal_dpa_negotiation", "legal_ediscovery", "legal_employment",
    "legal_entity_mgmt", "legal_hold", "legal_intake",
    "legal_investigations", "legal_km", "legal_litigation_mgmt",
    "legal_matter_intake", "legal_matter_mgmt", "legal_msa_playbook",
    "legal_nda_desk", "legal_negotiation", "legal_obligations",
    "legal_privacy", "legal_privacy_litigation", "legal_research",
    "legal_saas_agreement", "legal_settlement", "legal_subpoena",
    "legal_vendor_contract_review",
}

EXPECTED_SKILLS = {
    "breach-notification-timeline", "cite-sources-or-mark-unverified",
    "clm-metadata-extraction", "conflict-of-interest-review",
    "contract-obligation-extraction", "contract-redline-playbook",
    "contract-risk-scoring", "data-retention-schedule-build",
    "decision-memo-author", "dpa-review", "draft-for-human-review",
    "due-diligence-data-room", "ediscovery-scoping", "engagement-scoping",
    "ephemeral-data-preservation-map", "evidence-cited-finding",
    "extract-from-document", "force-majeure-review",
    "forensic-evidence-preservation", "incident-response-playbook",
    "incident-severity-classification", "lease-abstract",
    "liability-cap-analysis", "litigation-hold-scope",
    "msa-negotiation-prep", "nda-review-redline", "privacy-dpia",
    "prompt-injection-review", "public-records-request-handling",
    "records-management-program", "records-retention-schedule",
    "redact-pii-before-egress", "redact-secrets-in-output",
    "regulatory-applicability-scan", "regulatory-change-impact",
    "require-human-gate-checklist", "security-questionnaire-review",
    "sla-terms-review", "sow-author", "structured-questionnaire-run",
    "third-party-risk-tiering", "threat-model-stride",
    "vendor-contract-renewal", "vendor-risk-assessment",
    "write-to-audit-trail",
}


def test_builtin_roster_is_exactly_the_firm_legal_roster():
    domains = available_domains()
    assert set(domains) == EXPECTED_DOMAINS
    assert {suite_for(name) for name in domains} == {"legal"}


def test_builtin_roster_does_not_claim_unavailable_citator_integrations():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(builtin_dir().glob("*.toml"))
    ).lower()
    assert "mcp_servers" not in text
    assert "keycite" not in text
    assert "shepard" not in text
    assert "westlaw" not in text
    assert "lexis" not in text


def test_builtin_skill_library_is_exact_and_publish_valid():
    skills = load_builtin_skills()
    assert {skill.name for skill in skills} == EXPECTED_SKILLS
    results = {
        path.name: validate_skill_file(path)
        for path in sorted(builtin_skills_dir().glob("*.md"))
    }
    assert results
    assert not {
        name: result.errors
        for name, result in results.items()
        if not result.ok
    }
