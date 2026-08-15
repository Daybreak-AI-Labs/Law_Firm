# Running the full SWE-bench DGM uplift on a persistent VM

The DGM cycle needs a host that stays up for a few hours. This session's container
recycles mid-solve, which is why it can't finish here. A plain cloud VM fixes that.
The VM only *conducts* — the LLM solve is an API call and grading runs on Modal —
so a small, cheap box is plenty. **Do not** use serverless / spot / preemptible
instances (they recycle). No GPU needed.

Expected cost: VM ≈ pennies; the run ≈ $50–100 (LLM + Modal). Runtime ≈ 1–3 hours.

---

## 1. Spin up the box

Any on-demand Linux VM works. Simplest: a DigitalOcean **Basic Droplet**, Ubuntu
22.04, 2 vCPU / 4 GB. (AWS Lightsail t3.medium, GCP e2-medium, or Hetzner CX22 are
equivalent.) SSH in as a sudo user.

## 2. One-time setup (paste on the VM)

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git tmux
git clone https://github.com/Daybreak-AI-Labs/Lightwork.git
cd Lightwork
bash .devcontainer/post-create.sh          # editable-installs all packages + deps
pip install modal                           # grading backend client
```

## 3. Secrets (bring your own — nothing secret is committed)

```bash
# a) Anthropic key (the model)
export ANTHROPIC_API_KEY=sk-ant-...          # your key

# b) Modal token (the grading backend). Either copy your existing ~/.modal.toml
#    onto the box, OR run:
modal token new                              # opens a browser auth flow

# c) Ed25519 signing keys for the governed ledger — generate fresh on the box:
mkdir -p ~/dgm-keys
python3 - <<'PY'
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as ser
from pathlib import Path
p = Ed25519PrivateKey.generate(); d = Path.home()/"dgm-keys"; d.mkdir(exist_ok=True)
(d/"operator.priv.hex").write_text(p.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
(d/"operator.pub").write_bytes(p.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw))
print("keys written to", d)
PY
```

## 4. Launch it (unattended, inside tmux so it survives disconnects)

```bash
tmux new -s dgm
cd ~/Lightwork
export ANTHROPIC_API_KEY=sk-ant-...          # re-export inside tmux
python benchmarks/realswe_dgm.py \
    --keys ~/dgm-keys --out-dir ~/realswe_dgm \
    --ids sympy__sympy-15976,sympy__sympy-14531,pytest-dev__pytest-5631 \
    --max-dollars 60
# detach with:  Ctrl-b then d      (the run keeps going)
```

For the larger, more statistically meaningful run, use the full driver instead:

```bash
# 10-instance corpus (stage once, then run the governed uplift):
python benchmarks/build_dgm_manifest.py --n 10 --seed 1729 \
    --stage-dir ~/dgm_stage --manifest ~/dgm_stage/manifest.jsonl
python benchmarks/dgm_live.py --manifest ~/dgm_stage/manifest.jsonl \
    --keys ~/dgm-keys --ledger ~/dgm_stage/ledger.json \
    --solver benchmarks/solvers/agent_v0 \
    --container-grade --container-backend modal \
    --min-samples 5 --held-out-frac 0.5
```

## 5. When it finishes — read + independently verify the result

```bash
tmux attach -t dgm                            # watch progress / see the verdict
tail -f ~/realswe_dgm.log 2>/dev/null || tail ~/realswe_dgm/*.log

# independent audit of the signed ledger (don't trust, verify):
python benchmarks/audit_ledger.py \
    --ledger ~/realswe_dgm/ledger.json --keys ~/dgm-keys
```

The driver is **resumable** (each solve is checkpointed to `~/realswe_dgm/verdicts.json`)
and **cost-capped** (`--max-dollars`). If the VM ever reboots, just re-run the same
command — it skips finished solves and never re-pays. When it completes it prints the
baseline → candidate held-out resolved-rate, the PROMOTED/REFUSED verdict, and signs
it into the ledger, which the auditor above re-verifies.

## 6. Tear down

```bash
# copy the signed ledger off the box first:
scp user@vm:~/realswe_dgm/ledger.json .
# then destroy the droplet/instance from the provider console (stops all cost).
```
