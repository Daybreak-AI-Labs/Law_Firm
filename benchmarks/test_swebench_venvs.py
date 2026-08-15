"""Unit tests for per-instance venv interpreter selection (_venv_python).

Operators build one venv per instance under MAVERICK_SWEBENCH_VENVS so that
mixed-era instances can be graded on one host; _venv_python resolves the
interpreter path (or "" when none is configured / present).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_gov():
    p = Path(__file__).resolve().parent / "swebench_governed.py"
    spec = importlib.util.spec_from_file_location("swebench_governed_mod", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["swebench_governed_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _inst(mod, iid: str, repo_path: Path):
    return mod.Instance(
        instance_id=iid,
        repo_path=repo_path,
        fail_to_pass=[],
        pass_to_pass=[],
    )


class TestVenvPython:
    def test_unset_returns_empty(self, monkeypatch, tmp_path):
        mod = _load_gov()
        monkeypatch.delenv("MAVERICK_SWEBENCH_VENVS", raising=False)
        inst = _inst(mod, "django__django-1", tmp_path)
        assert mod._venv_python(inst) == ""

    def test_existing_interpreter_returned(self, monkeypatch, tmp_path):
        mod = _load_gov()
        iid = "django__django-1"
        interp = tmp_path / iid / "bin" / "python"
        interp.parent.mkdir(parents=True)
        interp.write_text("#!/bin/sh\n")
        monkeypatch.setenv("MAVERICK_SWEBENCH_VENVS", str(tmp_path))
        inst = _inst(mod, iid, tmp_path)
        assert mod._venv_python(inst) == str(interp)

    def test_missing_instance_dir_returns_empty(self, monkeypatch, tmp_path):
        mod = _load_gov()
        monkeypatch.setenv("MAVERICK_SWEBENCH_VENVS", str(tmp_path))
        inst = _inst(mod, "django__django-does-not-exist", tmp_path)
        assert mod._venv_python(inst) == ""
