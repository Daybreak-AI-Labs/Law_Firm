#!/usr/bin/env python3
"""Stage a SWE-bench Verified corpus for a container-graded DGM cycle.

``dgm_live.py`` consumes a JSONL manifest whose rows point at LOCAL checkouts
(``repo_path``): the solver under improvement reads the repo to author its fix.
Grading, however, happens in the OFFICIAL per-instance container
(``--container-grade``), so the local checkout only needs the source at
``base_commit`` -- no era venvs, no deps, no test environment. That is what
this tool stages, for $0:

  1. deterministically sample N Verified instance ids (seeded -- auditable,
     not hand-picked; same sampler as the governed container run);
  2. shallow-checkout each repo at ``base_commit`` under ``--stage-dir``;
  3. write the manifest JSONL next to it.

The checkout deliberately does NOT apply the grader's ``test_patch``: the
solver sees the honest SWE-bench inputs (problem statement + repo at
base_commit), never the graded tests -- reading the tests it will be graded on
is the overfit-to-tests cheat the boundary exists to stop. The test_patch
still rides along IN the manifest row because the container's official eval
script applies it at grading time.

    python benchmarks/build_dgm_manifest.py --n 12 --seed 1729 \
        --stage-dir ~/dgm_stage --manifest ~/dgm_stage/manifest.jsonl
    python benchmarks/dgm_live.py --manifest ~/dgm_stage/manifest.jsonl \
        --keys ~/dgm-keys --container-grade --min-samples 5 ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "packages" / "maverick-core"))

from swebench_container_run import _materialize_repo, load_verified, sample_ids  # noqa: E402


def build(ids: list[str], stage_dir: Path, manifest: Path) -> int:
    """Stage each instance and write the manifest. Returns count staged.
    A failed checkout is reported and SKIPPED (loudly), never silently kept:
    a manifest row with a broken repo_path would zero the solver's context."""
    instances = load_verified(ids)
    stage_dir.mkdir(parents=True, exist_ok=True)
    rows, failed = [], []
    for iid in ids:
        raw = instances.get(iid)
        if raw is None:
            failed.append((iid, "not in SWE-bench Verified"))
            continue
        dst = stage_dir / "repos" / iid
        try:
            if not (dst / ".git").exists():
                _materialize_repo(raw, dst)
            rows.append({
                "instance_id": iid,
                "repo_path": str(dst),
                "base_commit": raw["base_commit"],   # exact SHA to reset to pre-run
                "fail_to_pass": raw["FAIL_TO_PASS"],
                "pass_to_pass": raw["PASS_TO_PASS"],
                "gold_patch": raw["patch"],
                "test_patch": raw["test_patch"],
                "brief": raw["problem_statement"],
                "language": "python",
            })
            print(f"  [staged] {iid}")
        except Exception as e:  # loud skip -- never a silent broken row
            failed.append((iid, f"{type(e).__name__}: {e}"))
            print(f"  [FAILED] {iid}: {e}")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"  manifest -> {manifest}  ({len(rows)} staged, {len(failed)} failed)")
    for iid, why in failed:
        print(f"    failed: {iid}: {why}")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=12, help="sample size (ignored with --ids)")
    ap.add_argument("--seed", type=int, default=1729, help="deterministic sample seed")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated instance_ids (overrides --n/--seed)")
    ap.add_argument("--stage-dir", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, default=None,
                    help="output JSONL (default: <stage-dir>/manifest.jsonl)")
    args = ap.parse_args(argv)

    if args.ids.strip():
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    else:
        ids = sample_ids(sorted(load_verified().keys()), args.n, args.seed)
    manifest = args.manifest or (args.stage_dir / "manifest.jsonl")
    staged = build(ids, args.stage_dir, manifest)
    return 0 if staged == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
