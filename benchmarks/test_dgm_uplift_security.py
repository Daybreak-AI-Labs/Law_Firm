import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load(name: str):
    p = Path(__file__).parent / name
    spec = importlib.util.spec_from_file_location(f"benchmarks_{p.stem}", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dgm_uplift = _load("dgm_uplift.py")


def test_run_solver_exposes_only_isolated_instance_repo(tmp_path, monkeypatch):
    # Ampersands are command separators in cmd.exe.  Keeping one in both the
    # solver and work paths makes this an end-to-end regression for Windows
    # shell rendering as well as the original isolated-repo assertion.
    solver_dir = tmp_path / "solver & still-one-argument"
    solver_dir.mkdir()
    (solver_dir / "solver.py").write_text(
        "from pathlib import Path\n"
        "def solve(instance):\n"
        "    Path(instance.repo_path, 'tampered').write_text('x', encoding='utf-8')\n"
        "    return ''\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    inst = SimpleNamespace(
        instance_id="inst-1",
        repo_path=repo,
        fail_to_pass=[],
        pass_to_pass=[],
        gold_patch="",
        brief="",
        language="python",
        total_tests=0,
    )
    monkeypatch.setattr(dgm_uplift, "score_instance", lambda *args, **kwargs: False)

    work = tmp_path / "work & still-one-argument"
    assert dgm_uplift.run_solver(solver_dir, [inst], work, timeout=10) == {
        "inst-1": False
    }

    assert not (repo / "tampered").exists()
    assert list(work.rglob("tampered"))


@pytest.mark.parametrize("unsafe", ["%PATH%", "!PATH!", "line\nnext", 'a"b', "a\0b"])
def test_windows_shell_quote_rejects_expansion_and_control_characters(unsafe):
    with pytest.raises(ValueError, match="unsafe character"):
        dgm_uplift._windows_shell_quote_arg(unsafe)


def test_windows_shell_quote_contains_cmd_metacharacters():
    quoted = dgm_uplift._windows_shell_quote_arg(r"C:\tmp & whoami\solver.py")
    assert quoted == '"C:\\tmp & whoami\\solver.py"'
