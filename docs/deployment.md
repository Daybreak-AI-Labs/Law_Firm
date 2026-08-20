# Deployment

Three supported run modes, all installed from one reviewed repository commit.

## Local reviewed checkout

For most users.

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm
cd Law_Firm
git checkout --detach <reviewed-full-40-character-commit-sha>
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python scripts/install_release_cohort.py --source-root . \
  --target-python python --core-extra release-runtime
maverick init
```

Runs as your user. Stores everything under `~/.maverick/`. The sandbox
`workdir` defaults to `~/maverick-workspace/`. Nothing listens on a
network port unless you start the dashboard.

## Docker

Build the private image from the same reviewed commit; no public registry image
is published or trusted by this repository. Secure deployments must pin the
base image by its reviewed digest and run the resulting local image by immutable
image ID, not by its mutable build tag.

```bash
PYTHON_DIGEST=@sha256:<reviewed-python-base-image-digest>
docker build -f deploy/docker/Dockerfile \
  --build-arg "PYTHON_DIGEST=${PYTHON_DIGEST}" \
  -t law-firm:reviewed .
LAW_FIRM_IMAGE_ID="$(docker image inspect --format '{{.Id}}' law-firm:reviewed)"
docker volume create law-firm-state
docker run -it --rm \
  -v law-firm-state:/home/maverick/.maverick \
  "$LAW_FIRM_IMAGE_ID" init

docker run -it --rm \
  -p 127.0.0.1:8765:8765 \
  -v law-firm-state:/home/maverick/.maverick \
  -v ~/maverick-workspace:/workspace \
  --env-file /operator-custody/maverick.env \
  "$LAW_FIRM_IMAGE_ID" \
  dashboard
```

Queue goals from the web UI at `http://127.0.0.1:8765`.

The state volume contains client data and must be encrypted, access-controlled,
and backed up with the retained encrypted-backup command. Keep the env file
outside the repository and data volume with operator-only permissions. A nested
container sandbox needs an independently reviewed runtime boundary; do not
treat this application container as a sandbox for untrusted execution.

When `[sandbox] backend = "docker"`, secure/container-required mode separately
requires `[sandbox] image` to be a reviewed immutable
`repository@sha256:<64-hex-digest>` or local `sha256:<64-hex-image-id>`.

## VPS

Always-on dashboard deployment for the firm's authenticated users.

The `vps` deployment target generates:

- A `systemd` unit at `/etc/systemd/system/maverick.service`
- A Caddy reverse proxy config for authenticated HTTPS dashboard access
- Config under `/etc/maverick/config.toml` (`MAVERICK_CONFIG` env)

```bash
MAVERICK_REF=<lowercase-full-40-character-commit-sha>
curl -fsSLo /tmp/maverick-install.sh \
  "https://raw.githubusercontent.com/Daybreak-AI-Labs/Law_Firm/${MAVERICK_REF}/deploy/vps/install.sh"
sudo MAVERICK_REF="$MAVERICK_REF" bash /tmp/maverick-install.sh
sudo systemctl enable --now maverick
```
