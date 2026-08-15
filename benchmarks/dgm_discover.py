#!/usr/bin/env python3
"""Genuine capability DISCOVERY under governance -- the agent, not the author,
writes the fix, and must INFER a hidden rule from examples.

Each trial hides an operation behind a nonce token ``@`` (a per-trial secret:
one of add, sub, mul, max, min, absolute-difference, ...). The baseline solver
cannot do ``@``. The agent (the real coding-role LLM via
``dgm_uplift.llm_solver_proposer``) is shown ONLY the solver source and a handful
of HELD-IN examples ``"A @ B -> answer"`` -- never the operator's name, never the
held-out tasks. It must reverse-engineer the operation and implement it
*generally*. The governed gate then measures HELD-OUT resolved-rate and decides:

  * discovered + generalises   -> PROMOTED (held-out lift), signed to the ledger
  * memorised the shown pairs   -> caught by the OVERFIT guard
  * failed to infer / no gain   -> REFUSED

This is inductive program synthesis judged by a held-out split and gated by a
signed, independently-auditable promotion -- the auditable "novel capability"
claim. Metered: every LLM call is priced; a hard $ ceiling stops the run.

    python benchmarks/dgm_discover.py --trials 3 --max-dollars 5     # price it
    python benchmarks/dgm_discover.py --trials 100 --max-dollars 40  # scale it
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "packages" / "maverick-core"))

import dgm_uplift  # noqa: E402

# Hidden operations the agent must infer from I/O examples. Chosen so induction
# from ~10 integer examples is feasible for a capable model (no bitwise noise).
_HIDDEN = {
    "add":     lambda a, b: a + b,
    "sub":     lambda a, b: a - b,
    "mul":     lambda a, b: a * b,
    "max":     lambda a, b: max(a, b),
    "min":     lambda a, b: min(a, b),
    "absdiff": lambda a, b: abs(a - b),
    "sqsum":   lambda a, b: a * a + b * b,
}

_BASELINE = '''\
def solve(instance):
    """Evaluate 'A @ B' for integers A, B. '@' is an operation to be learned;
    until then, unknown -> "0"."""
    a, _at, b = instance.spec.split()
    a, b = int(a), int(b)
    return "0"
'''


@dataclass
class Task:
    instance_id: str
    spec: str
    gold: str
    language: str = "python"


# --- LLM cost meter (wrap LLM.complete; hard ceiling) --------------------------
from maverick import llm as _llm  # noqa: E402

_SPEND = {"usd": 0.0, "calls": 0, "cap": 1e9}
_ORIG_COMPLETE = _llm.LLM.complete


def _metered_complete(self, system, messages, *a, **kw):
    if _SPEND["usd"] >= _SPEND["cap"]:
        raise RuntimeError(f"cost ceiling ${_SPEND['cap']:.2f} reached "
                           f"(${_SPEND['usd']:.2f} spent) -- stopping")
    resp = _ORIG_COMPLETE(self, system, messages, *a, **kw)
    try:
        c = _llm._response_call_cost(kw.get("model"), resp)
    except Exception:
        c = None
    _SPEND["usd"] += c or 0.0
    _SPEND["calls"] += 1
    return resp


_llm.LLM.complete = _metered_complete


def _corpus(rng: random.Random, fn, trial: int) -> list[Task]:
    tasks: list[Task] = []
    seen: set[str] = set()
    while len(tasks) < 24:
        a, b = rng.randint(1, 40), rng.randint(1, 40)
        spec = f"{a} @ {b}"
        if spec in seen:
            continue
        seen.add(spec)
        tasks.append(Task(f"t{trial}-{len(tasks):02d}-{rng.randint(0,1<<30):x}",
                          spec, str(fn(a, b))))
    return tasks


def _feedback(held_in: list[Task]) -> str:
    ex = "\n".join(f"  solve() on instance.spec='{t.spec}' must return '{t.gold}' "
                   f"(baseline returned '0')" for t in held_in[:10])
    return ("The solver must compute 'A @ B'. '@' is a fixed binary operation on "
            "the two integers; infer it from these examples and implement it "
            "GENERALLY (do not hard-code these specific inputs):\n" + ex)


def _score_fn(inst, answer, workroot, *, timeout: float = 600.0) -> bool:
    return (answer or "") == inst.gold


def _make_keys(run_dir: Path) -> Path:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    keys = run_dir / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    (keys / "operator.priv.hex").write_text(priv.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    (keys / "operator.pub").write_bytes(priv.public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw))
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--max-dollars", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=20260712)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="persist keys/ledger/rows here and RESUME (skip done trials); "
                         "cumulative spend survives restarts. Required for large runs on "
                         "a recyclable host.")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    _SPEND["cap"] = args.max_dollars

    import os
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        print("NO PROVIDER KEY: set ANTHROPIC_API_KEY (this run makes real LLM calls).")
        return 2

    from maverick.self_improvement import PromotionLedger

    # Persistent (resumable) or ephemeral run dir. A large run on a recyclable
    # host MUST use --out-dir so a restart resumes instead of re-paying.
    persistent = args.out_dir is not None
    out = Path(args.out_dir) if persistent else Path(tempfile.mkdtemp(prefix="dgm_discover_"))
    out.mkdir(parents=True, exist_ok=True)
    keys = out / "keys"
    keys = keys if (keys / "operator.priv.hex").exists() else _make_keys(out)
    ledger = PromotionLedger(path=out / "ledger.json")
    rows_path = out / "rows.jsonl"
    spend_path = out / "spend.json"

    # Resume: which trials already ran, and how much we already spent.
    done: dict[int, dict] = {}
    if rows_path.exists():
        for line in rows_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["trial"]] = r
    if spend_path.exists():
        try:
            _SPEND["usd"] = float(json.loads(spend_path.read_text()).get("usd", 0.0))
        except Exception:
            pass

    print(f"MAVERICK -- CAPABILITY DISCOVERY: {args.trials} trials "
          f"(hard cap ${args.max_dollars:.2f}; {len(done)} already done; "
          f"cum spend ${_SPEND['usd']:.2f})")
    print("=" * 74)
    for t in range(args.trials):
        if t in done:
            continue
        if _SPEND["usd"] >= args.max_dollars:
            print(f"  [stop] cost ceiling ${args.max_dollars:.2f} reached at trial {t}")
            break
        # Deterministic per-trial seed -> a resumed trial is identical, and the
        # corpus never depends on how many trials ran before it.
        rng = random.Random(f"{args.seed}-{t}")
        name = rng.choice(list(_HIDDEN))
        fn = _HIDDEN[name]
        tasks = _corpus(rng, fn, t)
        sdir = out / f"solver_{t}"
        sdir.mkdir(exist_ok=True)
        (sdir / "solver.py").write_text(_BASELINE)
        held_in, held_out = dgm_uplift.split_instances(tasks, held_out_frac=0.5)

        before = _SPEND["usd"]
        try:
            patch = dgm_uplift.llm_solver_proposer(sdir, _feedback(held_in))
        except Exception as e:
            patch = ""
            print(f"  trial {t:4d} [{name:7s}] proposer error: {e}")
        spent = _SPEND["usd"] - before

        verdict = "empty"
        uplift = 0.0
        if patch:
            res = dgm_uplift.govern_solver_change(
                sdir, patch, tasks, keys_dir=keys, ledger=ledger,
                workroot=out / f"gov{t}", held_out_frac=0.5, timeout=60,
                score_fn=_score_fn)
            uplift = res.uplift
            verdict = ("PROMOTED" if res.promoted else
                       "OVERFIT" if res.overfit else "REFUSED")
        row = {"trial": t, "op": name, "verdict": verdict,
               "uplift": round(uplift, 3), "usd": round(spent, 4)}
        with open(rows_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        spend_path.write_text(json.dumps({"usd": _SPEND["usd"], "calls": _SPEND["calls"]}))
        done[t] = row
        # keep the run dir from ballooning: solver/gov scratch is not needed once graded
        import shutil as _sh
        _sh.rmtree(sdir, ignore_errors=True)
        _sh.rmtree(out / f"gov{t}", ignore_errors=True)
        if (len(done)) % 25 == 0:
            p = sum(1 for r in done.values() if r["verdict"] == "PROMOTED")
            print(f"  {len(done):4d}/{args.trials}  promoted={p}  cum ${_SPEND['usd']:.2f}")

    # --- audit + summary (over ALL persisted rows, across restarts) ------------
    all_rows = list(done.values())
    print("\n" + "=" * 74)
    audit = subprocess.run(
        [sys.executable, str(_HERE / "audit_ledger.py"),
         "--ledger", str(out / "ledger.json"), "--keys", str(keys)],
        capture_output=True, text=True)
    for line in audit.stdout.splitlines():
        if "records |" in line or "AUDIT OK" in line or "INVALID" in line:
            print("  " + line.strip())

    n = len(all_rows)
    promoted = [r for r in all_rows if r["verdict"] == "PROMOTED"]
    overfit = [r for r in all_rows if r["verdict"] == "OVERFIT"]
    print("\n" + "=" * 74)
    print(f"  TRIALS RUN: {n}   TOTAL LLM SPEND: ${_SPEND['usd']:.2f}  "
          f"(${_SPEND['usd']/max(n,1):.3f}/trial)")
    print(f"  DISCOVERED + generalised (PROMOTED): {len(promoted)}/{n}")
    print(f"  memorised (OVERFIT, caught)        : {len(overfit)}/{n}")
    print(f"  failed to infer / no gain          : {n - len(promoted) - len(overfit)}/{n}")
    if promoted:
        import collections
        byop = collections.Counter(r["op"] for r in promoted)
        print(f"  operations discovered              : {dict(sorted(byop.items()))}")
        print(f"  mean held-out uplift on promotions : "
              f"+{sum(r['uplift'] for r in promoted)/len(promoted):.3f}")
    print(f"  ledger signatures re-verified      : audit exit {audit.returncode} (0=all valid)")

    if persistent or args.keep:
        print(f"\n  kept: {out}")
    else:
        import shutil
        shutil.rmtree(out, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
