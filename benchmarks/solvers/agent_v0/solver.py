"""Agent-based DGM solver v0 -- a BOUNDED coding-agent solver with real headroom.

The naive single-shot ``baseline_v0`` resolves 0/6 real SWE-bench Verified bugs,
so the governed DGM loop has nothing to measurably improve (you can't show uplift
from zero). This solver instead drives the PROVEN maverick coding agent
(``swebench_governed.llm_proposer`` -- opaque anti-cheat, worktree-diff capture,
per-instance sandbox), so it actually resolves some real instances: a NONZERO
baseline with room to grow.

What the DGM improves is the AGENT-BUDGET POLICY below -- the self-improving
harness idea made concrete: the proposer edits these knobs (and only these; the
agent machinery is outside the editable ``solver.py`` surface, so it can't be
gamed) to resolve more held-out bugs. ``solve(instance) -> str`` returns a
unified diff or ``""`` and NEVER raises (an exception would crash the uplift
eval); offline/keyless falls back to ``""``.
"""
from __future__ import annotations

import os

# --- improvable agent-budget policy (the DGM tunes THESE) ---------------------
# Deliberately conservative so there is headroom: more steps, more independent
# attempts, and a retry generally resolve more instances at more cost -- exactly
# the trade the governed loop gets to make and then PROVE on held-out bugs.
MAX_STEPS = 55            # agent turn budget per instance
BEST_OF_N = 1             # independent attempts; the resolving one is kept
RETRY_ON_EMPTY = True     # one more attempt if the first emits no diff


def _clamp(value: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def solve(instance) -> str:
    """Run the bounded coding agent on ``instance`` and return its unified diff.

    Never raises: any failure (no key, agent error, import problem) becomes ``""``
    so a single bad instance can't crash the DGM's uplift evaluation."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        return ""
    prev = {k: os.environ.get(k) for k in
            ("MAVERICK_MAX_STEPS", "MAVERICK_BEST_OF_N", "MAVERICK_RETRY_ON_EMPTY")}
    try:
        # Cost guardrails: per-attempt $ is already bounded by the agent's
        # MAVERICK_INSTANCE_HARD_CAP, so steps can't run cost away; BEST_OF_N is
        # the real multiplier, so cap it at 2 -- the proposer may tune the budget
        # up, but not into a runaway.
        os.environ["MAVERICK_MAX_STEPS"] = str(_clamp(MAX_STEPS, 1, 120))
        os.environ["MAVERICK_BEST_OF_N"] = str(_clamp(BEST_OF_N, 1, 2))
        os.environ["MAVERICK_RETRY_ON_EMPTY"] = "1" if RETRY_ON_EMPTY else "0"
        # benchmarks/ is already on sys.path in the DGM process (dgm_uplift
        # inserts it); the agent machinery lives there, OUTSIDE this editable
        # solver so the proposer can only tune the policy above, not the harness.
        from swebench_governed import llm_proposer
        return llm_proposer(instance) or ""
    except Exception:
        return ""
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
