# Reviewed legal skills

The core wheel ships exactly 45 local Markdown skills from
`packages/maverick-core/maverick/skills_builtin`. They are procedural prompt
material, not executable plugins, and no remote catalog or installer is present.
User-supplied skills remain untrusted input and require the same review and
matter-bound controls as any other prompt material.

The reviewed roster is:

`breach-notification-timeline`, `cite-sources-or-mark-unverified`,
`clm-metadata-extraction`, `conflict-of-interest-review`,
`contract-obligation-extraction`, `contract-redline-playbook`,
`contract-risk-scoring`, `data-retention-schedule-build`, `decision-memo-author`,
`dpa-review`, `draft-for-human-review`, `due-diligence-data-room`,
`ediscovery-scoping`, `engagement-scoping`, `ephemeral-data-preservation-map`,
`evidence-cited-finding`, `extract-from-document`, `force-majeure-review`,
`forensic-evidence-preservation`, `incident-response-playbook`,
`incident-severity-classification`, `lease-abstract`, `liability-cap-analysis`,
`litigation-hold-scope`, `msa-negotiation-prep`, `nda-review-redline`,
`privacy-dpia`, `prompt-injection-review`, `public-records-request-handling`,
`records-management-program`, `records-retention-schedule`,
`redact-pii-before-egress`, `redact-secrets-in-output`,
`regulatory-applicability-scan`, `regulatory-change-impact`,
`require-human-gate-checklist`, `security-questionnaire-review`,
`sla-terms-review`, `sow-author`, `structured-questionnaire-run`,
`third-party-risk-tiering`, `threat-model-stride`, `vendor-contract-renewal`,
`vendor-risk-assessment`, and `write-to-audit-trail`.

The release tests call `maverick.skills.validate_skill_file` for every shipped
file. Wheel-content and distribution-surface tests pin the count and prohibit
retired plugin/MCP acquisition surfaces from reappearing.
