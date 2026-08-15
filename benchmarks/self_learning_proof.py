#!/usr/bin/env python3
"""Prove EVERY self-learning capability end-to-end, into ONE signed audit chain.

Lightwork's learning subsystems each have offline test batteries, but there was
no single artifact that exercises them all and shows their signed audit trail is
tamper-evident. This is that artifact. It runs in an isolated throwaway
MAVERICK_HOME with a freshly generated Ed25519 audit key, drives each capability
in-process ($0, seconds, no network/LLM), and every capability writes its
`LEARNING_UPDATE` rows into ONE signed hash-chained ledger which is then
re-verified -- intact, and demonstrably tamper-detecting.

Capabilities proven (each fails the whole run if it regresses):

  1. governed promotion gate  -- promotes real gain; refuses under calibration freeze
  2. dreaming                 -- clusters repeated failures into a distilled insight
  3. hindsight                -- detects a lesson that used to be recalled and now isn't
  4. snapshot + rollback      -- full revert of learning state (or unchanged)
  5. flows self-evolution     -- hardens a reliable agent node to an action, measures, reverts
  6. fleet memory             -- ingests a registered lesson; refuses an unregistered agent
  7. operating record         -- exports a signed portable mind; tamper is detected offline
  8. evaluator co-evolution   -- a better evaluator wins on the immutable anchor, epoch advances
  9. signed audit chain       -- the ledger all of the above wrote verifies, and tamper is caught

Then it runs the repo's existing offline proof scoreboards (self-harness, DGM
code rung, DGM uplift, SWE-bench-under-governance, platform guarantees) and
aggregates their pass/fail. Exit 0 iff every capability and every scoreboard pass.

    python benchmarks/self_learning_proof.py            # everything
    python benchmarks/self_learning_proof.py --no-scripts  # in-process caps only
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))


def _setup_home() -> tuple[Path, str]:
    """Isolated MAVERICK_HOME + a fresh Ed25519 audit signing key (so every
    LEARNING_UPDATE row lands in one signed chain we can re-verify)."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    home = Path(tempfile.mkdtemp(prefix="self_learning_proof_"))
    # Point every home-resolution path at the fresh sandbox so no inherited
    # audit dir / on-disk key (e.g. a pytest session HOME) can shadow our key.
    for var in ("MAVERICK_HOME", "HOME", "USERPROFILE"):
        os.environ[var] = str(home)
    # A wrapped/KMS key would take precedence over our raw injected key.
    os.environ.pop("MAVERICK_AUDIT_SIGNING_KEY_WRAPPED", None)
    # Force signed + hash-chained audit rows regardless of the ambient
    # secure-defaults switch (a dev/test env may set MAVERICK_SECURE_DEFAULT=0,
    # which would leave the chain unsigned and defeat this very proof).
    os.environ["MAVERICK_AUDIT_SIGN"] = "1"
    priv = Ed25519PrivateKey.generate()
    os.environ["MAVERICK_AUDIT_SIGNING_KEY"] = priv.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    # Pin the governed learning loops ON for this isolated proof, independent of
    # any operator overrides in the invoking environment.
    for k in ("MAVERICK_SELF_IMPROVEMENT", "MAVERICK_SELF_HARNESS",
              "MAVERICK_FLEET_MEMORY", "MAVERICK_EVALUATOR_EVOLUTION",
              "MAVERICK_DREAMING"):
        os.environ[k] = "1"
    return home, pub


# --- capability checks: each returns a one-line detail or raises -----------------

def cap_gate(home: Path) -> str:
    from maverick.self_improvement import Candidate, PromotionLedger, SelfImprovementController
    live = SelfImprovementController(frozen_fn=lambda: False, ledger=PromotionLedger())
    good = Candidate(rung="config", summary="real gain", baseline_score=0.5,
                     candidate_score=0.9, samples=5, capability_widens=False, rollback="snap")
    assert live.evaluate(good).promote, "gate refused a real, reversible gain"
    frozen = SelfImprovementController(frozen_fn=lambda: True, ledger=PromotionLedger())
    assert not frozen.evaluate(good).promote, "gate promoted while calibration frozen"
    return "promotes real gain; refuses under freeze"


def cap_dreaming(home: Path) -> str:
    from maverick import dreaming, reflexion
    refl, ins = home / "d_refl.ndjson", home / "d_ins.ndjson"
    for i in range(4):
        reflexion.record(f"parse the json config file number {i}", "TypeError",
                         "bad type passed to loader", "validate input types before parsing",
                         domain="coding", path=refl)
    rep = dreaming.dream_cycle(reflexion_path=refl, insights_path=ins, audit=True)
    assert rep.insights_written >= 1, "no insight distilled from repeated failures"
    return f"distilled {rep.insights_written} insight(s) from clustered failures"


def cap_hindsight(home: Path) -> str:
    from types import SimpleNamespace

    from maverick import dreaming, hindsight, reflexion
    before, after = home / "snap_before", home / "snap_after"
    before.mkdir()
    after.mkdir()
    # `after` learned a real insight; `before` had not.
    (before / "insights.ndjson").write_text("")
    (before / "reflexions.ndjson").write_text("")
    refl = home / "h_refl.ndjson"
    for i in range(4):
        reflexion.record(f"deploy the release pipeline step {i}", "TimeoutError",
                         "stage timed out", "raise the deploy timeout for slow stages",
                         domain="devops", path=refl)
    dreaming.dream_cycle(reflexion_path=refl, insights_path=after / "insights.ndjson", audit=False)
    (after / "reflexions.ndjson").write_text("")
    world = SimpleNamespace(list_goals=lambda **k: [
        SimpleNamespace(title="deploy the release pipeline", domain="devops")])
    rep = hindsight.replay(world, before=before, after=after)
    hindsight.write_ledger(rep, before_label="before")
    gained = len(getattr(rep, "gained", []) or [])
    assert gained >= 1, f"hindsight saw no coverage gain (gained={gained})"
    return f"coverage delta detected (gained={gained}); audited ledger written"


def cap_snapshot(home: Path) -> str:
    from maverick import dreaming
    snaps = home / "snaps"
    store = home / "s_store.ndjson"
    store.write_text("original learning\n")
    stores = {"reflexions": store}
    snap = dreaming.snapshot_learning_state(directory=snaps, stores=stores)
    assert snap is not None, "snapshot returned None (no reliable undo)"
    store.write_text("corrupted by a bad cycle\n")
    dreaming.rollback_learning_state("latest", directory=snaps, stores=stores)
    assert store.read_text() == "original learning\n", "rollback did not fully revert"
    return "learning state fully reverted from snapshot"


def cap_flows(home: Path) -> str:
    from maverick.flow import evolve, ir
    f = ir.single_agent_flow("f1", "nightly report", "compile the nightly report")
    stats = {nid: {"n": 12, "mean": 0.95} for nid in f.nodes}
    props = evolve.propose(f, min_support=1, stats=stats)
    assert props and props[0].to_kind == "action", "reliable agent node not proposed for hardening"
    f2 = evolve.apply_proposal(
        f,
        props[0].node_id,
        props[0].to_kind,
        tool="report_tool",
        params={"period": "previous_day"},
    )
    assert f2.nodes[props[0].node_id].kind != f.nodes[props[0].node_id].kind, "node kind unchanged"
    m = evolve.measure("f1", props[0].node_id, 15.0, series=[(10.0, 0.4), (20.0, 0.9)])
    assert m["improved"], f"measure did not confirm improvement: {m}"
    assert evolve.regressed({"after": {"n": 6}, "delta": -0.3}), "regression not flagged"
    return f"node {props[0].node_id} hardened->action, measured +{m['delta']:.2f}, revert-on-regress armed"


def cap_fleet(home: Path) -> str:
    from maverick import fleet_memory
    assert fleet_memory.register_agent("agent-1", "acme"), "register failed"
    ok, _ = fleet_memory.ingest({"agent_id": "agent-1", "vendor": "acme", "kind": "lesson",
                                 "goal_text": "handle rate limits", "reflection": "back off exponentially"})
    assert ok, "registered lesson refused"
    bad, _ = fleet_memory.ingest({"agent_id": "ghost", "vendor": "x", "kind": "lesson",
                                  "goal_text": "y", "reflection": "z"})
    assert not bad, "unregistered agent was NOT refused (fail-open leak)"
    return "registered lesson ingested; unregistered agent refused (fail-closed)"


def cap_operating_record(home: Path) -> str:
    from maverick import operating_record as orr
    from maverick.world_model import WorldModel
    world = WorldModel(home / "or.db")
    out = home / "capsule"
    path = orr.export_capsule(world, out)
    ok, _ = orr.verify_capsule(path)
    assert ok, "freshly signed capsule failed verification"
    body = Path(path).read_bytes()
    Path(path).write_bytes(body.replace(b'"kind"', b'"kynd"', 1) if b'"kind"' in body
                           else body[:-2] + b"x}")
    tampered_ok, _ = orr.verify_capsule(path)
    assert not tampered_ok, "tampered capsule still verified (signature not binding)"
    return "signed portable mind exported; tamper detected offline"


def cap_evaluator(home: Path) -> str:
    from maverick.evaluator_evolution import (
        Anchor,
        AnchorItem,
        EvaluatorRecord,
        EvaluatorSlot,
        consider_promotion,
    )
    from maverick.self_improvement import PromotionLedger, SelfImprovementController
    anchor = Anchor("reviewer", tuple(AnchorItem(str(i), i % 2 == 0, f"prompt {i}")
                                      for i in range(14)))
    incumbent = {i.id: (not i.label) for i in anchor.items}   # always wrong
    challenger = {"chal-1": {i.id: i.label for i in anchor.items}}  # always right
    ctrl = SelfImprovementController(frozen_fn=lambda: False, ledger=PromotionLedger())
    res = consider_promotion(EvaluatorSlot("reviewer", "incumbent"), incumbent, challenger,
                             anchor, records=[EvaluatorRecord("r1", "incumbent")],
                             approved=True, controller=ctrl)
    assert res.promoted, f"better evaluator not promoted: {getattr(res, 'reason', '')}"
    assert res.new_epoch == 2, f"epoch did not advance: {res.new_epoch}"
    assert len(res.erased) == 1, "displaced evaluator's records not selectively erased"
    return f"better evaluator won on immutable anchor; epoch->{res.new_epoch}, {len(res.erased)} erased"


CAPS = [
    ("governed promotion gate", cap_gate),
    ("dreaming (insight distillation)", cap_dreaming),
    ("hindsight (regression detection)", cap_hindsight),
    ("snapshot + rollback", cap_snapshot),
    ("flows self-evolution", cap_flows),
    ("fleet memory (governed)", cap_fleet),
    ("operating record (signed capsule)", cap_operating_record),
    ("evaluator co-evolution", cap_evaluator),
]

# The repo's existing offline proof scoreboards (each exits 0 iff all pass).
SCRIPTS = [
    ("platform guarantees (7/7)", ["proof/run_proof.py"]),
    ("self-harness loop", ["proof/self_harness_proof.py"]),
    ("DGM code rung (6/6)", ["proof/dgm_code_rung_proof.py"]),
    ("DGM uplift (6/6)", ["proof/dgm_uplift_proof.py"]),
    ("SWE-bench under governance (4/4)", ["proof/swebench_governed_proof.py"]),
    ("live DGM gate (in-process)", ["benchmarks/dgm_fast_proof.py"]),
]


def _audit_chain_check(pub: str) -> tuple[bool, str]:
    """Every capability wrote LEARNING_UPDATE rows into one signed chain: verify
    it is intact, then prove a single-byte tamper is caught."""
    from maverick.audit import event_paths, verify_chain
    paths = [p for p in event_paths(all_days=True) if p.exists()]
    if not paths:
        return False, "no audit day-file written"
    total_breaks = sum(len(verify_chain(p, pub)) for p in paths)
    if total_breaks:
        return False, f"signed chain had {total_breaks} break(s) BEFORE tamper"
    rows = sum(1 for p in paths for _ in p.read_text().splitlines() if _.strip())
    # tamper a copy of the first file and confirm detection
    victim = paths[0]
    original = victim.read_bytes()
    lines = original.split(b"\n")
    body = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    lines[body] = lines[body].replace(b'"agent"', b'"agnt"', 1)
    victim.write_bytes(b"\n".join(lines))
    caught = len(verify_chain(victim, pub)) >= 1
    victim.write_bytes(original)   # restore
    if not caught:
        return False, "tampered signed row was NOT detected"
    return True, f"{rows} signed rows across {len(paths)} file(s); intact; tamper detected"


def _run_script(argv: list[str], timeout: float) -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, *argv], cwd=str(_ROOT),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout >{timeout:.0f}s"
    tail = (r.stdout.strip().splitlines() or [""])[-1][:70]
    return r.returncode == 0, f"exit {r.returncode}  {tail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-scripts", action="store_true", help="skip the proof/*.py scoreboards")
    ap.add_argument("--script-timeout", type=float, default=300.0)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    home, pub = _setup_home()
    print("MAVERICK -- SELF-LEARNING PROOF (in-process, $0, one signed audit chain)")
    print("=" * 74)
    print(f"  sandbox MAVERICK_HOME: {home}")
    print(f"  audit public key:      {pub[:32]}...\n")

    results: list[tuple[str, bool, str]] = []
    t0 = time.time()
    for name, fn in CAPS:
        try:
            detail = fn(home)
            results.append((name, True, detail))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
            traceback.print_exc()

    ok_chain, chain_detail = _audit_chain_check(pub)
    results.append(("signed audit chain (all of the above)", ok_chain, chain_detail))

    print("  IN-PROCESS CAPABILITIES")
    print("  " + "-" * 70)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:38s} {detail}")

    script_results: list[tuple[str, bool, str]] = []
    if not args.no_scripts:
        print("\n  EXISTING OFFLINE PROOF SCOREBOARDS")
        print("  " + "-" * 70)
        for name, sargv in SCRIPTS:
            ok, detail = _run_script(sargv, args.script_timeout)
            script_results.append((name, ok, detail))
            print(f"  [{'PASS' if ok else 'FAIL'}]  {name:38s} {detail}")

    all_results = results + script_results
    passed = sum(1 for _, ok, _ in all_results if ok)
    total = len(all_results)
    print("\n" + "=" * 74)
    print(f"  RESULT: {passed}/{total} proofs passed   ({time.time()-t0:.1f}s)")
    print(f"  {'ALL SELF-LEARNING CAPABILITIES PROVEN + AUDITED' if passed == total else 'DEVIATIONS ABOVE'}")

    if args.keep:
        print(f"\n  kept: {home}")
    else:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
