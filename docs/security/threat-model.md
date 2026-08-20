# Law-firm threat model

This is the working threat model for the Bjerken and Day firm build. It covers
the retained local dashboard, worker, governed agent runtime, matter storage,
knowledge ingestion, read-only connectors, and encrypted disaster-recovery
path. It does not describe capabilities removed from this fork.

The review method is STRIDE: spoofing, tampering, repudiation, information
disclosure, denial of service, and elevation of privilege.

## Security boundary

```text
named firm user
      |
      v
local dashboard / operator CLI
      |
      v
durable goal + MatterContext ----> signed local queue envelope ----> worker
      |                                                        |
      +--------------------------------------------------------+
                               |
                               v
                    governed agent + fixed tool ceiling
                               |
               +---------------+----------------+
               |                                |
               v                                v
     local encrypted state          approved model/search/read-only
     knowledge and audit             connector destinations only
```

The firm runtime has no inbound MCP or gRPC execution service, no external
plugin loader, no remote skill/catalog installer, and no browser/computer/shell
tool in the model-visible firm registry. Those absences are security controls,
not optional configuration.

## Required execution invariants

Every production goal run must satisfy all of these conditions:

1. The durable goal identifies a positive existing matter and a known enabled
   legal domain whose workflow ends in a review or approval gate.
2. The matter has a positive client id, canonical matter number, jurisdiction,
   and a valid `local_only` or `approved_services` egress policy.
3. The authenticated principal is a named active matter member with an
   execution-capable role. Viewer membership, global administration, and the
   shared dashboard bearer do not bypass the ethical wall.
4. The runner binds an immutable `MatterContext` before model, sandbox, or tool
   work. External dispatch refreshes durable authority so membership revocation,
   a moved goal, or changed matter policy fails closed.
5. Queue producers sign the exact matter id and principal. Workers authenticate
   the versioned envelope, re-resolve durable authority, and compare it
   immediately before dispatch. Matterless or tampered jobs do no work.
6. The tool registry intersects the fixed firm ceiling with the exact legal
   profile and bound matter. Missing context exposes only pure context-free
   helpers; later registration cannot shadow a retained first-party tool.

The executable contracts live in `maverick/matter_context.py`,
`maverick/runner.py`, `maverick/queue_dispatcher.py`, and
`maverick/tools/__init__.py`.

## Assets

- Client, matter, membership, goal, conversation, and deliverable records.
- Matter attachments and the per-domain knowledge index.
- Provider and connector credentials.
- Audit events, approval/signoff records, and release evidence.
- At-rest encryption, audit-signing, queue-signing, backup-signing, and backup-
  encryption keys.
- Encrypted backup archives and restore staging directories.

Matter confidentiality is the primary asset. A successful user authentication
does not imply authority to see or execute work in every matter.

## Threats and controls

### Spoofing

| Threat | Control |
| --- | --- |
| Caller forges another user | Named dashboard identity is required for matter operations; the static shared bearer is rejected for execution. |
| User claims membership in another matter | Membership and role are read from durable matter state; there is no administrator bypass. |
| Queue message forges a principal or matter | The bounded versioned envelope is HMAC-authenticated and the worker re-resolves both values before dispatch. |
| Webhook or retired remote protocol starts work | Matterless producer paths and the remote MCP/gRPC execution surfaces are absent or fail closed. |

### Tampering

| Threat | Control |
| --- | --- |
| Goal is moved or its domain/policy changes after enqueue | Worker and external-dispatch chokepoints refresh authority and reject snapshot drift. |
| Audit or signoff evidence is modified | Secure defaults enable the signed audit chain; release/signoff transitions are recorded through durable audit outboxes. Operators must retain the verification key independently for third-party evidence. |
| Backup archive is modified | Restore authenticates the AES-256-GCM envelope and signed manifest before staging. Wrong-key and tampered archives fail closed. |
| CLI source file changes between inspection and install | `--from-file` uses one identity-bound regular-file handle and installs the exact bytes that were parsed and linted. |
| Hostile archive expands beyond its declared size | Attachment and document ZIP readers validate metadata and enforce bounded reads before decompression. |

### Repudiation

| Threat | Control |
| --- | --- |
| Attorney disputes a release decision | Signoff is bound to the immutable deliverable/release payload and recorded with audit outbox evidence. A later payload mutation invalidates the prior signoff. |
| Operator disputes a restore | Backup create/verify/restore are explicit local CLI operations; restore requires deliberate confirmation and logs the verified source. |
| Audit key is co-located with the evidence | Treat co-located signing keys as integrity checks only. Independent key custody is required for credible external verification. |

### Information disclosure

| Threat | Control |
| --- | --- |
| IDOR exposes another matter | Object access is checked against the exact client/matter membership, and denials do not disclose object existence. |
| Provider or connector receives data from a local-only matter | Central model/HTTP dispatch checks the refreshed per-matter egress mode before bytes leave the process. |
| Connector mutates an external legal system | The retained Carta, Clio, Contractbook, DocuSign, and Ironclad connector schemas are GET-only. |
| Untrusted PDF/DOCX exploits a parser | Rich-document parsing uses a bounded child process and has no in-process fallback on isolation failure. The in-process escape hatch is explicitly trusted/test-only. |
| Logs or outputs contain secrets | Secret scrubbing and audit redaction run before retained logs/exports; adversarial and ReDoS regressions are tested. |
| Backup captures live encryption keys | Backup staging excludes key material. Backup encryption uses a separately custodied `MAVERICK_BACKUP_ENCRYPTION_KEY`, never a key stored in the data root or archive. |

### Denial of service

| Threat | Control |
| --- | --- |
| Runaway model/tool work exhausts budget | Dollar, token, tool-call, depth, and wall-time ceilings stop dispatch. |
| Parser child floods IPC or hangs | Input, stdout, and stderr are byte-capped; the child has a hard timeout and is killed on failure. |
| ZIP bomb exhausts disk or memory | Archive entry counts, compressed/uncompressed sizes, aggregate output, and read lengths are bounded. |
| Queue replay duplicates work | Envelopes are expiring and nonce/message-id bound; terminal consumption requires a fresh signed submission. |

### Elevation of privilege

| Threat | Control |
| --- | --- |
| Viewer or global admin executes client work | Only responsible-attorney, attorney, and staff matter roles may execute; resolution has no global-admin bypass. |
| Subagent acquires a broader tool set | Capabilities and the firm registry are narrow-only at every agent boundary. Runtime skill/tool acquisition is removed. |
| Tool shadows a trusted connector/helper | Secure registration refuses duplicate first-party names and any name outside the fixed ceiling. |
| Application-layer egress guard is bypassed | Use host, container, or VPC default-deny egress as the hard boundary. The Python guard is defense in depth, not a packet firewall. |

## Key custody

Do not use one key for multiple purposes. At minimum, custody must distinguish:

- at-rest encryption;
- audit signing;
- local/network queue signing where enabled;
- backup manifest signing; and
- backup encryption.

The backup encryption key must be injected by the operator from outside the
Maverick data root. A backup that includes its own decryption key is not a
confidential backup.

## Out of scope and residual risk

- A local administrator or root attacker can read process memory and replace
  executable code.
- A compromised approved model or SaaS endpoint can misuse data deliberately
  sent to it. `approved_services` is an authorization decision, not a provider
  integrity guarantee.
- The application egress guard covers supported dispatch paths; raw sockets,
  subprocess clients, native libraries, and a compromised interpreter require
  deployment-level network containment.
- Process isolation reduces parser blast radius but is not equivalent to a
  separately sandboxed VM.
- Secure defaults can be explicitly weakened for development. Production
  startup and deployment validation must reject that posture for client data.
- Tests and proof artifacts establish named invariants only; they are not a
  penetration test, legal-compliance attestation, or efficacy benchmark.

## Review triggers

Review this model whenever a new outbound destination, parser format, tool,
matter role, queue transport, authentication mode, or release path is added.
Any proposal to restore remote execution, external plugins, runtime acquisition,
or an unbounded browser/computer tool requires a new threat-model decision
before implementation.
