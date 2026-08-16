# Firm-platform refactor plan

The Maverick fork will be reduced by measured, reversible slices rather than by deleting
every optional client or integration in one change. A surface is removed only after its
runtime imports, configuration, installer choices, tests, documentation, and replacement
workflow have been identified together.

## Product boundary

The supported product is the private Bjerken and Day web application and its Python agent
runtime. The first production posture is:

- two named firm users;
- matter-scoped storage and authorization;
- the dashboard as the primary interface;
- one approved hosted model provider, with an optional local provider;
- one production sandbox;
- attorney approval before sending, filing, signing, or changing a system of record; and
- encrypted local data, encrypted backups, and provider-transmission audit events.

MCP, email ingestion, the desktop shell, and other clients remain candidates until the firm
chooses its actual daily workflow. They are not removed merely because they are optional.

## Removal gate

Before deleting a capability, record all of the following in its pull request:

1. The firm workflow that replaces it, or confirmation that no firm workflow uses it.
2. Every production import, API route, CLI command, configuration key, and installer step.
3. Every test, document, workflow, dependency, and lockfile coupled to the capability.
4. Any security property that must survive the deletion.
5. A focused test proving that the retained application still starts and performs its core
   workflow.

Historical source does not need to be kept in the active tree; Git preserves it. The gate
exists to prevent a repository-size cleanup from silently breaking a retained runtime seam.

## Sequence

### 1. Client-data readiness

Implement matter sensitivity, provider allowlists, cross-matter isolation, encrypted backup
and restore, and transmission auditing before real client files enter the system.

### 2. Matter-centered application model

Add clients, prospective clients, matters, parties, conflicts checks, jurisdictions,
documents, versions, deadlines, approvals, time entries, and trust transactions. Agent goals
link to matters; generic goal facts do not substitute for legal records.

### 3. Deployment and provider decisions

Choose the deployment target, provider set, sandbox, authentication method, and approved
ingress channels. Delete alternatives only after these decisions are recorded and the chosen
path has an operational backup and restore test.

### 4. Auxiliary surfaces

Evaluate mobile clients, editor plugins, browser extensions, widgets, public SDKs, and sample
clients one family at a time. Remove a family together with its dedicated CI and documentation
only when no retained workflow consumes it.

### 5. Enterprise and federation layers

Remove hosted-customer administration, fleet federation, external-agent enrollment, SCIM,
SAML, and tenant billing after matter-level authorization has been separated from tenant-level
infrastructure. Matter isolation and ethical walls remain mandatory.

### 6. Reviewed improvement

Replace autonomous production prompt/model promotion with draft-to-final diffs and explicit
attorney promotion of clauses, templates, and instructions. Preserve evaluation and rollback
controls until their replacements are proven.

## Definition of done

The refactor is complete when a clean setup installs only the supported packages and the firm
can intake a prospective client, run and approve a conflicts check, open a jurisdiction-bound
matter, ingest and retrieve matter-scoped documents, create an attorney-reviewed draft, record
a deadline, and restore the encrypted database from backup.
