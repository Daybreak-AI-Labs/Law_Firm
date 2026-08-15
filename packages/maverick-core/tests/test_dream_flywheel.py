"""`maverick dream` turns the flywheel as part of the nightly cycle when the
data engine is on -- and doesn't when it's off."""
from __future__ import annotations

from click.testing import CliRunner
from maverick import flywheel
from maverick.cli import main


class _DreamRep:
    def summary(self):
        return "dream ok"


def _stub_dreaming(monkeypatch):
    monkeypatch.setattr("maverick.dreaming.enabled", lambda: True)
    monkeypatch.setattr("maverick.dreaming.settings", lambda: {"snapshots": False})
    monkeypatch.setattr("maverick.dreaming.dream_cycle", lambda *a, **k: _DreamRep())


def test_dream_runs_the_flywheel_when_enabled(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_DATA_ENGINE", "1")
    monkeypatch.setattr(
        "maverick.flywheel.maybe_run",
        lambda: flywheel.FlywheelReport(n_episodes=5, guardrails=("g",), predicted_lift=0.5))

    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "dream ok" in res.output
    assert "[flywheel]" in res.output


def test_dream_skips_the_flywheel_when_data_engine_off(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.delenv("MAVERICK_DATA_ENGINE", raising=False)
    monkeypatch.setattr("maverick.config.get_data_engine", lambda: {"enable": False})
    # if it DID run, this would raise -- proving it was skipped
    def _boom():
        raise AssertionError("flywheel should not run when the data engine is off")
    monkeypatch.setattr("maverick.flywheel.maybe_run", _boom)

    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[flywheel]" not in res.output


# `maverick dream` also operates the self-harness on the same nightly beat when
# [self_harness] auto_run is on -- and doesn't when explicitly paused or the
# loop is disabled (the flywheel pattern never breaks dreaming).

def test_dream_runs_the_self_harness_when_auto_run(tmp_path, monkeypatch):
    from maverick.self_harness import SelfHarnessReport
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "auto_run": True}})
    rep = SelfHarnessReport(model_id="model-a")
    rep.mined, rep.promoted = 2, 1
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models",
        lambda **k: {"model-a": (rep, 3)})

    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness] model-a: mined=2 promoted=1 demoted=0 retired=3" in res.output


def test_dream_skips_the_self_harness_without_auto_run(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")   # loop ON, auto_run OFF
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True}})

    def _boom(**k):
        raise AssertionError("self-harness should not run without auto_run")
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models", _boom)
    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness]" not in res.output


def test_dream_runs_transfer_sweep_when_enabled(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "auto_run": True, "transfer_auto": True}})
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models",
        lambda **k: {})
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_transfer_sweep",
        lambda **k: {"m1": {"m2": {"attempted": ["ln"], "promoted": ["ln"],
                                   "skipped": []}}})
    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness transfer] m1 -> m2: attempted=1 promoted=1" in res.output


def test_dream_skips_transfer_without_the_knob(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "auto_run": True}})   # transfer_auto OFF
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models",
        lambda **k: {})

    def _boom(**k):
        raise AssertionError("transfer sweep should not run without transfer_auto")
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_transfer_sweep", _boom)
    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness transfer]" not in res.output


def test_dream_harvests_corpus_when_configured(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    cpath = tmp_path / "corpus.json"
    cpath.write_text("{}")
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": True, "auto_run": True,
                         "corpus_harvest": "propose",
                         "eval_corpus": str(cpath)}})
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models",
        lambda **k: {})
    monkeypatch.setattr("maverick.llm.model_for_role", lambda role: "m1")
    monkeypatch.setattr(
        "maverick.self_harness_eval.harvest_corpus_candidates",
        lambda refl, goals, **k: [{"goal": "g", "expected": "e"}])
    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness corpus] 1 candidate case(s) staged for review" in res.output
    from maverick import self_harness_eval as ev
    assert ev.load_pending(cpath)["m1"] == [{"goal": "g", "expected": "e"}]


def test_dream_skips_the_self_harness_when_disabled(tmp_path, monkeypatch):
    _stub_dreaming(monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "0")
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "self_harness": {"enable": False, "auto_run": True}})

    def _boom(**k):
        raise AssertionError("self-harness should not run while disabled")
    monkeypatch.setattr(
        "maverick.self_improvement_runner.run_self_harness_all_models", _boom)
    res = CliRunner().invoke(main, ["--db", str(tmp_path / "w.db"), "dream"])
    assert res.exit_code == 0, res.output
    assert "[self-harness]" not in res.output
