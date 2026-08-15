#!/bin/bash
# PARALLEL free oracle sweep: same ceiling measurement as pod_preflight.sh but
# sharded across N workers so a 32-core box finishes in minutes, not an hour.
# Kills any running sequential sweep first (it's superseded, not lost -- this
# regrades everything). Output: preflight_shard_*.log + a merged summary with
# the winnable [PASS] id list the round-4 launcher consumes.
# Usage: bash ~/Lightwork/benchmarks/pod_preflight_fast.sh [workers]   (default 8)

STAGE=~/swebench_stage
VENVS=$STAGE/venvs
W="${1:-8}"
cd "$STAGE" || { echo "!!! no $STAGE"; exit 1; }
[ -f round3_manifest.jsonl ] || { echo "!!! round3_manifest.jsonl missing"; exit 1; }

echo "[1/3] stopping any previous sweep + sharding manifest into $W parts..."
pkill -f "proposer oracle" 2>/dev/null; sleep 2
rm -f preflight_shard_*.jsonl preflight_shard_*.log
# round-robin shard so heavy families spread across workers
awk -v W="$W" 'NF{print > ("preflight_shard_" (NR % W) ".jsonl")}' round3_manifest.jsonl
ls preflight_shard_*.jsonl | while read -r f; do echo "    $f: $(wc -l < "$f") instances"; done

echo "[2/3] launching $W parallel oracle workers (free, ~5-15 min)..."
export MAVERICK_SWEBENCH_VENVS=$VENVS MAVERICK_SUPPRESS_SANDBOX_WARNING=1
i=0
for f in preflight_shard_*.jsonl; do
    MAVERICK_SWEBENCH_FORENSICS=$STAGE/forensics_pf_$i \
    nohup python3 ~/Lightwork/benchmarks/swebench_governed.py \
        --manifest "$f" --proposer oracle \
        --keys ~/dgm-keys --ledger "/tmp/pf_shard_$i.ledger" --timeout 900 \
        > "preflight_shard_$i.log" 2>&1 &
    i=$((i+1))
done
echo "    workers launched: $i"

echo "[3/3] waiting for all workers, then merging..."
wait
grep -hE '\[PASS\][[:space:]]+[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+[[:space:]]+resolved under governance$' \
    preflight_shard_*.log \
    | grep -oE '[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+' | sort -u > winnable_ids.txt
# also write a merged log so pod_round4.sh's preflight_oracle.log path works
cat preflight_shard_*.log > preflight_oracle.log
n_pass=$(wc -l < winnable_ids.txt)
n_noenv=$(grep -c "ungradable in this environment" preflight_oracle.log)
n_fail=$(grep -cE "^\s+\[FAIL\]" preflight_oracle.log)
n_cheat=$(grep -cE "^\s+\[CHEAT\]" preflight_oracle.log)
echo "======================================================================"
echo "  ORACLE CEILING (gold patch, this environment)"
echo "    WINNABLE (oracle PASS): $n_pass    <-- Tier-B denominator + target set"
echo "    ungradable-here:        $n_noenv"
echo "    gold-not-resolving:     $n_fail"
echo "    gold-gate-refused:      $n_cheat"
echo "======================================================================"
echo "  winnable ids -> $STAGE/winnable_ids.txt"
echo "  NEXT: pilot 5 instances:   bash ~/Lightwork/benchmarks/pod_round4.sh B 5"
echo "        full run (if pilot earns it): bash ~/Lightwork/benchmarks/pod_round4.sh B"
