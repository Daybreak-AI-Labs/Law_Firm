from pathlib import Path

CORE_PACKAGE = Path(__file__).resolve().parents[1] / "maverick"
SHIELD_PACKAGE = Path(__file__).resolve().parents[2] / "maverick-shield" / "maverick_shield"


def test_non_firm_cli_orphans_are_not_shipped() -> None:
    retired = (
        "backport_tool.py",
        "ebpf_monitor.py",
        "golden_path.py",
        "licensing.py",
        "profiling_daemon.py",
        "sigstore_signing.py",
    )
    assert not [name for name in retired if (CORE_PACKAGE / name).exists()]


def test_retained_safety_and_shield_operator_tools_stay_present() -> None:
    assert (CORE_PACKAGE / "safety_report.py").is_file()
    for name in ("probe_model.py", "probe_train.py", "redteam.py", "redteam_corpus.jsonl"):
        assert (SHIELD_PACKAGE / name).is_file()
