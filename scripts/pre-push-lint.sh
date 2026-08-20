#!/usr/bin/env bash
# Run the full CI "lint" job locally before pushing.
#
# Plain `ruff`/`pytest` miss the bespoke gates that have repeatedly failed CI
# *after* a green local run: the detect-secrets baseline check (which only scans
# git-TRACKED files), the `shell=True` source grep, and the deprecation/a11y/
# schema gates. This mirrors the lint job in .github/workflows/ci.yml so
# those surface here instead of on CI.
#
# Usage:  bash scripts/pre-push-lint.sh
# Tip:    `git add -A` first — the secret scan only sees tracked files.
#
# Exits non-zero if any gate fails; runs them all so you see every failure at once.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

ppl_state_dir="$(mktemp -d "${TMPDIR:-/tmp}/maverick-prepush.XXXXXXXX")" || {
  echo "FAILED — could not create isolated pre-push workspace."
  exit 1
}
cleanup_pre_push() {
  case "$ppl_state_dir" in
    "${TMPDIR:-/tmp}"/maverick-prepush.*) rm -rf -- "$ppl_state_dir" ;;
    *) printf 'refusing to clean unexpected path: %s\n' "$ppl_state_dir" >&2 ;;
  esac
}
trap cleanup_pre_push EXIT

# CI gates must validate repository defaults, not inherit the operator's active
# tenant/config or race through shared /tmp filenames.
export MAVERICK_HOME="$ppl_state_dir/maverick-home"
unset MAVERICK_CONFIG
mkdir -p "$MAVERICK_HOME"
ppl_output="$ppl_state_dir/gate.out"

fail=0
run() {  # run "<name>" <command...>
  local name="$1"; shift
  if "$@" >"$ppl_output" 2>&1; then
    printf '  ok   %s\n' "$name"
  else
    printf '  FAIL %s\n' "$name"; sed 's/^/       /' "$ppl_output" | tail -20
    fail=1
  fi
}

echo "== ruff =="
run "ruff check ." python -m ruff check .

echo "== vulture (dead code) =="
run "vulture" python -m vulture

echo "== bare 'import tomllib' (needs the 3.10 tomli fallback) =="
if grep -rn --include='*.py' -E '^import[[:space:]]+tomllib([[:space:]]|$)' apps packages benchmarks; then
  echo "  FAIL bare 'import tomllib' (use the try/except tomli fallback)"; fail=1
else
  echo "  ok   no bare tomllib"
fi

echo "== shell=True confined to maverick/sandbox/ =="
hits="$(grep -rn --include='*.py' 'shell=True' apps packages | grep -v '/sandbox/' | grep -v '/tests/' || true)"
if [ -n "$hits" ]; then
  echo "  FAIL shell=True outside sandbox/ (also catches the literal in comments):"; echo "$hits" | sed 's/^/       /'; fail=1
else
  echo "  ok   no stray shell=True"
fi

echo "== detect-secrets vs audited baseline (TRACKED files only) =="
if python -c 'import detect_secrets' >/dev/null 2>&1; then
  scan_dir="$ppl_state_dir/secrets"
  scan_baseline="$scan_dir/scan.baseline"
  scan_log="$scan_dir/detect-secrets.out"
  # Git Bash on Windows creates the already-isolated directory successfully
  # but returns non-zero for `mkdir -m`, producing a false gate failure. The
  # unique 0700 parent is the security boundary; use portable mkdir here.
  if ! mkdir "$scan_dir" || ! cp .secrets.baseline "$scan_baseline"; then
    echo "  FAIL could not prepare detect-secrets baseline"
    fail=1
  elif ! python -m detect_secrets scan --baseline "$scan_baseline" \
      >"$scan_log" 2>&1; then
    echo "  FAIL detect-secrets scanner error"
    sed 's/^/       /' "$scan_log" | tail -20
    fail=1
  elif SCAN_BASELINE="$scan_baseline" python - <<'PY'
import json, os, sys
old = json.load(open(".secrets.baseline"))["results"]
new = json.load(open(os.environ["SCAN_BASELINE"]))["results"]
def pairs(results):
    return {
        (filename.replace("\\", "/"), item["hashed_secret"])
        for filename, items in results.items()
        for item in items
    }
extra = sorted(pairs(new) - pairs(old))
if extra:
    print("new secret(s) not in .secrets.baseline:")
    for f, h in extra:
        print(f"  {f}  (sha1 {h[:16]}...)")
    sys.exit(1)
PY
  then
    echo "  ok   no new secrets"
  else
    echo "  FAIL new secret(s) — add '# pragma: allowlist secret' or re-audit the baseline"
    fail=1
  fi
else
  echo "  skip detect-secrets not installed (python -m pip install 'detect-secrets>=1.5')"
fi

echo "== custom CI gates =="
run "deprecations --ci"       python -m maverick.deprecations --ci
run "a11y_audit --ci"         python -m maverick.a11y_audit --ci
run "schema_migrations --ci"  python -m maverick.schema_migrations --ci

echo
if [ "$fail" -ne 0 ]; then
  echo "FAILED — fix the gates above before pushing."; exit 1
fi
echo "All lint gates passed. (The security job — pip-audit/bandit/SBOM — runs separately in CI.)"
