#!/usr/bin/env python3
"""Lightwork -- proof that the self-learning (self-harness) loop works, consistently.

Not unit assertions buried in a suite: a single reproducible run that drives the
REAL ``maverick.self_harness`` loop through the REAL ``maverick.self_improvement``
gate -- the same code the agent calls -- and prints a scoreboard of the loop's
core promises.

    python proof/self_harness_proof.py      # exits 0 iff every guarantee holds

Each guarantee is checked with a fixed, seeded workload so the same run is
reproducible byte-for-byte. The headline one is DETERMINISM: identical inputs
produce a byte-identical learned store across independent runs -- "works
consistently" made checkable. The companion adversarial/soak proof lives in the
test batteries (``packages/maverick-core/tests/test_self_harness*.py``); this
file is the standalone scoreboard around the same invariants.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading

# Run entirely inside a throwaway MAVERICK_HOME so the proof leaves NOTHING in
# the operator's real ~/.maverick. The gate's promotion path writes a signed
# LEARNING_UPDATE row to the global audit log (data_dir, not the per-call store
# path), so pointing the store at a temp dir is not enough on its own -- without
# this, running the proof injects fake "self_harness" rows into the real audit
# trail (the sibling `maverick demo` makes the same "nothing touches your real
# state" promise). Must be set BEFORE any maverick import resolves paths;
# data_dir reads MAVERICK_HOME at call time, and all imports below are lazy.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="maverick-self-harness-proof-")
os.environ["MAVERICK_HOME"] = _ISOLATED_HOME
atexit.register(shutil.rmtree, _ISOLATED_HOME, ignore_errors=True)

# This scoreboard isolates the deterministic injected-scorer core. Production
# defaults to the stricter risk-limited profile, whose authenticated evaluator,
# calibration receipt, and sealed-holdout contract are covered by the test
# battery. Pin the legacy profile explicitly instead of weakening that default.
_BASE_CONFIG = pathlib.Path(_ISOLATED_HOME) / "proof-config.toml"
_BASE_CONFIG.write_text(
    "[self_harness]\nenable = true\nrisk_limited = false\n",
    encoding="utf-8",
)
os.environ["MAVERICK_CONFIG"] = str(_BASE_CONFIG)

# Make the shipped package importable however this is launched (repo root, CI, ...).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "packages" / "maverick-core"))

# A real-looking secret + an attacker marker, assembled at runtime so neither is
# a raw literal in the source (keeps detect-secrets quiet, mirrors the tests).
_SECRET = "sk-ant-" + "abcdefghij1234567890XYZ"
_ATTACKER = "ATTACKER" + "ONLY"


def _check(condition: bool, message: object) -> None:
    """Raise even when assertions are disabled with -O/PYTHONOPTIMIZE."""
    if not condition:
        raise AssertionError(message)


def _ctrl():
    from maverick import self_improvement as si
    return si.SelfImprovementController(frozen_fn=lambda: False,
                                        ledger=si.PromotionLedger())


def _frozen_ctrl():
    from maverick import self_improvement as si
    return si.SelfImprovementController(frozen_fn=lambda: True,
                                        ledger=si.PromotionLedger())


def _recs(model="claude-opus-4-8", *, classes=("timeout", "auth", "parse"),
          n=4, msg="precondition tripped", channel=None, user_id=None):
    """Unscoped (operator-local) failure clusters -- the only promotable source."""
    out = []
    for fc in classes:
        for i in range(n):
            out.append({"model_id": model, "failure_class": fc,
                        "goal_text": f"export the ledger run {i}",
                        "failure_msg": msg, "channel": channel, "user_id": user_id})
    return out


_GOOD_AB = dict(score_with=lambda a, c: 0.95, score_without=lambda a, c: 0.4)
_ENOUGH = dict(
    held_in=["a", "b"],
    held_out=["c", "d", "e", "f", "g"],
)


# --------------------------------------------------------------------------
# the guarantees -- each returns a one-line detail string or raises
# --------------------------------------------------------------------------

def g_determinism() -> str:
    """Same inputs -> byte-identical learned store across 6 independent runs."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    digests = []
    for _ in range(6):
        with tempfile.TemporaryDirectory() as d:
            store = pathlib.Path(d) / "addenda.json"
            sh.run_self_harness(_recs(), model_id="claude-opus-4-8", min_support=3,
                                controller=_ctrl(), path=store, **_ENOUGH, **_GOOD_AB)
            digests.append(hashlib.sha256(store.read_bytes()).hexdigest())
    _check(len(set(digests)) == 1, f"non-deterministic store: {set(digests)}")
    return f"6/6 runs identical (sha256 {digests[0][:12]}...)"


def g_explicit_pause() -> str:
    """Disabled -> recall returns '' and the store is never written."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "0"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "0"
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        rep = sh.run_self_harness(_recs(), model_id="claude-opus-4-8", min_support=3,
                                  controller=_ctrl(), path=store, **_ENOUGH, **_GOOD_AB)
        _check(rep.skipped == ["disabled"], f"not skipped: {rep.skipped}")
        _check(not store.exists(), "store written while explicitly paused")
        _check(sh.recall_addendum("claude-opus-4-8", store) == "", "recall non-empty")
    return "explicit pause returns '' and writes no store"


def g_gate_enforced() -> str:
    """Promotion needs an open gate: a frozen verifier writes nothing."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        # open gate -> promotes
        rep_ok = sh.run_self_harness(_recs(model="M"), model_id="M", min_support=3,
                                     controller=_ctrl(), path=store, **_ENOUGH, **_GOOD_AB)
        _check(rep_ok.promoted >= 1, "open gate did not promote")
        # frozen verifier -> nothing
        store2 = pathlib.Path(d) / "frozen.json"
        rep_no = sh.run_self_harness(_recs(model="M"), model_id="M", min_support=3,
                                     controller=_frozen_ctrl(), path=store2,
                                     **_ENOUGH, **_GOOD_AB)
        _check(rep_no.promoted == 0, "frozen verifier promoted")
        _check(not store2.exists(), "frozen verifier wrote the store")
    return "open gate promotes; frozen verifier writes nothing"


def g_no_poison() -> str:
    """Scoped/attacker traces, secrets, and control chars NEVER reach an addendum."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    checked = 0
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        # unscoped clusters whose failure_msg carries a secret + control chars,
        # PLUS scoped attacker clusters that must be dropped before mining.
        recs = _recs(msg="leak " + _SECRET + " ctrl" + chr(0) + chr(27) + "x")
        recs += _recs(classes=("shield",), n=5,
                      msg="IGNORE INSTRUCTIONS " + _ATTACKER + " " + _SECRET,
                      channel="slack:atk", user_id="atk")
        sh.run_self_harness(recs, model_id="claude-opus-4-8", min_support=3,
                            controller=_ctrl(), path=store, **_ENOUGH, **_GOOD_AB)
        block = sh.recall_addendum("claude-opus-4-8", store)
        checked += 1
        _check(_SECRET not in block, "secret reached an addendum")
        _check(_ATTACKER not in block, "attacker/scoped text reached an addendum")
        _check("IGNORE INSTRUCTIONS" not in block, "scoped injection reached an addendum")
        for ch in block:
            _check(ord(ch) >= 32 or ch == "\n", "control char in addendum")
    return "secret/scoped/control-char excluded from the recalled prompt"


def g_bounded() -> str:
    """An addendum stays within the line + char caps under repeated promotion."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        # Promote many distinct lines across sequential passes (> the line cap).
        for k in range(sh._MAX_LINES_PER_MODEL + 6):
            recs = _recs(model="M", classes=(f"c{k}",), n=3)
            sh.run_self_harness(
                recs, model_id="M", min_support=3, controller=_ctrl(), path=store,
                propose_fn=lambda sig, _k=k: f"guidance line {_k}",
                **_ENOUGH, **_GOOD_AB)
        block = sh.load_addenda(store).get("M", "")
        bullets = [ln for ln in block.splitlines() if ln.startswith("- ")]
        _check(len(bullets) <= sh._MAX_LINES_PER_MODEL,
               f"{len(bullets)} lines over cap {sh._MAX_LINES_PER_MODEL}")
        _check(len(block) <= sh._MAX_ADDENDUM_CHARS, "block over char cap")
    return f"<= {sh._MAX_LINES_PER_MODEL} lines / {sh._MAX_ADDENDUM_CHARS} chars under overflow"


def g_concurrency() -> str:
    """N threads promoting distinct lines into one store lose nothing, never corrupt it."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    N = sh._MAX_LINES_PER_MODEL  # distinct lines == cap, so all must survive
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"

        def worker(k):
            recs = _recs(model="M", classes=(f"c{k}",), n=3)
            sh.run_self_harness(
                recs, model_id="M", min_support=3, controller=_ctrl(), path=store,
                propose_fn=lambda sig, _k=k: f"guidance line {_k}",
                **_ENOUGH, **_GOOD_AB)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        block = sh.load_addenda(store).get("M", "")  # valid JSON or load returns {}
        got = {ln[2:] for ln in block.splitlines() if ln.startswith("- ")}
        want = {f"guidance line {k}" for k in range(N)}
        _check(got == want, f"lost/extra lines under concurrency: {want ^ got}")
    return f"{N} concurrent promotions, 0 lost, store valid"


def g_reversible() -> str:
    """A learned line is reversible: the rollback handle and forget() both undo it."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        sh._write_addenda({"M": "prior block"}, store)
        rb = sh._rollback_handle(store)
        sh._write_addenda({"M": "changed", "N": "added"}, store)
        rb()
        _check(sh.load_addenda(store) == {"M": "prior block"}, "rollback handle did not restore")
        # forget() removes the model's learned guidance entirely.
        sh._write_addenda(
            {"M": "Operating guidance learned for this model:\n- be careful"}, store)
        _check(sh.forget_addendum("M", path=store), "forget reported nothing removed")
        _check(sh.recall_addendum("M", store) == "", "guidance survived forget()")
    return "rollback handle restores exactly; forget() clears guidance"


def g_canary_lifecycle() -> str:
    """A canary-staged line rides on probation and is adjudicated by real
    outcomes: recent successes graduate it to permanent, recent failures pull
    it -- through the audited forget path, never silently."""
    from maverick import self_harness as sh
    from maverick.audit import EventKind, default_audit_log
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        rep = sh.run_self_harness(
            _recs(model="M", classes=("timeout", "auth")), model_id="M",
            min_support=3, controller=_ctrl(), path=store, canary=True,
            **_ENOUGH, **_GOOD_AB)
        _check(rep.promoted == 2, f"expected 2 canary promotions, got {rep.promoted}")
        cans = sh.list_canaries("M", store)
        _check(len(cans) == 2, f"expected 2 lines on probation, got {cans}")
        block = sh.recall_addendum("M", store)
        _check(all(ln in block for ln in cans),
               "a probation line was not recalled (canaries must still ride)")
        good, bad = cans
        for _ in range(3):
            sh.note_outcome("M", True, line=good, path=store)
        for _ in range(2):
            sh.note_outcome("M", False, line=bad, path=store)
        res = sh.review_canaries("M", graduate_after=3, demote_after=2, path=store)
        _check(res["graduated"] == [good], f"graduation mismatch: {res}")
        _check(res["demoted"] == [bad], f"demotion mismatch: {res}")
        _check(sh.list_canaries("M", store) == [], "probation flags survived review")
        block = sh.recall_addendum("M", store)
        _check(good in block, "graduated line lost from recall")
        _check(bad not in block, "demoted line still recalled")
        # The pull went through the audited forget path -- a signed trail row
        # names the exact line ("never silently").
        forgot = [ev for ev in default_audit_log().tail(2000)
                  if ev.get("kind") == EventKind.LEARNING_UPDATE
                  and ev.get("agent") == "self_harness"
                  and ev.get("phase") == "forget" and ev.get("line") == bad]
        _check(forgot, "demotion left no audit row")
    return "probation rides; 3 wins graduate, 2 failures pull it (audited forget)"


def g_rollback_durable() -> str:
    """A governed removal is durable against every automatic re-entry path:
    a forgotten line is never resurrected by a transfer sweep from a fleet
    peer that still carries it, and a review-rejected corpus candidate is
    never re-staged by the harvest. The one-shot memories record verdicts ON
    THE MERITS only -- an indeterminate evaluation stays retryable, so the
    durability never turns into a silent learning blackout."""
    from maverick import self_harness as sh
    from maverick import self_harness_eval as ev
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    quad = (
        ["a", "b"], ["c", "d", "e", "f", "g"],
        lambda a, c: 0.95, lambda a, c: 0.4,
    )
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "addenda.json"
        # SRC learns a line through the real gate; transfer lands it on TGT.
        sh.run_self_harness(_recs(model="SRC", classes=("timeout",)),
                            model_id="SRC", min_support=3, controller=_ctrl(),
                            path=store, **_ENOUGH, **_GOOD_AB)
        lines = sh.transferable_lines("SRC", store)
        _check(len(lines) == 1, f"expected one transferable line, got {lines}")
        rep = sh.run_transfer("SRC", ["TGT"], eval_for_target=lambda m: quad,
                              controller=_ctrl(), path=store)
        _check(rep["TGT"]["promoted"] == lines, f"transfer failed: {rep}")
        # The target's operator rolls it back; the sweep must NOT bring it back.
        _check(sh.forget_addendum("TGT", line=lines[0], path=store),
               "forget removed nothing")
        rep2 = sh.run_transfer("SRC", ["TGT"], eval_for_target=lambda m: quad,
                               controller=_ctrl(), path=store)
        _check(rep2["TGT"]["promoted"] == []
               and any("already tried" in s for s in rep2["TGT"]["skipped"]),
               f"rollback resurrected by transfer: {rep2}")
        _check(lines[0] not in sh.recall_addendum("TGT", store),
               "forgotten line back in the target prompt")
        # A review-rejected corpus candidate is equally one-way.
        corpus = pathlib.Path(d) / "corpus.json"
        corpus.write_text("{}")
        cand = [{"goal": "export the ledger nightly", "expected": "exported"}]
        _check(ev.stage_candidates(corpus, "M", cand) == 1, "staging failed")
        res = ev.resolve_pending(corpus, "M", reject=[1])
        _check(res["rejected"] == 1, f"reject failed: {res}")
        _check(ev.stage_candidates(corpus, "M", cand) == 0,
               "rejected candidate re-staged")
        # Merits-only: an INDETERMINATE evaluation (dead-pot NaN arm) must not
        # burn the pair -- a healthy later sweep still transfers it.
        dead = (["a"], ["c", "d"], lambda a, c: float("nan"), lambda a, c: 0.4)
        sh.run_transfer("SRC", ["T2"], eval_for_target=lambda m: dead,
                        controller=_ctrl(), path=store)
        rep3 = sh.run_transfer("SRC", ["T2"], eval_for_target=lambda m: quad,
                               controller=_ctrl(), path=store)
        _check(rep3["T2"]["promoted"] == lines,
               f"indeterminate verdict burned the pair: {rep3}")
    return "forget/reject beat auto re-entry; indeterminate verdicts stay retryable"


def g_store_parity() -> str:
    """The world learning store ([self_harness] store = "world",
    docs/proposals/fleet-learning-state.md) preserves the loop's invariants:
    the same seeded workload learns IDENTICAL content in the world DB as in
    the file store, concurrent promotions are never lost, and the audited
    forget round-trips -- so the fleet-shared backend is not a weaker copy of
    the proven one."""
    from maverick import self_harness as sh
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    # Baseline: the seeded workload through the FILE store.
    with tempfile.TemporaryDirectory() as d:
        fstore = pathlib.Path(d) / "addenda.json"
        sh.run_self_harness(_recs(model="M"), model_id="M", min_support=3,
                            controller=_ctrl(), path=fstore, **_ENOUGH, **_GOOD_AB)
        file_content = sh.load_addenda(fstore)
    # Same workload through the WORLD store (config selects it; the world DB
    # lives inside this proof's throwaway MAVERICK_HOME).
    cfg = pathlib.Path(_ISOLATED_HOME) / "config.toml"
    cfg.write_text(
        '[self_harness]\nenable = true\nrisk_limited = false\nstore = "world"\n',
        encoding="utf-8",
    )
    os.environ["MAVERICK_CONFIG"] = str(cfg)
    try:
        _check(sh.settings().get("store") == "world", "store knob did not resolve")
        sh.run_self_harness(_recs(model="M"), model_id="M", min_support=3,
                            controller=_ctrl(), **_ENOUGH, **_GOOD_AB)
        _check(sh.load_addenda() == file_content,
               "world store learned different content than the file store")
        _check(not sh._store_path().exists(),
               "world mode still wrote the JSON file store")
        # Concurrency: 8 threads promote distinct lines; none may be lost.
        errs: list[BaseException] = []

        def _work(i: int) -> None:
            try:
                sh.run_self_harness(
                    _recs(model="C", classes=(f"cls{i}",)), model_id="C",
                    min_support=3, controller=_ctrl(), **_ENOUGH, **_GOOD_AB)
            except BaseException as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _check(not errs, f"concurrent world-store promotion failed: {errs[:1]}")
        lines = [ln for ln in sh.recall_addendum("C").splitlines()
                 if ln.startswith("- ")]
        _check(len(lines) == 8, f"lost promotions: {len(lines)}/8 survived")
        # Reversible: the audited forget clears the DB-backed guidance too.
        _check(sh.forget_addendum("M"), "world-store forget removed nothing")
        _check(sh.recall_addendum("M") == "", "forgotten guidance still recalled")
    finally:
        os.environ["MAVERICK_CONFIG"] = str(_BASE_CONFIG)
        cfg.unlink(missing_ok=True)
    return "same content as files; 8/8 concurrent promotions; forget round-trips"


def g_corpus_store() -> str:
    """Phase 2 of the fleet store: the eval-corpus family (live cases,
    harvest pending, reject memory) lives in the world DB with the SAME
    lifecycle semantics as the files -- staging, durable rejection, accepted
    merges -- and the export/import round trip preserves every
    operator-authored field, so hand-editing stays first-class."""
    from maverick import self_harness_eval as ev
    os.environ["MAVERICK_SELF_HARNESS"] = "1"
    cpath = pathlib.Path(_ISOLATED_HOME) / "corpus.json"
    cfg = pathlib.Path(_ISOLATED_HOME) / "config.toml"
    cfg.write_text(
        '[self_harness]\nenable = true\nrisk_limited = false\nstore = "world"\n'
        f"eval_corpus = {json.dumps(str(cpath))}\n",
        encoding="utf-8",
    )
    os.environ["MAVERICK_CONFIG"] = str(cfg)
    try:
        _check(ev.stage_candidates(cpath, "M", [
            {"goal": "export the ledger", "expected": "exported"},
            {"goal": "reconcile the books", "expected": "reconciled"}]) == 2,
               "staging failed")
        _check(not cpath.exists() and not ev.pending_corpus_path(cpath).exists(),
               "world mode still wrote corpus files")
        res = ev.resolve_pending(cpath, "M", accept=[1], reject=[2])
        _check(res["merged"] == 1 and res["rejected"] == 1,
               f"review verdicts failed: {res}")
        _check(ev.stage_candidates(cpath, "M", [
            {"goal": "reconcile the books", "expected": "x"}]) == 0,
               "rejected candidate re-staged through the DB store")
        # Export -> hand-edit -> import: operator fields survive both ways.
        ev.import_corpus(cpath, {"M": [{"goal": "hand-authored",
                                        "expected": "y", "notes": "keep"}],
                                 "_meta": {"owner": "ops"}})
        raw = ev._load_raw(cpath)
        by_goal = {r["goal"]: r for r in raw["M"]}
        _check(by_goal["hand-authored"]["notes"] == "keep"
               and raw["_meta"] == {"owner": "ops"}
               and "export the ledger" in by_goal,
               f"round trip lost operator data: {raw}")
    finally:
        os.environ["MAVERICK_CONFIG"] = str(_BASE_CONFIG)
        cfg.unlink(missing_ok=True)
    return "stage/reject/accept as world rows; export-import keeps every field"


GUARANTEES = [
    ("determinism (consistent)", g_determinism),
    ("explicit pause", g_explicit_pause),
    ("governed gate enforced", g_gate_enforced),
    ("no trace poisoning", g_no_poison),
    ("bounded addendum", g_bounded),
    ("concurrency safe", g_concurrency),
    ("reversible + auditable", g_reversible),
    ("canary lifecycle", g_canary_lifecycle),
    ("rollback durability", g_rollback_durable),
    ("fleet store parity", g_store_parity),
    ("fleet corpus store", g_corpus_store),
]


def main() -> int:
    print("=" * 78)
    print("  MAVERICK -- SELF-LEARNING HARNESS PROOF   (real loop, real gate)")
    print("=" * 78)
    proven = 0
    failed = 0
    for label, fn in GUARANTEES:
        try:
            detail = fn()
        except Exception as e:  # a proof must report its own failure
            failed += 1
            print(f"  [FAIL]  {label:26}  {type(e).__name__}: {e}")
        else:
            proven += 1
            print(f"  [PASS]  {label:26}  {detail}")
    print("=" * 78)
    print(f"  {proven} guarantees PROVEN, {failed} failed")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
