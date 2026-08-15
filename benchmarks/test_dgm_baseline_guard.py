"""Regression: dgm_uplift.score_instance must require the graded FAIL_TO_PASS to
FAIL at baseline before crediting a resolve.

Without the guard, a mis-seeded instance (its FAIL_TO_PASS already pass with no
fix) let ANY applying non-test patch score all_pass -- inflating the
valuation-facing DGM resolved-rate.
"""
from __future__ import annotations

import pathlib
import sys
from pathlib import Path

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))


def _seed(repo: Path, core_body: str) -> None:
    (repo / "calc").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "calc"\nversion = "0.0.0"\n', encoding="utf-8")
    (repo / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
        encoding="utf-8")
    (repo / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "calc" / "core.py").write_text(core_body, encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from calc.core import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")


def test_score_instance_rejects_mis_seeded(tmp_path, monkeypatch):
    import dgm_uplift
    from swebench_governed import Instance
    monkeypatch.delenv("MAVERICK_TEST_PYTHON", raising=False)
    monkeypatch.delenv("MAVERICK_SWEBENCH_VENVS", raising=False)
    repo = tmp_path / "misseed"
    _seed(repo, "def add(a, b):\n    return a + b\n")     # already correct -> no bug
    # A valid, applying, non-test patch. The baseline guard fires first, so this
    # never gets credited regardless.
    patch = ("diff --git a/calc/core.py b/calc/core.py\n"
             "--- a/calc/core.py\n+++ b/calc/core.py\n"
             "@@ -1,2 +1,3 @@\n def add(a, b):\n+    # touched\n     return a + b\n")
    inst = Instance(instance_id="calc__misseed-1", repo_path=repo,
                    fail_to_pass=["tests/test_core.py::test_add"], pass_to_pass=[],
                    gold_patch="", brief="mis-seeded (no real bug)")
    assert dgm_uplift.score_instance(inst, patch, tmp_path / "w1", timeout=120.0) is False


def test_score_instance_credits_a_genuine_fix(tmp_path, monkeypatch):
    import difflib

    import dgm_uplift
    from swebench_governed import Instance
    monkeypatch.delenv("MAVERICK_TEST_PYTHON", raising=False)
    monkeypatch.delenv("MAVERICK_SWEBENCH_VENVS", raising=False)
    repo = tmp_path / "genuine"
    broken = "def add(a, b):\n    return a - b\n"                 # real bug
    _seed(repo, broken)
    fixed = "def add(a, b):\n    return a + b\n"
    d = difflib.unified_diff(broken.splitlines(keepends=True), fixed.splitlines(keepends=True),
                             fromfile="a/calc/core.py", tofile="b/calc/core.py")
    gold = "diff --git a/calc/core.py b/calc/core.py\n" + "".join(d)
    inst = Instance(instance_id="calc__add-1", repo_path=repo,
                    fail_to_pass=["tests/test_core.py::test_add"], pass_to_pass=[],
                    gold_patch=gold, brief="add subtracts")
    assert dgm_uplift.score_instance(inst, gold, tmp_path / "w2", timeout=120.0) is True
