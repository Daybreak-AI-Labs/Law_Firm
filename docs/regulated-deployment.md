# Regulated firm deployment

This is a deployment checklist, not a legal attestation. Qualified counsel and
the firm's security owner remain responsible for retention, professional rules,
client commitments, incident response, and release decisions.

## Required boundary

A production run is admitted only when durable state proves:

- a positive matter and client id, canonical matter number, and jurisdiction;
- a named principal with an active attorney/staff matter membership;
- a legal domain whose workflow ends in review or approval;
- goal owner, matter, domain, purpose, and signed queue identity still match;
- the matter's current `egress_mode` permits attempted external work.

There is no administrator or shared-bearer bypass. Workers re-resolve this
authority immediately before dispatch, and tool/model egress rechecks it again.

## Baseline configuration

```toml
[enterprise]
mode = true
require = true

[security]
secure_defaults = true
approvals_required = 2

[safety]
profile = "strict"
scan_input = true
scan_tool_calls = true
scan_output = true

[audit]
sign = true

[encryption]
at_rest = true

[retention]
audit_days = 365
episodes_days = 90
events_days = 365
```

Tune retention only through a counsel-approved schedule. Inject application,
audit, backup, queue, and OIDC keys from independent operator custody. None may
live in the data root or encrypted backup archive.

Set `MAVERICK_REQUIRE_ENTERPRISE=1`, `MAVERICK_REQUIRE_SHIELD=1`, and
`MAVERICK_CONFIG_STRICT=1`. Use named OIDC identities; the static dashboard
bearer is not an execution identity.

## Network and model boundary

`[enterprise] mode = true` blocks cloud model dispatch and applies the guarded
HTTP send policy. Each matter defaults to `local_only`; only a responsible
attorney can move it to reviewed `approved_services` mode.

Application guards are defense in depth, not a packet firewall. Enforce default-
deny host/container/VPC egress and isolate parsers and subprocesses. The legal
roster grants no agent shell/code execution, but deployment containment remains
required for libraries and operator processes.

## Storage, audit, and recovery

- Keep `MAVERICK_HOME` on a private encrypted volume.
- Require AES-256-GCM at-rest sealing and pin the injected key digest.
- Require the Ed25519 audit chain with off-host key custody.
- Create, verify, and restore disaster-recovery archives only through the local
  `maverick backup` CLI. Restore authenticates and decrypts before staging.
- Exercise wrong-key, tamper, rollback, and key-exclusion scenarios. Never copy
  custody keys into the data root.

## Release and operations gate

Before first service and after every upgrade:

```powershell
maverick config-lint
maverick doctor
maverick domains-lint --ci
maverick audit verify
maverick backup verify <archive>
```

Also verify the five-wheel cohort, OIDC login and matter membership, mid-run
revocation, approval/release audit linkage, parser isolation, local knowledge
model digest, queue tamper rejection, and encrypted restore into an empty staging
root. Any failed check blocks deployment.
