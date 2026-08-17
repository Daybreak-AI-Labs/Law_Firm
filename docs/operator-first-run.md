# Operator first run

This is the shortest path from an installed Maverick build to a working
runtime and an evidence-visible operator cockpit. It separates deterministic
configuration checks from live connectivity checks so a deployment script can
fail for the right reason.

## 1. Initialize once, retry safely

Interactive:

```bash
maverick init
```

Recommended defaults, without prompts:

```bash
maverick init --fast
```

Reviewed headless configuration:

```bash
maverick init --from-file ./config.toml
```

All three paths are safe to retry. When generated config and secret files are
byte-for-byte equivalent (ignoring platform line endings), the installer
verifies their private custody and leaves their inode, modification time, and
watcher state unchanged. A changed file is still backed up before replacement.

`init`, `doctor`, `version`, and `config-lint` do not resolve or
open the world database. They therefore remain usable when a malformed client
binding or config file is the problem you need to repair.

## 2. Verify live dependencies

```bash
maverick doctor
```

`doctor` performs the live and environment-sensitive checks. Fix red rows and
re-run it. Yellow rows need operator judgment but do not necessarily block a
normal run.

After starting the dashboard, deployment probes have distinct meanings:

- `GET /livez` — the process accepts traffic.
- `GET /healthz` — database, provider configuration, and runner health.
- `GET /readyz` — health plus deeper client-binding, Shield, agent-trust, and
  replica-safety posture.

Do not use liveness as a readiness gate.

## 3. Reach first value

```bash
maverick dashboard
```

Open `http://127.0.0.1:8765/start`. The checklist reflects actual workspace
state:

1. a provider credential or self-hosted endpoint is configured;
2. a reusable workflow or governed agent exists;
3. a goal has run or an automation is armed.

The page exposes separate **runtime** and **assurance deployment** preflight
results. Completing the three workspace steps is reported as runtime-ready
only when the normal `run` profile is ready. Runtime readiness never implies
that the stricter cockpit/assurance profile is ready, and an assurance blocker
does not contradict an otherwise runnable workspace.
Run a first task from the UI — for example, *"Summarize the deployment
controls and list unresolved gaps."*

## 4. Make AI evidence visible

Open `http://127.0.0.1:8765/security/assurance`.

The cockpit prioritizes one next action and tracks four production-evidence
milestones:

1. production policy bound to model and context digests;
2. first governed delivery receipt recorded;
3. receipt signatures and both receipt chains verified;
4. regulatory impact queue current.

Integrity failures outrank queue work. Synthetic demo records never count
toward those four production milestones.

To inspect the API contract, use `/docs`. The normal production sequence is:

```text
PUT  /api/v1/security/assurance/gateway/policies/{policy_id}
POST /api/v1/security/assurance/gateway/deliver
GET  /api/v1/security/assurance/gateway/summary
GET  /api/v1/security/assurance/gateway/regulatory-impacts?pending_first=true
GET  /api/v1/security/assurance/gateway/regulatory-impacts/{impact_id}
POST /api/v1/security/assurance/gateway/assurance-packet  (Idempotency-Key required)
```

The cockpit stores and displays hash-only interaction receipts; it does not
render raw prompts or generated content.

Impact list responses are cursor-paginated and action-needed first, so pending
reviews remain reachable beyond the first 500 records. Retrying packet issuance
with the same tenant, actor, profile, and `Idempotency-Key` returns the exact
original bytes and does not append a second attestation.

The cockpit and packet are evidence surfaces, not legal certification. A
qualified human remains responsible for regulatory classification, impact
disposition, and reliance on exported evidence.

### Synthetic demo safety

The no-network demo is available only when the tenant is empty or already
contains the same deterministic demo scenario. Use an empty or dedicated demo
tenant. Every demo record is labeled synthetic, the cockpit reports “Synthetic
demo only,” and no production assurance conclusion is shown.
