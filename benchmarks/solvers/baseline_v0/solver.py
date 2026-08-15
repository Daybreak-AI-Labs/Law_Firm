"""Baseline DGM solver v0 -- a deliberately SIMPLE single-shot LLM patch author.

This is the artifact the governed Darwin-Godel loop (``benchmarks/dgm_live.py``)
improves. ``solve(instance) -> str`` returns a unified diff for the instance's
repo, or ``""`` on any failure (it NEVER raises -- an exception out of a solver
would crash the whole uplift eval, so offline / keyless / error paths all fall
back to an empty patch = "no fix proposed").

It is intentionally naive so the loop has real headroom to improve it:
  * ONE model call, no retry, no self-repair;
  * no test execution / feedback loop (it never runs the tests it is fixing);
  * a terse prompt;
  * the cheapest role model (``summarizer`` -> Haiku by default).

The model id comes from ``maverick.config.get_role_model`` with a fallback to
``maverick.llm.ROLE_MODELS`` -- never a hard-coded id (kernel rule 2). Spend is
bounded by a small per-call ``maverick.budget.Budget`` (every cap raised above
what one call can cross, since ``record_tokens`` enforces ALL caps, not just
dollars). When the actual per-call cost is wanted for run accounting, set
``MAVERICK_DGM_SPEND_LOG`` to a file path and each call appends its real dollar
cost (the driver sums it; a no-op when unset).
"""
from __future__ import annotations

import os
from pathlib import Path

# The cheapest per-role default in maverick.llm.ROLE_MODELS is ``summarizer``
# (Haiku, $1/$5 per Mtok) -- the sensible pick for a throwaway baseline solver.
_ROLE = "summarizer"
_MAX_TOKENS = 3000
# Per-call caps. record_tokens() enforces EVERY cap at record time, so each is
# raised comfortably above what one 3k-output call can reach (a cap crossed on
# an axis you didn't mean to bound still kills the call).
_MAX_DOLLARS = 0.10
_MAX_OUTPUT_TOKENS = 8000
_MAX_INPUT_TOKENS = 400_000
_MAX_WALL_SECONDS = 180.0
_MAX_TEST_SRC_BYTES = 4000


def _model() -> str | None:
    """Cheapest role model, config-overridable; never a hard-coded id."""
    from maverick.config import get_role_model
    from maverick.llm import ROLE_MODELS
    return get_role_model(_ROLE) or ROLE_MODELS.get(_ROLE)


def _failing_test_sources(instance) -> list[tuple[str, str]]:
    """The (path, source) of each small failing-test file, best-effort."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tid in getattr(instance, "fail_to_pass", None) or []:
        rel = str(tid).split("::", 1)[0].strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        p = Path(instance.repo_path) / rel
        try:
            if p.is_file() and p.stat().st_size <= _MAX_TEST_SRC_BYTES:
                out.append((rel, p.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def _extract_diff(text: str) -> str:
    """Pull a unified diff out of a possibly fenced model reply."""
    text = (text or "").strip()
    if "```" in text:
        for part in text.split("```"):
            body = part.removeprefix("diff").removeprefix("patch").strip("\n")
            if body.lstrip().startswith(("diff --git", "--- ")):
                return body
    return text


def _log_spend(dollars: float) -> None:
    """Append this call's real $ cost to MAVERICK_DGM_SPEND_LOG (no-op if unset)."""
    path = os.environ.get("MAVERICK_DGM_SPEND_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{dollars:.6f}\n")
    except OSError:
        pass


def _build_prompt(instance) -> tuple[str, str]:
    system = (
        "You are a bug-fixing coder. Given a bug brief and the failing test "
        "file(s), output ONLY a unified diff (git format, `diff --git a/... "
        "b/...`) that patches the project SOURCE so the failing tests pass. "
        "Never edit the tests. No prose, no explanation -- just the diff.")
    parts = [f"Bug brief:\n{getattr(instance, 'brief', '') or '(none)'}\n"]
    for rel, src in _failing_test_sources(instance):
        parts.append(f"Failing test file `{rel}`:\n```python\n{src}\n```\n")
    parts.append("Reply with the unified diff only.")
    return system, "\n".join(parts)


def solve(instance) -> str:
    """Return a unified diff fixing ``instance``'s bug, or ``""``. Never raises.

    Offline / keyless -> ``""`` immediately, so the governed offline eval paths
    never touch the network or crash.
    """
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        return ""
    try:
        from maverick.budget import Budget
        from maverick.llm import LLM

        system, user = _build_prompt(instance)
        budget = Budget(
            max_dollars=_MAX_DOLLARS,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            max_input_tokens=_MAX_INPUT_TOKENS,
            max_wall_seconds=_MAX_WALL_SECONDS,
            max_tool_calls=1,
        )
        resp = LLM().complete(
            system, [{"role": "user", "content": user}],
            budget=budget, max_tokens=_MAX_TOKENS, model=_model())
        _log_spend(budget.dollars)
        return _extract_diff(getattr(resp, "text", "") or "")
    except Exception:
        return ""
