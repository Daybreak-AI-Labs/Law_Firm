#!/bin/bash
# FREE Tier-B pre-flight. Runs the ORACLE proposer (gold patches, no LLM,
# ~$0) across the whole staged pool to (1) prove the sympy fix grades on the
# REAL pod staging and (2) establish the true gradable denominator + ceiling:
# every instance the gold patch resolves is one the agent COULD resolve; every
# one it can't is an environment gap, not an agent failure. Nothing here spends
# LLM budget. Run before any paid Tier-B launch.

STAGE=~/swebench_stage
VENVS=$STAGE/venvs

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo "[1/3] using benchmark code from the current checkout..."
cd "$REPO_ROOT" || exit 1
# Reinstall core so the local checkout's coding_mode.py is the imported one.
# Do not refresh files from a mutable remote branch here: this script executes
# the benchmark harness below and passes operator signing keys to it.
python3 -m pip install -e "$REPO_ROOT/packages/maverick-core" --no-deps -q 2>/dev/null
echo "    code ready"

echo "[2/3] sanity: two sympy instances via oracle on the REAL staging..."
cd "$STAGE" || exit 1
export MAVERICK_SWEBENCH_VENVS=$VENVS MAVERICK_SUPPRESS_SANDBOX_WARNING=1
for iid in sympy__sympy-24066 sympy__sympy-24213; do
    # write the 1-line manifest INTO $STAGE so _load_manifest derives
    # repo_path from $STAGE/repos (not /tmp/repos).
    grep -F "\"$iid\"" round3_manifest.jsonl > "$STAGE/pf_$iid.jsonl" 2>/dev/null
    if [ ! -s "$STAGE/pf_$iid.jsonl" ]; then echo "    (skip $iid: not in manifest)"; continue; fi
    python3 "$REPO_ROOT/benchmarks/swebench_governed.py" \
        --manifest "$STAGE/pf_$iid.jsonl" --proposer oracle \
        --keys ~/dgm-keys --ledger "/tmp/pf_$iid.ledger" --timeout 900 2>/dev/null \
        | grep -E "resolved under governance:|\[(PASS|FAIL|NOENV)\]" | head -3
done

echo "[3/3] FULL-POOL oracle sweep (free) -> true gradable denominator + ceiling..."
echo "    (this grades every instance with its gold patch; several minutes, \$0)"
MAVERICK_SWEBENCH_VENVS=$VENVS MAVERICK_SUPPRESS_SANDBOX_WARNING=1 \
MAVERICK_SWEBENCH_FORENSICS=$STAGE/forensics_preflight \
nohup python3 "$REPO_ROOT/benchmarks/swebench_governed.py" \
    --manifest round3_manifest.jsonl --proposer oracle \
    --keys ~/dgm-keys --ledger $STAGE/preflight_oracle_ledger.json --timeout 900 \
    > $STAGE/preflight_oracle.log 2>&1 &
echo "    launched. watch: tail -f $STAGE/preflight_oracle.log"
echo "    when done, the final scoreboard's 'resolved of GRADABLE' is the CEILING"
echo "    (max the agent could hit) and the gradable denominator for Tier B."
