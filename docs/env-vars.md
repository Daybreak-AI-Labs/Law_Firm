# Operator environment variables

The reviewed firm deployment keeps policy in `config.toml` and injects secrets
through the process environment or the file-backed secret provider. This page
lists variables required to establish the retained trust boundary; it is not a
catalog of test seams or internal tuning flags.

Never commit `.env`, custody keys, provider keys, or OIDC secrets. On a shared
host, prefer `MAVERICK_SECRETS_BACKEND=file` with a root-owned
`MAVERICK_SECRETS_DIR` rather than process-wide environment variables.

## Paths and startup

| Variable | Purpose |
| --- | --- |
| `MAVERICK_HOME` | Private data root. Defaults to `~/.maverick`; keep it on an encrypted, access-controlled volume. |
| `MAVERICK_CONFIG` | Explicit `config.toml` path. |
| `MAVERICK_CONFIG_STRICT=1` | Abort startup when configuration lint finds an error. |
| `MAVERICK_REQUIRE_ENTERPRISE=1` | Make regulated-deployment verification a blocking startup preflight. |
| `MAVERICK_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `MAVERICK_LOG_FORMAT` | `text` or structured `json`. |

## Identity

Named OIDC identities are the execution boundary. A static dashboard bearer can
protect a local operator endpoint, but it is deliberately rejected as a client-
matter execution principal.

| Variable | Purpose |
| --- | --- |
| `MAVERICK_OIDC_ENABLED=1` | Enable OIDC verification and browser login. |
| `MAVERICK_OIDC_ISSUER` | Exact trusted issuer. |
| `MAVERICK_OIDC_AUDIENCE` | Required token audience. |
| `MAVERICK_OIDC_JWKS_URI` | HTTPS signing-key endpoint. |
| `MAVERICK_OIDC_ALGORITHMS` | Explicit accepted asymmetric algorithms. |
| `MAVERICK_OIDC_CLIENT_ID` | Browser-login client identifier. |
| `MAVERICK_OIDC_CLIENT_SECRET` | Browser-login client secret. Inject through the secret provider. |
| `MAVERICK_OIDC_REDIRECT_URI` | Exact registered callback URI. |
| `MAVERICK_OIDC_SESSION_SECRET` | High-entropy dashboard session-cookie key. |
| `MAVERICK_OIDC_AUTHORIZATION_ENDPOINT` | Explicit authorization endpoint when discovery is not used. |
| `MAVERICK_OIDC_TOKEN_ENDPOINT` | Explicit token endpoint when discovery is not used. |
| `MAVERICK_DASHBOARD_TOKEN` | Optional local operator bearer only; never a named matter principal. |
| `MAVERICK_DASHBOARD_SESSION_SECRET` | Dashboard session signing secret where configured separately. |

## Key custody

Application data, audit, queue, and disaster-recovery archives have separate
keys. Do not reuse them or store them inside `MAVERICK_HOME` or a backup archive.

| Variable | Purpose |
| --- | --- |
| `MAVERICK_ENCRYPTION_KEY` | 32-byte AES-256-GCM application data key, hex or base64. |
| `MAVERICK_ENCRYPTION_KEY_DIGEST` | `sha256:<digest>` pin for the injected data key. |
| `MAVERICK_AUDIT_SIGNING_KEY` | Raw Ed25519 audit signing key, hex or base64. |
| `MAVERICK_AUDIT_SIGNING_KEY_WRAPPED` | KMS-wrapped audit key when that path is used. |
| `MAVERICK_AUDIT_REQUIRE_OFFHOST_KEY=1` | Refuse co-located audit signing-key custody. |
| `MAVERICK_BACKUP_ENCRYPTION_KEY` | Independent 32-byte AES-256-GCM backup archive key. |
| `MAVERICK_BACKUP_SIGNING_KEY` | Independent backup manifest signing key. |
| `MAVERICK_QUEUE_SIGNING_KEY` | At least 32 bytes for signed network queue envelopes. |
| `MAVERICK_SECRETS_BACKEND` | `env` or `file`. |
| `MAVERICK_SECRETS_DIR` | Directory containing one secret per named file for the `file` backend. |

## Matter-safe runtime and egress

| Variable | Purpose |
| --- | --- |
| `MAVERICK_ENTERPRISE=1` | Enforce the deployment egress floor and governed-action defaults. |
| `MAVERICK_SECURE_DEFAULT=1` | Keep at-rest encryption, signed audit, strict Shield behavior, and consent defaults enabled. Firm deployments must not set this to `0`. |
| `MAVERICK_REQUIRE_SHIELD=1` | Require an active Shield at preflight. |
| `MAVERICK_CONSENT_MODE` | Use `ask`, `dashboard`, or `auto-deny`; do not use `auto-approve` for client work. |
| `MAVERICK_MAX_CONCURRENT_GOALS` | Global in-process goal limit. |
| `MAVERICK_MAX_CONCURRENT_GOALS_PER_PRINCIPAL` | Per-principal concurrency limit. |
| `MAVERICK_DEFAULT_MAX_DOLLARS` | Default per-goal cost ceiling. |
| `MAVERICK_DEFAULT_MAX_WALL_SECONDS` | Default per-goal wall-clock ceiling. |
| `MAVERICK_MAX_STEPS` | Maximum agent steps. |

Matter identity, client identity, jurisdiction, domain, purpose, membership, and
per-matter `egress_mode` come from durable records, not environment variables.

## Local knowledge

| Variable | Purpose |
| --- | --- |
| `MAVERICK_EMBED_PROVIDER` | `local`; `deterministic` is for tests and smoke runs only. |
| `MAVERICK_EMBED_MODEL` | Absolute path to the provisioned on-box embedding model. |
| `MAVERICK_EMBED_MODEL_DIGEST` | `sha256:<digest>` pin for the admitted model tree. |
| `MAVERICK_ISOLATE_PARSERS=1` | Require process-isolated untrusted PDF/DOCX parsing. |
| `MAVERICK_KNOWLEDGE_MAX_DOC_BYTES` | Ingest byte ceiling. |
| `MAVERICK_KNOWLEDGE_MAX_PDF_PAGES` | PDF page ceiling. |
| `MAVERICK_KNOWLEDGE_MAX_DOCX_UNCOMPRESSED_BYTES` | DOCX expanded-content ceiling. |

## Signed queue

| Variable | Purpose |
| --- | --- |
| `MAVERICK_QUEUE_REDIS_DSN` | Redis queue DSN. Non-loopback endpoints require verified TLS. |
| `MAVERICK_QUEUE_REDIS_CA_CERTS` | Trusted CA bundle for queue TLS. |
| `MAVERICK_QUEUE_REDIS_CERTFILE` / `MAVERICK_QUEUE_REDIS_KEYFILE` | Optional client certificate pair. |
| `MAVERICK_QUEUE_NAMESPACE` | Deployment-specific queue namespace. |
| `MAVERICK_QUEUE_ENVELOPE_TTL_SECONDS` | Maximum signed-envelope age. |

## Observability

| Variable | Purpose |
| --- | --- |
| `MAVERICK_PROMETHEUS_PORT` | Enable the metrics listener. |
| `MAVERICK_PROMETHEUS_ADDR` | Bind address; defaults to loopback. |
| `MAVERICK_OTEL_EXPORTER` | Enable OTLP export. |
| `MAVERICK_OTEL_ENDPOINT` | Collector endpoint, subject to deployment and matter egress policy. |
| `MAVERICK_OTEL_HEADERS` | Collector headers; treat as secret-bearing. |
| `MAVERICK_SENTRY_DSN` | Optional Sentry endpoint, subject to the same egress approval. |

Backups are available only through the local operator CLI and are encrypted and
authenticated before restore staging. There is no normal plaintext restore path.
