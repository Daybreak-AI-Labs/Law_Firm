# Configuration

The installer writes the private firm configuration to
`~/.maverick/config.toml`. Set `MAVERICK_CONFIG` to use a different file. Keep
credentials and encryption keys in the operator's secret manager rather than
the TOML file; string values may reference an environment variable as
`${VARIABLE_NAME}`.

Run these checks after every change:

```bash
maverick config-lint
maverick doctor
```

Set `MAVERICK_CONFIG_STRICT=1` for a deployment that must refuse startup when
config lint reports an error.

## Firm confidentiality boundary

Every new matter starts in `local_only`. A responsible attorney may change one
matter to `approved_services`, but that decision is only one half of the
authorization. The operator must also name each permitted provider and HTTPS
host exactly:

```toml
[firm]
approved_providers = []
approved_hosts = []
approved_local_hosts = []
```

- `approved_providers` contains canonical model-provider names that the firm
  has contracted and reviewed.
- `approved_hosts` contains exact public hostnames. Wildcards and suffix
  matching are not used.
- `approved_local_hosts` contains exact, reviewed non-loopback on-premises
  hosts. A private or link-local IP is not trusted merely because of its
  address class.

Loopback model endpoints do not need a host-list entry, but they still require
live matter membership. Public HTTP is denied; allowed non-loopback traffic
uses HTTPS. Empty lists are the safe default.

## Model selection

Secure firm runs require one exact `provider:model-id` pin. Every role in the
run uses that model; the runtime does not silently fail over, substitute a
cheaper model, or route client material to another provider:

```toml
[models]
default = "ollama:firm-model"

[providers.ollama]
base_url = "http://127.0.0.1:11434"
```

Prefer a reviewed loopback runtime for client data. A hosted provider also
requires the exact `[firm]` entry above and a matter whose responsible attorney
has selected `approved_services`. Do not put API keys directly in this file;
use the environment or configured secret provider described in
[environment variables](env-vars.md). `maverick init --fast` is non-interactive,
so it requires an explicit `MAVERICK_MODEL_OVERRIDE=provider:model-id`; it fails
without writing configuration when that pin is missing or malformed.

## Matter knowledge

Knowledge retrieval is local-only. A local embedding model must be an absolute,
already-provisioned directory with a pinned SHA-256 digest. A repository name
is not accepted as a runtime download instruction.

```toml
[knowledge]
enable = true
embedder = "local"
store = "sqlite"
model = "/srv/maverick/models/legal-embedder"
model_digest = "sha256:<full-model-directory-digest>"
dim = 384
path = "/srv/maverick/knowledge"
```

For deterministic tests without a model, use `embedder = "deterministic"` and
the matching dimension. Do not use that test embedder as evidence of retrieval
quality. Matter-required knowledge fails closed when its exact collection or
parser dependency is unavailable.

## Safety, budgets, and sandbox

The following is a conservative starting point, not a substitute for a
matter-specific legal review:

```toml
[budget]
max_dollars = 5.0
max_wall_seconds = 600
max_tool_calls = 30

[safety]
profile = "strict"
scan_input = true
scan_tool_calls = true
scan_output = true

[sandbox]
backend = "docker"
workdir = "~/maverick-workspace"
timeout = 60
require_container = true
allow_network = false
allow_root = false
```

Budget limits are per run and tool/model work cannot bypass them. Shield scans
are defense in depth; they do not replace authorization, qualified-attorney
review, or the egress boundary. If a retained workflow needs a sandbox, use a
reviewed container backend with no network and no root rather than host
execution.

## Named-user authentication

The dashboard supports its retained named invite/session flow and OIDC. A
complete OIDC browser-login example is:

```toml
[auth.oidc]
enabled = true
issuer = "https://identity.example.com"
audience = "law-firm-dashboard"
algorithms = ["RS256", "ES256"]
client_id = "law-firm-dashboard"
redirect_uri = "https://law.example.com/auth/callback"
```

Supply `MAVERICK_OIDC_CLIENT_SECRET` and a high-entropy
`MAVERICK_OIDC_SESSION_SECRET` through operator custody. OIDC accepts only the
retained asymmetric algorithms. A shared static dashboard bearer is not a
named person and is rejected as a matter-execution principal even if a row is
manually added for it.

## Queue and worker

The local queue is appropriate for a single reviewed host. For a separate
worker, the retained ARQ/Redis transport requires a signed, deployment-scoped
queue and verified transport:

```toml
[queue]
backend = "arq"
namespace = "bjerken-day-prod"
redis_dsn = "rediss://redis.internal.example:6379/0"
```

Supply `MAVERICK_QUEUE_SIGNING_KEY` as at least 32 bytes from a secret manager.
For a private CA, set `MAVERICK_QUEUE_REDIS_CA_CERTS`; client certificates and
keys must be configured as a pair. Non-loopback Redis without verified TLS is
refused. Queue envelopes carry a signed goal, matter, and principal—not the
plaintext matter number—and the worker re-resolves current durable authority
before execution.

## At-rest, audit, and backup keys

Provision separate keys for separate purposes:

- `MAVERICK_ENCRYPTION_KEY` seals sensitive durable fields and files.
- `MAVERICK_AUDIT_SIGNING_KEY` signs the append-only audit chain when
  `[audit] sign = true`.
- `MAVERICK_QUEUE_SIGNING_KEY` authenticates worker envelopes.
- `MAVERICK_BACKUP_SIGNING_KEY` authenticates backup manifests.
- `MAVERICK_BACKUP_ENCRYPTION_KEY` encrypts backup archives with AES-256-GCM.

The two backup keys must be independent and must never be stored below the data
root or inside an archive. See [encryption](encryption.md) and
[environment variables](env-vars.md) for exact encodings and custody rules.

## Governed local improvement

The retained learning loop is local and matter-scoped:

```toml
[self_learning]
enable = true
allow_provider_egress = false
distill_local = true

[self_harness]
enable = true
risk_limited = true

[self_improvement]
enable = true
capture = true
max_auto_rung = "policy"
require_signed_approval = true
approver_keys = ["<hex-encoded-ed25519-public-key>"]
```

Keep learning-only provider egress disabled for client data. Learned records
must carry their exact matter key; absence never falls back to a global
client-derived store. Runtime agents cannot acquire tools or promote code.
Promotion requires held-out evidence, the retained harness controls, and an
explicit operator decision recorded in audit.

## Files and precedence

`MAVERICK_CONFIG` selects the base TOML. Secret-provider and environment values
override their corresponding non-secret settings. The dashboard may maintain
its private reviewed settings overlay next to the base config; it does not
authorize a matter's egress mode or replace durable matter membership.

Do not reuse a data directory for two writable control planes. SQLite and the
hash-chained ledgers assume the retained single-writer lock. For installation
and process topology, see [deployment](deployment.md).
