# External security review: scope and reproducible checks

This page is the starting point for an independent security review of the
Bjerken and Day firm build. Read the [threat model](threat-model.md) first.

The central assertion to attack is narrow: client work must not be read,
executed, transmitted, or released without current named-user authority in the
exact matter and the required attorney gate.

## Priority review surfaces

| Priority | Surface | Expected invariant |
| --- | --- | --- |
| 1 | Dashboard identity and object authorization | A named user sees only matters for which durable membership permits the requested action; denials do not reveal object existence. |
| 2 | Goal runner, queue producer, and worker | No matterless job runs. Principal, matter, client metadata, legal domain, jurisdiction, role, and egress policy are revalidated before external work. |
| 3 | Attorney review and release | Signoff is bound to the immutable release payload; later mutations invalidate it; release produces durable audit evidence. |
| 4 | Model, web-search, and connector egress | `local_only` matters cannot transmit client data. Approved-service traffic is host/scheme constrained and uses verified TLS outside loopback. |
| 5 | Attachments and knowledge ingestion | Matter access is enforced; filenames and archives are bounded; untrusted PDF/DOCX parsing is isolated and fails closed. |
| 6 | Storage, audit, and disaster recovery | Sensitive state is encrypted, the audit chain is verifiable, backups exclude key material, and restore authenticates before staging. |
| 7 | Runtime/tool supply chain | The model-visible tool ceiling is fixed. External plugins, remote skills/catalogs, MCP, gRPC execution, and matterless producers are absent. |

## Installation for review

Use an isolated environment and verify imports resolve to this checkout before
believing test results:

```bash
python -m venv .venv-audit
.venv-audit/bin/python -m pip install -e ./packages/maverick-core
.venv-audit/bin/python -m pip install --no-deps \
  -e ./packages/maverick-shield \
  -e ./packages/maverick-knowledge \
  -e ./packages/maverick-dashboard \
  -e ./apps/installer-cli
.venv-audit/bin/python -c "import maverick; print(maverick.__file__)"
```

On Windows, use `.venv-audit\\Scripts\\python.exe` in place of
`.venv-audit/bin/python`.

## Focused invariant suite

Run these before the broad package suites:

```bash
python -m pytest -q \
  packages/maverick-core/tests/test_matter_context.py \
  packages/maverick-core/tests/test_runner_matter_context.py \
  packages/maverick-core/tests/test_worker_matter_context.py \
  packages/maverick-core/tests/test_queue_dispatcher.py \
  packages/maverick-core/tests/test_matterless_producers_retired.py

python -m pytest -q \
  packages/maverick-core/tests/test_firm_tool_registry.py \
  packages/maverick-core/tests/test_matter_egress.py \
  packages/maverick-core/tests/test_ssrf_guard.py \
  packages/maverick-core/tests/test_ssrf_pinning.py

python -m pytest -q \
  packages/maverick-core/tests/test_parser_isolation.py \
  packages/maverick-core/tests/test_attachments.py \
  packages/maverick-core/tests/test_docx_redline.py \
  packages/maverick-core/tests/test_privacy_ops.py \
  packages/maverick-knowledge/tests/test_parse.py

python -m pytest -q \
  packages/maverick-core/tests/test_backup.py \
  packages/maverick-core/tests/test_world_encryption_coverage.py \
  packages/maverick-core/tests/test_approval_audit_outbox.py \
  packages/maverick-core/tests/test_signoff_audit_outbox.py

python -m pytest -q \
  packages/maverick-dashboard/tests/test_authz_project_scoping.py \
  packages/maverick-dashboard/tests/test_named_auth_boundary.py \
  packages/maverick-dashboard/tests/test_release_boundary.py \
  packages/maverick-dashboard/tests/test_matter_attorney_release.py
```

Then run each retained package suite and preserve the full logs, interpreter
version, dependency inventory, commit id, and configuration used.

## Static and supply-chain checks

Use the versions pinned by CI and treat tool absence as “not measured,” not a
pass:

```bash
python -m ruff check .
python -m bandit -q -r packages apps -lll -ii -s B613 -x '*/tests/*,*/test_*.py'
detect-secrets scan --baseline .secrets.baseline
python -m pip_audit
python -m maverick.reachability --ci
python -m maverick.domain_lint
```

Build every retained wheel, install those wheels into a second clean
environment, run `pip check`, resolve `maverick.__file__`, and smoke-test the
operator CLI and dashboard entry point. Editable-install success alone does not
prove package completeness.

## Adversarial cases an auditor should add

- Revoke membership after enqueue and immediately before model/tool dispatch.
- Move a goal to a different matter, client, domain, or jurisdiction after the
  producer signs the queue envelope.
- Insert the shared dashboard bearer into `matter_memberships` and attempt to
  execute.
- Mutate every field covered by an attorney signoff, then attempt release.
- Attempt cross-matter attachment enumeration and download using guessed ids.
- Redirect an approved host to loopback/private addresses and disable TLS
  verification on an external HTTPS destination.
- Feed malformed PDFs/DOCX files, oversized ZIP metadata, high-ratio ZIP bombs,
  child stdout/stderr floods, and parser timeouts.
- Restore a backup with the wrong encryption key, a modified GCM tag, a changed
  manifest, traversal names, duplicate entries, and the live data-root key
  directory included.
- Try to import or advertise removed MCP, gRPC, external-plugin, catalog, and
  runtime-acquisition modules from a built wheel.

## Evidence limits

Passing repository tests establishes only the named regression contracts.
Static checks do not establish runtime isolation, and proof fixtures do not
establish customer outcomes or model quality. Record skipped tests and platform-
specific exclusions. A clean report still requires independent penetration
testing, deployment configuration review, key-custody review, backup/restore
exercise, and counsel-owned policy controls.

## Known residuals to evaluate

- Application-layer egress enforcement is defense in depth, not a packet-level
  boundary. Validate host/container/VPC default-deny policy independently.
- Local administrators can replace code and read process memory.
- Approved providers can see data deliberately sent to them; vendor trust and
  retention terms remain organizational risks.
- Parser child-process isolation is weaker than a dedicated VM/container.
- Co-located audit-signing keys do not provide credible third-party attribution.
- Secure defaults can be explicitly weakened for development; production
  startup must prove the intended posture rather than infer it from defaults.

Report vulnerabilities through the private process in `SECURITY.md`, not a
public issue. Include the exact commit, platform, configuration, and a minimal
reproducer.
