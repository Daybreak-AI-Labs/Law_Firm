#!/bin/bash
# Diagnostics for a pod self-test failure. Prints everything needed to
# pinpoint why an instance graded "ungradable-here". Read-only + one
# verbose dep-install retry; spends nothing.

echo "=== python / pytest ==="
python3 --version
python3 -m pytest --version 2>&1 | head -2

echo "=== oracle preflight log (full) ==="
cat /tmp/oracle.log 2>/dev/null

echo "=== preflight forensics (if any) ==="
for f in /tmp/forensics/*.json; do
    [ -e "$f" ] || continue
    echo "--- $f"; cat "$f"
done

echo "=== direct baseline test run: pylint-8898 ==="
if cd ~/swebench_stage/repos/pylint-dev__pylint-8898 2>/dev/null; then
    python3 -m pytest tests/config/test_config.py -q 2>&1 | tail -15
else
    echo "repo dir missing"
fi

echo "=== imports ==="
python3 -c "import pylint, astroid; print('pylint OK:', pylint.__version__)" 2>&1
python3 -c "import flask; print('flask OK:', flask.__version__)" 2>&1
python3 -c "import requests; print('requests OK:', requests.__version__)" 2>&1

echo "=== requests family install retry (verbose tail) ==="
d=$(ls -d ~/swebench_stage/repos/psf__requests-* 2>/dev/null | head -1)
echo "dir: ${d:-none found}"
[ -n "$d" ] && python3 -m pip install -e "$d" 2>&1 | tail -8

echo "=== END - send everything above to Claude ==="
