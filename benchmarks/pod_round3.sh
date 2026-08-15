#!/bin/bash
# Round 3: grow the gradable pool. Stages the large modern SWE-bench Verified
# families (sphinx, sympy, seaborn, pytest), builds ONE VENV PER INSTANCE so
# each repo grades against its own era's dependencies (MAVERICK_SWEBENCH_VENVS),
# free self-tests, then launches the paid run capped at $2.50/task, $60 total.
# Usage:  bash ~/Lightwork/benchmarks/pod_round3.sh
# Everything before the paid launch is free. Expect: staging ~30-60 min,
# venv builds ~30-90 min (parallel), then the run itself runs for hours --
# it is nohup'd and survives closing the terminal.

STAGE=~/swebench_stage
VENVS=$STAGE/venvs
REPOS=$STAGE/repos

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    line=$(grep -m1 'export ANTHROPIC_API_KEY=' ~/.bashrc 2>/dev/null)
    [ -n "$line" ] && eval "$line"
fi
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "!!! ANTHROPIC_API_KEY not set"; exit 1; }
echo "key OK (${ANTHROPIC_API_KEY:0:13}..., length ${#ANTHROPIC_API_KEY})"

echo "[1/5] staging new families: sphinx, sympy, seaborn, pytest (~30-60 min)..."
mkdir -p "$STAGE" && cd "$STAGE" || exit 1
if [ ! -f round3_new.jsonl ]; then
    python3 ~/Lightwork/benchmarks/fetch_swe_bench_verified.py \
        --repos sphinx-doc/sphinx,sympy/sympy,mwaskom/seaborn,pytest-dev/pytest \
        --out-manifest round3_new.jsonl --stage --repos-dir ./repos \
        > /tmp/fetch3.log 2>&1 \
        || {
            # A handful of failed clones must not kill the round: the manifest is
            # written before staging, and an unstaged instance later pre-gates
            # NOENV at $0. Only die if staging failed WHOLESALE.
            staged=$(ls -d ./repos/*/ 2>/dev/null | wc -l)
            if [ -f round3_new.jsonl ] && [ "$staged" -ge 50 ]; then
                echo "    WARN: fetcher exited non-zero but $staged repos staged; continuing"
                tail -2 /tmp/fetch3.log
            else
                echo "!!! FETCH FAILED - last lines:"; tail -5 /tmp/fetch3.log; exit 1
            fi
        }
fi
cat slice_manifest.jsonl round3_new.jsonl > round3_manifest.jsonl
echo "    new: $(wc -l < round3_new.jsonl)  |  combined manifest: $(wc -l < round3_manifest.jsonl)"

echo "[2/5] building per-instance venvs (parallel x8; skips ones already built)..."
mkdir -p "$VENVS"
build_one() {
    d="${1%/}"; iid=$(basename "$d"); v="$VENVS/$iid"
    [ -x "$v/bin/python" ] && return 0
    case "$iid" in
        psf__requests-*) return 0 ;;   # py<=3.9-era source; cannot run on this host
    esac
    python3 -m venv "$v" >/dev/null 2>&1 || { echo "    !!! $iid: venv create failed"; return 1; }
    "$v/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1
    # The repo's own pins ARE the era isolation. Try test extras, fall back to bare.
    "$v/bin/python" -m pip install -q -e "$d[test]"    > "/tmp/venv_$iid.log" 2>&1 \
    || "$v/bin/python" -m pip install -q -e "$d[testing]" >> "/tmp/venv_$iid.log" 2>&1 \
    || "$v/bin/python" -m pip install -q -e "$d"          >> "/tmp/venv_$iid.log" 2>&1 \
    || { echo "    !!! $iid: install failed (see /tmp/venv_$iid.log)"; rm -rf "$v"; return 1; }
    # pytest 8+ breaks several eras' conftests; pin below 8 -- EXCEPT for the
    # pytest-dev family, whose editable install IS the runner under test.
    case "$iid" in
        pytest-dev__pytest-*) : ;;
        *) "$v/bin/python" -m pip install -q 'pytest<8' >> "/tmp/venv_$iid.log" 2>&1 ;;
    esac
    echo "    $iid venv OK"
}
export -f build_one
export VENVS
ls -d "$REPOS"/*/ | xargs -P 8 -I{} bash -c 'build_one "$@"' _ {}
echo "    venvs ready: $(ls "$VENVS" 2>/dev/null | wc -l)"

echo "[3/5] FREE SELF-TEST (oracle via the instance's venv)..."
cd "$STAGE" || exit 1
export MAVERICK_SWEBENCH_VENVS=$VENVS
python3 ~/Lightwork/benchmarks/swebench_governed.py \
    --manifest one_instance.jsonl --proposer oracle \
    --keys ~/dgm-keys --ledger /tmp/preflight3_ledger.json --timeout 300 \
    > /tmp/oracle3.log 2>&1
tail -3 /tmp/oracle3.log
grep -q "resolved under governance: 1/1" /tmp/oracle3.log \
    || { echo "!!! SELF-TEST DID NOT PASS - paid run NOT started. Send this screen to Claude."; exit 1; }

echo "[4/5] SELF-TEST PASSED - launching PAID round 3 (caps: \$2.50/task, \$60 total)..."
MAVERICK_INSTANCE_HARD_CAP=2.5 MAVERICK_SUPPRESS_SANDBOX_WARNING=1 \
MAVERICK_SWEBENCH_FORENSICS=$STAGE/forensics_round3 \
MAVERICK_SWEBENCH_VENVS=$VENVS \
nohup python3 ~/Lightwork/benchmarks/swebench_governed.py \
    --manifest round3_manifest.jsonl --proposer llm \
    --keys ~/dgm-keys --ledger slice3_ledger.json --timeout 900 \
    --abort-at-dollars 60 --max-consecutive-failures 6 \
    > slice3.log 2>&1 &

echo "[5/5] ROUND 3 RUNNING (expect several hours; survives closing the terminal)"
echo "    watch:   tail -f $STAGE/slice3.log"
echo "    Ctrl-C stops the watching only, never the run."
