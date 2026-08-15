"""Unit tests for per-instance venv interpreter selection in the DGM eval loop.

Mirrors ``test_swebench_venvs.py``: the governed SWE-bench harness points
``MAVERICK_TEST_PYTHON`` at each instance's era-correct venv under
MAVERICK_SWEBENCH_VENVS; the DGM uplift loop must resolve the SAME interpreter
so both harnesses grade with one python per instance. ``_instance_test_python``
returns the interpreter path (or "" when none is configured / present).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_dgm():
    p = Path(__file__).resolve().parent / "dgm_uplift.py"
    spec = importlib.util.spec_from_file_location("dgm_uplift_venv_mod", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dgm_uplift_venv_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Inst:
    def __init__(self, iid: str):
        self.instance_id = iid


class TestInstanceTestPython:
    def test_unset_returns_empty(self, monkeypatch):
        mod = _load_dgm()
        monkeypatch.delenv("MAVERICK_SWEBENCH_VENVS", raising=False)
        assert mod._instance_test_python(_Inst("django__django-1")) == ""

    def test_existing_interpreter_returned(self, monkeypatch, tmp_path):
        mod = _load_dgm()
        iid = "django__django-1"
        interp = tmp_path / iid / "bin" / "python"
        interp.parent.mkdir(parents=True)
        interp.write_text("#!/bin/sh\n")
        monkeypatch.setenv("MAVERICK_SWEBENCH_VENVS", str(tmp_path))
        assert mod._instance_test_python(_Inst(iid)) == str(interp)

    def test_missing_instance_dir_returns_empty(self, monkeypatch, tmp_path):
        mod = _load_dgm()
        monkeypatch.setenv("MAVERICK_SWEBENCH_VENVS", str(tmp_path))
        assert mod._instance_test_python(_Inst("django__django-does-not-exist")) == ""
