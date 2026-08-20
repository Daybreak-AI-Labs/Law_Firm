# Repository guidance

Read `AGENTS.md` first; it is the authoritative build, test, and editing guide.

This is the private Bjerken and Day law-firm runtime. The supported Python
cohort is exactly the five distributions in `release-cohort.toml`: core,
Shield, dashboard, knowledge, and installer. Build and install them from one
reviewed commit with `scripts/install_release_cohort.py`; the repository root is
not itself an installable Python project.

## Firm boundary

- Execution is always tied to one durable client matter, named principal,
  active membership, jurisdiction, and executable legal profile.
- Every shipped legal profile ends in attorney review or approval. Drafts are
  not legal advice and cannot be released without qualified-attorney sign-off
  on the exact current artifact.
- Client data is encrypted at rest. Queue, audit, backup-signing, and separately
  custodied backup-encryption keys are mandatory operational boundaries.
- Matter egress is local-only unless the responsible attorney and operator both
  authorize the exact retained provider/host route.
- Local learning remains matter-scoped and review-gated. It must not transfer
  client content or guidance between matters.
- The execution backends are exactly local and Docker. Secure/container-required
  Docker operation requires an immutable image digest and fails closed.

## Validation

Run commands from the repository root through the intended interpreter:

```bash
python -m pytest -q
python -m ruff check .
maverick domains-lint --ci
python -m maverick.migration_governance --ci
python -m maverick.schema_migrations --ci
python -m maverick.reachability --ci
```

Use `python -m pytest`, never a bare `pytest`. Before trusting a result, confirm
`maverick.__file__` resolves inside this checkout. Do not broaden a green
focused test or static check into a production-readiness claim.

Dashboard tests use a same-origin `TestClient` and an isolated temporary world
database. New security changes need failure-path tests for authorization,
rollback/recovery, tamper, and revoked authority where applicable.
