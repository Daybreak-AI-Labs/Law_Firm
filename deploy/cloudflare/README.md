# Vendor console behind Cloudflare (Tunnel + Access)

Put the **Daybreak vendor console** (`apps/vendor-console`) on your own domain —
`console.daybreak.example` — so staff sign in from anywhere and flip customer
feature checkboxes from a browser, without exposing the box that holds the
license-signing root key to the internet.

**The shape:** the console keeps running privately (a small VPS, an office
box — SQLite + the signing key never leave it). `cloudflared` dials **out** to
Cloudflare and publishes it; **Cloudflare Access** puts your staff SSO in front
of the staff UI. No inbound ports, no public IP, no rewrite of the console as a
Worker (deliberate: the signing key belongs in one place you control, not at
the edge — see "Why not a Worker" below).

```
staff browser ──► Cloudflare Access (SSO) ──► tunnel ──► daybreak-console :8900
customer deployments ──► /api/v1/* (service token or licence bearer) ──┘
```

## 1. Run the console

```bash
cd apps/vendor-console && pip install -e .
export VENDOR_CONSOLE_SIGNING_KEY=<publisher private key hex>   # prod: from KMS
export VENDOR_CONSOLE_SECRET=<random 32+ char session secret>
export VENDOR_CONSOLE_TRUST_PROXY=1    # cloudflared is a trusted reverse proxy
daybreak-console                       # 127.0.0.1:8900
```

## 2. Publish it with a tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create daybreak-console
cloudflared tunnel route dns daybreak-console console.daybreak.example
cloudflared tunnel --config deploy/cloudflare/cloudflared.yml run
```

`cloudflared.yml` in this directory maps the hostname to `localhost:8900`.
Run it under systemd (`cloudflared service install`) next to the console.

## 3. Gate the staff UI with Cloudflare Access

In Zero Trust → Access → Applications, add a self-hosted app for
`console.daybreak.example` with **two policies**:

1. **Bypass** for the machine paths customer deployments poll — add a path
   rule for `/api/v1/*` and `/healthz` with a *Bypass* (or *Service Auth*)
   action. Deployments authenticate with their **serve token** (Bearer); they
   can't complete an SSO dance.
2. **Allow** everything else only for your staff identity provider (Google
   Workspace / Entra / Okta) — e.g. emails ending in `@daybreak.example`,
   ideally + a group. This sits **in front of** the console's own
   login + TOTP + RBAC, it does not replace it (defense in depth: Access keeps
   the login page itself off the open internet).

Point customer deployments at the public serve API:

```toml
[license]
enforce = true
api_url = "https://console.daybreak.example/api/v1/license"
api_token = "${MAVERICK_LICENSE_API_TOKEN}"   # the customer's serve token
refresh_interval_seconds = 60                 # near-instant feature propagation
```

## Why not a Worker?

The console holds the Ed25519 **license-signing root key** — the root of trust
for every customer entitlement and release manifest. Moving it into a Worker
means the key material lives in Cloudflare secret storage and signs at the
edge; a tunnel keeps signing on hardware you own (or a KMS you control) and
Cloudflare only ever proxies HTTPS. The console is also stateful
(SQLite + tamper-evident audit chain), which D1 could host but would gain
nothing except a rewrite. If the serve API ever needs global low-latency
scale, the right split is a read-only Worker cache of *already-signed* license
docs — signatures still minted off-edge; the design leaves that open.
