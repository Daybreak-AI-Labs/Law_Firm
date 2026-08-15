#!/usr/bin/env bash
# One-shot setup for running the governed SWE-bench DGM uplift on a Mac mini.
# The Mac is only the "conductor": the LLM solve is an API call and grading runs
# on Modal (x86 Linux, in the cloud) -- so Apple Silicon vs Intel does not matter,
# and you must NOT grade with local Docker. Run this once from the repo root.
#
#   chmod +x benchmarks/setup_mac.sh && ./benchmarks/setup_mac.sh
#
# Idempotent: safe to re-run. Nothing paid happens here.
set -euo pipefail

echo "==> 1/6  Homebrew + tools"
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
brew list git        >/dev/null 2>&1 || brew install git
brew list tmux       >/dev/null 2>&1 || brew install tmux

PY="$(brew --prefix)/opt/python@3.11/bin/python3.11"
echo "==> 2/6  Python venv (.venv) with $($PY --version)"
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null

echo "==> 3/6  Editable-install the workspace packages (same order as CI)"
pip install -e ./packages/maverick-core
for p in maverick-shield maverick-channels maverick-evolve maverick-dashboard \
         maverick-mcp maverick-knowledge; do
  pip install --no-deps -e "./packages/$p"
done
pip install --no-deps -e ./apps/installer-cli

echo "==> 4/6  Runtime + grading deps"
pip install 'questionary>=2.0' 'rich>=13.7' 'fastapi>=0.110' 'uvicorn>=0.27' \
            'jinja2>=3.1' 'httpx>=0.27' 'python-multipart>=0.0.9' \
            'pyjwt[crypto]' cffi 'openai>=1.30' modal
# SWE-bench run path needs these two: `datasets` loads SWE-bench Verified,
# `swebench` is the official grader/harness used inside the Modal container.
# (anthropic ships with maverick-core; modal is above.)
pip install datasets swebench

echo "==> 5/6  Ed25519 signing keys for the governed ledger (~/dgm-keys)"
python - <<'PY'
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as ser

d = Path.home() / "dgm-keys"
priv_path = d / "operator.priv.hex"
pub_path = d / "operator.pub"

# The private key signs governance approvals, so keep both new and existing
# installs restrictive even when the user's default umask is permissive.
os.umask(0o077)
d.mkdir(mode=0o700, exist_ok=True)
os.chmod(d, 0o700)

if priv_path.exists():
    os.chmod(priv_path, 0o600)
    print("  ~/dgm-keys already present, hardened permissions")
else:
    p = Ed25519PrivateKey.generate()
    priv = p.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex()
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(priv)
    pub_path.write_bytes(p.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw))
    print("  wrote", d)

if pub_path.exists():
    os.chmod(pub_path, 0o644)
PY

echo "==> 6/6  Modal auth check"
if modal profile current >/dev/null 2>&1; then
  echo "  Modal token OK"
else
  echo "  Modal not authed yet -> run:  source .venv/bin/activate && modal token new"
fi

echo
echo "Setup done. Next:"
echo "  1) source .venv/bin/activate"
echo "  2) export ANTHROPIC_API_KEY=sk-ant-...      (your key)"
echo "  3) (if step 6 said 'not authed')  modal token new"
echo "  4) ./benchmarks/preflight_mac.sh            # \$0 check the whole stack works"
echo "  5) see benchmarks/RUN_DGM_ON_MAC.md         # then the paid run"
