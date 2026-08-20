# Law-firm architecture

This repository is the private practice platform for Bjerken and Day. Its
architectural boundary is an exact client matter, a named human principal, and
a reviewed legal profile. The dashboard is the only retained interactive
product surface; the CLI is for installation and local operations.

## Retained release cohort

The repository ships five Python distributions as one lockstep cohort:

| Distribution | Responsibility |
|---|---|
| `maverick-agent` | Matter-bound runner, durable SQLite world, providers, fixed tools, audit, queue, backup, and governed local improvement |
| `maverick-dashboard` | Named-user client/matter intake, matter goals, attachments, review, sign-off, release, and operator health |
| `maverick-knowledge` | Local deterministic embedding, bounded document parsing, chunking, and matter knowledge retrieval |
| `maverick-shield` | Input, tool-argument, and output screening |
| `maverick-installer` | Private configuration wizard and operator bootstrap |

`release-cohort.toml` is the source of truth for package names, paths, modules,
and the shared version. `scripts/install_release_cohort.py` validates, builds,
installs, and imports the cohort under the reviewed dependency constraints.

## Mandatory execution path

1. A named attorney creates or selects a client and opens a matter with a firm
   matter number, jurisdiction, reviewed legal domain, and responsible-attorney
   membership. Those records commit atomically.
2. A goal is stored with that exact matter, owner principal, and legal domain.
   Matterless external producers are retired.
3. The queue producer resolves durable `MatterContext` and signs the envelope's
   version, goal, matter, and principal. Plaintext matter numbers are not copied
   into queue messages.
4. The worker verifies the signature, re-resolves the durable goal and active
   membership, and compares the signed matter and principal immediately before
   dispatch.
5. The runner binds a frozen context containing the matter, client,
   jurisdiction, principal, membership role, legal domain, purpose, source, and
   current egress mode before model, sandbox, or tool work.
6. Provider and HTTP dispatch refresh that durable authority. Revocation,
   reassignment, a domain change, incomplete matter metadata, or a changed
   egress policy fails closed.
7. Draft output and artifacts remain unreleased until a qualified attorney
   signs off on the exact current goal version and release digest. Release and
   sign-off decisions are written through the audit outbox.

There is no administrator bypass for matter execution. Viewer membership and
the shared dashboard bearer are not execution identities.

## Confidentiality and ethical walls

Each matter defaults to `local_only`. Loopback model endpoints remain subject
to the live matter membership check. Public provider or tool egress requires
both:

- a responsible-attorney change of that exact matter to
  `approved_services`; and
- an exact operator allowlist entry in `[firm] approved_providers` or
  `[firm] approved_hosts`.

Non-loopback HTTP must use HTTPS, metadata-service destinations are denied, and
private/link-local addresses do not become trusted merely because of their IP
class. Denials are audited.

Client, matter, attachment, conversation, artifact-title, and artifact-content
fields use authenticated at-rest sealing when the operator provisions the
encryption key. Equality lookup uses purpose-separated keyed digests instead of
plaintext metadata. Attachment content uses opaque content-addressed paths.
Untrusted PDF and DOCX parsing runs in a bounded child process and fails closed
when isolation cannot be established.

Backups are separately signed and streaming-encrypted with AES-256-GCM under an
operator-custodied key that must not live in the data root. Restore authenticates
and decrypts the complete archive before transactional staging.

## Audit and operations

The audit writer is append-only and hash chained, with optional Ed25519 signing
and retained local or S3 Object-Lock WORM export. Audit verification, encrypted
backup create/verify/restore, schema migration governance, config lint, and
legal-profile lint are local operator CLI functions.

SQLite is the canonical firm world and uses a single-writer control-plane lock.
The signed queue can move work to a worker process, but every worker resolves
the same durable matter authority before execution. `/livez`, `/healthz`, and
`/readyz` expose distinct process, dependency, and readiness posture.

## Governed local improvement

Reflexion, dreaming, rehearsal, experience, and distilled skills carry the
exact matter key (and owner where required). A missing matter never falls back
to a global client-derived store. Candidate generation and evaluation are
offline; runtime agents cannot acquire tools or promote code. Promotion is an
explicit operator action governed by the retained self-harness and audit path.

## Deliberately absent

The firm build has no inbound or outbound MCP runtime, gRPC server/plugin host,
external plugin entry points, remote skill/catalog acquisition, fleet/global
learning plane, messaging-channel package, browser/computer-use runtime,
model-visible shell or generic code-execution tool, SCIM or SAML provisioning,
or public PyPI/release workflow. Git history is the
archive for those inherited surfaces.

## Known product limits

- The 31 reviewed profiles are a compact legal roster; dedicated VA,
  family-law, estate/probate, and state-specific real-estate workflows still
  need attorney-authored profiles and live evaluations.
- Conflict intake performs exact normalized-name screening and returns only an
  opaque potential-conflict result. Alias, affiliate, and fuzzy research remains
  a conflicts-counsel workflow.
- Technical controls do not approve a cloud vendor's retention terms,
  privilege posture, or professional-responsibility suitability. The firm must
  review those before adding an allowlist entry.
- A local administrator can read process memory and replace the executable;
  host hardening and operator key custody remain deployment responsibilities.
