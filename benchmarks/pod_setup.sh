#!/bin/bash
# One-shot pod bootstrap for the SWE-bench-under-governance slice run.
# Run from anywhere:  bash ~/Lightwork/benchmarks/pod_setup.sh
# Assumes: repo already cloned to ~/Lightwork, ANTHROPIC_API_KEY exported
# (or saved in ~/.bashrc). Installs everything, stages the Verified slice,
# self-tests for free, and starts the capped paid run only if the test passes.

if [ -z "$ANTHROPIC_API_KEY" ]; then
    line=$(grep -m1 'export ANTHROPIC_API_KEY=' ~/.bashrc 2>/dev/null)
    [ -n "$line" ] && eval "$line"
fi
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "!!! ANTHROPIC_API_KEY is not set. Run the export line first, then rerun me."
    exit 1
fi
echo "key OK (${ANTHROPIC_API_KEY:0:13}..., length ${#ANTHROPIC_API_KEY})"

# Pods often ship several Pythons; bare `pip` can belong to a different
# interpreter than `python3`, silently splitting the install. Pin every
# pip call (including post-create.sh's) to python3's own pip.
pip() { python3 -m pip "$@"; }
export -f pip
python3 -m pip install -q --upgrade pip >/dev/null 2>&1

echo "[1/5] installing Lightwork packages (few minutes)..."
cd ~/Lightwork || { echo "!!! ~/Lightwork missing - clone the repo first"; exit 1; }
bash .devcontainer/post-create.sh > /tmp/postcreate.log 2>&1 \
    && echo "    install OK" \
    || { echo "!!! INSTALL FAILED - last lines:"; tail -5 /tmp/postcreate.log; exit 1; }

echo "[2/5] signing keys..."
python3 -m pip install -q cryptography >/dev/null 2>&1
python3 - <<'PY'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pathlib import Path
k = Path.home() / "dgm-keys"; k.mkdir(exist_ok=True)
priv = ed25519.Ed25519PrivateKey.generate()
(k / "operator.priv.hex").write_text(priv.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
    serialization.NoEncryption()).hex())
(k / "operator.pub").write_bytes(priv.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw))
print("    keys OK ->", k)
PY

echo "[3/5] staging SWE-bench Verified instances (few minutes)..."
python3 -m pip install -q datasets >/dev/null 2>&1
mkdir -p ~/swebench_stage && cd ~/swebench_stage
python3 ~/Lightwork/benchmarks/fetch_swe_bench_verified.py \
    --repos psf/requests,pylint-dev/pylint,pallets/flask \
    --out-manifest slice_manifest.jsonl --stage --repos-dir ./repos \
    > /tmp/fetch.log 2>&1 \
    && echo "    staged $(wc -l < slice_manifest.jsonl) instances" \
    || { echo "!!! FETCH FAILED - last lines:"; tail -5 /tmp/fetch.log; exit 1; }

echo "[4/5] instance test dependencies..."
cd ~/swebench_stage/repos
# Install the SELF-TEST instance's clone (pylint-8898) so its era's deps
# (tomlkit, astroid 3.x) land -- NOT the alphabetically-first old clone,
# whose 2.x-era deps differ. requests clones are py<=3.9 code and cannot
# run on this pod's 3.10; the run's free pre-gate skips them at $0.
for d in pylint-dev__pylint-8898 $(ls -d pallets__flask-* 2>/dev/null | head -1); do
    [ -d "$d" ] || continue
    python3 -m pip install -e "./$d" > "/tmp/deps_${d}.log" 2>&1 \
        && echo "    $d deps OK" \
        || { echo "!!! DEPS FAILED for $d - last lines:"; tail -5 "/tmp/deps_${d}.log"; }
done
# Era pins, verified against the staged instances: flask-2.3-era conftest
# uses a private pytest API removed in pytest 8 (_pytest.monkeypatch.notset)
# and needs werkzeug<3; pylint-8898's suite passes under pytest 7 too.
python3 -m pip install -q 'pytest<8' 'werkzeug>=2.3,<3.0' 'blinker>=1.6.2' >/dev/null 2>&1 \
    && echo "    era pins OK (pytest<8, werkzeug<3)"
python3 - <<'PY'
import importlib.util
need = ["tomlkit", "astroid", "platformdirs", "dill", "flask", "pytest", "werkzeug", "blinker"]
missing = [m for m in need if importlib.util.find_spec(m) is None]
print("    dep check:", "all OK" if not missing else f"MISSING: {missing}")
PY

echo "[5/5] FREE SELF-TEST..."
cd ~/swebench_stage
python3 ~/Lightwork/proof/swebench_governed_proof.py 2>&1 | tail -2
grep 8898 slice_manifest.jsonl > one_instance.jsonl 2>/dev/null \
    || head -1 slice_manifest.jsonl > one_instance.jsonl
python3 ~/Lightwork/benchmarks/swebench_governed.py \
    --manifest one_instance.jsonl --proposer oracle \
    --keys ~/dgm-keys --ledger /tmp/preflight_ledger.json --timeout 300 \
    > /tmp/oracle.log 2>&1
tail -4 /tmp/oracle.log

if grep -q "resolved under governance: 1/1" /tmp/oracle.log; then
    echo ""
    echo "================ SELF-TEST PASSED - STARTING PAID RUN ================"
    cd ~/swebench_stage
    MAVERICK_INSTANCE_HARD_CAP=3.0 MAVERICK_SUPPRESS_SANDBOX_WARNING=1 \
    nohup python3 ~/Lightwork/benchmarks/swebench_governed.py \
        --manifest slice_manifest.jsonl --proposer llm \
        --keys ~/dgm-keys --ledger slice_ledger.json --timeout 700 \
        --abort-at-dollars 40 --max-consecutive-failures 4 \
        > slice.log 2>&1 &
    echo "PAID RUN STARTED. Caps: \$3/task, \$40 total, stops after 4 fails in a row."
    echo "Watch it:   tail -f ~/swebench_stage/slice.log"
    echo "(Ctrl-C stops the watching only, never the run.)"
else
    echo ""
    echo "!!! SELF-TEST DID NOT PASS - paid run NOT started."
    echo "!!! Copy everything on this screen and send it to Claude."
fi
