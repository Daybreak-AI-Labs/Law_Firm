"""Q2 2026 batch 5: apply_patch, compute (sympy), email, pandas_query, git_advanced, cosign workflow."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------- apply_patch ----------

def test_apply_patch_requires_patch():
    from maverick.tools.apply_patch import apply_patch

    class _Sandbox:
        workdir = "."

    out = apply_patch(_Sandbox()).fn({"patch": ""})
    assert "patch is required" in out


def test_apply_patch_rejects_path_traversal(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    _make_git_repo(tmp_path)
    (tmp_path / "a").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    from maverick.tools.apply_patch import apply_patch

    class _Sandbox:
        workdir = str(tmp_path)

    bad_patch = (
        "diff --git a/../etc/passwd b/../etc/passwd\n"
        "--- a/../etc/passwd\n"
        "+++ b/../etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-hi\n+pwned\n"
    )
    out = apply_patch(_Sandbox()).fn({"patch": bad_patch})
    assert "path-traversal" in out


def test_apply_patch_dry_run_lists_files(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    _make_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("line1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    (tmp_path / "a.txt").write_text("line1\nline2\n")
    proc = subprocess.run(
        ["git", "-C", str(tmp_path), "diff"], capture_output=True, check=True,
    )
    patch_text = proc.stdout.decode()
    # Reset so the patch applies cleanly against the worktree.
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "--", "a.txt"], check=True,
    )

    from maverick.tools.apply_patch import apply_patch

    class _Sandbox:
        workdir = str(tmp_path)

    out = apply_patch(_Sandbox()).fn({"patch": patch_text, "dry_run": True})
    assert "DRY RUN" in out
    assert "a.txt" in out


def test_apply_patch_applies_real_patch(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    _make_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("line1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    (tmp_path / "a.txt").write_text("line1\nline2\n")
    proc = subprocess.run(
        ["git", "-C", str(tmp_path), "diff"], capture_output=True, check=True,
    )
    patch_text = proc.stdout.decode()
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "--", "a.txt"], check=True,
    )
    assert (tmp_path / "a.txt").read_text() == "line1\n"

    from maverick.tools.apply_patch import apply_patch

    class _Sandbox:
        workdir = str(tmp_path)

    out = apply_patch(_Sandbox()).fn({"patch": patch_text})
    assert "applied to 1 file" in out
    assert (tmp_path / "a.txt").read_text() == "line1\nline2\n"


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_git_repo(p: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "commit.gpgsign", "false"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "tag.gpgsign", "false"], check=True)


# ---------- compute (sympy) ----------

_HAS_SYMPY = importlib.util.find_spec("sympy") is not None


















# ---------- email tool ----------











# ---------- pandas_query ----------

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None
















# ---------- git_advanced ----------









# ---------- cosign workflow ----------

