# PIA Concierge — Deployment & Installation Guide

This is the guide for **partners and client IT teams** installing the PIA
Concierge agent for an organization. It covers the **standalone agent SKU**
(sold on its own — no Lightwork platform) on every common target: Docker,
AWS, GCP, Azure, a plain Linux VM, Windows, and macOS. The last section
covers the platform-bundled install.

The standalone agent is a single Python web service. It has **no database
server, no queue, no external services** — state is a directory of JSON files
(`PIA_DATA_DIR`), and the only outbound call it makes is the HTTPS POST that
files completed assessments into the client's OneTrust tenant (or the built-in
mock while piloting).

---

## 1. What you are installing

| Component | Standalone agent | With Lightwork |
|---|---|---|
| Chat / voice / guided intake, risk scoring, OneTrust filing + in-OneTrust review, document append, vendor memory, speed metrics | ✅ | ✅ |
| Ed25519 signed audit chain, governed approval queue, privacy workspace, full framework catalog with control citations, cross-run learning, connectors, Art. 28 DPA clause review, model-assisted chat | — (upsell) | ✅ |

The running service shows its own capability matrix at **`/about`** — use it
during handover to show the client exactly what is active.

**Sizing:** 1 vCPU / 512 MB RAM is comfortable for hundreds of assessments a
year; disk grows by a few KB per assessment plus uploaded documents. It is a
single-process service — run one instance per tenant (do not load-balance two
instances over one data directory).

## 2. Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `PIA_STANDALONE` | auto | `1` forces the standalone build (also automatic when the platform isn't installed). Set it explicitly in every standalone deployment. |
| `PIA_HOST` | `127.0.0.1` | Bind address. Keep loopback for desktop installs; containers listen on `0.0.0.0` internally, but publish host ports to `127.0.0.1` unless a TLS/auth proxy or firewall is already enforcing access (see §8). |
| `WORLD_PORT` | `8890` | HTTP port. |
| `PIA_DATA_DIR` | `./.pia-standalone` | Persistent state (assessment records). Back this up. |
| `PIA_BASE_URL` | `http://127.0.0.1:8890` | The public URL the agent puts in intake emails/links. Set to the real HTTPS URL in production. |
| `ONETRUST_HOSTNAME` | built-in mock | The client's OneTrust API base (e.g. `https://customer.my.onetrust.com`). Leave default to pilot against the built-in mock tenant. |
| `ONETRUST_TOKEN` | demo token | OAuth/bearer token for OneTrust. Store in a secret manager, never in shell history or unit files readable by all. |
| `EMAIL_USER` / `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` | in-process inbox | Standalone delivers intake mail to the built-in inbox page by default; point at the client's SMTP relay to send real mail. |
| `PIA_SEED_TENANT` | `0` | `1` seeds a year of demo history (sales demos only — never for a production tenant). |
| `PIA_MANUAL_BASELINE_HOURS` | `6` | The stated manual-PIA baseline used by the speed metrics. |

**Health check:** `GET /health` returns `{"ok": true, ...}`. Wire every
platform's probe to it.

---

## 3. Docker (the recommended path everywhere)

The container is the same artifact on AWS, GCP, Azure, and on-prem. From the
`pia-concierge` product directory:

```bash
docker build -f Dockerfile.standalone -t pia-concierge:1.0 .

docker run -d --name pia --restart unless-stopped \
  -p 127.0.0.1:8890:8890 \
  -v pia-data:/data \
  -e PIA_BASE_URL=https://pia.client.example \
  -e ONETRUST_HOSTNAME=https://customer.my.onetrust.com \
  -e ONETRUST_TOKEN=$(cat /run/secrets/onetrust-token) \
  pia-concierge:1.0
```

Verify: `curl -s http://127.0.0.1:8890/health` → `{"ok": true}`, then open
`/about` for the capability matrix. Only a local reverse proxy should reach the container port; put TLS/auth in front (§8) before exposing it to a network.

To ship the image to a client without a registry:
`docker save pia-concierge:1.0 | gzip > pia-concierge-1.0.tgz` → they
`docker load < pia-concierge-1.0.tgz`.

## 4. AWS

**Option A — App Runner (simplest managed):**
1. Push the image to ECR: `aws ecr create-repository --repository-name pia-concierge`,
   then `docker tag` + `docker push` the ECR URI.
2. Create an App Runner service from the ECR image: port **8890**, CPU 0.5 vCPU /
   1 GB, env vars from §2 (put `ONETRUST_TOKEN` in Secrets Manager and reference
   it), health check path `/health`.
3. App Runner terminates TLS for you; put the client's SSO in front with an
   ALB + OIDC or Cloudflare Access if the service must not be public (§8).
4. **Persistence caveat:** App Runner's filesystem is ephemeral. For a real
   tenant, prefer Option B with an EFS volume; App Runner is fine for pilots.

**Option B — ECS on Fargate (production):**
1. Image in ECR (as above). Create an EFS filesystem + access point for `/data`.
2. Task definition: 0.5 vCPU / 1 GB, container port 8890, EFS volume mounted at
   `/data`, env from §2, secrets from Secrets Manager, health check
   `CMD-SHELL curl -f http://localhost:8890/health || exit 1`.
3. Service behind an ALB (HTTPS listener, ACM cert) with target-group health
   check `/health`; restrict the ALB with the client's SSO (OIDC action) or
   security groups + VPN.
4. Backups: EFS automatic backups on.

**Option C — plain EC2:** treat it as the Linux VM install (§7) on an
Amazon Linux 2023/Ubuntu instance behind an ALB.

## 5. GCP

**Option A — Cloud Run (simplest managed):**
1. `gcloud artifacts repositories create pia --repository-format=docker ...`,
   `docker tag` + `docker push` to Artifact Registry.
2. Deploy:
   ```bash
   gcloud run deploy pia-concierge \
     --image REGION-docker.pkg.dev/PROJECT/pia/pia-concierge:1.0 \
     --port 8890 --cpu 1 --memory 512Mi --min-instances 1 --max-instances 1 \
     --no-allow-unauthenticated \
     --set-env-vars PIA_BASE_URL=https://pia.client.example,ONETRUST_HOSTNAME=... \
     --set-secrets ONETRUST_TOKEN=onetrust-token:latest
   ```
3. `--min/max-instances 1` matters: single-writer state. **Persistence caveat:**
   Cloud Run's disk is in-memory; for a real tenant mount a Cloud Storage FUSE
   volume (`--add-volume name=data,type=cloud-storage,bucket=...` +
   `--add-volume-mount volume=data,mount-path=/data`) or use Option B.
4. Front with IAP / Identity-Aware Proxy or keep `--no-allow-unauthenticated`
   and grant run.invoker to the client's workforce identities.

**Option B — Compute Engine VM:** the Linux VM install (§7) on a `e2-small`,
behind an HTTPS load balancer with IAP.

## 6. Azure

**Option A — Container Apps (simplest managed):**
1. Push to ACR: `az acr create -n clientpia --sku Basic`, `az acr login`,
   `docker tag` + `docker push clientpia.azurecr.io/pia-concierge:1.0`.
2. Create an Azure Files share for state, then:
   ```bash
   az containerapp env create -n pia-env -g rg-pia
   az containerapp create -n pia-concierge -g rg-pia --environment pia-env \
     --image clientpia.azurecr.io/pia-concierge:1.0 \
     --target-port 8890 --ingress external --min-replicas 1 --max-replicas 1 \
     --cpu 0.5 --memory 1Gi \
     --env-vars PIA_BASE_URL=https://pia.client.example ONETRUST_HOSTNAME=... \
     --secrets onetrust-token=... --env-vars ONETRUST_TOKEN=secretref:onetrust-token
   ```
   Mount the Azure Files share at `/data` (storage mount on the environment).
3. Turn on Container Apps **built-in authentication** (Entra ID) so only the
   client's tenant can reach it; health probe `/health`.

**Option B — Azure VM:** the Linux VM install (§7) on a `B1ms`, behind
Application Gateway with Entra ID auth.

## 7. Plain Linux VM (on-prem, EC2, GCE, Azure VM)

```bash
# 1. Prereqs: Python 3.10–3.12 + venv
sudo apt-get update && sudo apt-get install -y python3 python3-venv   # Debian/Ubuntu
# (RHEL/Amazon Linux: sudo dnf install -y python3.12)

# 2. Unpack the product directory (from the delivered archive) and install
sudo mkdir -p /opt/pia-concierge /var/lib/pia-concierge
sudo tar -xzf pia-concierge-1.0.tgz -C /opt/pia-concierge --strip-components=1
cd /opt/pia-concierge
python3 -m venv .venv && .venv/bin/pip install -r requirements-standalone.txt

# 3. Dedicated user
sudo useradd --system --home /var/lib/pia-concierge pia
sudo chown -R pia:pia /var/lib/pia-concierge
```

`/etc/systemd/system/pia-concierge.service`:

```ini
[Unit]
Description=PIA Concierge standalone agent
After=network-online.target
Wants=network-online.target

[Service]
User=pia
WorkingDirectory=/opt/pia-concierge
Environment=PIA_STANDALONE=1
Environment=PIA_HOST=127.0.0.1
Environment=WORLD_PORT=8890
Environment=PIA_DATA_DIR=/var/lib/pia-concierge
Environment=PIA_BASE_URL=https://pia.client.example
Environment=ONETRUST_HOSTNAME=https://customer.my.onetrust.com
# Secrets: use a root-owned 0600 env file, not inline values.
EnvironmentFile=-/etc/pia-concierge/secrets.env
ExecStart=/opt/pia-concierge/.venv/bin/python serve_standalone.py
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/pia-concierge
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now pia-concierge
curl -s http://127.0.0.1:8890/health
```

Keep `PIA_HOST=127.0.0.1` and put nginx/Caddy on the same box in front (§8).

## 8. TLS, authentication, and exposure (read before any non-desktop install)

- The agent itself speaks plain HTTP and has **no login screen**. Intake links
  are reachable by case id **by design** (requesters click them from email).
  Therefore in any networked deployment you MUST front it with:
  1. **TLS** (ALB/ACM, Cloud Run/Container Apps managed certs, or nginx+Let's
     Encrypt on a VM), and
  2. **an authenticating proxy for the operator surfaces** — SSO (IAP, ALB
     OIDC, Entra built-in auth, Cloudflare Access) covering `/`, `/onetrust`,
     `/servicenow`, `/mail`, `/about`, while `/intake/*` and `/health` may stay
     reachable to requesters if the client wants email-link intake to work
     without SSO. Example nginx split:
     ```nginx
     location /intake/ { proxy_pass http://127.0.0.1:8890; }
     location /health  { proxy_pass http://127.0.0.1:8890; }
     location / { include /etc/nginx/sso.conf; proxy_pass http://127.0.0.1:8890; }
     ```
- Set `PIA_BASE_URL` to the public HTTPS URL so emailed intake links are right.
- `ONETRUST_TOKEN` lives in the platform's secret manager (Secrets Manager /
  Secret Manager / Key Vault) — never in images, unit files, or shell history.
- Standalone keeps **no audit trail** (a Lightwork feature) — set the client's
  expectation during handover, and point the proxy's access logs at their SIEM
  if they need request-level logging.

## 9. Desktop installs (pilot / single-reviewer)

**Windows 10/11 (PowerShell):**
```powershell
# Install Python 3.12 from python.org or: winget install Python.Python.3.12
cd C:\pia-concierge          # the unpacked product directory
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-standalone.txt
$env:PIA_STANDALONE = "1"
$env:PIA_DATA_DIR   = "$env:LOCALAPPDATA\PIAConcierge"
.venv\Scripts\python serve_standalone.py
# open http://127.0.0.1:8890
```
Auto-start: Task Scheduler → "At log on" → run
`C:\pia-concierge\.venv\Scripts\python.exe serve_standalone.py` with the env
vars set on the task. Loopback-only by default — nothing is exposed to the LAN.

**macOS:**
```bash
brew install python@3.12
cd ~/pia-concierge
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-standalone.txt
PIA_STANDALONE=1 PIA_DATA_DIR="$HOME/Library/Application Support/PIAConcierge" \
  .venv/bin/python serve_standalone.py
```
Auto-start: a `launchd` plist in `~/Library/LaunchAgents` running the same
command.

**Linux desktop:** `bash run_standalone.sh` (it sets sane defaults and a local
data dir).

## 10. Operations

- **Backup:** `PIA_DATA_DIR` is the whole state. Snapshot it (EFS/Azure Files
  backups, GCS versioning, or a nightly `tar` of `/var/lib/pia-concierge`).
  Restore = put the directory back and start the service.
- **Upgrade:** replace the image tag (or `git`/archive contents + re-run pip)
  and restart. State is forward-compatible JSON; take a backup first. There is
  no migration step in the standalone SKU.
- **Reset a pilot:** stop, delete `PIA_DATA_DIR`, start.
- **Monitoring:** probe `/health`; alert on non-200. Logs go to stdout —
  journald / CloudWatch / Cloud Logging / Log Analytics pick them up natively.
- **Go-live checklist:** `PIA_BASE_URL` is the public HTTPS URL · TLS + SSO in
  front · `ONETRUST_HOSTNAME`/`ONETRUST_TOKEN` point at the client tenant and a
  test filing appears there · SMTP relay configured (or the in-process inbox is
  the accepted workflow) · `PIA_SEED_TENANT` is `0` · `/about` reviewed with
  the client · backup schedule in place.

## 11. Installing WITH the Lightwork platform

When the client buys Lightwork, the agent runs inside the platform install:
1. Install the platform per the repo root (`bash .devcontainer/post-create.sh`
   installs the eight packages; see the top-level README/docs for production
   layout), configure `~/.maverick/config.toml` via the installer wizard
   (`apps/installer-cli`).
2. Launch the bundled demo/product pair with `bash run_demo.sh` (dashboard
   :8765 + agent :8890 in ONE process — the Ed25519 audit chain requires a
   single writing process), or embed the agent app behind the same reverse
   proxy as the dashboard.
3. Do **not** set `PIA_STANDALONE` — the capability layer auto-detects the
   platform and everything in the right-hand column of §1 lights up, including
   the signed audit chain and the `/privacy` workspace.
4. Migrating a standalone pilot into the platform: keep `PIA_DATA_DIR` as the
   historical archive; new assessments are written to the governed platform
   store. (A one-shot importer is on the roadmap; scope it in the SOW if the
   client needs pilot history inside the workspace.)

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/health` fine locally, unreachable remotely | `PIA_HOST` is loopback (correct!) — reach it through the reverse proxy, or set `PIA_HOST=0.0.0.0` only with TLS+auth in front. |
| Filing shows `error (401/403)` from OneTrust | Wrong/expired `ONETRUST_TOKEN` or wrong `ONETRUST_HOSTNAME`; test with the client's API credentials directly. |
| Intake email links point at localhost | Set `PIA_BASE_URL` to the public URL and restart. |
| Two instances corrupting state | Run exactly one instance per data directory (min/max instances = 1). |
| Port 1025 bind warning at startup | The in-process SMTP capture couldn't bind; harmless unless you rely on the built-in inbox — set `EMAIL_SMTP_PORT` to a free port. |
| Assessments vanish after container restart | `/data` wasn't a mounted volume — mount persistent storage (§3–§6). |
