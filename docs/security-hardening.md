# Security hardening

This guide describes the retained private law-firm runtime. It is an operator
checklist, not a claim that configuration alone establishes privilege,
professional-responsibility compliance, or vendor suitability. Start with the
[threat model](security/threat-model.md) and record the firm's decisions.

## 1. Use named human identities

- Require the named invite/session flow or a fully configured asymmetric OIDC
  browser login.
- Give each person only the matters and role they need. Matter execution
  requires an active membership; viewers cannot execute.
- Do not treat a global administrator role as a matter-membership bypass.
- Do not use the shared dashboard bearer for client work. The runtime rejects
  that synthetic principal even if someone manually inserts a membership row.
- Revoke a departing user's membership and session. Live model and HTTP
  dispatch re-check durable authority so an already-running task loses access
  at the next privileged boundary.

The firm build does not include SAML or SCIM provisioning. Offboarding remains
an explicit identity-provider, dashboard-session, and matter-membership
procedure.

## 2. Require a complete matter before execution

A runnable goal must resolve to all of the following durable records:

- a positive client and matter identifier;
- a nonblank firm matter number and jurisdiction;
- a named principal with active non-viewer membership;
- a reviewed, enabled legal domain with terminal review and approval gates;
- an execution purpose, source, and current matter egress mode.

The producer signs the queue version, goal, matter, and principal. The worker
verifies that envelope and independently re-resolves the current goal,
membership, client, jurisdiction, domain, and egress policy immediately before
dispatch. A moved goal, revoked user, changed domain, incomplete legacy matter,
tampered envelope, or missing context fails before model or sandbox work.

Do not create alternate producer paths around this choke point. The inherited
remote execution, scheduled matterless goals, external webhook execution, and
generic protocol servers are not part of the firm runtime.

## 3. Default every matter to local-only

New matters use `local_only`. Public model or tool egress requires two
independent decisions:

1. the responsible attorney changes that exact matter to
   `approved_services`; and
2. the operator adds the exact provider and HTTPS host to `[firm]`.

Keep `[firm].approved_providers`, `approved_hosts`, and
`approved_local_hosts` empty until contracts, retention, privilege, security,
and professional-responsibility terms have been reviewed. Entries are exact;
wildcards and suffix matching are not authority.

Loopback runtimes remain subject to the live matter check. Non-loopback HTTP
must use HTTPS. Metadata-service destinations are denied, and private or
link-local addresses require explicit local-host approval. Test both permitted
and denied destinations before admitting client data and confirm denials appear
in audit.

## 4. Protect data and keys

Provision `MAVERICK_ENCRYPTION_KEY` from an operator-controlled secret manager.
The retained stores seal sensitive client, matter, conversation, attachment,
artifact-title, and artifact-content values with authenticated encryption.
Equality search uses purpose-separated keyed digests; it does not make metadata
guess-resistant if an attacker also obtains the key.

Attachment filenames and storage paths are opaque. Untrusted PDF and DOCX
parsing executes in a bounded child process, caps its IPC and archive reads,
and fails closed if isolation cannot be established. Keep parser dependencies
pinned and exercise malformed, oversized, and decompression-bomb fixtures after
upgrades.

Use separate keys for at-rest sealing, audit signing, queue authentication,
backup signing, and backup encryption. Never place key files under the Maverick
data root. Host administrators can read process memory and replace the binary,
so harden the OS, service account, secret injection, and filesystem permissions
as part of the deployment.

## 5. Keep the tool ceiling small

The firm build intentionally removes messaging channels, browser and computer
control, model-visible shell and generic code execution, remote skill/catalog
acquisition, external plugins, MCP, gRPC, fleet execution, and public package
release machinery. Do not reintroduce an inherited protocol merely to satisfy
an old configuration or document.

Retained web research and system-of-record actions still cross trust
boundaries. Keep them disabled unless a legal profile requires them, restrict
destinations and credentials, and retain simulate/review/commit controls for
writes. A sandbox is not an authorization mechanism; matter context, tool
policy, budget, and egress checks must all pass first.

## 6. Bind approval to exact content

Agent output is a draft. Release requires a qualified attorney's sign-off on
the current goal version and the exact artifact digest. Editing the content,
moving the goal, or superseding an artifact invalidates stale approval.
Sign-off and release decisions use the audit outbox so business state and the
corresponding audit event commit together.

Use Ed25519 approval keys for self-improvement promotions and keep private keys
outside the data root. Do not use a generic administrator click as a substitute
for the responsible or qualified attorney required by the matter policy.

## 7. Sign and verify audit records

Enable audit signing and inject the private key through operator custody:

```toml
[audit]
sign = true
```

```bash
maverick audit verify
```

The local audit log is append-only and hash chained. Optional WORM export may
target a reviewed local retention mount or S3 Object Lock, but the export is
only as strong as the bucket policy, retention mode, key custody, and operator
monitoring. Verify the local chain and the retained copy during recovery drills.

## 8. Encrypt and rehearse disaster recovery

Set independent `MAVERICK_BACKUP_SIGNING_KEY` and
`MAVERICK_BACKUP_ENCRYPTION_KEY` values from operator custody. Then exercise the
local operator boundary:

```bash
maverick backup create /operator-custody/firm.mvkb
maverick backup verify /operator-custody/firm.mvkb
maverick backup restore /operator-custody/firm.mvkb
```

Creation streams an authenticated AES-256-GCM archive and excludes key
material. Normal restore authenticates and decrypts the complete archive before
transactional staging. A wrong key, tamper, missing key, malformed member, or
oversized archive fails closed.

Require deliberate restore confirmation, restore to a controlled host, and
test rollback as well as success. A backup that has never been restored is not
recovery evidence.

## 9. Govern local improvement

Keep learning records matter-scoped and encrypted. Leave learning-only provider
egress off. Candidate evaluation should use held-out evidence and the retained
risk-limited self-harness; runtime agents cannot acquire tools or adopt code.
Promotion remains an explicit operator action with signed approval and audit.

Review the promotion corpus for cross-matter leakage, prompt injection, weak
judges, and repeated-query overfitting. Green fixture tests establish only the
tested controls, not legal quality or an improvement in client outcomes.

## 10. Deployment gate

Before enabling client data:

```bash
maverick config-lint
maverick doctor
maverick domains-lint
maverick audit verify
maverick backup verify /operator-custody/firm.mvkb
```

Also verify:

- the process imports all five distributions from the reviewed checkout;
- `/livez`, `/healthz`, and `/readyz` report the expected process, dependency,
  and readiness state;
- only one writable control plane owns the SQLite data root;
- named login, membership revocation, viewer denial, and shared-bearer denial
  work in the deployed topology;
- local-only blocks public providers and HTTP tools, while each deliberately
  approved service works only for its approved matter;
- parser failure, queue tamper, stale approval, wrong backup key, and audit
  tamper all fail closed;
- qualified counsel has reviewed the 31 enabled legal profiles and the firm's
  jurisdiction-specific workflow.

Record versions, commands, timestamps, and evidence. Do not generalize a clean
lint, a fixture proof, or a partial test run into a production-readiness claim.
See [regulated deployment](regulated-deployment.md) and
[audit readiness](security/audit-readiness.md) for the retained evidence plan.
