#!/bin/bash
# The governed DGM live run: the LLM improves the agent's OWN solver, and the
# change is promoted only on real held-out uplift through the full gate
# (editable-surface boundary, overfit refusal, cheat-propagation guard,
# capability non-escalation, Ed25519-signed + reversible). Corpus = the
# oracle-winnable set, split held-in/held-out. Grades on era-correct venvs.
# Usage:  bash ~/Lightwork/benchmarks/pod_dgm.sh
# Cheap (~$5-10): the solver under improvement is deliberately small. Free
# self-test first; nohup'd.

STAGE=~/swebench_stage
VENVS=$STAGE/venvs
cd "$STAGE" || { echo "!!! no $STAGE"; exit 1; }

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    line=$(grep -m1 'export ANTHROPIC_API_KEY=' ~/.bashrc 2>/dev/null)
    [ -n "$line" ] && eval "$line"
fi
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "!!! ANTHROPIC_API_KEY not set"; exit 1; }
echo "key OK (${ANTHROPIC_API_KEY:0:13}..., length ${#ANTHROPIC_API_KEY})"

echo "[1/3] building the DGM corpus from the oracle-winnable set..."
# Prefer the winnable set (gold patch resolves under governance here); fall
# back to the round-4 gradable manifest if that's what's present.
if [ -s winnable_ids.txt ]; then
    IDS=winnable_ids.txt
elif [ -f preflight_oracle.log ]; then
    grep -E '\[PASS\][[:space:]]+[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+[[:space:]]+resolved under governance$' \
        preflight_oracle.log | grep -oE '[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+' | sort -u > winnable_ids.txt
    IDS=winnable_ids.txt
else
    echo "!!! no winnable set found -- run the oracle preflight first"; exit 1
fi
python3 - "$IDS" <<'PY'
import json, sys
ids = {l.strip() for l in open(sys.argv[1]) if l.strip()}
kept = 0
with open("round3_manifest.jsonl") as src, open("dgm_corpus.jsonl", "w") as dst:
    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("instance_id") in ids:
            dst.write(line + "\n"); kept += 1
print(f"    dgm_corpus.jsonl: {kept} winnable instances")
PY
# The gate's evidence floor is --min-samples 10 on the HELD-OUT half, so the
# GRADABLE corpus must be >=20. The winnable list can be inflated (a family
# whose env broke since the oracle sweep grades NOENV now -- observed live:
# 12 pytest instances); the venv-aware --gradable-only pre-filter drops those
# for free and dgm_live refuses at $0 if what's left can't meet the floor.
n=$(wc -l < dgm_corpus.jsonl)
[ "$n" -ge 20 ] || { echo "!!! corpus too small ($n); need >=20 (min-samples 10 on the held-out half)"; exit 1; }

echo "[2/3] FREE self-test: offline DGM proof must be 6/6..."
python3 ~/Lightwork/proof/dgm_uplift_proof.py 2>&1 | grep -E "PROVEN|failed" \
    | grep -q "6 guarantees PROVEN" \
    || { echo "!!! DGM proof not 6/6 -- not spending. Send this to Claude."; exit 1; }
echo "    proof 6/6 OK"

echo "[3/3] launching the governed DGM live run (~\$5-10; nohup'd)..."
MAVERICK_SWEBENCH_VENVS=$VENVS MAVERICK_SUPPRESS_SANDBOX_WARNING=1 \
nohup python3 ~/Lightwork/benchmarks/dgm_live.py \
    --manifest dgm_corpus.jsonl \
    --keys ~/dgm-keys --ledger dgm_live_ledger.json \
    --held-out-frac 0.5 --min-samples 10 --gradable-only \
    --timeout 900 --abort-at-dollars 15 \
    > dgm_live.log 2>&1 &
echo "    RUNNING. watch: tail -f $STAGE/dgm_live.log"
echo "    result: held-in/held-out rates both solvers, VERDICT (PROMOTED/REFUSED/OVERFIT),"
echo "    signed ledger dgm_live_ledger.json. Send the scoreboard to Claude when done."
