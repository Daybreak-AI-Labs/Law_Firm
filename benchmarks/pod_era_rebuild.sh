#!/bin/bash
# Era-correct venv rebuild. The pod's system python is 3.10, so every venv was
# built on 3.10 -- but most SWE-bench Verified instances were authored for
# OLDER Python (mostly 3.9), which is why old-era sympy/sphinx/requests/pytest
# fail the pre-gate ("baseline cannot run its own passing tests") on 3.10.
# This installs the era-correct interpreters (deadsnakes) and rebuilds each
# instance's venv on the Python version SWE-bench actually uses for it, taken
# from the authoritative `swebench` spec map (family fallback if unavailable).
# Then re-run pod_preflight.sh to get the NEW, larger winnable ceiling.
#
# Usage:  bash ~/Lightwork/benchmarks/pod_era_rebuild.sh
# Free (no LLM). Rebuilds in place: builds <iid>.new, swaps only on success, so
# a currently-working venv can never regress. Expect 30-90 min (parallel x6).

STAGE=~/swebench_stage
VENVS=$STAGE/venvs
REPOS=$STAGE/repos
cd "$STAGE" || { echo "!!! no $STAGE"; exit 1; }
[ -f round3_manifest.jsonl ] || { echo "!!! round3_manifest.jsonl missing"; exit 1; }

echo "[1/4] installing era-correct Python interpreters (deadsnakes)..."
apt-get update -y -q >/dev/null 2>&1
# deadsnakes is already an apt source on this image. 3.9 covers almost all of
# these families; 3.8/3.11 cover the tails.
for pv in 3.8 3.9 3.11; do
    if command -v "python${pv}" >/dev/null 2>&1; then
        echo "    python${pv} already present"
        continue
    fi
    apt-get install -y -q "python${pv}" "python${pv}-venv" "python${pv}-dev" \
        "python${pv}-distutils" >/dev/null 2>&1
    if command -v "python${pv}" >/dev/null 2>&1; then
        echo "    python${pv} installed"
    else
        echo "    python${pv} NOT available (skip; instances needing it stay as-is)"
    fi
done

echo "[2/4] resolving each instance's era-correct Python (swebench spec map)..."
python3 -m pip install -q swebench >/dev/null 2>&1
python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path
stage = Path.home() / "swebench_stage"
try:
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
except Exception:
    MAP_REPO_VERSION_TO_SPECS = {}
# Family fallback: SWE-bench uses 3.9 for essentially all of these repos.
FALLBACK = {
    "sympy/sympy": "3.9", "sphinx-doc/sphinx": "3.9", "mwaskom/seaborn": "3.9",
    "pytest-dev/pytest": "3.9", "pallets/flask": "3.9",
    "pylint-dev/pylint": "3.9", "psf/requests": "3.9",
}
out = {}
for line in (stage / "round3_manifest.jsonl").read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    iid = row["instance_id"]
    repo = row.get("repo", "")
    ver = row.get("version", "")
    spec = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(ver, {})
    py = str(spec.get("python", "") or FALLBACK.get(repo, "3.9")).strip()
    # normalize e.g. "3.9.19" -> "3.9"
    py = ".".join(py.split(".")[:2]) if py else "3.9"
    out[iid] = py
(stage / "instance_python.json").write_text(json.dumps(out))
print("    python distribution:", dict(Counter(out.values())))
print("    instances mapped:", len(out))
PY

echo "[3/4] rebuilding venvs on the era-correct interpreter (parallel x6)..."
mkdir -p "$VENVS"
rebuild_one() {
    d="${1%/}"; iid=$(basename "$d"); v="$VENVS/$iid"
    case "$iid" in
        psf__requests-1142|psf__requests-1724|psf__requests-1766|psf__requests-1921)
            # 2013-era requests bundles its own broken urllib3; skip.
            return 0 ;;
    esac
    # look up era python; default 3.9
    pv=$(python3 -c "import json,sys;print(json.load(open('$VENVS/../instance_python.json')).get('$iid','3.9'))" 2>/dev/null)
    [ -z "$pv" ] && pv=3.9
    py="python${pv}"
    command -v "$py" >/dev/null 2>&1 || { echo "    !!! $iid: $py missing, kept 3.10"; return 0; }
    # already on the right python?
    if [ -x "$v/bin/python" ] && "$v/bin/python" --version 2>&1 | grep -q " ${pv}\."; then
        return 0
    fi
    rm -rf "$v.new"
    "$py" -m venv "$v.new" >/dev/null 2>&1 || { echo "    !!! $iid: $py venv failed"; rm -rf "$v.new"; return 1; }
    "$v.new/bin/python" -m pip install -q --upgrade pip setuptools wheel >/dev/null 2>&1
    log="/tmp/rebuild_$iid.log"
    if "$v.new/bin/python" -m pip install -q -e "$d[test]" > "$log" 2>&1 \
       || "$v.new/bin/python" -m pip install -q -e "$d[testing]" >> "$log" 2>&1 \
       || "$v.new/bin/python" -m pip install -q -e "$d" >> "$log" 2>&1; then
        case "$iid" in
            pytest-dev__pytest-*) : ;;
            *) "$v.new/bin/python" -m pip install -q 'pytest<8' >> "$log" 2>&1 ;;
        esac
        rm -rf "$v"; mv "$v.new" "$v"
        echo "    $iid rebuilt on $py"
    else
        echo "    !!! $iid: install failed on $py (kept old); tail:"; tail -2 "$log"
        rm -rf "$v.new"
    fi
}
export -f rebuild_one
export VENVS
ls -d "$REPOS"/*/ | xargs -P 6 -I{} bash -c 'rebuild_one "$@"' _ {}

echo "[4/4] done. venvs now:"
for pv in 3.8 3.9 3.10 3.11; do
    c=$(for v in "$VENVS"/*/bin/python; do "$v" --version 2>&1; done 2>/dev/null | grep -c " ${pv}\.")
    echo "    python${pv}: $c venvs"
done
echo "NEXT: re-run the oracle ceiling:  bash ~/Lightwork/benchmarks/pod_preflight.sh"
echo "      (its new [PASS] count is the expanded winnable pool for Tier B)"
