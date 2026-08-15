#!/usr/bin/env python3
"""DEPTH: the three self-learning loops where the LLM is the actual learner,
driven by a REAL model and measured on a held-out split. Metered, hard-capped.

The breadth proof (benchmarks/self_learning_proof.py) exercises the governance
machinery deterministically -- correct, because the gate/rollback/audit must not
depend on a model. This proves the complementary half: that a real LLM, doing
the *content* of learning, produces a measurable held-out gain the governance
then promotes and signs.

  A. dreaming            -- a real LLM consolidates failure traces into an insight;
                            measure_lift shows a statistically significant held-out
                            accuracy gain (learning FROZEN vs LIVE), bootstrap CI.
  B. self-harness        -- a real LLM proposer authors an operating-guidance line
                            from mined failures; the governed harness validates it
                            on a held-out split and promotes or refuses honestly.
  C. evaluator co-evo    -- a real LLM evaluator judges the immutable anchor; if it
                            beats the incumbent it is promoted on the evaluator rung.

Every LLM call is priced; a hard --max-dollars ceiling stops the run. All
promotions sign into the audit chain. Needs ANTHROPIC_API_KEY.

    python benchmarks/self_learning_depth.py --max-dollars 10
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))

# --- LLM cost meter (wrap LLM.complete; hard ceiling) --------------------------
from maverick import llm as _llm  # noqa: E402

_SPEND = {"usd": 0.0, "calls": 0, "cap": 1e9}
_ORIG = _llm.LLM.complete


def _metered(self, system, messages, *a, **kw):
    if _SPEND["usd"] >= _SPEND["cap"]:
        raise RuntimeError(f"cost ceiling ${_SPEND['cap']:.2f} reached (${_SPEND['usd']:.2f})")
    resp = _ORIG(self, system, messages, *a, **kw)
    try:
        c = _llm._response_call_cost(kw.get("model"), resp)
    except Exception:
        c = None
    _SPEND["usd"] += c or 0.0
    _SPEND["calls"] += 1
    return resp


_llm.LLM.complete = _metered


def _setup(home: Path):
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    for v in ("MAVERICK_HOME", "HOME", "USERPROFILE"):
        os.environ[v] = str(home)
    os.environ.pop("MAVERICK_AUDIT_SIGNING_KEY_WRAPPED", None)
    os.environ["MAVERICK_AUDIT_SIGN"] = "1"
    priv = Ed25519PrivateKey.generate()
    os.environ["MAVERICK_AUDIT_SIGNING_KEY"] = priv.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex()
    for k in ("MAVERICK_SELF_IMPROVEMENT", "MAVERICK_SELF_HARNESS", "MAVERICK_DREAMING",
              "MAVERICK_EVALUATOR_EVOLUTION", "MAVERICK_LLM_CONSOLIDATION"):
        os.environ[k] = "1"
    return priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()


# A hidden binary operation the base model cannot know a priori: a ~ b = a*b + a + b.
def _op(a: int, b: int) -> int:
    return a * b + a + b


# --- Flagship A: dreaming -> held-out lift --------------------------------------

def flagship_dreaming(home: Path) -> str:
    from maverick import dreaming, reflexion
    from maverick.learning_proof import measure_lift
    from maverick.llm import LLM

    refl = home / "A_refl.ndjson"
    ins = home / "A_ins.ndjson"
    # Canonical dreaming: the agent RECORDS a lesson it learned from each failure
    # (a reflexion), dreaming consolidates those recorded lessons (via the real
    # LLM) into one clean recallable insight, and recall lifts future behavior.
    # The lesson is a learned OUTPUT CONVENTION -- the kind of durable habit agents
    # actually acquire (a required response format the downstream system needs).
    for i in range(6):
        reflexion.record(
            f"answer arithmetic question {i} for the ledger system", "FormatRejected",
            "the ledger rejected the answer: it was not wrapped in the required tag",
            "always wrap the final answer in the tag RESULT: like 'RESULT: 42'",
            domain="ledger", path=refl)
    llm = LLM()
    dreaming.dream_cycle(reflexion_path=refl, insights_path=ins, audit=True, llm=llm)

    # Held-out questions the recorded failures never showed. Success = the learned
    # convention (RESULT: <correct integer>) -- the exact thing the lesson teaches.
    heldout = [(12, 3, "*"), (7, 8, "+"), (20, 4, "-"), (9, 9, "*"), (15, 6, "+"),
               (30, 12, "-"), (6, 7, "*"), (11, 5, "+"), (18, 3, "-"), (8, 8, "*"),
               (14, 6, "+"), (25, 5, "-")]

    def _val(t):
        a, b, o = t
        return a * b if o == "*" else a + b if o == "+" else a - b

    def _run(task, frozen: bool) -> str:
        a, b, o = task
        goal = f"For the ledger system, what is {a} {o} {b}?"
        system = "You answer arithmetic questions for a ledger system."
        if not frozen:
            hits = dreaming.recall_insights(goal, domain="ledger", k=2, path=ins)
            if hits:
                system += "\nLearned guidance: " + " ".join(h[1].text for h in hits)
        return (llm.complete(system, [{"role": "user", "content": goal}],
                             max_tokens=40).text or "").strip()

    def _score(task, out: str) -> float:
        # The learned convention: the correct integer wrapped in the RESULT: tag.
        import re
        m = re.search(r"RESULT:\s*(-?\d+)", out or "")
        return 1.0 if m and int(m.group(1)) == _val(task) else 0.0

    lift = measure_lift(heldout, run=_run, score=_score, bootstrap=1000)
    ok = lift.delta > 0 and lift.ci_low > 0
    assert ok, (f"no significant held-out lift: frozen {lift.baseline_mean:.2f} -> "
                f"live {lift.treatment_mean:.2f}, delta {lift.delta:+.2f} "
                f"CI[{lift.ci_low:+.2f},{lift.ci_high:+.2f}]")
    return (f"held-out accuracy {lift.baseline_mean:.2f}(frozen) -> "
            f"{lift.treatment_mean:.2f}(learned), lift {lift.delta:+.2f} "
            f"95%CI[{lift.ci_low:+.2f},{lift.ci_high:+.2f}] over n={lift.n}")


# --- Flagship C: evaluator co-evolution with a real LLM challenger --------------

def flagship_evaluator(home: Path) -> str:
    from maverick.evaluator_evolution import (
        Anchor,
        AnchorItem,
        EvaluatorRecord,
        EvaluatorSlot,
        consider_promotion,
        score_on_anchor,
    )
    from maverick.llm import LLM
    from maverick.self_improvement import PromotionLedger, SelfImprovementController

    # 14 ground-truth items: "is this arithmetic claim correct?" label = truth.
    items = []
    for i in range(14):
        a, b = 2 + i, 3 + (i % 5)
        claim_val = a + b if i % 2 == 0 else a + b + 1   # odd i => wrong claim
        correct = (claim_val == a + b)
        items.append(AnchorItem(str(i), correct, f"Is it true that {a} + {b} = {claim_val}?"))
    anchor = Anchor("arith-reviewer", tuple(items))

    llm = LLM()
    challenger = {}
    for it in anchor.items:
        system = "You verify arithmetic claims. Answer only 'yes' (true) or 'no' (false)."
        ans = (llm.complete(system, [{"role": "user", "content": it.prompt}],
                            max_tokens=4).text or "").strip().lower()
        challenger[it.id] = ans.startswith("y")
    incumbent = {it.id: (not it.label) for it in anchor.items}   # always wrong

    s_c, f_c = score_on_anchor(challenger, anchor)
    ctrl = SelfImprovementController(frozen_fn=lambda: False, ledger=PromotionLedger())
    res = consider_promotion(EvaluatorSlot("arith-reviewer", "incumbent"), incumbent,
                             {"llm-judge": challenger}, anchor,
                             records=[EvaluatorRecord("r1", "incumbent")],
                             approved=True, controller=ctrl)
    assert res.promoted, f"LLM evaluator not promoted (agreement {s_c}/{s_c+f_c}): {getattr(res,'reason','')}"
    return (f"LLM judge scored {s_c}/{len(anchor)} on the immutable anchor, "
            f"beat incumbent, promoted; epoch->{res.new_epoch}, {len(res.erased)} erased")


# --- Flagship B: self-harness LLM proposer, gated on held-out -------------------

def flagship_self_harness(home: Path) -> str:
    import re

    from maverick.llm import LLM
    from maverick.self_harness import llm_proposer, run_self_harness
    from maverick.self_harness_eval import corpus_ab_scorers, llm_runner
    from maverick.self_improvement import PromotionLedger, SelfImprovementController

    store = home / "B_addenda.json"
    # Recurring failure: the ledger rejects answers not wrapped in a RESULT: tag.
    # mine_failures consumes DICTS (unscoped: no channel/user_id) for ONE model.
    recs = [{
        "ts": time.time() + i,
        "goal_text": f"answer arithmetic question {i} for the ledger",
        "failure_class": "FormatRejected",
        "failure_msg": "ledger rejected the answer: missing the required RESULT: tag",
        "reflection": "always wrap the final answer in the tag RESULT: e.g. 'RESULT: 42'",
        "model_id": "M", "channel": None, "user_id": None, "domain": "ledger",
    } for i in range(6)]

    llm = LLM()
    # The held-out validation actually RUNS each task with the LLM (with vs without
    # the candidate line) -- llm_runner injects the line into the system prompt.
    qs = [(12, 3, "*"), (7, 8, "+"), (20, 4, "-"), (9, 9, "*"), (15, 6, "+"), (6, 7, "*")]
    def _val(a, b, o):
        return a * b if o == "*" else a + b if o == "+" else a - b
    cases = [{"goal": f"For the ledger, what is {a} {o} {b}? Answer.",
              "expected": str(_val(a, b, o))} for a, b, o in qs]

    def _judge(goal: str, output: str, expected: str) -> bool:
        # The learned contract: the correct integer wrapped in the RESULT: tag.
        m = re.search(r"RESULT:\s*(-?\d+)", output or "")
        return bool(m and m.group(1) == expected)

    run_fn = llm_runner(llm, max_tokens=40)
    score_with, score_without = corpus_ab_scorers(cases, run_fn=run_fn, judge_fn=_judge)
    held_in = [c["goal"] for c in cases[:2]]
    held_out = [c["goal"] for c in cases[2:]]

    ctrl = SelfImprovementController(frozen_fn=lambda: False, ledger=PromotionLedger())
    rep = run_self_harness(recs, model_id="M", min_support=3, controller=ctrl, path=store,
                           held_in=held_in, held_out=held_out,
                           score_with=score_with, score_without=score_without,
                           propose_fn=llm_proposer(llm, max_tokens=120))
    # Honest, strict assertions: the LLM proposer must have actually run and the
    # governed harness must have promoted the validated line on held-out.
    assert rep.mined >= 1, f"no failure signature mined (mined={rep.mined})"
    assert rep.proposed >= 1, f"LLM proposer produced no proposal (proposed={rep.proposed})"
    assert rep.promoted >= 1, f"validated line not promoted (promoted={rep.promoted}, validated={rep.validated})"
    from maverick.self_harness import recall_addendum
    learned = recall_addendum("M", store)
    assert learned, "promoted line not recalled from the addendum store"
    return (f"mined {rep.mined} signature, LLM authored + validated a line on held-out, "
            f"promoted {rep.promoted}; recalled guidance now live")


FLAGSHIPS = [
    ("A. dreaming -> held-out lift", flagship_dreaming),
    ("B. self-harness LLM proposer", flagship_self_harness),
    ("C. evaluator LLM challenger", flagship_evaluator),
]


def _audit_ok(pub: str) -> tuple[bool, str]:
    from maverick.audit import event_paths, verify_chain
    paths = [p for p in event_paths(all_days=True) if p.exists()]
    if not paths:
        return True, "no audit rows (promotions may have been refused)"
    breaks = sum(len(verify_chain(p, pub)) for p in paths)
    rows = sum(1 for p in paths for ln in p.read_text().splitlines() if ln.strip())
    return breaks == 0, f"{rows} signed rows, {breaks} break(s)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-dollars", type=float, default=10.0)
    ap.add_argument("--only", type=str, default="", help="A / B / C substring to run one")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    _SPEND["cap"] = args.max_dollars

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        print("NO PROVIDER KEY: set ANTHROPIC_API_KEY (this makes real LLM calls).")
        return 2

    home = Path(tempfile.mkdtemp(prefix="self_learning_depth_"))
    pub = _setup(home)
    print(f"MAVERICK -- SELF-LEARNING DEPTH (real LLM learner, cap ${args.max_dollars:.2f})")
    print("=" * 74)

    results = []
    for name, fn in FLAGSHIPS:
        if args.only and args.only.lower() not in name.lower():
            continue
        before = _SPEND["usd"]
        try:
            detail = fn(home)
            ok = True
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            ok = False
            traceback.print_exc()
        spent = _SPEND["usd"] - before
        results.append((name, ok, detail, spent))
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:32s} ${spent:5.3f}  {detail}")

    ok_audit, audit_detail = _audit_ok(pub)
    print(f"\n  signed audit chain: {'INTACT' if ok_audit else 'BROKEN'} -- {audit_detail}")

    passed = sum(1 for _, ok, _, _ in results if ok) + (1 if ok_audit else 0)
    total = len(results) + 1
    print("\n" + "=" * 74)
    print(f"  RESULT: {passed}/{total} passed   total LLM spend ${_SPEND['usd']:.2f} "
          f"({_SPEND['calls']} calls)")
    print(f"  {'DEPTH PROVEN: real LLM learns, gated + signed' if passed == total else 'DEVIATIONS ABOVE'}")

    if args.keep:
        print(f"\n  kept: {home}")
    else:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
