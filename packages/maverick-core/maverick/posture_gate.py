"""Hold the production security posture to a shrinking set of known failures.

The suite runs with ``MAVERICK_SECURE_DEFAULT=0``. That is a defensible choice
-- it keeps every control's explicit on/off mechanics under test -- but on its
own it means ~19,000 passing tests describe a configuration no customer runs.
Production ships secure-by-default: audit signing on, at-rest encryption on,
consent fail-closed for high-risk actions, and the tool-risk ceiling applied.

Running ``packages/maverick-core/tests`` under the real posture
(``MAVERICK_TEST_SECURE_DEFAULT=1``) produces **91 failures out of 15,869**.
That number is the point of this module. It is scoped to maverick-core because
that is where every control the posture flips lives, and because that is the
tree the baseline was actually measured over -- widening the claim to the whole
workspace without measuring it would be the defect this repo keeps finding. It was projected as two to three weeks of work on the premise that
"the breakage is the work"; reading the failures, none is a product bug. They
are tests that assert a legacy default (``test_off_by_default`` for at-rest
encryption is correct to fail when encryption defaults on) or fixtures that
never granted consent because consent used to auto-approve.

So the posture job does not block PRs yet -- a red job that blocks every merge
gets switched off, and then nothing measures it at all. Instead the failing set
is committed here and ratcheted: a NEW failure under the production posture
fails the build, and a fixed one must be removed from the baseline. The list
only shrinks.

Usage in CI::

    MAVERICK_TEST_SECURE_DEFAULT=1 pytest -q -p no:randomly | tee posture.txt
    python -m maverick.posture_gate --ci --report posture.txt

``--regen`` rewrites the baseline from a report, for when the number moves
deliberately.

**The baseline is environment-sensitive, and the committed one was measured
locally.** CI resolves starlette 1.3.x / fastapi 0.140.x where local pins are
older, and on that runner 19 further tests fail -- none of which reproduce
locally under the same flag. So the CI job runs this WITHOUT ``--ci`` for now:
it reports the delta and uploads the report, rather than gating on a
measurement taken somewhere else. Flipping it to blocking needs a baseline
regenerated from that artifact, and those 19 explained rather than absorbed. A
baseline nobody can account for is an exemption list wearing a ratchet's
clothes, which is the failure this module was written to avoid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
BASELINE = PKG_ROOT / "posture.baseline.json"

#: pytest's short-summary line: ``FAILED path::test - reason``. Split rather
#: than regex-matched on ``\S+``: a parametrized node id can contain spaces
#: (``test_x[a b]``), and a ``\S+`` capture silently truncated 16 of 91
#: entries -- an undercounted baseline is a gate that lets real drift through.
_PREFIXES = ("FAILED ", "ERROR ")


def parse_report(text: str) -> set[str]:
    """Node ids that failed or errored, from a ``pytest -q`` report."""
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        for prefix in _PREFIXES:
            if line.startswith(prefix):
                node = line[len(prefix):].split(" - ", 1)[0].strip()
                if "::" in node:
                    out.add(node)
                break
    return out


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover
        return set()
    return set(data.get("failures") or [])


def drift(now: set[str], baseline: set[str]) -> tuple[list[str], list[str]]:
    """Return (newly failing, recorded-but-now-passing)."""
    return sorted(now - baseline), sorted(baseline - now)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", required=True,
                    help="captured `pytest -q` output from the posture run")
    ap.add_argument("--ci", action="store_true", help="exit non-zero on drift")
    ap.add_argument("--regen", action="store_true", help="rewrite the baseline")
    args = ap.parse_args(argv)

    path = Path(args.report)
    if not path.is_file():
        print(f"posture-gate: ERROR -- no report at {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    if "passed" not in text and "failed" not in text and "error" not in text:
        # A truncated or empty report must not read as "nothing failed" -- that
        # is the vacuous-gate failure this repo has been closing everywhere.
        print("posture-gate: ERROR -- the report contains no pytest summary; "
              "the posture run did not complete", file=sys.stderr)
        return 2

    now = parse_report(text)
    if args.regen:
        BASELINE.write_text(json.dumps({
            "_comment": "Tests that fail under the PRODUCTION security posture "
                        "(MAVERICK_TEST_SECURE_DEFAULT=1). A debt register to "
                        "shrink, not an exemption list. Regenerate with "
                        "`python -m maverick.posture_gate --regen --report <f>`.",
            "count": len(now),
            "failures": sorted(now),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"posture-gate: baseline rewritten -- {len(now)} failure(s)")
        return 0

    baseline = load_baseline()
    new, fixed = drift(now, baseline)
    print(f"posture-gate: {len(now)} failing under the production posture "
          f"({len(baseline)} recorded, {len(new)} new, {len(fixed)} fixed)")

    if fixed:
        print(f"\nposture-gate: {len(fixed)} recorded failure(s) now pass -- "
              "remove them from the baseline so it keeps shrinking:",
              file=sys.stderr)
        for node in fixed:
            print(f"  {node}", file=sys.stderr)
    if new:
        print(f"\nposture-gate: {len(new)} NEW failure(s) under the production "
              "posture. Production ships secure-by-default, so this is a test "
              "that does not hold in the configuration customers run:",
              file=sys.stderr)
        for node in new:
            print(f"  {node}", file=sys.stderr)
    if (new or fixed) and args.ci:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
