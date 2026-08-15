# DSAR Concierge — a sellable, standalone data-subject-request agent

The whole DSAR lifecycle on one small surface, sold on its own or bundled
with the Lightwork platform (same product, same records — the capability
seam decides what's on; see `/about` in the running app):

* **Intake** — a subject-facing form (`/request`) and a message webhook
  (`POST /webhook/message`) with a deterministic detector (kind + subject +
  matched phrases kept as provenance; nothing is silently opened).
* **Identity verification** — an emailed single-use token link. The
  statutory clock starts at submission and never waits for the inbox.
* **The clock** — `DSAR_SLA_DAYS` (default 30), SLA aging bands on the
  queue, overdue front and center.
* **Fulfillment** — access/portability packages assembled ONLY from the
  system extracts the operator provides (`package.json` download + cover
  note emailed to the subject). **Erasure is never destructive from
  here**: the agent prepares a structured per-system operator handoff and
  parks the request awaiting confirmation.
* **Counted value** — `/value.json` sums per-request value (env-overridable
  baselines) for the partner fleet console to roll up.
* **Licensing** — `LIGHTWORK_LICENSE` (+ `LIGHTWORK_LICENSE_PUBKEY`)
  verified via Ed25519. Unlicensed = evaluation mode: fully functional
  with an open-request cap. An upsell is a key swap, not a reinstall.

**With Lightwork** the same agent additionally mirrors every request into
the governed privacy workspace registers, lands every step on the signed
audit chain, and can use the platform's real subject-data export machinery.

## Run it

```bash
DSAR_OPERATOR_TOKEN=$(openssl rand -hex 24) bash run_standalone.sh
                                # http://127.0.0.1:8891  (operator queue;
                                # sign in as operator with DSAR_OPERATOR_TOKEN)
                                # /request  (subject-facing form)
                                # /about    (capability + license matrix)
```

Container + cloud installs: `Dockerfile.standalone` here, and the per-cloud
guides in `../pia-concierge/DEPLOYMENT.md` apply to this SKU with the env
names below.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `DSAR_STANDALONE` | unset | `1` forces standalone (also implied when the platform isn't installed) |
| `DSAR_HOST` / `DSAR_PORT` | `127.0.0.1` / `8891` | bind (set `DSAR_HOST=0.0.0.0` in containers) |
| `DSAR_DATA_DIR` | `.dsar-data` | the request store (mount `/data` in containers) |
| `DSAR_BASE_URL` | `http://127.0.0.1:8891` | public URL used in verification links |
| `DSAR_OPERATOR_USER` / `DSAR_OPERATOR_TOKEN` | `operator` / unset | HTTP Basic credentials required for the operator queue, case pages, package downloads, and workflow actions |
| `DSAR_SLA_DAYS` | `30` | the statutory clock |
| `EMAIL_USER` / `EMAIL_SMTP_PORT` | — | outbound mail identity (standalone captures locally) |
| `LIGHTWORK_LICENSE` / `LIGHTWORK_LICENSE_PUBKEY` | unset | signed license token + vendor public key |
| `VALUE_HOURLY_RATE`, `VALUE_DSAR_*_HOURS` | `$60`, 2.5/2.5/3.0h | the client's own value inputs |
