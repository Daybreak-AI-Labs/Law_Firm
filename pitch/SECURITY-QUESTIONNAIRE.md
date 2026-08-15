# Lightwork — Security & Trust Questionnaire (CAIQ-lite)

> **Lightwork, by Daybreak Labs** — pre-answered security assessment for your
> review team. Structured after the CSA Consensus Assessments Initiative
> Questionnaire (CAIQ) / Cloud Controls Matrix domains.
>
> Contact: `info@daybreakailabs.com` · daybreakailabs.com

---

## Read this first (honest posture)

- **Stage:** Lightwork is in **private alpha**. This document is **control
  coverage**, not a legal attestation or completed certification.
- **No certification is claimed.** We have **no completed SOC 2 / ISO 27001**
  today; we present control-to-mechanism mapping and can share our security
  overview and current SOC 2 readiness on request.
- **Deployment model matters.** Lightwork **self-hosts in your environment**
  (your VPC, on-prem, or air-gapped). Most controls below are enforced by
  **software you run**, not a service we operate — so *you* hold the data,
  keys, and network boundary.
- **"Enterprise mode" vs. default.** A bare developer install favors
  convenience and **fails open** on some paths. The hardened guarantees
  (egress lock, capability enforcement, dual-control, KMS) bind when you enable
  **Enterprise mode** / a **compliance profile** (e.g. `[compliance]
  profiles=['hipaa']`) — which is the supported posture for regulated
  production. Items below are flagged accordingly.

**Response legend:** ✅ Yes (implemented) · ◑ Configurable / Enterprise-mode ·
🟡 Partial · ⏳ Roadmap · ➖ N/A · ❓ On request

---

## 1. Governance, Risk & Compliance (GRC)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 1.1 | Is there a policy engine that governs agent actions? | ✅ | Every tool call clears a strictest-wins chokepoint: capability → policy (ALLOW / DENY / REQUIRE_HUMAN) → shield → budget → signed audit, before it executes. |
| 1.2 | Are high-risk / dollar-value actions gated for human approval? | ✅ | Amount-tier authority gate (e.g. deny wires > threshold; require-human above a dollar limit), plus EU AI Act Art-14 human-oversight holds. Demonstrable via the golden-path run. |
| 1.3 | Are prohibited uses enforced and non-overridable? | ✅ | Always-on hard-refusal floor rendered into every specialist pack (`domain_refusals`); no human-override path. |
| 1.4 | Is there documented control-to-framework mapping? | ◑ | `maverick compliance` maps live controls to EU AI Act, GDPR, NIST AI RMF, Colorado AI Act, NYC LL144, EEOC, CCPA/CPRA. Coverage report, **not** a legal attestation. |
| 1.5 | Are governance guarantees tested, not just asserted? | ✅ | Roster-wide invariant suite verifies six load-bearing invariants across all 2,020 specialist packs, fault-injected with a non-vacuity control. `maverick domains-lint` / `domains-audit`. |

## 2. Identity & Access Management (IAM)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 2.1 | Is SSO / OIDC supported? | ◑ | OpenID-Connect ID-token (JWT) verification against a configured issuer for `maverick serve` (`oidc.py`); a verified SSO user maps to a least-privilege principal. |
| 2.2 | Is role-based access control enforced? | ◑ | RBAC with per-role tool allow/deny and max-risk ceilings; global / channel / user ACLs; fail-closed on config error. |
| 2.3 | Is least privilege enforced for agents (not just users)? | ✅ | Attenuate-only capability tokens: a sub-agent can only **narrow** its parent's rights, never widen — enforced at every spawn and tool call. |
| 2.4 | Are per-action / short-lived credentials supported? | ◑ | Per-call capability tokens: a broad run-grant is exchanged for a single-tool, single-use, ~30s signed token per call (`tool_token`; `[capabilities] per_call_tokens`). |
| 2.5 | Is separation of duties enforced on approvals? | ✅ | Dual control: the principal who initiates an action can never be the one who approves it (N-of-M). |
| 2.6 | Is there an emergency stop / kill switch? | ✅ | File, in-process, and cluster-wide kill switch checked at every tool boundary (`maverick halt`). |

## 3. Data Security & Encryption (DSP / EKM)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 3.1 | Is data encrypted at rest? | ✅ | AES-256-GCM envelope encryption of memory + world-model content (`tenant/kms.py`); on by default. |
| 3.2 | Is customer-managed / BYOK key management supported? | ◑ | Cloud KMS BYOK — AWS KMS, GCP KMS, HashiCorp Vault transit. The KEK stays in the customer HSM; **fail-closed** (no silent downgrade to local keys). |
| 3.3 | Is data encrypted in transit? | ✅ | Standard TLS for all network surfaces; self-hosted, so transit stays inside your boundary. |
| 3.4 | Is tenant data isolated in multi-tenant deployments? | ◑ | Per-tenant Data Encryption Keys + Postgres row-level security; one tenant's key can never open another's. |
| 3.5 | Can the agent's outbound data paths be locked down? | ◑ | Enterprise-mode **egress lock** blocks the agent's outbound paths (model calls + HTTP tools) at the gate, so a prompt-injected agent can't exfiltrate through them. Pair with a network egress firewall for raw-shell paths. |
| 3.6 | Is sensitive data redacted in logs / telemetry? | ✅ | Secret/PII redaction at ingest and in support bundles (`privacy.py`); no required external telemetry. |

## 4. Logging, Monitoring & Audit (LOG)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 4.1 | Is there an immutable, tamper-evident audit trail? | ✅ | Append-only, **Ed25519-signed, hash-chained** Operating Record; altering any row breaks the chain. On by default. |
| 4.2 | Can the audit log be verified independently / offline? | ✅ | `maverick audit verify --pubkey <externally-held key>` walks signatures + hash chain with no network. (Verified in the golden-path demo: chain intact; a tampered amount is caught as `bad_hash`.) |
| 4.3 | Is whole-file deletion / truncation detectable? | ✅ | Cross-file tip anchors detect removal/truncation of an entire day-file, not just row edits. |
| 4.4 | Can the signing key be custodied off-host / in a KMS? | ◑ | Signing key can be held off-host / KMS-wrapped (`MAVERICK_AUDIT_SIGNING_KEY[_WRAPPED]`) to defend against a privileged local attacker re-signing history. |
| 4.5 | Can audit data be exported to a SIEM? | ✅ | `maverick audit export` (JSONL / ArcSight CEF) and `audit forward` to a SIEM collector (Splunk / Sentinel / QRadar). |

## 5. Application & Agent Security (AIS)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 5.1 | Is there defense against prompt injection / jailbreaks? | ◑ | Agent Shield screens input, tool calls, and output at three sinks, with a decode pre-pass that re-scans de-obfuscated variants. Detector strength depends on the configured backend (built-in rules vs. full shield). |
| 5.2 | Are runaway / cost-exhaustion loops bounded? | ✅ | Hard budget caps on dollars, wall-clock, input/output tokens, and tool calls — enforced atomically at record time. (Golden-path: a runaway loop is CAPPED.) |
| 5.3 | Is model-generated code sandboxed? | ◑ | Pluggable sandboxes (local subprocess, Docker, gVisor, Podman, devcontainer, Kubernetes, Firecracker, SSH, Modal); network-off by default. |
| 5.4 | Is there a default-deny posture for tools / network? | ◑ | Tool allow-lists per pack; sandbox network denied by default; egress lock in Enterprise mode. |
| 5.5 | Is provider/API rate-limiting handled safely? | ✅ | Reactive rate limiter plus a predictor; retries with backoff on provider rate-limit/connection errors. |
| 5.6 | Can you run without any third-party model provider? | ◑ | Local/self-hosted models supported (Ollama, vLLM, TGI) for fully offline operation. |

## 6. Infrastructure, Isolation & Deployment (IVS)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 6.1 | Can it be fully self-hosted with no required egress? | ✅ | Runs in your VPC, on-prem, or fully air-gapped; no telemetry, no call-home. |
| 6.2 | Is there an air-gap readiness check? | ◑ | `maverick airgap check` verifies no outbound path before you trust the box. (Config audit, not OS-level enforcement — pair with network controls.) |
| 6.3 | Is confidential computing supported? | 🟡 | SEV-SNP / TDX **detection** (`maverick confidential-compute`); detection only, not remote attestation. ⏳ attestation on roadmap. |
| 6.4 | Is enterprise hardening verifiable, not just configured? | ◑ | `maverick enterprise verify --require` **exercises** the egress lock and at-rest round-trip rather than reading flags. |
| 6.5 | What are the deploy targets? | ✅ | Docker, Kubernetes, single VPS, Firecracker microVMs; container image + native installers. |

## 7. Privacy & Data Subject Rights (DSP)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 7.1 | Are DSAR / access requests supported? | ◑ | `maverick dsar` assembles subject data; `maverick erase` performs right-to-erasure. |
| 7.2 | Are data-retention windows configurable? | ◑ | Configurable retention (e.g. audit 365d, episodes 90d, events 365d); GDPR Art. 5(1)(e) storage limitation. |
| 7.3 | Are ROPA / DPIA artifacts generated? | ◑ | `maverick ropa` (Art-30 records of processing), `maverick dpia` (Art-35), AI-Act self-classification scaffold. |
| 7.4 | Is customer data used to train shared/global models? | ✅ | **No.** Each customer's learning stays in their own boundary; no cross-customer pooling, no data network effect. Any federated sharing is opt-in, operator-run, signed, and consolidated-lessons-only. |

## 8. Vulnerability, SDLC & Supply Chain (TVM / STA)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 8.1 | Is there automated security gating in CI? | ✅ | Secret scanning (detect-secrets), lint, a red-team gate, and the roster-wide governance-invariant + connector fuzzing suites run in CI. |
| 8.2 | Are dependencies and config pinned / governed? | ✅ | Dependency floors pinned; migration-governance + contract checks gate schema/proto changes; new top-level deps require a config knob. |
| 8.3 | Is there a documented test suite? | ✅ | 9,000+ automated tests across Python 3.10–3.12. |
| 8.4 | Has a third-party penetration test been performed? | ❓ | Available to discuss with design partners; ⏳ formal third-party pentest is on the roadmap. |
| 8.5 | Is there a responsible-disclosure / security contact? | ✅ | `info@daybreakailabs.com`. |

## 9. Resilience & Change Safety (BCR)

| # | Question | Response | Detail / evidence |
|---|---|:---:|---|
| 9.1 | Are changes to the agent's learned behavior reversible? | ✅ | Every learned change is snapshotted and one command from rollback; a hindsight engine detects learning regressions before they ship. |
| 9.2 | Is there an emergency stop for in-flight work? | ✅ | Global kill switch (see 2.6). |
| 9.3 | Can you prove the system's guarantees on demand? | ✅ | `maverick proof-pack` emits an Ed25519-signed evidence bundle (governance, reliability, performance, shield) that verifies offline and refuses to fabricate. |

---

## Certifications & attestations (current state)

| Item | Status |
|---|---|
| SOC 2 Type I / II | ⏳ Not yet certified — readiness + evidence collector available; share status on request |
| ISO 27001 / 42001 | ⏳ Mapping available; not certified |
| HIPAA | ◑ Compliance-mode floor (forces redaction, at-rest encryption, egress lock, audit); not a BAA/attestation |
| GDPR / CCPA | ◑ DSAR, erasure, retention, ROPA, DPIA tooling; self-hosted data residency |
| Third-party pentest | ⏳ Roadmap / discuss with design partners |

## How to verify our claims (don't trust — check)

1. **Run the golden path:** `python -m maverick.golden_path` — drives the real
   governance/capability/audit/budget code (no model) and emits a signed,
   verifiable trail.
2. **Verify the chain offline:** `maverick audit verify --pubkey <key>`.
3. **Inspect the roster guarantees:** `maverick domains-lint` / `domains-audit`.
4. **Get the evidence bundle:** `maverick proof-pack`.

> Numbers and control states above reflect the current alpha and are subject to
> change. For your specific questionnaire (SIG, VSA, vendor-specific), send it
> over — we'll map it control-by-control. `info@daybreakailabs.com`.
