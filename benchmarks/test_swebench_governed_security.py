import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_swebench_governed():
    path = Path(__file__).parent / "swebench_governed.py"
    spec = importlib.util.spec_from_file_location("benchmarks_swebench_governed", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


swebench_governed = _load_swebench_governed()


def test_worktree_diff_neutralizes_checkout_clean_filters(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # Real SWE-bench instances are checked out at a base_commit, so _worktree_diff
    # captures the agent's edits with ``git diff HEAD`` (staged OR unstaged). Give
    # the fixture a HEAD; owned.pwn / .gitattributes stay NEW below, so they still
    # appear in the diff and still drive git to run the (neutralized) clean filter.
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "base"],
        cwd=repo, check=True)

    marker = tmp_path / "filter-ran"
    subprocess.run(
        [
            "git",
            "config",
            "filter.pwn.clean",
            f"sh -c 'echo pwned > {marker}; cat'",
        ],
        cwd=repo,
        check=True,
    )
    (repo / ".gitattributes").write_text("*.pwn filter=pwn\n", encoding="utf-8")
    (repo / "owned.pwn").write_text("hello\n", encoding="utf-8")

    diff = swebench_governed._worktree_diff(repo)

    assert diff.strip()
    assert "owned.pwn" in diff
    assert not marker.exists()
