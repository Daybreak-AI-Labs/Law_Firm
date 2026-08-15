# Distribution & Deployment — how Lightwork ships

> How a user gets Lightwork onto a Mac, Windows PC, Linux box, or a cloud
> (Azure / AWS / GCP). The integrated platform ships as PyInstaller binaries or
> a Python container; the four reduced standalone SKUs deliberately ship as
> licensed, versioned source archives rather than pretending to be PyPI
> distributions.

## TL;DR — what the release pipeline produces

One tagged `release.yml` run emits the customer-facing artifacts below for
**macOS (Apple Silicon), Windows, and Linux**, plus a cloud image:

| Channel | Artifact | Who it's for | Source exposed? |
|---|---|---|---|
| **Single-file binary** | `maverick-macos-arm64`, `maverick-windows-x86_64.exe`, `maverick-linux-x86_64` | technical users / CI / servers | **Bundled Python bytecode** — reverse-engineerable |
| **Container image** | `ghcr.io/daybreak-ai-labs/lightwork:<ver>` | **Azure / AWS / GCP**, k8s, on-prem Docker | **Yes** — installed Python remains readable |
| **Reduced standalone SKU** | `lightwork-<sku>-<ver>.zip` | local evaluation of GRC / platform hunt / environment hunt / model-risk assurance | **Yes** — licensed source archive |
| Supply-chain | Sigstore material for `release.yml` binaries, standalone archives, manifests, SBOMs, and the immutable container digest; `publish.yml` separately signs Python distributions/manifests | security / procurement review | n/a |

`desktop.yml` separately builds an **unsigned authenticated source-bootstrap
shell** as a private engineering workflow artifact. It requires network access
and GitHub authorization to clone the pinned private commit, installs readable
source, and is neither self-contained nor attached to product releases.

The standalone SKUs are **not PyPI packages**. Their explicit release manifests
drive deterministic ZIP builds and each GitHub Release carries SHA-256 checksums,
a CycloneDX SBOM, and Sigstore signature/certificate files. `publish.yml`
separately contains an opt-in, allowlisted Trusted Publishing path for the real
workspace Python distributions; standalone source archives never enter it.

**What "compiled" means (be honest with buyers):** PyInstaller bundles Python
**bytecode**, not source. That stops casual copying and means no `pip install`
gives anyone the packages — but a determined reverse-engineer can decompile
bytecode. If stronger IP protection is required, compile with **Nuitka**
(Python → C → native) or keep crown-jewel logic server-side/license-gated. See
§Hardening.

---

## macOS (incl. Mac Mini)

**Option A — download the published single-file binary:**
1. Download `maverick-macos-arm64` and its `.sig`/`.pem` files from the release.
2. Verify the checksum and Sigstore identity with `deploy/verify-release.sh`.
3. Mark it executable and run it from Terminal.

**Option B — build it locally on the Mac Mini** (no release needed; what we run
tomorrow): `scripts/build-macos-app.sh` — installs the packages non-editable
from this source tree, PyInstaller-compiles them into a `maverick` binary, and
wraps it into a double-click `Lightwork.app` that opens the local dashboard.
Compiled, no PyPI, no source shipped. See the script header for prerequisites
(Homebrew + `python@3.12`).

**Footprint:** ~200–300 MB app, no GPU, runs on any Apple-Silicon Mac Mini. The
model is called over an API (Anthropic / Azure OpenAI / Bedrock) **or** run
locally via Ollama for a fully air-gapped box (`maverick init` → choose Ollama).

---

## Windows & Linux

The product release currently publishes the standalone binary on each OS:

- **Windows:** `maverick-windows-x86_64.exe`.
- **Linux:** `maverick-linux-x86_64`.

The Tauri `.msi` / NSIS `.exe` / `.AppImage` / `.deb` outputs remain
engineering-only authenticated source-bootstrap artifacts until a compiled
payload, reliable release attachment, platform signing, and real-machine tests
exist.

To build locally on either OS, the recipe is identical to the Mac script:
install the packages non-editable, then `pyinstaller --clean --noconfirm
--distpath ../dist build/maverick.spec` (run from `build/`). PyInstaller must
run **on the target OS** — a Windows exe builds on Windows, a Linux binary on
Linux. CI's `release.yml` does all three on hosted runners.

---

## Cloud: Azure / AWS / GCP (the "drop it in" path)

The container image is the universal cloud artifact. Pull it and run — it needs
only a mounted config dir and a model key (or a private model endpoint your
cloud already offers, e.g. Azure OpenAI / Bedrock).

```bash
# the image (built + pushed by release.yml)
docker pull ghcr.io/daybreak-ai-labs/lightwork:latest
```

**AWS** — ECS/Fargate task, or plain EC2:
```bash
docker run -d -p 8765:8765 \
  -e MAVERICK_DASHBOARD_TOKEN=... \
  -e ANTHROPIC_API_KEY=...            # or point at Bedrock via the provider config
  -v /srv/maverick:/home/maverick/.maverick \
  ghcr.io/daybreak-ai-labs/lightwork:latest \
  dashboard --host 0.0.0.0 --port 8765
# Fargate: same image in a task definition; 0.5 vCPU / 1–2 GB is plenty.
```

**Azure** — Container Apps (serverless) or AKS:
```bash
az containerapp create -n lightwork -g <rg> --environment <env> \
  --image ghcr.io/daybreak-ai-labs/lightwork:latest \
  --target-port 8765 --ingress internal \
  --command maverick \
  --args dashboard --host 0.0.0.0 --port 8765 \
  --secrets model-key=... dashboard-token=... \
  --env-vars ANTHROPIC_API_KEY=secretref:model-key \
             MAVERICK_DASHBOARD_TOKEN=secretref:dashboard-token \
  --cpu 0.5 --memory 1Gi
# Point at Azure OpenAI (already in-tenant) instead of Anthropic — zero new
# model-vendor review for a bank that's already approved Azure OpenAI.
```

**GCP** — Cloud Run:
```bash
gcloud run deploy lightwork \
  --image ghcr.io/daybreak-ai-labs/lightwork:latest \
  --port 8765 --cpu 1 --memory 2Gi --no-allow-unauthenticated \
  --command=maverick \
  --args=dashboard,--host,0.0.0.0,--port,8765 \
  --set-secrets=ANTHROPIC_API_KEY=lightwork-model-key:latest,MAVERICK_DASHBOARD_TOKEN=lightwork-dashboard-token:latest
# Use Vertex/Gemini provider config instead of ANTHROPIC_API_KEY if preferred.
```

All three examples explicitly start the dashboard rather than the image's
one-shot default help command. A non-loopback bind also requires
`MAVERICK_DASHBOARD_TOKEN`; cloud IAM or private ingress is defense in depth,
not a replacement for the application's bearer-token boundary.

**k8s / on-prem:** the same image in a single-replica control-plane Deployment;
mount a PVC at `/home/maverick/.maverick` for the world DB + audit chain.
SQLite is the default; Postgres adds database HA/isolation and supports separate
remote-worker scale, but not multiple dashboard/serve replicas yet.

Governance + data stay in the customer's tenant; only the model call goes out
(and even that can be an in-tenant endpoint). That's the whole self-hosted
pitch, and the container is what makes it a 5-minute deploy on any cloud.

---

## Cut a release (populate the download page)

Downloads only exist once a release is cut. The pipeline is already wired; it
just needs to run on a tag:

```bash
# Manual dispatch must itself run from the exact tag. Checking out the tag later
# cannot change the GitHub OIDC identity used for keyless signing.
gh workflow run release.yml --ref v0.1.7 -f tag=v0.1.7
```

`release.yml` then: builds the 3-OS binaries → stages the GHCR image by immutable
commit → builds the four versioned standalone source archives → generates
checksums and SBOMs → keyless-signs the immutable container digest and release
artifacts → creates a draft GitHub Release → promotes and verifies the intended
GHCR tags → publishes that verified draft last. It does not dispatch or attach
the engineering Tauri source-bootstrap bundles.

The Release includes `maverick-container-v<version>.json`, which binds the
image, immutable digest, source revision, staging tag, and target-tag promotion
plan. The signed plan is created before mutable tags move; the workflow verifies
that every target tag resolves to the signed digest before it publishes the
draft Release.
Verify that digest without trusting a mutable tag:

```bash
IMAGE=ghcr.io/daybreak-ai-labs/lightwork
DIGEST=sha256:<digest-from-maverick-container-v0.1.7.json>
cosign verify "$IMAGE@$DIGEST" \
  --certificate-identity-regexp \
  '^https://github.com/Daybreak-AI-Labs/Lightwork/\.github/workflows/release\.yml@refs/tags/v0\.1\.7$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

**Before cutting a real (public) release:** merge the intended code to `main`,
decide signing (below), and bump the version. A release is public and
outward-facing — cut it deliberately.

---

## Signing / notarization (removes the scary prompts)

Release artifacts carry Sigstore identity/integrity material, but current
executables are **not platform code-signed or notarized**. Operating systems may
still show an unknown-developer warning. For design partners:

- **macOS:** Apple Developer Program membership ($99/yr) → a Developer ID cert →
  notarization. Wire the cert + notary creds as CI secrets; `tauri build` and the
  binary both get signed + stapled.
- **Windows:** an Authenticode code-signing cert (OV ~$200–400/yr, or EV) → sign
  the `.msi`/`.exe`.
- Already in place: **Sigstore** keyless signatures (`.sig`/`.pem`) on the raw
  release binaries, standalone archives, checksums, and SBOMs — verifiable with
  `cosign verify-blob` (`deploy/verify-release.sh`). The container digest is
  signed with `cosign sign`; the engineering Tauri bundles are not covered.

## Hardening (stronger IP protection, if needed)

- **Nuitka** compile (`nuitka --standalone --onefile`) → native machine code,
  far harder to decompile than PyInstaller bytecode.
- Keep the highest-value logic (e.g. proprietary pack generation / evaluator)
  **server-side** behind a licensed API, so the shipped binary orchestrates but
  doesn't contain it.
- License-gate the binary (offline license key checked at boot) to control who
  can run it, independent of who can copy it.
