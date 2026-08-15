"""SWE-bench harness smoke tests (dry-run only — no API calls)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def swebench():
    import sys
    p = Path(__file__).parent / "swe_bench.py"
    spec = importlib.util.spec_from_file_location("benchmarks_swe_bench", p)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations via sys.modules[cls.__module__],
    # so the module must be registered there before exec_module runs the
    # @dataclass decorator.
    sys.modules["benchmarks_swe_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_maverick_pipeline(swebench, monkeypatch):
    monkeypatch.setenv("MAVERICK_BENCH_DRY_RUN", "1")
    row = swebench.run_maverick("django__django-12345", "fix the bug")
    assert row.pipeline == "maverick"
    assert row.outcome == "dry-run"
    assert row.predicted_patch.startswith("---")


def test_dry_run_sonnet_single(swebench, monkeypatch):
    monkeypatch.setenv("MAVERICK_BENCH_DRY_RUN", "1")
    row = swebench.run_sonnet_single("x", "y")
    assert row.pipeline == "sonnet_single"
    assert row.outcome == "dry-run"


def test_load_instances_plain_ids(swebench, tmp_path):
    manifest = tmp_path / "smoke.txt"
    manifest.write_text("django__django-1\n# comment\nsympy__sympy-2\n")
    out = swebench.load_instances(manifest)
    assert [d["instance_id"] for d in out] == ["django__django-1", "sympy__sympy-2"]


def test_load_instances_json_per_line(swebench, tmp_path):
    manifest = tmp_path / "smoke.jsonl"
    manifest.write_text(
        '{"instance_id": "a", "brief": "fix a"}\n'
        '{"instance_id": "b", "brief": "fix b"}\n'
    )
    out = swebench.load_instances(manifest)
    assert out == [{"instance_id": "a", "brief": "fix a"},
                   {"instance_id": "b", "brief": "fix b"}]


def test_load_instances_carries_test_sets(swebench, tmp_path):
    """Wave 9 fix: fail_to_pass/pass_to_pass/language must round-trip."""
    manifest = tmp_path / "smoke.jsonl"
    manifest.write_text(
        '{"instance_id": "a", "brief": "fix", '
        '"fail_to_pass": ["t::x"], "pass_to_pass": ["t::y"], '
        '"language": "python"}\n'
    )
    out = swebench.load_instances(manifest)
    assert out[0]["fail_to_pass"] == ["t::x"]
    assert out[0]["pass_to_pass"] == ["t::y"]
    assert out[0]["language"] == "python"


def test_already_done_skips_resumed(swebench, tmp_path):
    """Wave 9: re-run skips (instance_id, pipeline) pairs already in CSV."""
    csv_path = tmp_path / "r.csv"
    rows = [
        swebench.Row(instance_id="a", pipeline="maverick", model_id="x"),
        swebench.Row(instance_id="b", pipeline="maverick", model_id="x"),
    ]
    swebench.write_csv(rows, csv_path)
    done = swebench.already_done(csv_path)
    assert ("a", "maverick") in done
    assert ("b", "maverick") in done
    assert ("c", "maverick") not in done


def test_write_csv_creates_header_once(swebench, tmp_path):
    rows = [
        swebench.Row(instance_id="a", pipeline="maverick", model_id="x"),
        swebench.Row(instance_id="b", pipeline="maverick", model_id="x"),
    ]
    out = tmp_path / "r.csv"
    swebench.write_csv(rows, out)
    swebench.write_csv(rows, out)  # second batch, no second header
    content = out.read_text()
    assert content.count("instance_id,") == 1
    assert content.count("\na,maverick") == 2  # appended


def test_main_dry_run_end_to_end(swebench, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAVERICK_BENCH_DRY_RUN", "1")
    manifest = tmp_path / "smoke.txt"
    manifest.write_text("foo__bar-1\nfoo__bar-2\n")
    out_csv = tmp_path / "results.csv"
    # main() reads sys.argv; exercise the module-level helpers directly.
    instances = [(d["instance_id"], d.get("brief", ""))
                 for d in swebench.load_instances(manifest)]
    rows = []
    for iid, brief in instances:
        for p in ("maverick", "sonnet_single"):
            rows.append(swebench._PIPELINE_FNS[p](iid, brief))
    swebench.write_csv(rows, out_csv)
    text = out_csv.read_text()
    # Two instances × two pipelines = 4 rows + 1 header.
    assert text.count("\n") >= 5
    assert "maverick" in text
    assert "sonnet_single" in text
