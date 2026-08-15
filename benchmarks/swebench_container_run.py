#!/usr/bin/env python3
"""Governed SWE-bench run in OFFICIAL per-instance containers.

This is the wiring that turns the container grader
(:mod:`swebench_container_grade`) into a governed *run*: it grades a random,
non-cherry-picked sample of SWE-bench Verified -- each instance inside its own
correct environment (the published ``sweb.eval.*`` image) -- and promotes every
resolved candidate through the same signed, reversible ledger the host path uses
(:func:`swebench_governed._promote`).

Why this matters: the old host path graded 500 different Python eras on one
interpreter, so instances died NOENV/EMPTY and money was thrown at things that
could never grade. Here the environment travels with the instance, so **every**
Verified instance is gradable -- which is what lets us grade a *random* sample
(or all of it) and answer the "you cherry-picked the easy environments"
criticism honestly. The governance chain is unchanged:

    anti-cheat boundary (host)  ->  grading-sensitive-config refusal (host)
      ->  baseline mis-seed guard (image: FAIL_TO_PASS must fail with no patch)
      ->  candidate graded in image -> OFFICIAL resolution
      ->  capability non-escalation proof -> human-signed promotion -> ledger

Proposers:
  * ``oracle`` -- the gold patch. $0 Anthropic; proves the pipeline + governance
    are correct across a diverse random sample (gold resolves, baseline fails,
    signed). This is the credibility artifact you can run before spending a cent
    on an agent.
  * (an LLM/agent proposer plugs in the same way -- swap the patch source; the
    governance and grading are identical. That run costs Anthropic $ and is the
    real solve-rate number.)

    # signed, non-cherry-picked artifact ($0 Anthropic, ~cents Modal):
    python benchmarks/swebench_container_run.py --n 4 --seed 1729 \
        --backend modal --keys ~/dgm-keys --ledger container_run_ledger.json
    # then verify it independently:
    python benchmarks/audit_ledger.py --ledger container_run_ledger.json --keys ~/dgm-keys
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "packages" / "maverick-core"))

import swebench_container_grade as CG  # noqa: E402

log = logging.getLogger(__name__)


@dataclass
class InstanceOutcome:
    """The governed verdict for one instance in a container run."""
    instance_id: str
    proposer: str
    boundary_ok: bool = False
    baseline_fails: bool = False    # FAIL_TO_PASS proven to fail with no patch
    resolved: bool = False          # official get_eval_report resolution
    promoted: bool = False          # signed into the ledger
    approver_id: str | None = None
    fail_to_pass: str = ""          # "pass/fail"
    pass_to_pass: str = ""
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    samples: int = 0
    cost_dollars: float = 0.0       # agent spend for this instance (0 for oracle)
    reason: str = ""

    @property
    def resolved_under_governance(self) -> bool:
        return self.boundary_ok and self.resolved and self.promoted

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["resolved_under_governance"] = self.resolved_under_governance
        return d


# --- sample selection ---------------------------------------------------------

def load_verified(ids: list[str] | None = None) -> dict[str, dict]:
    """All SWE-bench Verified instances as ``{id: instance_dict}`` (FAIL/PASS
    lists parsed). ``ids`` restricts the load to those instance_ids."""
    from datasets import load_dataset
    want = set(ids) if ids else None
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    out: dict[str, dict] = {}
    for row in ds:
        if want is not None and row["instance_id"] not in want:
            continue
        inst = dict(row)
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            if isinstance(inst.get(k), str):
                inst[k] = json.loads(inst[k])
        out[inst["instance_id"]] = inst
    return out


def sample_ids(all_ids: list[str], n: int, seed: int) -> list[str]:
    """A DETERMINISTIC random sample of ``n`` instance_ids -- seeded so the
    selection is reproducible and auditable (not hand-picked). Sorted input so
    the seed alone fixes the draw regardless of dataset row order."""
    pool = sorted(all_ids)
    if n <= 0 or n >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, n))


# --- proposers ----------------------------------------------------------------

def oracle_patch(instance: dict) -> str:
    """The gold patch (Princeton's fix). Proves the pipeline with no LLM."""
    return instance.get("patch", "") or instance.get("gold_patch", "")


def _materialize_repo(instance: dict, dst: Path) -> None:
    """Check out ``instance``'s repo at ``base_commit`` into ``dst`` -- the local
    tree the agent edits to PRODUCE a patch (grading then happens in the official
    image, so the local checkout only needs the source at base_commit, not the
    era environment). Fetch-by-SHA at depth 1 first (fast, minimal); full clone
    is the fallback for a server that refuses arbitrary-SHA fetches. List-arg
    subprocess, no shell -- consistent with swebench_governed's git helpers."""
    import shutil
    import subprocess
    url = f"https://github.com/{instance['repo']}"
    sha = instance["base_commit"]
    dst.mkdir(parents=True, exist_ok=True)

    def _git(*args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=600, check=check)

    _git("init", "-q", str(dst))
    _git("remote", "add", "origin", url, cwd=dst)
    try:
        _git("fetch", "--depth", "1", "-q", "origin", sha, cwd=dst)
        _git("checkout", "-q", "-b", "base", sha, cwd=dst)
    except subprocess.CalledProcessError:
        shutil.rmtree(dst, ignore_errors=True)
        _git("clone", "-q", url, str(dst))
        _git("checkout", "-q", "-b", "base", sha, cwd=dst)


def agent_patch(instance: dict) -> str:
    """Run the maverick coding agent against a fresh checkout of the instance at
    base_commit and return its proposed fix, reusing the proven
    ``swebench_governed.llm_proposer`` (best-of-N, retry-on-empty, opaque-mode
    anti-cheat, forensics sidecar). The agent produces the patch; the official
    container grades it -- so the agent's local era never matters. Needs a
    provider key (llm_proposer fails loud without one)."""
    import shutil
    import tempfile

    from swebench_governed import Instance, _sane_test_ids, llm_proposer
    iid = instance["instance_id"]
    td = Path(tempfile.mkdtemp(prefix=f"agent-{iid}-"))
    try:
        repo = td / "repo"
        _materialize_repo(instance, repo)
        f2p = _sane_test_ids(list(instance.get("FAIL_TO_PASS") or []),
                             instance_id=iid, which="FAIL_TO_PASS")
        p2p = _sane_test_ids(list(instance.get("PASS_TO_PASS") or []),
                             instance_id=iid, which="PASS_TO_PASS")
        inst = Instance(
            instance_id=iid, repo_path=repo, fail_to_pass=f2p, pass_to_pass=p2p,
            gold_patch=instance.get("patch", "") or "",
            brief=instance.get("problem_statement", "") or "",
            test_patch=instance.get("test_patch", "") or "", language="python")
        return llm_proposer(inst) or ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


PROPOSERS = {"oracle": oracle_patch, "agent": agent_patch}


# --- one instance, fully governed --------------------------------------------

def govern_one(instance: dict, patch: str, run_in_image, *, proposer: str,
               keys_dir: Path, ledger, namespace: str, timeout: float,
               check_baseline: bool) -> InstanceOutcome:
    """Grade ``patch`` for ``instance`` in its official image and, if it resolves
    under governance, sign it into the ledger. Never raises."""
    iid = instance["instance_id"]
    out = InstanceOutcome(instance_id=iid, proposer=proposer)
    if not (patch or "").strip():
        out.reason = "empty patch (proposer produced no diff)"
        return out

    # Grade under governance (boundary -> sensitive-config -> baseline -> image).
    # The oracle's candidate IS gold, so its gold-overlap warning is disabled;
    # any real proposer keeps it armed.
    gr = CG.governed_container_grade(
        instance, patch, run_in_image, timeout=timeout, namespace=namespace,
        check_baseline=check_baseline, check_gold_overlap=(proposer != "oracle"))
    out.boundary_ok = gr.boundary_ok
    out.fail_to_pass = f"{len(gr.fail_to_pass_passed)}/{len(gr.fail_to_pass_failed)}"
    out.pass_to_pass = f"{len(gr.pass_to_pass_passed)}/{len(gr.pass_to_pass_failed)}"
    if not gr.boundary_ok:
        out.reason = f"boundary refused: {gr.boundary_reason}"
        return out
    if gr.error:
        out.reason = gr.error
        return out
    # The mis-seed guard ran the baseline and required FAIL_TO_PASS to fail with
    # no candidate -- reaching here with check_baseline means that held.
    out.baseline_fails = check_baseline
    out.resolved = gr.resolved
    if not gr.resolved:
        out.reason = ("not resolved: "
                      f"FAIL_TO_PASS {out.fail_to_pass}, PASS_TO_PASS {out.pass_to_pass}")
        return out

    # Evidence for the ledger. Resolution means every graded test passes with the
    # candidate; the mis-seed guard proved FAIL_TO_PASS fail at baseline while
    # PASS_TO_PASS pass (SWE-bench definition), so baseline pass-rate is exactly
    # the PASS_TO_PASS fraction.
    n_f2p = len(gr.fail_to_pass_passed)
    n_p2p = len(gr.pass_to_pass_passed)
    total = n_f2p + n_p2p
    out.samples = total
    out.candidate_score = 1.0
    out.baseline_score = (n_p2p / total) if total else 0.0

    out.promoted, out.approver_id, why = _promote_container(
        instance, patch, gr, out, keys_dir=keys_dir, ledger=ledger, namespace=namespace)
    out.reason = why
    return out


def _promote_container(instance, patch, gr, out, *, keys_dir, ledger, namespace):
    """Sign a resolved container-graded candidate into the ledger, reusing the
    host path's :func:`swebench_governed._promote` (one signing/gate code path,
    no key material here). The rollback is image-relative: the candidate is never
    applied to a persistent local tree, so "revert" means "do not apply"."""
    from maverick.self_modify_capability import capability_delta
    from swebench_governed import GovernedResult, Instance, _promote

    iid = instance["instance_id"]
    inst_obj = Instance(
        instance_id=iid,
        repo_path=Path(f"(official-image:{namespace}/{iid})"),
        fail_to_pass=list(instance.get("FAIL_TO_PASS") or instance.get("fail_to_pass") or []),
        pass_to_pass=list(instance.get("PASS_TO_PASS") or instance.get("pass_to_pass") or []),
        gold_patch=instance.get("patch", "") or instance.get("gold_patch", ""),
        brief=(instance.get("problem_statement", "") or "")[:200])
    res = GovernedResult(
        instance_id=iid, boundary_ok=True, tests_resolved=True,
        baseline_score=out.baseline_score, candidate_score=out.candidate_score,
        samples=out.samples)

    # Capability non-escalation proof (best-effort; a delta error stays None so
    # the code rung falls back to demanding a proof -> fail-closed).
    cb = ca = None
    probe: tuple[str, ...] = ()
    try:
        cb, ca, probe = capability_delta(patch)
    except Exception:
        log.debug("capability_delta failed for %s", iid, exc_info=True)

    rollback = {"revert": f"do not apply candidate; re-grade baseline in "
                          f"{CG.instance_image(CG.make_spec(instance), namespace=namespace)}"}
    return _promote(inst_obj, patch, res, cb, ca, probe,
                    keys_dir=keys_dir, ledger=ledger, controller=None, rollback=rollback)


# --- run ----------------------------------------------------------------------

@dataclass
class RunReport:
    proposer: str
    seed: int
    n_requested: int
    outcomes: list[InstanceOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {"resolved_under_governance": 0, "resolved_not_promoted": 0,
             "not_resolved": 0, "boundary_refused": 0, "error": 0}
        for o in self.outcomes:
            if o.resolved_under_governance:
                c["resolved_under_governance"] += 1
            elif o.resolved and not o.promoted:
                c["resolved_not_promoted"] += 1
            elif not o.boundary_ok:
                c["boundary_refused"] += 1
            elif o.resolved is False and o.reason.startswith("not resolved"):
                c["not_resolved"] += 1
            else:
                c["error"] += 1
        return c


def run(instances: dict[str, dict], ids: list[str], run_in_image, *, proposer: str,
        keys_dir: Path, ledger, namespace: str, timeout: float,
        check_baseline: bool, abort_at_dollars: float = 0.0) -> RunReport:
    propose = PROPOSERS[proposer]
    rep = RunReport(proposer=proposer, seed=-1, n_requested=len(ids))
    spend = 0.0
    for iid in ids:
        if abort_at_dollars and spend >= abort_at_dollars:
            print(f"  [ABORT] cumulative agent spend ${spend:.2f} >= "
                  f"${abort_at_dollars:.2f} cap; stopping")
            break
        instance = instances.get(iid)
        if instance is None:
            rep.outcomes.append(InstanceOutcome(
                instance_id=iid, proposer=proposer,
                reason="instance not in SWE-bench Verified"))
            continue
        patch = propose(instance)
        o = govern_one(instance, patch, run_in_image, proposer=proposer,
                       keys_dir=keys_dir, ledger=ledger, namespace=namespace,
                       timeout=timeout, check_baseline=check_baseline)
        # Agent spend for this instance, read back from llm_proposer's forensics
        # sidecar (0 for the oracle, which spends nothing).
        if proposer != "oracle":
            from swebench_governed import _instance_spend
            o.cost_dollars = round(_instance_spend(iid), 4)
            spend += o.cost_dollars
        rep.outcomes.append(o)
        tag = ("RESOLVED+SIGNED" if o.resolved_under_governance else
               "resolved(no-promo)" if o.resolved else
               "boundary" if not o.boundary_ok else "not-resolved")
        cost = f"  ${o.cost_dollars:.2f}" if o.cost_dollars else ""
        print(f"  [{tag:18}] {iid:42} F2P {o.fail_to_pass:>7}  P2P {o.pass_to_pass:>7}{cost}"
              f"  {'' if o.resolved_under_governance else '-- ' + o.reason[:56]}")
    return rep


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    from maverick.self_improvement import PromotionLedger

    ap = argparse.ArgumentParser(
        description="Governed SWE-bench run in official per-instance containers.")
    ap.add_argument("--n", type=int, default=4, help="random sample size (0 = all 500)")
    ap.add_argument("--seed", type=int, default=1729, help="deterministic sample seed")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated instance_ids (overrides --n/--seed)")
    ap.add_argument("--proposer", choices=sorted(PROPOSERS), default="oracle")
    ap.add_argument("--backend", choices=("modal", "docker"), default="modal")
    ap.add_argument("--keys", required=True, type=Path,
                    help="dir with operator.priv.hex + operator.pub")
    ap.add_argument("--ledger", type=Path, default=Path("container_run_ledger.json"))
    ap.add_argument("--namespace", default=CG.DEFAULT_IMAGE_NAMESPACE)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--no-baseline", action="store_true",
                    help="skip the baseline mis-seed guard (halves container runs; "
                         "weakens the honesty proof -- not for a credibility run)")
    ap.add_argument("--report", type=Path, default=None,
                    help="write the JSON run report here (default: next to --ledger)")
    ap.add_argument("--best-of-n", type=int, default=1,
                    help="agent best-of-N sampling (default 1; higher = more spend)")
    ap.add_argument("--max-steps", type=int, default=75,
                    help="per-instance agent turn cap (MAVERICK_MAX_STEPS). The "
                         "benchmark floor is 25, which real django/sympy instances "
                         "blow past mid-fix -> 'no-diff'; 75 is a realistic budget.")
    ap.add_argument("--agent-wall-sec", type=float, default=None,
                    help="per-instance agent wall-clock cap (MAVERICK_INSTANCE_WALL_SEC)")
    ap.add_argument("--abort-at-dollars", type=float, default=0.0,
                    help="hard stop once cumulative agent spend reaches this (0 = off)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Agent-proposer plumbing: forensics sidecars (cost read-back) land next to
    # the ledger; best-of-N / max-steps / wall are cost+capability knobs. Set
    # MAVERICK_MAX_STEPS directly (not setdefault) so it overrides _BENCH_ENV's
    # 25-step floor -- the floor caps the agent mid-fix on real instances.
    # Oracle ignores all of this.
    if args.proposer != "oracle":
        import os
        os.environ.setdefault("MAVERICK_SWEBENCH_FORENSICS",
                              str(args.ledger.resolve().parent / "forensics"))
        os.environ["MAVERICK_BEST_OF_N"] = str(max(1, args.best_of_n))
        os.environ["MAVERICK_MAX_STEPS"] = str(max(1, args.max_steps))
        if args.agent_wall_sec is not None:
            os.environ["MAVERICK_INSTANCE_WALL_SEC"] = str(args.agent_wall_sec)

    if args.ids.strip():
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    else:
        all_ids = sorted(load_verified().keys())
        ids = sample_ids(all_ids, args.n, args.seed)
    instances = load_verified(ids)

    run_in_image = (CG.modal_runner() if args.backend == "modal" else CG.docker_runner())
    ledger = PromotionLedger(path=args.ledger)

    print("=" * 88)
    print(f"  GOVERNED SWE-BENCH IN OFFICIAL CONTAINERS   proposer={args.proposer}  "
          f"n={len(ids)}  seed={args.seed}  backend={args.backend}")
    print(f"  ledger={args.ledger}   baseline-guard={'OFF' if args.no_baseline else 'ON'}")
    print("=" * 88)

    rep = run(instances, ids, run_in_image, proposer=args.proposer, keys_dir=args.keys,
              ledger=ledger, namespace=args.namespace, timeout=args.timeout,
              check_baseline=not args.no_baseline, abort_at_dollars=args.abort_at_dollars)
    rep.seed = args.seed

    c = rep.counts()
    graded = len(rep.outcomes)
    total_cost = round(sum(o.cost_dollars for o in rep.outcomes), 2)
    print("=" * 88)
    print(f"  resolved_under_governance : {c['resolved_under_governance']}/{graded}")
    print(f"  resolved (not promoted)   : {c['resolved_not_promoted']}")
    print(f"  not resolved              : {c['not_resolved']}")
    print(f"  boundary refused          : {c['boundary_refused']}")
    print(f"  error                     : {c['error']}")
    if args.proposer != "oracle":
        per = round(total_cost / graded, 2) if graded else 0.0
        print(f"  agent spend               : ${total_cost:.2f} total  (~${per:.2f}/instance)")
    print("=" * 88)

    report_path = args.report or args.ledger.with_suffix(".report.json")
    report_path.write_text(json.dumps(
        {"proposer": args.proposer, "seed": args.seed, "n_requested": len(ids),
         "counts": c, "outcomes": [o.to_dict() for o in rep.outcomes]}, indent=2))
    print(f"  run report -> {report_path}")
    print(f"  verify the ledger independently:  python benchmarks/audit_ledger.py "
          f"--ledger {args.ledger} --keys {args.keys}")
    return 0 if c["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
