#!/bin/bash
# Round 4: push the resolve rate on the ALREADY-ATTEMPTED gradable pool with the
# three round-4 levers (best-of-N selection, retry-on-empty, localization
# pre-pass). Instead of re-staging, it rebuilds a manifest from the instances
# prior rounds actually attempted (their [PASS]/[FAIL]/[EMPTY] log lines) and
# re-runs just those, harder.
#
# Usage:  bash ~/Lightwork/benchmarks/pod_round4.sh A|B
#   A  best-of-N=4, $5/task, default role models,  total abort $120
#   B  best-of-N=4, $8/task, Opus-class worker,    total abort $400
#
# Targets ONLY the oracle-winnable set (preflight_oracle.log [PASS] ids), so
# real spend is ~n_winnable x per-instance, well under the abort ceiling. Run
# pod_preflight.sh first. Everything before the paid launch is free; the run is
# nohup'd and survives closing the terminal.

TIER="$1"
LIMIT="${2:-0}"   # optional: attempt only the first N winnable instances (pilot mode)
if [ -z "$TIER" ]; then
    echo "!!! tier argument required:  bash pod_round4.sh A|B [pilot_n]"
    exit 1
fi
case "$TIER" in
    A|a) TIER=A ;;
    B|b) TIER=B ;;
    *) echo "!!! unknown tier '$TIER' (expected A or B)"; exit 1 ;;
esac

STAGE=~/swebench_stage
VENVS=$STAGE/venvs

# Tier knobs. ABORT is a WORST-CASE ceiling; because round 4 targets only the
# ~20-30 oracle-winnable instances (not all 159), real spend lands well under it.
if [ "$TIER" = A ]; then
    BON=4; INSTANCE_CAP=5; ABORT=120
    OPUS_OVERRIDE=""
else
    BON=4; INSTANCE_CAP=8; ABORT=400
    # Force the coding worker (the orchestrator role that resolves the model for
    # coding-mode work AND seeds the best-of-N ladder) onto the Opus-class model.
    # MAVERICK_MODEL_OVERRIDE_<ROLE> is the supported per-role env override
    # (llm._resolve_model_for_role resolution order #1; beats config).
    OPUS_OVERRIDE="MAVERICK_MODEL_OVERRIDE_ORCHESTRATOR=claude-opus-4-8"  # pragma: allowlist secret
fi

PILOT_TAG=""
[ "$LIMIT" -gt 0 ] && PILOT_TAG=" (PILOT: first $LIMIT winnable)"
echo "======================================================================"
echo "  ROUND 4  TIER $TIER$PILOT_TAG"
echo "    best-of-N        : $BON"
echo "    per-instance cap : \$$INSTANCE_CAP"
echo "    total abort cap  : \$$ABORT   <-- expected WORST-CASE spend"
[ -n "$OPUS_OVERRIDE" ] && echo "    worker model     : claude-opus-4-8 (via $OPUS_OVERRIDE)"
echo "======================================================================"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    line=$(grep -m1 'export ANTHROPIC_API_KEY=' ~/.bashrc 2>/dev/null)
    [ -n "$line" ] && eval "$line"
fi
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "!!! ANTHROPIC_API_KEY not set"; exit 1; }
echo "key OK (${ANTHROPIC_API_KEY:0:13}..., length ${#ANTHROPIC_API_KEY})"

cd "$STAGE" || { echo "!!! no stage dir $STAGE (run round 3 first)"; exit 1; }

echo "[1/4] building gradable_manifest.jsonl from the ORACLE-WINNABLE set..."
if [ ! -f round3_manifest.jsonl ]; then
    echo "!!! round3_manifest.jsonl missing in $STAGE -- round 3 must have staged first"
    exit 1
fi
# The winnable set = instances the GOLD patch resolves under governance in this
# environment (preflight_oracle.log [PASS] lines). Spending the LLM on anything
# the gold patch itself can't resolve here (era-NOENV, gate-refused-even-on-gold)
# is pure waste, so target only the oracle ceiling. Falls back to the prior
# rounds' attempted ids if the oracle preflight wasn't run.
if [ -s winnable_ids.txt ]; then
    cp winnable_ids.txt attempted_ids.txt
    echo "    source: winnable_ids.txt (parallel oracle sweep)"
    # Parity guard: a winnable list computed BEFORE the venvs were (re)built was
    # computed in a DIFFERENT environment and lies about what is winnable NOW
    # (observed live: 12 "winnable" pytest instances came back NOENV/EMPTY in
    # the paid run -- 3 of them at full best-of-N price). Warn loudly.
    if [ -d "$VENVS" ] && [ winnable_ids.txt -ot "$VENVS" ]; then
        echo "    !!! WARNING: winnable_ids.txt is OLDER than $VENVS"
        echo "    !!! The winnable set predates the current venvs; instances it"
        echo "    !!! calls winnable may be NOENV/EMPTY now. Re-run pod_preflight.sh"
        echo "    !!! (the family breaker will stop any per-family bleed at \$0)."
    fi
elif [ -f preflight_oracle.log ]; then
    grep -E '\[PASS\][[:space:]]+[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+[[:space:]]+resolved under governance$' \
        preflight_oracle.log \
        | grep -oE '[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+' | sort -u > attempted_ids.txt
    echo "    source: preflight_oracle.log (oracle-winnable ceiling)"
else
    grep -hoE '\[(PASS|FAIL|EMPTY)\][[:space:]]+[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+' \
        slice3b.log slice3c.log 2>/dev/null \
        | awk '{print $2}' | sort -u > attempted_ids.txt
    echo "    source: slice3b/3c attempted ids (oracle preflight not found)"
fi
n_ids=$(wc -l < attempted_ids.txt)
echo "    winnable instance ids: $n_ids"
if [ "$n_ids" -eq 0 ]; then
    echo "!!! no winnable ids parsed -- run pod_preflight.sh first"
    exit 1
fi
# Filter the round-3 manifest down to those ids.
python3 - <<'PY'
import json
ids = set()
with open("attempted_ids.txt") as f:
    for line in f:
        line = line.strip()
        if line:
            ids.add(line)
kept = 0
with open("round3_manifest.jsonl") as src, open("gradable_manifest.jsonl", "w") as dst:
    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("instance_id") in ids:
            dst.write(line + "\n")
            kept += 1
print(f"    gradable_manifest.jsonl: {kept} instances")
PY
[ -s gradable_manifest.jsonl ] || { echo "!!! gradable_manifest.jsonl empty"; exit 1; }

echo "[2/4] FREE SELF-TEST (oracle via the instance's venv)..."
export MAVERICK_SWEBENCH_VENVS=$VENVS
python3 ~/Lightwork/benchmarks/swebench_governed.py \
    --manifest one_instance.jsonl --proposer oracle \
    --keys ~/dgm-keys --ledger /tmp/preflight4_ledger.json --timeout 300 \
    > /tmp/oracle4.log 2>&1
tail -3 /tmp/oracle4.log
grep -q "resolved under governance: 1/1" /tmp/oracle4.log \
    || { echo "!!! SELF-TEST DID NOT PASS - paid run NOT started. Send this screen to Claude."; exit 1; }

# FREE best-of-N smoke gate: exercise the EXACT diff-capture + reset path a
# paid best-of-N run depends on, on the first few real staged repos we're about
# to spend on. If capture comes back empty (the blobless-clone guard bug that
# silently produced universal [EMPTY] and burned real money), ABORT before
# spending a cent. This is the check that would have caught that $ loss for $0.
echo "[2b/4] FREE best-of-N smoke gate (capture+reset on real staged repos)..."
SMOKE_REPOS=$(head -3 gradable_manifest.jsonl \
    | python3 -c "import sys,json;[print('$STAGE/repos/'+json.loads(l)['instance_id']) for l in sys.stdin if l.strip()]" 2>/dev/null)
if [ -n "$SMOKE_REPOS" ]; then
    python3 ~/Lightwork/benchmarks/bon_smoke.py $SMOKE_REPOS \
        || { echo "!!! BEST-OF-N SMOKE FAILED - paid run NOT started (would have wasted money). Send this screen to Claude."; exit 1; }
else
    echo "!!! could not resolve smoke repos from manifest; refusing to spend blind"; exit 1
fi

echo "[3/4] SELF-TEST PASSED - launching PAID round 4 tier $TIER (caps: \$$INSTANCE_CAP/task, \$$ABORT total)..."
env $OPUS_OVERRIDE \
MAVERICK_BEST_OF_N=$BON \
MAVERICK_INSTANCE_HARD_CAP=$INSTANCE_CAP \
MAVERICK_SUPPRESS_SANDBOX_WARNING=1 \
MAVERICK_SWEBENCH_FORENSICS=$STAGE/forensics_round4 \
MAVERICK_SWEBENCH_VENVS=$VENVS \
nohup python3 ~/Lightwork/benchmarks/swebench_governed.py \
    --manifest gradable_manifest.jsonl --proposer llm \
    --keys ~/dgm-keys --ledger slice4_ledger.json --timeout 900 \
    --abort-at-dollars $ABORT --limit $LIMIT \
    --max-consecutive-failures 8 --max-family-failures 3 \
    > slice4.log 2>&1 &

echo "[4/4] ROUND 4 TIER $TIER RUNNING (expect several hours; survives closing the terminal)"
echo "    watch:   tail -f $STAGE/slice4.log"
echo "    Ctrl-C stops the watching only, never the run."
