"""World-DB learning store (fleet-shared state, phase 1) -- the store seam,
routing rule, lifecycle round-trips, and the migrate-store CLI."""
from __future__ import annotations

import pytest
from maverick import learning_store as ls
from maverick import self_harness as sh
from maverick import self_improvement as si


@pytest.fixture()
def world_home(tmp_path, monkeypatch):
    """Isolated MAVERICK_HOME with [self_harness] store = "world" configured
    (SQLite world DB inside the temp home)."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAVERICK_HOME", str(home))
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    monkeypatch.delenv("MAVERICK_WORLD_BACKEND", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "store": "world"}})
    return home


def _ctrl(monkeypatch, ledger_path):
    monkeypatch.setattr(si, "enabled", lambda: True)
    return si.SelfImprovementController(frozen_fn=lambda: False,
                                        ledger=si.PromotionLedger(path=ledger_path))


def _promote(model, line, ctrl, path=None):
    return sh.run_self_harness(
        [{"model_id": model, "failure_class": "timeout",
          "goal_text": f"task {i}", "failure_msg": "x"} for i in range(3)],
        model_id=model, controller=ctrl, min_support=3, path=path,
        held_in=["a", "b"], held_out=["c", "d", "e", "f", "g"],
        propose_fn=lambda s, _l=line: _l,
        score_with=lambda a, c: 0.95, score_without=lambda a, c: 0.4)


def test_store_knob_defaults_to_files(monkeypatch):
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {})
    assert config.get_self_harness()["store"] == "files"
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {
        "self_harness": {"store": "WORLD "}})
    assert config.get_self_harness()["store"] == "world"
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {
        "self_harness": {"store": "s3"}})
    assert config.get_self_harness()["store"] == "files"   # unknown -> files


def test_explicit_path_always_means_the_file_store(world_home, tmp_path,
                                                   monkeypatch):
    # The seam's contract: tests/tenant redirection pass explicit paths and
    # stay on files even when the world store is configured.
    ctrl = _ctrl(monkeypatch, tmp_path / "promotion-ledger.json")
    store = tmp_path / "explicit.json"
    rep = _promote("M", "file line", ctrl, path=store)
    assert rep.promoted == 1
    assert "file line" in store.read_text()
    assert ls.load_addenda_db() == {}          # world store untouched


def test_full_lifecycle_through_the_world_store(world_home, monkeypatch):
    # Promote -> recall -> outcome -> canary review -> forget, all with
    # path=None routed to the world DB; no JSON store file is ever written.
    ctrl = _ctrl(monkeypatch, world_home / "promotion-ledger.json")
    rep = _promote("M", "verify the export first", ctrl)
    assert rep.promoted == 1
    assert not sh._store_path().exists()       # nothing on disk at the default path
    assert "verify the export first" in sh.recall_addendum("M")
    assert ls.load_addenda_db()                # ...because it lives in the DB
    assert sh.line_provenance("M")             # sidecar records too
    sh.mark_canary("M", "verify the export first")
    for _ in range(2):
        sh.note_outcome("M", False, line="verify the export first")
    res = sh.review_canaries("M", demote_after=2)
    assert res["demoted"] == ["verify the export first"]
    assert sh.recall_addendum("M") == ""
    # The demotion recorded the transfer tried-memory in the DB (rollback
    # durability spans stores).
    assert ls.load_transfer_tried_db()


def test_transfer_one_shot_via_world_store(world_home, monkeypatch):
    ctrl = _ctrl(monkeypatch, world_home / "promotion-ledger.json")
    _promote("SRC", "solid line", ctrl)
    quad = (["a", "b"], ["c", "d", "e", "f", "g"],
            lambda a, c: 0.95, lambda a, c: 0.4)
    rep = sh.run_transfer("SRC", ["TGT"], eval_for_target=lambda m: quad,
                          controller=ctrl)
    assert rep["TGT"]["promoted"] == ["solid line"]
    assert sh.list_canaries("TGT") == ["solid line"]
    rep2 = sh.run_transfer("SRC", ["TGT"], eval_for_target=lambda m: quad,
                           controller=ctrl)
    assert rep2["TGT"]["attempted"] == []
    assert any("already present" in s for s in rep2["TGT"]["skipped"])


def test_world_store_concurrency_keeps_every_promotion(world_home, monkeypatch):
    # The battery's concurrency drill, against the DB store: 8 threads each
    # promote a distinct line; none may be lost to a read-modify-write race.
    import threading
    ctrl = _ctrl(monkeypatch, world_home / "promotion-ledger.json")
    errs: list[Exception] = []

    def work(i):
        try:
            _promote("M", f"guidance line {i}", ctrl)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    bullets = [ln for ln in sh.recall_addendum("M").splitlines()
               if ln.startswith("- ")]
    assert len(bullets) == 8






@pytest.fixture()
def world_corpus(tmp_path, monkeypatch):
    """world_home plus a configured eval_corpus path (world-routed)."""
    home = tmp_path / "home"
    cpath = tmp_path / "corpus.json"
    monkeypatch.setenv("MAVERICK_HOME", str(home))
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    monkeypatch.delenv("MAVERICK_WORLD_BACKEND", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "store": "world",
                         "eval_corpus": str(cpath)}})
    return cpath


def test_corpus_lifecycle_through_the_world_store(world_corpus):
    # stage -> review (reject remembered, accept merges) entirely as world
    # rows; no corpus/pending/rejected file ever appears on disk.
    from maverick import self_harness_eval as ev
    cpath = world_corpus
    assert ev.stage_candidates(cpath, "M", [
        {"goal": "g1", "expected": "e1"}, {"goal": "g2", "expected": "e2"}]) == 2
    assert not cpath.exists()
    assert not ev.pending_corpus_path(cpath).exists()
    assert [c["goal"] for c in ev.load_pending(cpath)["M"]] == ["g1", "g2"]
    res = ev.resolve_pending(cpath, "M", accept=[1], reject=[2])
    assert (res["merged"], res["rejected"]) == (1, 1)
    assert [c["goal"] for c in ev.load_eval_corpus(cpath)["M"]] == ["g1"]
    assert ev.load_rejected(cpath) == {"M": ["g2"]}
    # rejection stays durable through the DB path
    assert ev.stage_candidates(cpath, "M", [{"goal": "g2", "expected": "x"}]) == 0
    assert ls.load_corpus_db("live")            # rows really live in the DB


def test_explicit_other_corpus_path_stays_on_files(world_corpus, tmp_path):
    # Only the CONFIGURED corpus path routes; any other path is a plain file.
    from maverick import self_harness_eval as ev
    other = tmp_path / "other.json"
    other.write_text("{}")
    assert ev.stage_candidates(other, "M", [{"goal": "g", "expected": "e"}]) == 1
    assert ev.pending_corpus_path(other).exists()      # file sidecar written
    assert ev.load_pending(world_corpus) == {}         # world store untouched






def test_world_corpus_seals_machine_owned_rows(world_corpus, monkeypatch):
    import base64
    import sqlite3

    pytest.importorskip("cryptography")

    from maverick import self_harness_eval as ev
    from maverick.crypto_at_rest import is_sealed_str
    from maverick.paths import data_dir

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode("ascii"))
    cpath = world_corpus

    assert ev.stage_candidates(cpath, "M", [
        {"goal": "SECRET_CORPUS_GOAL", "expected": "SECRET_EXPECTED_HINT"},
    ]) == 1
    assert ev.load_pending(cpath)["M"][0]["goal"] == "SECRET_CORPUS_GOAL"

    conn = sqlite3.connect(data_dir("world.db"))
    try:
        row = conn.execute(
            "SELECT row FROM harness_corpus WHERE kind = 'pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "SECRET_CORPUS_GOAL" not in row
    assert "SECRET_EXPECTED_HINT" not in row
    assert is_sealed_str(row)

    res = ev.resolve_pending(cpath, "M", accept=[], reject=[1])
    assert res["rejected"] == 1
    assert ev.load_rejected(cpath) == {"M": ["SECRET_CORPUS_GOAL"]}
    conn = sqlite3.connect(data_dir("world.db"))
    try:
        rejected = conn.execute(
            "SELECT row FROM harness_corpus WHERE kind = 'rejected'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "SECRET_CORPUS_GOAL" not in rejected
    assert is_sealed_str(rejected)


