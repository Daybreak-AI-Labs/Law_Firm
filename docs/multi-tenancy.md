# Multi-tenancy

Maverick can isolate many tenants (organizations / workspaces / end-users) on
shared infrastructure, or run one instance per tenant. This page is the map of
what is isolated, how to provision, and where the boundaries are.

## Turning it on

Tenancy is **opt-in and fail-open**: with nothing configured, Maverick is a
single-tenant install and behaves exactly as before.

- `MAVERICK_TENANT_BY_USER=1` (or `[tenancy] by_user = true`) — each channel
  user becomes their own tenant (`<channel>:<principal>`), with isolated world
  DB, memory, audit, and knowledge.
- `MAVERICK_TENANT=<id>` or an explicit `set_tenant(...)` scope — pin a tenant
  for a process / request.
- A bound client (`[client] id` / `MAVERICK_CLIENT_ID`) — one deployment = one
  enterprise client; that client id is the tenant floor.

## What is isolated per tenant

| Concern | Isolation | Where |
|---------|-----------|-------|
| World DB (goals, conversations, facts) | separate `world.db` per tenant | `~/.maverick/tenants/<id>/world.db` |
| Cross-session memory | per-tenant dir | `tenants/<id>/memory/` |
| Audit log | per-tenant, signed/hash-chained | `tenants/<id>/audit/` |
| Knowledge store | per-tenant via Workspace | `tenants/<id>/knowledge.db` |
| Encryption-at-rest key | distinct DEK per tenant (AEAD-bound); per-tenant BYOK + fleet KEK rotation | `tenant/kms.py`, `tenant/kms_fleet.py` |
| **Config & credentials** | per-tenant overlay | `tenants/<id>/config.toml` |
| **Calibration / learning-freeze** | per-tenant | `tenants/<id>/calibration*` |
| **Concurrency ceiling** | per-tenant, from plan | `billing.entitlements` |
| **RBAC role** | per-tenant membership overrides global | `dashboard-tenant-roles.json` |
| Spend cap | per-tenant `max_daily_dollars` (clamps the per-run budget) | tenant registry |

## Per-tenant credentials

Each tenant supplies its own provider API keys, model choices, and budget by
dropping a `config.toml` at `~/.maverick/tenants/<id>/config.toml`. It overlays
the global config (highest precedence) only while that tenant is active:

```toml
# ~/.maverick/tenants/acme/config.toml
[providers.anthropic]
api_key = "${ACME_ANTHROPIC_API_KEY}"   # ${VAR} interpolates from the env

[models]
orchestrator = "anthropic:claude-opus-4-8"

[budget]
max_dollars = 50

# Per-tenant identity provider: in the one-instance-per-tenant model each
# tenant authenticates against its own IdP. Resolved from this overlay while
# the tenant is active, exactly like provider keys.
[auth.oidc]
issuer = "https://acme.example.com"
audience = "maverick-acme"
```

The provisioning API returns this path as `config_path`. The same overlay drives a tenant's `[channels.*]` bot
identities and `[auth.oidc]` provider — so credentials, models, budget, channel
bots and IdP are all per-tenant in the one-instance-per-tenant model.

## Plan tiers are enforced

A tenant's plan (`free` / `pro` / `enterprise`, or operator-defined under
`[billing.plans]`) gates real behaviour, not just labels:

- **concurrency** — `max_concurrent_goals` caps a tenant's in-flight goals;
- **channels** — a plan without the `channels` feature is refused at the
  channel door;
- **audit_export** — `maverick audit export --tenant <id>` requires the
  `audit_export` feature.

Enforcement is fail-open at the edges: single-tenant and unprovisioned per-user
tenants are never gated — only a tenant an operator explicitly put on a limited
plan is denied.

## Provisioning

Tenants are provisioned over the admin REST API:

```text
# REST (admin only)
GET/POST           /api/v1/admin/tenants
GET/DELETE         /api/v1/admin/tenants/{id}
POST               /api/v1/admin/tenants/{id}/{suspend,resume,plan,quota}
GET/PUT/DELETE     /api/v1/admin/tenants/{id}/roles[/{principal}]
```

A suspended or over-quota tenant is refused at the channel door before any goal
runs; a tenant at its plan's concurrency ceiling is told to retry.

## Channels are a per-instance boundary

Channels (Slack, Telegram, email, …) are **global listeners** built once at
startup: one bot identity per channel type, per process. Inbound replies route
back to the originating channel — there is **no cross-tenant reply leak** — but
all tenants behind one process share that one bot identity and its allow-lists.

When tenants need **distinct bot identities** (their own Slack workspace bot,
their own inbound email address/webhook), run **one Maverick instance per
tenant**. The Helm chart (`deploy/helm`) plus the per-tenant config overlay make
this cheap: one release per tenant, each with its own `[channels.*]` and
credentials. The server logs an advisory at startup when it detects a
multi-tenant deployment using shared global channels.

For purely API/dashboard-driven tenants (no inbound chat bots), a single
multi-tenant instance is fine.

## Scaling

SQLite is single-writer: one control-plane replica per state volume. Scale by
keeping one dashboard/serve replica per deployment; the app-layer
`_tenant_scope` predicate isolates tenants.

