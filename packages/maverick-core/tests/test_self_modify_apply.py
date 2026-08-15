"""Low-level apply + rollback hardening.

Pins that the reversibility handle is a real workspace snapshot: an applied
change can be mechanically reverted, unsafe artifacts are rejected, a failed
apply auto-reverts, and missing authorization context fails closed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from maverick import self_modify as sm
from maverick import self_modify_apply as ap


def _tree(root: Path) -> Path:
    t = root / "tree"
    (t / "pkg").mkdir(parents=True)
    (t / "pkg" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    return t


def _patch() -> str:
    return ("diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
            "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 99\n")


def _store(root: Path) -> Path:
    s = root / "snaps"
    s.mkdir()
    return s


def _surface(*globs: str) -> sm.EditableSurface:
    return sm.EditableSurface(globs or ("pkg/*.py",))


class TestRollbackPoint:
    def test_creates_a_snapshot_id(self, tmp_path):
        snap = ap.create_rollback_point(_tree(tmp_path), store=_store(tmp_path))
        assert snap and snap.startswith("snap-")


class TestApplyChange:
    def test_applies_and_returns_real_rollback(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        res = ap.apply_change(
            _patch(), tree=tree, store=store, surface=_surface())
        assert res.ok is True
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 99\n"
        assert res.rollback is not None
        # the handle mechanically restores the pre-change tree
        assert res.rollback.revert() is True
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"

    def test_refuses_a_caller_supplied_snapshot_handle(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        snap = ap.create_rollback_point(tree, store=store)
        res = ap.apply_change(
            _patch(), tree=tree, snapshot_id=snap, store=store,
            surface=_surface(),
        )
        assert res.ok is False
        assert "fresh pre-apply snapshot" in res.reason
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"

    def test_failed_apply_auto_reverts(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        bad = ("diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
               "@@ -1 +1 @@\n-DOES_NOT_MATCH\n+x\n")
        res = ap.apply_change(bad, tree=tree, store=store, surface=_surface())
        assert res.ok is False
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"   # untouched

    def test_apply_refused_without_a_rollback_point(self, tmp_path, monkeypatch):
        tree, store = _tree(tmp_path), _store(tmp_path)
        monkeypatch.setattr(ap, "create_rollback_point", lambda *a, **k: None)
        res = ap.apply_change(
            _patch(), tree=tree, store=store, surface=_surface())
        assert res.ok is False and "no rollback point" in res.reason
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"

    def test_refuses_an_omitted_editable_surface(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        res = ap.apply_change(_patch(), tree=tree, store=store)
        assert res.ok is False
        assert "explicit editable surface" in res.reason
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"

    def test_refuses_a_forged_editable_surface(self, tmp_path):
        class FakeSurface:
            @staticmethod
            def classify(_path):
                return "editable"

        tree, store = _tree(tmp_path), _store(tmp_path)
        res = ap.apply_change(
            _patch(), tree=tree, store=store,
            surface=FakeSurface(),  # type: ignore[arg-type]
        )
        assert res.ok is False
        assert "invalid editable surface authority" in res.reason
        assert list(store.iterdir()) == []
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"


class TestRevert:
    def test_revert_restores_snapshot(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        snap = ap.create_rollback_point(tree, store=store)
        (tree / "pkg" / "a.py").write_text("MUTATED\n", encoding="utf-8")
        assert ap.revert_change(snap, tree=tree, store=store) is True
        assert (tree / "pkg" / "a.py").read_text() == "VALUE = 1\n"

    def test_revert_bad_id_is_false_not_raise(self, tmp_path):
        assert ap.revert_change("snap-9999", tree=_tree(tmp_path),
                                store=_store(tmp_path)) is False


class TestBoundaryReCheck:
    def test_refuses_a_control_plane_patch(self, tmp_path):
        # Defense in depth: apply_change re-runs the boundary and refuses a patch
        # that touches the control plane, even if a caller skipped the gate.
        tree, store = _tree(tmp_path), _store(tmp_path)
        cp = ("diff --git a/packages/maverick-core/maverick/verifier.py "
              "b/packages/maverick-core/maverick/verifier.py\n"
              "--- a/packages/maverick-core/maverick/verifier.py\n"
              "+++ b/packages/maverick-core/maverick/verifier.py\n"
              "@@ -1 +1 @@\n-x\n+y\n")
        res = ap.apply_change(cp, tree=tree, store=store, surface=_surface("**"))
        assert res.ok is False and "protected" in res.reason

    @pytest.mark.parametrize(
        "patch",
        [
            (
                "diff --git a/pkg/link.py b/pkg/link.py\n"
                "new file mode 120000\n--- /dev/null\n+++ b/pkg/link.py\n"
                "@@ -0,0 +1 @@\n+../../outside.py\n"
            ),
            (
                "diff --git a/pkg/submodule b/pkg/submodule\n"
                "new file mode 160000\n--- /dev/null\n+++ b/pkg/submodule\n"
                "@@ -0,0 +1 @@\n+Subproject commit " + ("a" * 40) + "\n"
            ),
            (
                "diff --git a/pkg/blob.bin b/pkg/blob.bin\n"
                "new file mode 100644\n"
                "GIT binary patch\nliteral 1\nIc${MZ000310RR91\n"
            ),
        ],
    )
    def test_refuses_unsafe_diff_artifacts_before_snapshot(self, tmp_path, patch):
        tree, store = _tree(tmp_path), _store(tmp_path)
        res = ap.apply_change(
            patch, tree=tree, store=store, surface=_surface("pkg/**"))
        assert res.ok is False
        assert list(store.iterdir()) == []

    def test_refuses_existing_symlink_target(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_text("VALUE = 1\n", encoding="utf-8")
        target = tree / "pkg" / "alias.py"
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        patch = (
            "diff --git a/pkg/alias.py b/pkg/alias.py\n"
            "--- a/pkg/alias.py\n+++ b/pkg/alias.py\n"
            "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
        )
        res = ap.apply_change(
            patch, tree=tree, store=store, surface=_surface())
        assert res.ok is False and "filesystem alias" in res.reason
        assert outside.read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_refuses_hard_linked_target(self, tmp_path):
        tree, store = _tree(tmp_path), _store(tmp_path)
        outside = tmp_path / "outside.py"
        try:
            os.link(tree / "pkg" / "a.py", outside)
        except OSError:
            pytest.skip("hard links unavailable")
        res = ap.apply_change(
            _patch(), tree=tree, store=store, surface=_surface())
        assert res.ok is False and "hard-linked" in res.reason
        assert outside.read_text(encoding="utf-8") == "VALUE = 1\n"
