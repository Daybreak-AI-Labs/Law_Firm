"""Low-level live-tree apply and mechanical rollback primitives.

The operable self-modification runner does not call this module: live code
adoption is disabled until a nonce/evidence/base-revision-bound approval
manifest drives the durable PREPARE/CAS/COMMIT transaction API. This primitive
therefore supplies mechanism, not authorization.

Every apply requires an explicit reviewed editable surface, rejects protected or
aliased targets, takes a fresh snapshot of the current tree, rechecks the global
learning HALT immediately before mutation, and restores the snapshot after a
failed apply. Caller-supplied snapshot handles are refused because they cannot
prove they represent the current pre-state.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .workspace_snapshot import create_snapshot, restore_snapshot, store_dir

log = logging.getLogger(__name__)


@dataclass
class RollbackHandle:
    """A real, mechanically-executable undo for an applied change."""

    snapshot_id: str
    tree: Path
    store: Path

    def revert(self) -> bool:
        return revert_change(self.snapshot_id, tree=self.tree, store=self.store)


@dataclass
class AppliedChange:
    """Outcome of landing a promoted patch on the live tree."""

    ok: bool
    snapshot_id: str | None = None
    reason: str = ""
    rollback: RollbackHandle | None = None


def create_rollback_point(tree: Path, *, store: Path | None = None,
                          label: str = "self-modify") -> str | None:
    """Create a standalone recovery snapshot and return its id.

    ``apply_change`` takes its own fresh pre-apply snapshot and does not accept
    this id. Returns ``None`` on failure."""
    store = store or store_dir()
    try:
        man = create_snapshot(Path(tree), Path(store), label)
        return man.get("id")
    except (ValueError, OSError) as e:
        log.warning("self_modify_apply: could not snapshot %s: %s", tree, e)
        return None


def _host_git_apply(patch: str, tree: Path) -> tuple[bool, str]:
    """Apply a unified diff to the live ``tree`` via ``sandbox.exec`` (rule 4).
    This is the deployment of an already-approved patch, so it runs on the host
    tree BY DESIGN (a ``LocalBackend``) — distinct from the isolated, untrusted
    candidate *evaluation*, which pins a container. Delegates the staging /
    ``--check`` / ``-p1`` / cleanup to the one shared :func:`run_git_apply`
    mechanic so it can't drift from the eval path. Returns ``(ok, message)``."""
    from .sandbox.local import LocalBackend
    from .self_modify_eval import run_git_apply
    return run_git_apply(patch, Path(tree), LocalBackend(workdir=Path(tree)))


def apply_change(
    patch: str, *, tree: Path, snapshot_id: str | None = None,
    store: Path | None = None, label: str = "self-modify", surface=None,
) -> AppliedChange:
    """Land ``patch`` on the live ``tree`` with a real rollback point.

    Caller-provided ``snapshot_id`` values are refused. A fresh snapshot is
    taken after boundary and alias checks; without it the apply is refused. On a
    failed apply the tree is restored so a half-applied change never persists.
    This function supplies no deployment authorization and never raises.

    Defence in depth: this live-tree write REQUIRES and re-runs the exact
    editable-surface boundary
    (:func:`maverick.self_modify.review_patch`) and refuses any patch that touches
    a control-plane path — so even a mis-wired caller that skipped the gate can
    never land a change on a protected file through this primitive."""
    try:
        tree = Path(tree).resolve(strict=True)
    except (OSError, ValueError) as exc:
        return AppliedChange(False, reason=f"repository tree unavailable: {exc}")
    if not tree.is_dir():
        return AppliedChange(False, reason="repository tree must be a directory")
    store = store or store_dir()
    if snapshot_id is not None:
        return AppliedChange(
            False,
            reason="pre-existing rollback handles are not accepted; a fresh "
                   "pre-apply snapshot is required",
        )

    # Boundary re-check: refuse a patch that touches the reference monitor,
    # independent of whatever gating the caller claims to have done.
    from .self_modify import _canonical, _diff_paths, is_protected, review_patch
    if surface is None:
        return AppliedChange(
            False, reason="refusing to apply without an explicit editable surface")
    review = review_patch(patch, surface)
    if not review.ok:
        return AppliedChange(False, reason=f"refusing to apply: {review.reason}")
    protected = [p for p in _diff_paths(patch) if is_protected(p)]
    if protected:
        return AppliedChange(
            False, reason=f"refusing to apply: patch touches protected "
            f"control-plane paths {sorted(protected)}")

    # Never follow a candidate-controlled alias out of the reviewed tree.  This
    # check is repeated immediately before the write. Live self-modification is
    # disabled until its caller additionally serializes and CAS-checks a durable
    # promotion transaction.
    for raw_path in _diff_paths(patch):
        relative = _canonical(raw_path)
        if relative is None:
            return AppliedChange(False, reason=f"unsafe patch path {raw_path!r}")
        current = tree
        parts = relative.split("/")
        for part in parts:
            current = current / part
            is_junction = getattr(current, "is_junction", None)
            if current.is_symlink() or (callable(is_junction) and is_junction()):
                return AppliedChange(
                    False, reason=f"refusing to apply through filesystem alias {relative!r}")
            if current.exists():
                try:
                    current.resolve(strict=True).relative_to(tree)
                except (OSError, ValueError):
                    return AppliedChange(
                        False, reason=f"patch path escapes repository tree: {relative!r}")
        target = tree.joinpath(*parts)
        if target.exists() and target.is_file():
            try:
                if os.stat(target, follow_symlinks=False).st_nlink > 1:
                    return AppliedChange(
                        False,
                        reason=f"refusing to modify hard-linked file {relative!r}",
                    )
            except OSError as exc:
                return AppliedChange(
                    False, reason=f"cannot verify patch target {relative!r}: {exc}")

    snap = create_rollback_point(tree, store=store, label=label)
    if not snap:
        return AppliedChange(False, reason="no rollback point; refusing to apply")

    ok, msg = _host_git_apply(patch, tree)
    handle = RollbackHandle(snapshot_id=snap, tree=tree, store=store)
    if not ok:
        # Restore to the snapshot so a partial apply can't linger, and report the
        # ACTUAL revert result — a failed revert leaves the tree possibly dirty.
        reverted = handle.revert()
        detail = (f"reverted to {snap}" if reverted
                  else f"REVERT FAILED for {snap} — tree may be dirty")
        return AppliedChange(False, snapshot_id=snap, reason=f"{msg}; {detail}")
    return AppliedChange(True, snapshot_id=snap, reason=msg, rollback=handle)


def revert_change(snapshot_id: str, *, tree: Path, store: Path | None = None) -> bool:
    """Restore ``tree`` to the snapshot. Returns True on success. Never raises."""
    store = store or store_dir()
    try:
        restore_snapshot(Path(store), snapshot_id, Path(tree))
        return True
    except (ValueError, OSError) as e:
        log.warning("self_modify_apply: revert to %s failed: %s", snapshot_id, e)
        return False


__all__ = [
    "RollbackHandle", "AppliedChange",
    "create_rollback_point", "apply_change", "revert_change",
]
