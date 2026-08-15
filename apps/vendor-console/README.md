# Daybreak Vendor Console

**Internal control plane** for running Lightwork as a business — customers,
license lifecycle, a serve API connected deployments poll, fleet health, and a
tamper-evident vendor audit log.

> This is **not** the product dashboard a customer runs (`maverick dashboard`).
> It is Daybreak-internal and must never ship to a client. It holds the
> license-signing **root key** — run it behind auth on a trusted network, and
> keep the signing key in a KMS/HSM in production.

## Run it

```bash
cd apps/vendor-console
pip install -e .                      # or: pip install -e . -e ../../packages/maverick-core
export VENDOR_CONSOLE_SIGNING_KEY=<publisher private key hex>   # prod: from KMS
export VENDOR_CONSOLE_SECRET=<random 32+ char session secret>
daybreak-console                      # → http://127.0.0.1:8900
```

First visit walks you through creating the **owner** account and enrolling TOTP
(required). After that: add customers, issue/upsell/revoke signed licenses, and
watch fleet health.

Generate the publisher keypair once (the public half is what customers embed as
their trust anchor — `maverick.entitlements._EMBEDDED_PUBKEYS`):

```bash
python -m maverick.entitlements keygen   # private → KMS/VENDOR_CONSOLE_SIGNING_KEY
```

## Connecting a customer deployment (the serve API)

When you create a customer you get a **serve token** (shown once). A *connected*
deployment uses it to pull its current signed license:

- `[license] api_url` → `https://<console>/api/v1/license`
- `MAVERICK_LICENSE_API_TOKEN` → the serve token

`maverick.entitlements.refresh_from_server()` then fetches + verifies + persists
the license, and each poll records a fleet check-in. **Air-gapped** customers
skip this entirely — download the signed license file and hand-deliver it.

## Configuration

| Env var | Purpose |
|---|---|
| `VENDOR_CONSOLE_SIGNING_KEY` | publisher Ed25519 **private** key hex (KMS in prod). Dev: auto-generated to `~/.daybreak/publisher.key` with a warning. |
| `VENDOR_CONSOLE_SECRET` | HMAC key for session cookies. Dev: random per-process (logs everyone out on restart). |
| `VENDOR_CONSOLE_DB` | SQLite path (default `~/.daybreak/vendor-console.db`). |
| `VENDOR_CONSOLE_HOST` / `PORT` | bind address (default `127.0.0.1:8900`). |
| `VENDOR_CONSOLE_INSECURE` | dev/loopback only — disables the `Secure` cookie flag (which is **on by default** for every non-loopback host). |
| `VENDOR_CONSOLE_TRUST_PROXY` | honour `X-Forwarded-For` for client IPs (only behind a trusted reverse proxy). |

## Security posture

Hardened after an adversarial review (auth/session, web, correctness):
single-use TOTP, server-side session revocation on logout, rate-limited +
timing-equalized login, `Secure` cookies + RBAC (DB-derived, never the cookie
claim), an `Origin` CSRF check, header-only serve tokens, atomic first-owner
bootstrap, and a lock-serialized, tamper-evident audit chain. The signing
private key is never stored in the DB or returned by any route.

## Roles

`owner` / `admin` can mint + revoke licenses and manage customers; `support` /
`viewer` are read-only (support gains the ticket desk in a later drop).

## Releases & support (serve/intake APIs)

A connected deployment authenticates every machine call with its **serve token**
(Bearer header):

- `GET /api/v1/license` → current signed license (`refresh_from_server`)
- `GET /api/v1/release?channel=…&version=…` → the latest **signed release
  manifest** for the customer's channel (the deployment runs
  `release_update.plan_upgrade` to decide). Publish releases under **Releases**
  (signed with the same publisher key as licenses; artifacts by `name sha256
  size`); assign a customer to `stable`/`edge` on their page.
- `POST /api/v1/support` with a redacted support bundle → files a **ticket**
  (deduped by `correlation_id`, priority raised when readiness checks fail).
  Work them under **Support desk** (status workflow, assignment, internal notes).

## Status

**Built:** license lifecycle (customers → issue/upsell/revoke → serve) with a
**per-feature checkbox matrix** on the customer page (à-la-carte grants +
explicit denials, driven by the kernel's gated-feature registry; a connected
deployment picks changes up on its ~60 s auto-refresh poll), **release
publishing** (signed manifests + channels + serve API), **support desk** (bundle
intake → triaged tickets), fleet check-in board, local + TOTP auth with RBAC, and
a hash-chained vendor audit trail over every action. Hosting it on your own
domain: [`deploy/cloudflare/`](../../deploy/cloudflare/README.md) (Tunnel +
Access). **Next:** billing/usage (invoices via `billing.py`), SSO, and the
concrete per-deployment update hooks.
