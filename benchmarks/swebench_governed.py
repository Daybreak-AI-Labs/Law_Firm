#!/usr/bin/env python3
"""SWE-bench under governance: score real bug-fix patches through the real gate.

The naked SWE-bench number ("X% resolved") measures a proposer's raw coding
ability. Lightwork's differentiator is not that number in isolation -- it is that
*every accepted fix passed a governance chain*: an anti-cheat boundary that
structurally refuses test/config edits, a held-out regression gate on the
project's own tests, a capability non-escalation check, and a human-signed,
reversible, audited promotion. No other entry on the SWE-bench leaderboard reports
its result under a signed reference monitor.

This module is the wiring. Given a SWE-bench instance (issue, repo@base_commit,
``FAIL_TO_PASS`` / ``PASS_TO_PASS`` test ids, and a candidate patch) it returns a
:class:`GovernedResult` and, on success, records a signed promotion to a ledger.

    resolved_under_governance = boundary_ok AND tests_resolved AND gate_promoted

The candidate patch comes from a *proposer*. Two are provided:

* ``oracle`` -- returns the instance's gold patch. Proves the governance pipeline
  end-to-end with no LLM and no key (the gold patch is Princeton's, the tests are
  the real project's). Use it to validate the harness.
* ``llm`` -- generates a patch with the coding-mode agent. THIS is the capability
  measurement and needs ``ANTHROPIC_API_KEY`` (or another provider). It is a thin
  seam here; the agent loop lives in ``benchmarks/swe_bench.py``.

External SWE-bench grading requires an isolated sandbox by default: tests run
via ``sandbox.exec`` only after a caller supplies a sandbox factory (for example
a container backend). The no-Docker/host-exec path is reserved for trusted local
fixtures and requires an explicit ``--allow-host-exec`` acknowledgement.

    python benchmarks/swebench_governed.py --manifest instances.jsonl \
        --proposer oracle --keys ./keys --ledger ./swebench_ledger.json

Manifest format is the one ``benchmarks/fetch_swe_bench_verified.py`` emits: one
JSON object per line with ``instance_id, repo, base_commit, brief, fail_to_pass,
pass_to_pass, gold_patch, language`` and a local ``repo_path`` (a checked-out tree
at ``base_commit``) or a ``repos/<instance_id>`` sibling directory.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "packages" / "maverick-core"))

log = logging.getLogger(__name__)


@dataclass
class Instance:
    """One SWE-bench task."""

    instance_id: str
    repo_path: Path                 # a checked-out tree at base_commit
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    gold_patch: str = ""
    brief: str = ""
    language: str = "python"
    # The GRADER's test fixture (SWE-bench semantics): applied to BOTH arms
    # before running, because the graded FAIL_TO_PASS tests are usually added
    # or updated by it. Distinct from the candidate patch, which the anti-cheat
    # boundary still refuses if it touches tests.
    test_patch: str = ""
    # False when the instance can't yield a trustworthy resolve verdict here --
    # e.g. all its FAIL_TO_PASS ids were dropped as malformed at load, so a
    # candidate would be graded only on PASS_TO_PASS (which pass regardless of
    # the fix). govern_candidate marks such an instance ungradable, never
    # resolved.
    gradable: bool = True
    ungradable_reason: str = ""

    @property
    def total_tests(self) -> int:
        return len(self.fail_to_pass) + len(self.pass_to_pass)


@dataclass
class GovernedResult:
    """The governance verdict on one candidate patch for one instance."""

    instance_id: str
    boundary_ok: bool = False       # anti-cheat: no test/config edits, not gold-copy
    boundary_reason: str = ""
    baseline_score: float = 0.0     # pre-patch pass-rate (FAIL_TO_PASS should fail)
    candidate_score: float = 0.0    # post-patch pass-rate
    tests_resolved: bool = False    # all FAIL_TO_PASS pass AND all PASS_TO_PASS pass
    # True when the env can't grade the instance at all (baseline can't run its
    # own passing tests). Reported separately from unresolved; never resolved.
    ungradable: bool = False
    capability_widens: bool | None = None
    promoted: bool = False          # the governed gate signed + recorded it
    approver_id: str | None = None
    samples: int = 0
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def resolved_under_governance(self) -> bool:
        return self.boundary_ok and self.tests_resolved and self.promoted

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "resolved_under_governance": self.resolved_under_governance,
            "boundary_ok": self.boundary_ok,
            "tests_resolved": self.tests_resolved,
            "promoted": self.promoted,
            "baseline_score": round(self.baseline_score, 4),
            "candidate_score": round(self.candidate_score, 4),
            "capability_widens": self.capability_widens,
            "approver_id": self.approver_id,
            "samples": self.samples,
            "reason": self.reason,
            "warnings": self.warnings,
        }


# --- proposers ----------------------------------------------------------------

def oracle_proposer(inst: Instance) -> str:
    """The gold patch. Proves the pipeline with no LLM; the fix is Princeton's."""
    return inst.gold_patch


def _neutralized_filter_config(repo: Path) -> list[str]:
    """Return ``git -c`` args that turn worktree-declared filters into no-ops.

    The benchmark agent controls the checkout, including ``.gitattributes`` and
    ``.git/info/attributes``. ``git add -N``/``git diff`` may invoke clean
    filters named by those files, so discover any literal ``filter=<name>``
    declarations and override those filter drivers before asking host git for a
    patch.
    """
    import re

    names: set[str] = set()
    candidates = list(repo.rglob(".gitattributes"))
    info_attributes = repo / ".git" / "info" / "attributes"
    if info_attributes.exists():
        candidates.append(info_attributes)

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(re.findall(r"(?:^|\s)filter=([A-Za-z0-9_.-]+)(?=$|\s)", text))

    args: list[str] = []
    for name in sorted(names):
        args.extend([
            "-c", f"filter.{name}.clean=cat",
            "-c", f"filter.{name}.smudge=cat",
            "-c", f"filter.{name}.process=",
            "-c", f"filter.{name}.required=false",
        ])
    return args


def _worktree_diff(repo: Path) -> str:
    """The agent's edits vs HEAD as a raw, git-appliable unified diff.

    Uses ``git diff HEAD`` (not bare ``git diff``) so it captures the change
    whether the agent left it UNSTAGED or ``git add``-ed it -- a coding agent
    often stages its edit, which bare ``git diff`` (working-vs-index) then shows
    as EMPTY, silently dropping a perfectly good fix and forcing the fallback to
    the model's hand-written prose diff (which LLMs miscount -> "corrupt patch").
    ``git add -N`` first so brand-new files also appear. When HEAD is the grader
    test-fixture commit, this excludes test_patch by construction. Literal git
    output, so it always applies with ``git apply -p1``. Hardened env (no
    ext-diff/textconv, no global/system/local config) mirrors
    ``swe_bench._git_diff`` while also refusing repository-local filter
    configuration from the agent-controlled checkout (a filter driver could
    otherwise run arbitrary commands during the diff)."""
    import subprocess

    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_LOCAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
           "PATH": os.environ.get("PATH", "")}
    try:
        filter_config = _neutralized_filter_config(repo)
        git = ["git", *filter_config, "-C", str(repo)]
        subprocess.run([*git, "add", "-N", "."],
                       capture_output=True, timeout=60, env=env)
        out = subprocess.run(
            [*git, "diff", "HEAD", "--no-ext-diff", "--no-textconv"],
            capture_output=True, text=True, timeout=60, env=env)
        return out.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _desanitize_csv_patch(diff: str) -> str:
    """Undo ``swe_bench._sanitize_patch_for_csv``'s formula-injection guard: a
    leading ``'`` prepended when the diff's first non-whitespace char is one of
    ``= + - @``. That guard is correct for a CSV cell but makes the diff
    non-appliable, which is why ``row.predicted_patch`` must never go straight
    to ``git apply``. Fallback only -- the worktree diff is preferred."""
    if diff[:1] == "'" and diff[1:2] in ("=", "+", "-", "@"):
        return diff[1:]
    return diff


# Benchmark env the coding agent needs, applied with setdefault so an operator
# or the CLI can override any of them. These mirror
# benchmarks/RUNBOOK_SWE_BENCH_VERIFIED.md: OPAQUE=1 is the anti-cheat honesty
# gate (blocks test-file reads / git log -p / network), MAX_STEPS bounds turns,
# LONG_CMD_TIMEOUT lets pytest run through the shell tool. Consent is deliberately
# NOT part of this unconditional default set: agent_v0 consumes untrusted
# benchmark rows, so llm_proposer may auto-approve shell only after the sandbox
# gate has verified an isolated backend (or the operator explicitly opted in to
# host execution), scopes it to a container-required sandbox, and restores it
# after the run.
_BENCH_ENV = {
    "MAVERICK_CODING_MODE": "1",
    "MAVERICK_BENCHMARK_OPAQUE": "1",
    "MAVERICK_USE_SKILLS": "0",
    "MAVERICK_MAX_STEPS": "25",
    "MAVERICK_LONG_CMD_TIMEOUT": "600",
}


_ISOLATED_SANDBOX_BACKENDS = {
    "devcontainer",
    "docker",
    "firecracker",
    "gvisor",
    "kubernetes",
    "modal",
    "podman",
}
_HOST_EXEC_OPT_IN = "MAVERICK_SWEBENCH_ALLOW_HOST_EXEC"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_sandbox_backend() -> str:
    try:
        from maverick.config import load_config
        cfg = (load_config() or {}).get("sandbox", {}) or {}
    except Exception:
        cfg = {}
    return str(cfg.get("backend") or "local").strip().lower()


def _bench_sandbox_is_isolated(backend: str) -> bool:
    return backend in _ISOLATED_SANDBOX_BACKENDS or backend.startswith("ep:")


def _ensure_untrusted_agent_sandbox() -> None:
    """Fail closed before running the coding agent on untrusted instances.

    The coding agent may execute shell commands suggested by benchmark-controlled
    briefs/repositories.  The default Maverick sandbox backend is ``local``,
    which runs those commands on the host.  Require an isolated backend unless
    the operator has made an explicit, auditable host-exec opt-in.
    """
    backend = _configured_sandbox_backend()
    if _bench_sandbox_is_isolated(backend) or _truthy_env(_HOST_EXEC_OPT_IN):
        return
    raise RuntimeError(
        "refusing to run agent_v0 on an untrusted benchmark instance with "
        f"sandbox backend {backend!r}; configure [sandbox] backend to docker, "
        "podman, gvisor, devcontainer, kubernetes, firecracker, modal, or an "
        f"entry-point backend, or set {_HOST_EXEC_OPT_IN}=1 to explicitly "
        "accept host shell execution."
    )

# Round 4 (lever 2): appended to the brief on the retry attempt when the first
# run produced no patch at all. Kept terse and imperative -- the agent already
# has the full brief; this only fixes the missing-deliverable failure mode.
_RETRY_ON_EMPTY_ADDENDUM = (
    "\n\n---\nIMPORTANT: your previous attempt produced NO patch. You MUST "
    "finish this attempt with a concrete unified diff (git-format) OR staged "
    "edits to the working tree. Do not stop until you have written a real code "
    "change that addresses the issue."
)


def llm_proposer(inst: Instance) -> str:
    """Generate a patch with the coding-mode agent (needs a provider key).

    Adapter over the proven ``benchmarks/swe_bench.py`` solver (``run_maverick``:
    coding mode, best-of-N via ``MAVERICK_BEST_OF_N``). Runs the agent with the
    instance's checked-out tree as cwd, then takes the agent's proposed fix as
    the de-sanitized prose diff (coding mode's deliverable IS the emitted unified
    diff; ``row.predicted_patch`` is CSV-sanitized -- a leading ``'`` guard byte
    would make ``git apply`` reject it, so :func:`_desanitize_csv_patch` strips
    it). Falls back to the agent's actual working-tree edits (:func:`_worktree_diff`,
    always ``git apply``-able) when no prose diff was emitted.

    Sets the benchmark env (:data:`_BENCH_ENV`) via ``setdefault``. Fails closed
    up front (:func:`_ensure_untrusted_agent_sandbox`) unless an isolated sandbox
    backend is configured or the operator made an explicit host-execution opt-in.
    Only then, when the operator did not explicitly choose a consent mode, does
    the runner temporarily grant shell consent -- and still scoped to a
    container-required sandbox -- so model-generated shell commands never default
    to the unsandboxed local host backend.

    Writes a per-instance forensics sidecar (``<ledger-dir>/forensics/`` or
    ``MAVERICK_SWEBENCH_FORENSICS``) with the model's result text, both patch
    captures, cost/turns. Resets the tree afterward (``git reset --hard`` + ``git
    clean -fdx``): point ``repo_path`` ONLY at a dedicated instance checkout.
    Fail-loud on a missing key -- a keyless run must not silently score 0.
    """
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        raise RuntimeError(
            "llm_proposer needs a provider key (set ANTHROPIC_API_KEY, or set "
            "MAVERICK_PROVIDER_READY=1 if another provider is configured in "
            "~/.maverick/config.toml). Use --proposer oracle for the keyless "
            "pipeline proof.")
    # Fail closed before any env mutation or agent run: refuse untrusted
    # benchmark rows unless an isolated sandbox backend is configured (or the
    # operator opted in to host exec).
    _ensure_untrusted_agent_sandbox()
    env_restore: dict[str, str | None] = {}
    for k, v in _BENCH_ENV.items():
        if k not in os.environ:
            env_restore[k] = None
            os.environ[k] = v

    auto_approve_shell = "MAVERICK_CONSENT_MODE" not in os.environ
    if auto_approve_shell:
        env_restore["MAVERICK_CONSENT_MODE"] = None
        os.environ["MAVERICK_CONSENT_MODE"] = "auto-approve"

    from maverick.config import reset_config_cache

    def restore():
        return None
    row = tree_diff = prose_diff = None
    attempt1_cost = 0.0
    base_sha = ""
    try:
        from swe_bench import run_maverick

        base_sha = _git_head(inst.repo_path)
        # Give the agent the GRADED tests so its own test verifier gives REAL
        # feedback. The FAIL_TO_PASS/PASS_TO_PASS the agent is told to satisfy are
        # added by the grader's test_patch, which does NOT exist at base_commit --
        # so without this the agent runs "test not found" -> 0/N on everything,
        # concludes its fix broke the suite, thrashes, and emits no diff (the
        # observed failure). Commit test_patch as a fixture on top of base_commit:
        # the tests now exist and run, opaque mode still blocks READING their
        # source, and committing it keeps the candidate diff clean (git diff against
        # this commit excludes the fixture).
        fixture_sha = base_sha
        if (inst.test_patch or "").strip() and base_sha:
            if _commit_fixture(inst.repo_path, inst.test_patch):
                fixture_sha = _git_head(inst.repo_path)

        # Point the agent's sandbox at THIS instance's checkout. build_sandbox()
        # reads [sandbox] workdir from config (default ~/maverick-workspace) and
        # NEVER consults cwd -- the overlay is the one runtime knob it honors.
        restore = _point_sandbox_at(inst.repo_path, require_container=auto_approve_shell)

        def _attempt(brief_text: str):
            # Reset to the fixture commit (base + test_patch), not bare base, so
            # run_maverick's pre-run reset keeps the graded tests present.
            r = run_maverick(
                inst.instance_id, brief_text,
                fail_to_pass=inst.fail_to_pass, pass_to_pass=inst.pass_to_pass,
                gold_patch=inst.gold_patch, language=inst.language,
                base_commit=fixture_sha)
            # Candidate = the agent's source edits ONLY: diff against the fixture
            # commit excludes the committed test_patch. Fall back to the desanitized
            # prose diff if the tree is clean (agent emitted a diff without editing).
            td = _worktree_diff(inst.repo_path)
            pd = _desanitize_csv_patch(getattr(r, "predicted_patch", "") or "")
            return r, td, pd

        row, tree_diff, prose_diff = _attempt(inst.brief)
        chosen = tree_diff if tree_diff.strip() else prose_diff
        # Round 4 (lever 2): retry ONCE if the first attempt produced no patch
        # at all (neither a working-tree diff nor a prose diff). A single empty
        # attempt is often a formatting miss, not a capability ceiling; a
        # pointed second try recovers a real fraction of them. Gated by
        # MAVERICK_RETRY_ON_EMPTY (default ON; "0" disables).
        if (os.environ.get("MAVERICK_RETRY_ON_EMPTY", "1") != "0"
                and not (chosen or "").strip()):
            attempt1_cost = float(getattr(row, "cost_dollars", 0.0) or 0.0)
            # Hard reset BETWEEN attempts to the fixture commit (NOT base_sha):
            # drop attempt 1's edits but keep the graded-test fixture so
            # attempt 2 still gets real verifier feedback.
            _hard_reset(inst.repo_path, fixture_sha)
            row, tree_diff, prose_diff = _attempt(inst.brief + _RETRY_ON_EMPTY_ADDENDUM)
            chosen = tree_diff if tree_diff.strip() else prose_diff
        return chosen
    finally:
        # Sidecar cost = attempt1 + attempt2 (extra_cost) so total-spend
        # accounting via _instance_spend sums both attempts.
        _dump_forensics(inst, row, tree_diff, prose_diff, extra_cost=attempt1_cost)
        restore()
        reset_config_cache()
        for k, old in env_restore.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        # Restore the tree to the pristine base_commit (drop the fixture commit
        # AND the agent's edits) so the governed evaluator copies a clean base.
        _hard_reset(inst.repo_path, base_sha)


def _git_head(repo: Path) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _commit_fixture(repo: Path, test_patch: str) -> bool:
    """Apply ``test_patch`` and commit it as a throwaway fixture on top of the
    current commit. Returns True on success. Best-effort: on any failure the
    caller proceeds without the fixture (agent gets weaker feedback, still
    correct)."""
    import subprocess

    from maverick.self_modify_eval import git_apply
    if not getattr(git_apply(test_patch, repo), "ok", False):
        log.warning("_commit_fixture: test_patch did not apply to %s", repo)
        return False
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "swebench", "GIT_AUTHOR_EMAIL": "swebench@local",
           "GIT_COMMITTER_NAME": "swebench", "GIT_COMMITTER_EMAIL": "swebench@local",
           "PATH": os.environ.get("PATH", "")}
    try:
        subprocess.run(["git", "-C", str(repo), "add", "-A"],
                       capture_output=True, timeout=60, env=env, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                        "swebench: grader test fixture (throwaway)"],
                       capture_output=True, timeout=60, env=env, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        log.warning("_commit_fixture: could not commit fixture in %s", repo, exc_info=True)
        return False


def _hard_reset(repo: Path, sha: str) -> None:
    """Reset ``repo`` hard to ``sha`` (or HEAD) and clean untracked; best-effort."""
    import subprocess
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": os.environ.get("PATH", "")}
    try:
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", sha or "HEAD"],
                       capture_output=True, timeout=60, env=env)
        subprocess.run(["git", "-C", str(repo), "clean", "-fdx"],
                       capture_output=True, timeout=120, env=env)
    except (subprocess.SubprocessError, OSError):
        log.warning("_hard_reset: could not reset %s", repo, exc_info=True)


def _point_sandbox_at(repo: Path, *, require_container: bool = False):
    """Redirect the agent's sandbox workdir to ``repo`` via a config overlay,
    reset the config cache so it takes effect, and return a restore() callable.
    build_sandbox() reads ``[sandbox] workdir`` from config and never consults
    cwd; a MAVERICK_CONFIG_OVERLAY file is the runtime knob it honors. When
    ``require_container`` is true, the overlay also refuses the unsandboxed
    local backend before benchmark shell consent is auto-approved."""
    import tempfile

    from maverick.config import reset_config_cache
    overlay = Path(tempfile.gettempdir()) / f"mav_sbx_overlay_{os.getpid()}.toml"
    body = f'[sandbox]\nworkdir = "{repo}"\n'
    if require_container:
        body += "require_container = true\n"
    overlay.write_text(body, encoding="utf-8")
    prev = os.environ.get("MAVERICK_CONFIG_OVERLAY")
    os.environ["MAVERICK_CONFIG_OVERLAY"] = str(overlay)
    reset_config_cache()

    def restore():
        if prev is None:
            os.environ.pop("MAVERICK_CONFIG_OVERLAY", None)
        else:
            os.environ["MAVERICK_CONFIG_OVERLAY"] = prev
        try:
            overlay.unlink()
        except OSError:
            pass
    return restore


def _dump_forensics(inst, row, tree_diff, prose_diff, extra_cost: float = 0.0) -> None:
    """Best-effort per-instance forensics sidecar; never raises into the run.

    ``extra_cost`` is added to the row's own cost before it is written -- used by
    the retry-on-empty path so the sidecar (which ``_instance_spend`` reads back
    for total-spend accounting) reflects attempt1 + attempt2, not just the second
    attempt's row."""
    try:
        base = os.environ.get("MAVERICK_SWEBENCH_FORENSICS")
        outdir = Path(base) if base else (_FORENSICS_DIR or Path.cwd() / "forensics")
        outdir.mkdir(parents=True, exist_ok=True)
        rec = {
            "instance_id": inst.instance_id,
            "cost_dollars": round(
                float(getattr(row, "cost_dollars", 0.0) or 0.0)
                + float(extra_cost or 0.0), 4),
            "tokens_out": int(getattr(row, "tokens_out", 0) or 0),
            "outcome": getattr(row, "outcome", ""),
            "result_text": (getattr(row, "extra", {}) or {}).get("run_text", ""),
            "tree_diff": tree_diff or "",
            "prose_diff_desanitized": prose_diff or "",
            "chosen": "tree" if (tree_diff or "").strip() else "prose",
        }
        (outdir / f"{inst.instance_id}.json").write_text(
            json.dumps(rec, indent=2), encoding="utf-8")
    except Exception:
        log.debug("forensics dump failed for %s", inst.instance_id, exc_info=True)


_FORENSICS_DIR: Path | None = None


PROPOSERS: dict[str, Callable[[Instance], str]] = {
    "oracle": oracle_proposer,
    "llm": llm_proposer,
}


# --- the governed scorer ------------------------------------------------------

def _score(workdir: Path, inst: Instance, sandbox, *, timeout: float
           ) -> tuple[float, bool, int, int, int]:
    """Run the instance's tests in ``workdir``; return
    ``(score, all_pass, samples, fail_to_pass_passing, fail_to_pass_failing)``.

    The FAIL_TO_PASS pass/fail counts are exposed so the baseline gate can
    require the GRADED FAIL_TO_PASS tests specifically to FAIL at baseline (the
    SWE-bench definition of "there is a bug"). Both are needed: a `-k` substring
    selection can run a passing SIBLING, so ``fail_to_pass_passing`` alone can
    reach the requested count even while the real target fails -- the failing
    count is what distinguishes "all targets pass (mis-seeded)" from "a target
    fails (real bug) but a sibling also passed".

    Prepends the isolated copy (and its ``src/``) to PYTHONPATH so the copy
    SHADOWS any cross-instance editable install (a ``__editable__.*.pth`` on
    sys.path). Without this, a src-layout repo (no package at the copy root)
    imports the ORIGINAL checkout via the editable finder, so both arms test the
    SAME unpatched tree and the candidate patch is invisible -- a silent
    honesty inversion. Best-effort for the no-Docker path; per-instance Docker
    (no cross-instance .pth at all) is the real isolation."""
    from maverick.coding_mode import run_failing_tests
    prev = os.environ.get("PYTHONPATH")
    parts = [str(workdir), str(Path(workdir) / "src")]
    os.environ["PYTHONPATH"] = os.pathsep.join(parts + ([prev] if prev else []))
    try:
        res = run_failing_tests(
            workdir, inst.fail_to_pass, inst.pass_to_pass, sandbox,
            timeout=timeout, language=inst.language)
    finally:
        if prev is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = prev
    samples = res.fail_to_pass_total + res.pass_to_pass_total
    return (res.score, res.all_pass, samples,
            res.fail_to_pass_passing, res.fail_to_pass_failing)


# Grading-sensitive files: the base defensive_validate only WARNS on these, but
# a candidate that edits conftest.py / pytest.ini / setup.cfg [tool:pytest] /
# pyproject can make the graded test pass WITHOUT fixing the bug. A governed,
# un-inflatable number must refuse them outright (stricter than the harness).
_GRADING_SENSITIVE_RE = re.compile(
    r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|setup\.py|setup\.cfg|"
    r"pyproject\.toml|requirements[^/]*\.txt|Makefile)$")


def _grading_sensitive_paths(patch: str) -> list[str]:
    """Paths in ``patch`` that could rig the grader rather than fix the bug."""
    from maverick.self_modify import _diff_paths
    return sorted({p for p in _diff_paths(patch or "") if _GRADING_SENSITIVE_RE.search(p)})


def govern_candidate(
    inst: Instance,
    patch: str,
    *,
    keys_dir: Path,
    ledger=None,
    workroot: Path,
    sandbox_factory: Callable[[Path], object] | None = None,
    materialize: Callable[[Path, Path], None] | None = None,
    apply_fn: Callable[[str, Path], object] | None = None,
    timeout: float = 600.0,
    controller=None,
    check_gold_overlap: bool = True,
    allow_host_exec: bool = False,
) -> GovernedResult:
    """Drive one candidate patch through the full governance chain. Never raises.

    Order (each gate independent, fail-closed):
      1. anti-cheat boundary (``defensive_validate``) -- refuse edits to tests /
         setup / requirements, and near-verbatim gold copies;
      2. held-out fitness -- baseline vs candidate on the project's own tests in
         isolated copies (``resolved`` = every FAIL_TO_PASS + PASS_TO_PASS passes);
      3. capability non-escalation proof;
      4. human-signed, reversible promotion recorded to the ledger.

    ``check_gold_overlap=False`` disables ONLY the gold-copy similarity check --
    for the ``oracle`` proposer, whose candidate IS the gold patch by definition
    (overlap is 100% and meaningless there). Test/setup-edit refusal always
    stays on; LLM runs must keep the overlap check armed.
    """
    from maverick.coding_mode import defensive_validate
    from maverick.self_modify_capability import capability_delta
    from maverick.self_modify_eval import _default_materialize, git_apply, resolve_eval_sandbox

    out = GovernedResult(instance_id=inst.instance_id)
    if not (patch or "").strip():
        out.reason = "empty patch (proposer produced no diff)"
        return out

    materialize = materialize or _default_materialize
    if sandbox_factory is None and not allow_host_exec:
        def make_sb(wd):
            raise RuntimeError(
                "external SWE-bench evaluation requires an explicit sandbox_factory "
                "or --allow-host-exec for trusted local fixtures; refusing to run "
                "untrusted tests on the host")
    else:
        make_sb = sandbox_factory or (lambda wd: resolve_eval_sandbox(None, Path(wd)))

    # 1. Anti-cheat boundary -- structural, before any test is run.
    dv = defensive_validate(patch, fail_to_pass=inst.fail_to_pass,
                            pass_to_pass=inst.pass_to_pass,
                            gold_patch=inst.gold_patch if check_gold_overlap else "")
    out.warnings = list(dv.warnings)
    out.boundary_ok = bool(dv.ok)
    if not dv.ok:
        out.boundary_reason = "; ".join(dv.warnings) or "patch rejected by anti-cheat boundary"
        out.reason = f"boundary refused: {out.boundary_reason}"
        return out
    # Stricter than the base harness: hard-refuse candidate edits to
    # grading-sensitive config (conftest/pytest.ini/setup*/pyproject/...).
    sensitive = _grading_sensitive_paths(patch)
    if sensitive:
        out.boundary_ok = False
        out.boundary_reason = f"candidate edits grading-sensitive config: {sensitive}"
        out.reason = f"boundary refused: {out.boundary_reason}"
        return out
    # An instance whose graded FAIL_TO_PASS were all dropped as malformed at
    # load (or was flagged ungradable) can never be a real "resolved" -- its
    # candidate would be graded only on PASS_TO_PASS. Refuse to grade it.
    if not inst.gradable:
        out.ungradable = True
        out.reason = f"ungradable: {inst.ungradable_reason or 'no gradable FAIL_TO_PASS tests'}"
        return out
    out.boundary_reason = "no test/config edits; not a gold copy"

    # 2. Held-out fitness on isolated copies. Upstream SWE-bench order: apply the
    #    CANDIDATE first, THEN the grader's test_patch, then run. The baseline
    #    arm gets ONLY test_patch (no candidate).
    base_dir, cand_dir = workroot / "baseline", workroot / "candidate"
    try:
        materialize(inst.repo_path, base_dir)
        materialize(inst.repo_path, cand_dir)
    except Exception as e:
        out.reason = f"could not isolate workspace: {e}"
        return out
    try:
        sb_base, sb_cand = make_sb(base_dir), make_sb(cand_dir)
    except Exception as e:
        out.reason = f"sandbox refused (isolation policy): {e}"
        return out
    # Sandboxes exist first so patch application is routed through the governed
    # backend (not the host). Apply the CANDIDATE first, then the grader's
    # test_patch to BOTH arms -- the graded FAIL_TO_PASS usually don't exist at
    # base_commit. The baseline arm gets ONLY test_patch (no candidate).
    def _apply(p, wd, sb):
        if apply_fn is not None:
            return apply_fn(p, wd)
        return git_apply(p, wd, sandbox=sb)
    applied = _apply(patch, cand_dir, sb_cand)
    if not getattr(applied, "ok", False):
        out.reason = f"patch did not apply: {getattr(applied, 'reason', 'unknown')}"
        return out
    if (inst.test_patch or "").strip():
        for arm_name, arm_dir, arm_sb in (
                ("baseline", base_dir, sb_base), ("candidate", cand_dir, sb_cand)):
            tp = _apply(inst.test_patch, arm_dir, arm_sb)
            if not getattr(tp, "ok", False):
                out.reason = (f"grader test_patch did not apply to {arm_name}: "
                              f"{getattr(tp, 'reason', 'unknown')}")
                return out
    out.baseline_score, base_all_pass, out.samples, base_f2p_pass, base_f2p_fail = _score(
        base_dir, inst, sb_base, timeout=timeout)
    # The graded FAIL_TO_PASS tests MUST fail at baseline -- that IS the bug.
    # Checking only `base_all_pass` (every test passes) missed the case where the
    # FAIL_TO_PASS already pass at baseline but a PASS_TO_PASS is (env-)failing:
    # the gate let it through, and a candidate that merely made the PASS_TO_PASS
    # pass would be scored RESOLVED with no bug ever fixed. Mis-seeded ==
    # "all FAIL_TO_PASS pass": ZERO of them fail AND enough ran to cover the
    # requested set. The `fail==0` term is load-bearing -- a `-k` sibling can
    # inflate the passing count to the requested total while a real target still
    # fails, and without it that genuinely-buggy instance would be wrongly
    # dropped as mis-seeded.
    f2p_all_pass_at_baseline = (
        bool(inst.fail_to_pass) and base_f2p_fail == 0
        and base_f2p_pass >= len(inst.fail_to_pass))
    if base_all_pass or f2p_all_pass_at_baseline:
        out.ungradable = True
        out.reason = ("mis-seeded/shadowed: baseline already passes the graded "
                      "FAIL_TO_PASS tests (no failing bug, or an editable install "
                      "shadows the isolated copy -- grade in per-instance Docker)")
        return out
    out.candidate_score, out.tests_resolved, _, _, _ = _score(cand_dir, inst, sb_cand, timeout=timeout)
    if not out.tests_resolved:
        # Honest-reporting distinction: if the BASELINE can't even run the
        # instance's previously-passing tests, this environment can't grade the
        # instance (deps missing / wrong interpreter) -- that's "ungradable
        # here", not "the agent failed". Never counted as resolved either way
        # (fail-closed); the tag keeps the scoreboard honest in both directions.
        if out.baseline_score == 0.0 and inst.pass_to_pass:
            out.ungradable = True
            out.reason = ("ungradable in this environment: baseline cannot run "
                          "the instance's own passing tests (install its deps "
                          "or grade in the per-instance Docker env)")
        else:
            out.reason = (f"not resolved: candidate {out.candidate_score:.3f} "
                          f"(baseline {out.baseline_score:.3f}); some FAIL/PASS_TO_PASS failing")
        return out

    # 3. Capability non-escalation proof (best-effort; a delta error stays None
    #    so the code rung falls back to demanding a proof -> fail-closed).
    cb = ca = None
    probe: tuple[str, ...] = ()
    try:
        cb, ca, probe = capability_delta(patch)
        if cb is not None and ca is not None and probe:
            out.capability_widens = any(ca.permits(t) and not cb.permits(t) for t in probe)
    except Exception:
        log.debug("capability_delta failed for %s", inst.instance_id, exc_info=True)

    # 4. Human-signed, reversible promotion -> ledger.
    out.promoted, out.approver_id, why = _promote(
        inst, patch, out, cb, ca, probe, keys_dir=keys_dir, ledger=ledger, controller=controller)
    out.reason = why or ("resolved under governance" if out.promoted else "promotion refused")
    return out


def _promote(inst, patch, res, cb, ca, probe, *, keys_dir, ledger, controller,
             rollback: dict | None = None):
    """Build the candidate, sign it with the operator key, run the governed gate.

    ``rollback`` overrides the recorded revert action. The host path leaves it
    None (revert the worktree at ``inst.repo_path``); container grading passes an
    image-relative revert, since the candidate is never applied to a persistent
    local tree."""
    import hashlib
    import os

    from maverick import approval_signing as asig
    from maverick.self_improvement import Candidate, SelfImprovementController

    priv_path = Path(keys_dir) / "operator.priv.hex"
    if not priv_path.exists():
        return False, None, f"no operator signing key at {priv_path}"
    priv = priv_path.read_text().strip()

    cand = Candidate(
        rung="code",
        summary=f"swe-bench {inst.instance_id}: {inst.brief[:80]}",
        baseline_score=res.baseline_score, candidate_score=res.candidate_score,
        samples=res.samples, payload=patch,
        payload_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        capability_before=cb, capability_after=ca, probe_tools=probe,
        rollback=rollback or {"revert": f"git -C {inst.repo_path} checkout -- ."},
        id=inst.instance_id)
    sig = asig.sign_request(asig.ApprovalRequest.for_candidate(cand), priv)
    cand = Candidate(**{**cand.__dict__, "approval_signature": sig})

    prev = {k: os.environ.get(k) for k in ("MAVERICK_APPROVER_KEYS_DIR", "MAVERICK_SELF_IMPROVEMENT")}
    os.environ["MAVERICK_APPROVER_KEYS_DIR"] = str(keys_dir)
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    try:
        if controller is None:
            # For an instance-fix promotion, `samples` is the INSTANCE's own
            # test count (FAIL_TO_PASS + PASS_TO_PASS), not a corpus size. The
            # default code-rung min_samples=10 is calibrated for solver
            # self-improvement (samples = held-out instances) and misfires
            # here: a correct fix in a project that ships 8 tests was refused
            # on sample count alone (pylint-6386, round 3, live). One passing
            # test suite IS the evidence unit for a bug fix; every other gate
            # (boundary, baseline-must-fail, capability, signature) is
            # unchanged.
            from maverick.self_improvement import _RUNG_POLICY
            policy = {k: dict(v) for k, v in _RUNG_POLICY.items()}
            policy["code"]["min_samples"] = 1
            controller = SelfImprovementController(ledger=ledger, rung_policy=policy)
        ctrl = controller
        verdict = ctrl.promote(cand)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if verdict.ok:
        return True, verdict.approver_id, "resolved under governance"
    return False, verdict.approver_id, f"gate refused: {verdict.blocking_reason}"


# --- CLI ----------------------------------------------------------------------

def _sane_test_ids(ids: list, *, instance_id: str, which: str) -> list[str]:
    """Drop malformed test ids, LOUDLY (no silent caps).

    The upstream SWE-bench dataset built FAIL_TO_PASS/PASS_TO_PASS by
    comma-splitting test-output lines, so a parametrized id whose params
    contain a comma (``test_x[foo, bar-expected2]``) arrives truncated
    (``test_x[foo,``). Passing such an id to pytest aborts the WHOLE chunk
    ("not found"), zeroing every other test with it. Filter the malformation
    signature -- unbalanced brackets or a trailing comma -- and say so."""
    keep: list[str] = []
    for t in ids:
        t = str(t).strip()
        if not t:
            continue
        if t.count("[") != t.count("]") or t.endswith((",", "\\")):
            print(f"  [note ]  {instance_id}: dropping malformed {which} id "
                  f"(upstream dataset truncation): {t!r}")
            continue
        keep.append(t)
    return keep


def _load_manifest(path: Path) -> list[Instance]:
    out: list[Instance] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        iid = row["instance_id"]
        repo_path = row.get("repo_path") or str(Path(path).parent / "repos" / iid)
        raw_f2p = list(row.get("fail_to_pass") or [])
        f2p = _sane_test_ids(raw_f2p, instance_id=iid, which="FAIL_TO_PASS")
        # If the row HAD FAIL_TO_PASS but sanitizing emptied it (all ids were
        # malformed upstream truncations), the instance can't be graded as
        # resolved -- a candidate would face only PASS_TO_PASS. Flag it.
        gradable, why = True, ""
        if raw_f2p and not f2p:
            gradable, why = False, (
                f"all {len(raw_f2p)} FAIL_TO_PASS ids were malformed (upstream "
                "truncation); no gradable failing test remains")
        elif not raw_f2p:
            gradable, why = False, "manifest row has no FAIL_TO_PASS tests"
        out.append(Instance(
            instance_id=iid, repo_path=Path(repo_path),
            fail_to_pass=f2p,
            pass_to_pass=_sane_test_ids(list(row.get("pass_to_pass") or []),
                                        instance_id=iid, which="PASS_TO_PASS"),
            gold_patch=row.get("gold_patch", "") or "",
            brief=row.get("brief", "") or "",
            language=row.get("language", "python") or "python",
            test_patch=row.get("test_patch", "") or "",
            gradable=gradable, ungradable_reason=why))
    return out


def main(argv: list[str] | None = None) -> int:
    import tempfile

    from maverick.self_improvement import PromotionLedger

    ap = argparse.ArgumentParser(description="Run SWE-bench instances under governance.")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--proposer", choices=sorted(PROPOSERS), default="oracle")
    ap.add_argument("--keys", required=True, type=Path, help="dir with operator.priv.hex + operator.pub")
    ap.add_argument("--ledger", type=Path, default=Path("swebench_ledger.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-arm test-run timeout (seconds); does NOT bound the agent")
    ap.add_argument("--abort-at-dollars", type=float, default=0.0,
                    help="hard stop once cumulative agent spend reaches this (0 = no cap)")
    ap.add_argument("--max-consecutive-failures", type=int, default=0,
                    help="abort after this many non-resolved instances in a row (0 = off) "
                         "-- a circuit breaker so a broken env doesn't burn a whole budget")
    ap.add_argument("--max-family-failures", type=int, default=3,
                    help="skip a repo family's remaining instances after this many "
                         "consecutive PAID non-resolves in it (0 = off). A family whose "
                         "env is broken fails identically instance after instance "
                         "(observed live: 3 pytest EMPTYs in a row at full best-of-N "
                         "price); this stops the bleed at $0 without killing the run "
                         "for healthy families. Ignored for the oracle proposer.")
    ap.add_argument("--agent-wall-sec", type=float, default=None,
                    help="per-instance agent wall-clock cap (sets MAVERICK_INSTANCE_WALL_SEC)")
    ap.add_argument("--agent-max-steps", type=int, default=None,
                    help="per-instance agent turn cap (sets MAVERICK_MAX_STEPS)")
    ap.add_argument(
        "--allow-host-exec", action="store_true",
        help=("DANGEROUS: allow trusted local fixture tests to run on the host "
              "when no sandbox factory/container is supplied. Do not use for "
              "attacker-controlled repos, test commands, or patches."))
    args = ap.parse_args(argv)

    # Surface the env knobs run_maverick reads as first-class flags.
    if args.agent_wall_sec is not None:
        os.environ["MAVERICK_INSTANCE_WALL_SEC"] = str(args.agent_wall_sec)
    if args.agent_max_steps is not None:
        os.environ["MAVERICK_MAX_STEPS"] = str(args.agent_max_steps)

    instances = _load_manifest(args.manifest)
    if args.limit:
        instances = instances[: args.limit]
    propose = PROPOSERS[args.proposer]
    ledger = PromotionLedger(path=args.ledger)
    # Forensics sidecars land next to the ledger so a failed apply/grade is
    # debuggable without a costly re-run.
    global _FORENSICS_DIR
    _FORENSICS_DIR = args.ledger.resolve().parent / "forensics"

    print("=" * 78)
    print(f"  SWE-BENCH UNDER GOVERNANCE   proposer={args.proposer}  n={len(instances)}")
    print("=" * 78)
    resolved = boundary_refused = unresolved = ungradable = skipped = 0
    spend = 0.0
    consec_fail = 0
    family_fail: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="swebench-gov-") as td:
        for i, inst in enumerate(instances):
            if args.abort_at_dollars and spend >= args.abort_at_dollars:
                print(f"  [ABORT] cumulative spend ${spend:.2f} >= "
                      f"${args.abort_at_dollars:.2f} cap; stopping")
                break
            if args.max_consecutive_failures and consec_fail >= args.max_consecutive_failures:
                print(f"  [ABORT] {consec_fail} consecutive non-resolved instances "
                      "(circuit breaker); stopping -- check the environment")
                break
            # Family circuit breaker (paid proposers only): once a repo family
            # has burned max_family_failures paid attempts in a row with nothing
            # banked, its environment is broken, not its instances hard -- every
            # further attempt is the same money for the same nothing. Skip the
            # rest of the family at $0. Never gates the oracle: preflight must
            # sweep everything to compute an honest winnable set.
            fam = _family(inst.instance_id)
            if (args.proposer != "oracle" and args.max_family_failures
                    and family_fail.get(fam, 0) >= args.max_family_failures):
                skipped += 1
                print(f"  [SKIP]  {inst.instance_id:40}  family breaker ($0 spent): "
                      f"{family_fail[fam]} consecutive paid non-resolves in {fam}; "
                      "fix its env (venv/deps) and re-run this family")
                continue
            # Per-instance venv interpreter (SWE-bench era isolation): point
            # MAVERICK_TEST_PYTHON at this instance's venv for the whole body
            # (pre-gate + propose + govern) so both baseline grading and the
            # agent's test feedback run against its own deps, then restore.
            # No venv configured -> vpy == "" -> the env is left untouched.
            vpy = _venv_python(inst)
            prev_tp = os.environ.get("MAVERICK_TEST_PYTHON")
            if vpy:
                os.environ["MAVERICK_TEST_PYTHON"] = vpy
            try:
                # Free environment pre-gate for paid proposers: never spend agent
                # dollars on an instance this environment provably can't grade.
                # The oracle stays un-gated so preflight exercises the full chain.
                if args.proposer != "oracle":
                    why = _env_pregate(inst, Path(td) / f"pre{i}", timeout=args.timeout)
                    if why:
                        ungradable += 1
                        print(f"  [NOENV]  {inst.instance_id:40}  pre-gate ($0 spent): {why}")
                        continue
                try:
                    patch = propose(inst)
                except RuntimeError as e:
                    # Missing provider key: config problem, not a per-instance
                    # failure. Abort loudly instead of skipping 500 instances.
                    print(f"  [ABORT] {e}")
                    return 2
                except Exception as e:
                    # A per-instance proposer crash scores that instance 0 and the
                    # run continues -- one bad instance must not kill a $1k run.
                    print(f"  [ERROR] {inst.instance_id:40}  proposer failed: {e}")
                    unresolved += 1
                    consec_fail += 1
                    family_fail[fam] = family_fail.get(fam, 0) + 1
                    continue
                # Attribute agent spend from the forensics sidecar (best-effort).
                cost = _instance_spend(inst.instance_id)
                spend += cost
                r = govern_candidate(inst, patch, keys_dir=args.keys, ledger=ledger,
                                     workroot=Path(td) / f"i{i}", timeout=args.timeout,
                                     check_gold_overlap=(args.proposer != "oracle"),
                                     allow_host_exec=args.allow_host_exec)
                tag = _verdict_tag(r)
                if tag == "PASS":
                    resolved += 1
                    consec_fail = 0
                    family_fail[fam] = 0
                elif tag == "NOENV":
                    ungradable += 1  # ungradable-here does not count against the breakers
                else:  # EMPTY / CHEAT / FAIL all count against both breakers
                    if tag == "CHEAT":
                        boundary_refused += 1
                    else:
                        unresolved += 1
                    consec_fail += 1
                    family_fail[fam] = family_fail.get(fam, 0) + 1
                # Paid runs show what each verdict COST inline -- three EMPTYs at
                # full best-of-N price should be visible in the log as money, not
                # discovered later in the provider console. Oracle lines stay
                # suffix-free (pod launchers anchor regexes on their line ends).
                cost_note = f"   [spent ~${cost:.2f}]" if args.proposer != "oracle" else ""
                print(f"  [{tag}]  {inst.instance_id:40}  {r.reason}{cost_note}")
            finally:
                if vpy:
                    if prev_tp is None:
                        os.environ.pop("MAVERICK_TEST_PYTHON", None)
                    else:
                        os.environ["MAVERICK_TEST_PYTHON"] = prev_tp
    n = len(instances) or 1
    gradable_total = resolved + boundary_refused + unresolved
    print("=" * 78)
    print(f"  resolved under governance: {resolved}/{len(instances)} ({100*resolved/n:.1f}% of all)"
          f"   |  boundary-refused: {boundary_refused}  |  unresolved: {unresolved}"
          f"   |  ungradable-here: {ungradable}"
          + (f"  |  family-skipped: {skipped}" if skipped else ""))
    if gradable_total:
        print(f"  resolved of GRADABLE (excl. ungradable): {resolved}/{gradable_total} "
              f"({100*resolved/gradable_total:.1f}%)")
    if ungradable:
        print("  NOTE: ungradable instances need their deps / the per-instance Docker env;"
              " they are NOT counted as resolved. Report both figures honestly.")
    print(f"  agent spend (attributed): ~${spend:.2f}   |   signed ledger: {args.ledger}")
    print("=" * 78)
    return 0


def _verdict_tag(r: GovernedResult) -> str:
    """Scoreboard tag for a governed verdict. EMPTY (no diff produced) is an
    honest zero, not an anti-cheat refusal; counting it as CHEAT inflated
    boundary-refused on the scoreboard."""
    if r.resolved_under_governance:
        return "PASS"
    if not r.boundary_ok:
        return "EMPTY" if r.reason.startswith("empty patch") else "CHEAT"
    if r.ungradable:
        return "NOENV"
    return "FAIL"


def _family(instance_id: str) -> str:
    """Repo family of an instance id: ``pytest-dev__pytest-10051`` ->
    ``pytest-dev__pytest`` (the id minus its trailing issue number). Instances
    of one family share a repo, an interpreter era, and a venv -- so they break
    together, which is what makes a per-family circuit breaker sound."""
    return instance_id.rsplit("-", 1)[0]


def _venv_python(inst: Instance) -> str:
    """Absolute path to this instance's venv interpreter, or "" if none.
    Operators build one venv per instance (pip install -e <repo> with its
    own era's deps) under MAVERICK_SWEBENCH_VENVS; grading and the agent's
    test feedback then run with that interpreter instead of host python3,
    which is what makes mixed-era instances gradable on one box."""
    base = os.environ.get("MAVERICK_SWEBENCH_VENVS", "").strip()
    if not base:
        return ""
    p = Path(base) / inst.instance_id / "bin" / "python"
    return str(p) if p.exists() else ""


def _env_pregate(inst: Instance, workroot: Path, *, timeout: float) -> str:
    """FREE baseline check before the (paid) proposer runs. Returns the
    ungradable reason, or "" when the instance is gradable here.

    govern_candidate already refuses these cases, but only AFTER the agent
    has spent real money on the instance. Materializing one baseline copy,
    applying the grader's test_patch, and running the instance's own tests
    costs nothing -- an environment that can't run them (missing deps, wrong
    interpreter era) or that already passes them (mis-seeded/shadowed) is
    skipped at $0 instead of ~$1 of agent spend per doomed instance."""
    from maverick.self_modify_eval import _default_materialize, git_apply, resolve_eval_sandbox
    base_dir = workroot / "pregate"
    try:
        _default_materialize(inst.repo_path, base_dir)
    except Exception as e:
        return f"could not isolate workspace: {e}"
    if (inst.test_patch or "").strip():
        tp = git_apply(inst.test_patch, base_dir)
        if not getattr(tp, "ok", False):
            return f"grader test_patch did not apply: {getattr(tp, 'reason', 'unknown')}"
    try:
        sb = resolve_eval_sandbox(None, base_dir)
    except Exception as e:
        return f"sandbox refused (isolation policy): {e}"
    score, all_pass, _, f2p_pass, f2p_fail = _score(base_dir, inst, sb, timeout=timeout)
    # Same gate as govern_candidate, run for free before spending: the graded
    # FAIL_TO_PASS must FAIL at baseline. Reject when all tests pass OR when the
    # FAIL_TO_PASS specifically already all pass (fail==0 AND enough ran) --
    # mis-seeded / shadowed copy. The fail==0 term stops a `-k` sibling pass from
    # masquerading as "all FAIL_TO_PASS pass".
    if all_pass or (inst.fail_to_pass and f2p_fail == 0
                    and f2p_pass >= len(inst.fail_to_pass)):
        return ("mis-seeded/shadowed: baseline already passes the graded "
                "FAIL_TO_PASS tests (no failing bug to fix)")
    if score == 0.0 and inst.pass_to_pass:
        return ("baseline cannot run the instance's own passing tests "
                "(missing deps / wrong interpreter era for this environment)")
    return ""


def _instance_spend(instance_id: str) -> float:
    """Read the agent $ cost for an instance from its forensics sidecar."""
    try:
        base = os.environ.get("MAVERICK_SWEBENCH_FORENSICS")
        outdir = Path(base) if base else (_FORENSICS_DIR or Path.cwd() / "forensics")
        f = outdir / f"{instance_id}.json"
        if f.exists():
            return float(json.loads(f.read_text()).get("cost_dollars", 0.0) or 0.0)
    except Exception:
        pass
    return 0.0


if __name__ == "__main__":
    sys.exit(main())
