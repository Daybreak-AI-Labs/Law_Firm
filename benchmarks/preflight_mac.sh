#!/usr/bin/env bash
# $0 preflight: prove the whole DGM + self-learning stack works on THIS Mac
# BEFORE you spend a cent on a real SWE-bench solve. Nothing here calls a paid
# model or Modal grading -- it runs the in-process governed gate, the signed
# audit chain, and the self-learning scoreboards, then a free Modal auth ping.
#
#   ./benchmarks/preflight_mac.sh
#
# Exit 0 = the stack is healthy and you're clear to run the paid uplift.
# Any FAIL below = fix it here (free) before touching benchmarks/RUN_DGM_ON_MAC.md.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

pass=0; fail=0
step () {  # step "name" cmd...
  local name="$1"; shift
  printf '  ==> %-42s ' "$name"
  if "$@" >/tmp/preflight_$$.log 2>&1; then
    echo "PASS"; pass=$((pass+1))
  else
    echo "FAIL"; fail=$((fail+1))
    sed 's/^/       | /' /tmp/preflight_$$.log | tail -n 12
  fi
  rm -f /tmp/preflight_$$.log
}

echo "======================================================================"
echo "  DGM / self-learning PREFLIGHT  (\$0 -- no model, no Modal grading)"
echo "======================================================================"

# 1. Python can import the kernel -> the editable installs took.
step "kernel imports (maverick-core)" \
  python -c "import maverick, maverick.self_improvement, maverick.approval_signing"

# 2. The REAL governed DGM gate + Ed25519 signing + audit chain, in-process.
#    This is the same gate the paid run uses -- just with an instant score_fn.
step "governed DGM gate (3 verdicts, signed)" \
  python benchmarks/dgm_fast_proof.py

# 3. The full self-learning surface (in-process caps + one signed audit chain).
#    --no-scripts keeps it fast and dependency-light; still exercises signing.
step "self-learning stack (breadth, signed)" \
  python benchmarks/self_learning_proof.py --no-scripts

# 3b. The SWE-bench run path's own deps (NOT exercised by the in-process proofs
#     above). Catches a missing datasets/swebench BEFORE any paid solve, so you
#     never discover it mid-run.
step "SWE-bench run-path deps (datasets, swebench)" \
  python -c "import datasets, swebench"

# 4. Signing keys are present, well-formed, and not world/group readable.
step "Ed25519 operator keys (~/dgm-keys)" \
  python -c "from pathlib import Path; import binascii, stat; d=Path.home()/'dgm-keys'; h=d/'operator.priv.hex'; b=binascii.unhexlify(h.read_text().strip()); assert len(b)==32, len(b); assert stat.S_IMODE(d.stat().st_mode)==0o700, oct(stat.S_IMODE(d.stat().st_mode)); assert stat.S_IMODE(h.stat().st_mode)==0o600, oct(stat.S_IMODE(h.stat().st_mode))"

# 5. Modal reachable (grading backend). Free -- just checks the token/profile.
step "Modal auth (grading backend)" \
  modal profile current

echo "----------------------------------------------------------------------"
if [ "$fail" -eq 0 ]; then
  echo "  ALL $pass CHECKS PASSED -- stack is healthy on this Mac."
  echo "  You are clear to run the paid uplift: see benchmarks/RUN_DGM_ON_MAC.md"
  exit 0
else
  echo "  $fail check(s) FAILED, $pass passed -- fix above BEFORE any paid run."
  echo "  (Common: Modal not authed -> 'modal token new'; keys missing -> re-run setup_mac.sh)"
  exit 1
fi
