#!/bin/bash
# FREE pre-flight for the DGM live run. Everything that can go wrong tomorrow
# that is NOT in the code is pod STATE: a stale checkout (missing solver.py or
# today's fixes), missing staging (winnable set / manifest / era venvs), or a
# corpus too small to meet the gate's held-out evidence floor. This checks all
# of it for $0 and prints a single GREEN/RED verdict. Run it BEFORE pod_dgm.sh.
#
#   bash ~/Lightwork/benchmarks/pod_dgm_preflight.sh
set -u
STAGE=~/swebench_stage
VENVS=$STAGE/venvs
# Derive the repo root from THIS script's location so the check works wherever
# the repo is cloned (pod ~/Lightwork or elsewhere), not a hardcoded path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { echo "  [ OK ] $1"; }
bad() { echo "  [RED ] $1"; fail=1; }

echo "======================================================================"
echo "  DGM LIVE PRE-FLIGHT (free; run before pod_dgm.sh)"
echo "======================================================================"

# 1. Checkout freshness: the launcher uses the DEFAULT solver dir and today's
#    fixes (separate-tree grading, venv-aware filter, _score 5-tuple). A stale
#    checkout is exactly what crashed CI earlier.
echo "[1/5] repo checkout ..."
git -C "$REPO" fetch origin main -q 2>/dev/null
LOCAL=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)
REMOTE=$(git -C "$REPO" rev-parse origin/main 2>/dev/null)
[ -n "$LOCAL" ] && [ "$LOCAL" = "$REMOTE" ] \
    && ok "on latest origin/main ($LOCAL)" \
    || bad "NOT on latest main (local ${LOCAL:0:8}, remote ${REMOTE:0:8}) -- run: git -C $REPO pull --ff-only origin main"
[ -f "$REPO/benchmarks/solvers/baseline_v0/solver.py" ] \
    && ok "default solver present" || bad "MISSING benchmarks/solvers/baseline_v0/solver.py (stale checkout)"

# 2. Signing keys.
echo "[2/5] signing keys ..."
[ -f ~/dgm-keys/operator.priv.hex ] && [ -f ~/dgm-keys/operator.pub ] \
    && ok "~/dgm-keys present" || bad "MISSING ~/dgm-keys/{operator.priv.hex,operator.pub}"

# 3. Staging: winnable set + round-3 manifest + era venvs.
echo "[3/5] staging ..."
[ -s "$STAGE/round3_manifest.jsonl" ] && ok "round3_manifest.jsonl present" \
    || bad "MISSING $STAGE/round3_manifest.jsonl (re-stage needed)"
if [ -s "$STAGE/winnable_ids.txt" ]; then
    ok "winnable_ids.txt present ($(wc -l < "$STAGE/winnable_ids.txt") ids)"
elif [ -f "$STAGE/preflight_oracle.log" ]; then
    ok "preflight_oracle.log present (winnable set derivable)"
else
    bad "no winnable_ids.txt or preflight_oracle.log (run the oracle preflight)"
fi
[ -d "$VENVS" ] && ok "era venvs dir present ($(ls "$VENVS" 2>/dev/null | wc -l) venvs)" \
    || bad "MISSING $VENVS -- --gradable-only will drop everything to host python"

# 4. Corpus size vs the gate floor (min-samples 10 on the held-out half => >=20).
echo "[4/5] corpus size ..."
if [ -s "$STAGE/round3_manifest.jsonl" ] && { [ -s "$STAGE/winnable_ids.txt" ] || [ -f "$STAGE/preflight_oracle.log" ]; }; then
    IDS="$STAGE/winnable_ids.txt"
    [ -s "$IDS" ] || IDS="$STAGE/preflight_oracle.log"
    n=$(python3 - "$STAGE/round3_manifest.jsonl" "$IDS" <<'PY'
import json,re,sys
man,idsf=sys.argv[1],sys.argv[2]
txt=open(idsf).read()
ids=set(re.findall(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+',txt))
k=sum(1 for l in open(man) if l.strip() and json.loads(l).get("instance_id") in ids)
print(k)
PY
)
    [ "${n:-0}" -ge 20 ] && ok "corpus $n >= 20 (pre-filter; --gradable-only may reduce it)" \
        || bad "corpus only ${n:-0} (<20) -- dgm_live will NO-RUN at \$0 after filtering"
else
    bad "cannot compute corpus size (missing manifest or ids)"
fi

# 5. Offline proof must be 6/6 (the free capability self-test the paid run gates on).
echo "[5/5] offline DGM proof 6/6 ..."
python3 "$REPO/proof/dgm_uplift_proof.py" 2>&1 | grep -q "6 guarantees PROVEN" \
    && ok "DGM proof 6/6" || bad "DGM proof NOT 6/6 -- do not spend"

echo "======================================================================"
if [ "$fail" -eq 0 ]; then
    echo "  VERDICT: GREEN -- safe to run:  bash ~/Lightwork/benchmarks/pod_dgm.sh"
else
    echo "  VERDICT: RED -- fix the [RED] items above BEFORE spending. Send this to Claude."
fi
echo "======================================================================"
exit "$fail"
