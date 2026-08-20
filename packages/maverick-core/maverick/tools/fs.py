"""Filesystem tools backed by the sandbox.

v0.1.1 fix: ``read_file`` / ``list_dir`` no longer interpolate the
LLM-supplied path into a shell command. They use ``pathlib`` directly
and verify the resolved path stays inside the sandbox workdir.

``write_file`` already used pathlib; tightened the path-traversal
check to match.

The shell tool (`shell.py`) intentionally exposes shell execution —
that's its purpose. Shield's `scan_tool_call` chokepoint guards it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import Tool

MAX_READ_BYTES = 8000


def _safe_resolve(sandbox, user_path: str) -> Path:
    """Resolve `user_path` relative to sandbox.workdir, refusing traversal.

    Raises ValueError if the resolved path escapes the workspace.
    """
    workdir = Path(sandbox.workdir).resolve()
    candidate = (workdir / user_path).resolve()
    try:
        candidate.relative_to(workdir)
    except ValueError as e:
        raise ValueError(
            f"path {user_path!r} escapes the workspace"
        ) from e
    return candidate


def _fd_real_path(fd: int) -> Path | None:
    """The real filesystem path the descriptor is bound to, or ``None`` when the
    platform doesn't expose one.

    Linux: ``/proc/self/fd/<fd>`` is a kernel symlink to the inode actually
    opened, so reading it is immune to any symlink swapped in along the path
    AFTER the open. Returns ``None`` on platforms without ``/proc`` (macOS,
    Windows) or for non-file descriptors (pipes/sockets) — callers then fall
    back to the resolve-time containment check (no weaker than before).
    """
    try:
        link = os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return None
    # Do not strip Linux's optional " (deleted)" marker here. That suffix is
    # also legal in live filenames, and removing it can transform an outside
    # sibling such as "<workdir> (deleted)" into "<workdir>" before the
    # containment check. Treat the kernel-reported path literally: deleted
    # descriptors will either be rejected by containment or fall back to the
    # existing resolve-time guard on platforms that do not expose /proc.
    if not link.startswith("/"):
        return None  # e.g. "pipe:[...]", "anon_inode:..." — not a real path
    return Path(link)


def _open_contained(workdir: Path, target: Path, flags: int, mode: int = 0o666) -> int:
    """Open ``target`` and verify, THROUGH the opened descriptor, that the inode
    it is bound to still lives under ``workdir``.

    This closes the symlink TOCTOU between :func:`_safe_resolve` (which resolves
    + range-checks the path) and the actual open: a symlink swapped into any path
    component after the check would otherwise redirect the open outside the
    workspace. ``os.open`` follows symlinks exactly as before, so legitimate
    in-workspace symlinks keep working; we then confirm via :func:`_fd_real_path`
    that what we actually opened is contained — and that check cannot be raced
    because the descriptor is already bound to the resolved inode.

    Returns the open fd (caller owns it). Raises ``ValueError`` if the opened
    inode escaped the workspace (closing the fd first); ``OSError`` propagates.
    """
    fd = os.open(str(target), flags, mode)
    real = _fd_real_path(fd)
    if real is not None:
        try:
            real.relative_to(workdir)
        except ValueError as e:
            os.close(fd)
            raise ValueError(
                f"path {target.name!r} resolved outside the workspace after "
                "open (symlink race)"
            ) from e
    return fd


def read_text_contained(sandbox, target: Path, *, errors: str = "strict") -> str:
    """Read text from an already-resolved ``target`` through a descriptor that is
    verified to be inside ``sandbox.workdir`` (TOCTOU-safe).

    The sibling file tools (``str_replace_editor``, ``ast_edit``) share this so
    the symlink-race guard lives in one place. Raises ``ValueError`` if the
    opened inode escaped the workspace; ``OSError`` / ``UnicodeDecodeError``
    propagate so callers handle them exactly as ``Path.read_text`` would.
    """
    workdir = Path(sandbox.workdir).resolve()
    fd = _open_contained(workdir, target, os.O_RDONLY)
    with os.fdopen(fd, encoding="utf-8", errors=errors) as fh:
        return fh.read()


def write_text_contained(sandbox, target: Path, content: str) -> None:
    """Write ``content`` to an already-resolved ``target`` through a descriptor
    verified inside ``sandbox.workdir`` (TOCTOU-safe).

    Opens ``O_CREAT`` without ``O_TRUNC``, verifies containment, and only then
    truncates + writes — so a symlink swapped in after the path check can never
    truncate or write content to a file outside the workspace. Raises
    ``ValueError`` on escape; ``OSError`` propagates.
    """
    workdir = Path(sandbox.workdir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_contained(workdir, target, os.O_WRONLY | os.O_CREAT)
    # fdopen takes ownership of fd; close fd ourselves only if it (or ftruncate)
    # fails before that hand-off, to avoid a double close.
    try:
        os.ftruncate(fd, 0)
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        fh.write(content)



def read_file(sandbox) -> Tool:
    def fn(args: dict) -> str:
        path_arg = args["path"]
        try:
            target = _safe_resolve(sandbox, path_arg)
        except ValueError as e:
            return f"ERROR: {e}"
        if not target.exists():
            return f"ERROR: {target} not found"
        if not target.is_file():
            return f"ERROR: {target} is not a file"
        # Read through a descriptor whose bound inode is verified to be inside
        # the workspace — a symlink swapped in after _safe_resolve can't
        # redirect the read outside (TOCTOU).
        try:
            data = read_text_contained(sandbox, target, errors="replace")
        except (ValueError, PermissionError, OSError) as e:
            return f"ERROR: {e}"
        if len(data) > MAX_READ_BYTES:
            return data[:MAX_READ_BYTES] + f"\n... [truncated, total {len(data)} bytes]"
        return data

    return Tool(
        name="read_file",
        description=(
            "Read a file from the workspace, returning its contents. "
            "Files larger than the configured read ceiling are truncated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to read, workspace-relative. Examples: "
                        "`src/foo.py`, `lib/bar/baz.js`. Absolute paths "
                        "and `..` traversal are rejected."
                    ),
                },
            },
            "required": ["path"],
        },
        fn=fn,
        parallel_safe=True,
    )


def write_file(sandbox, goal_id: str | int | None = "default") -> Tool:
    quota_goal_id = "default" if goal_id is None else goal_id

    def fn(args: dict) -> str:
        try:
            target = _safe_resolve(sandbox, args["path"])
        except ValueError as e:
            return f"ERROR: {e}"
        # Opt-in per-run file-write quota (default off -> no-op).
        from ..file_quota import check_and_add
        ok, msg = check_and_add(
            len(args["content"].encode("utf-8", "replace")),
            goal_id=quota_goal_id,
        )
        if not ok:
            return f"ERROR: {msg}"
        content = args["content"]
        # TOCTOU-safe write: a symlink swapped in after the path check can never
        # truncate or write content to a file outside the workspace.
        try:
            write_text_contained(sandbox, target, content)
        except (ValueError, PermissionError, OSError) as e:
            return f"ERROR: {e}"
        return f"wrote {len(content)} bytes to {target}"

    return Tool(
        name="write_file",
        description=(
            "Write content to a file in the workspace, creating the "
            "file (and any missing parent directories) if it doesn't "
            "exist, or OVERWRITING the entire file if it does. Use "
            "structured editing for surgical changes to existing files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to write, workspace-relative. Parent "
                        "directories created as needed."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Complete file contents. Will overwrite any "
                        "existing file at the path. Use a trailing "
                        "newline."
                    ),
                },
            },
            "required": ["path", "content"],
        },
        fn=fn,
    )


def list_dir(sandbox) -> Tool:
    def fn(args: dict) -> str:
        try:
            target = _safe_resolve(sandbox, args.get("path", "."))
        except ValueError as e:
            return f"ERROR: {e}"
        if not target.exists():
            return f"ERROR: {target} not found"
        if not target.is_dir():
            return f"ERROR: {target} is not a directory"
        workdir = Path(sandbox.workdir).resolve()
        # Verify + list through one directory descriptor so a symlink swapped in
        # after the path check can't redirect the listing outside the workspace.
        try:
            fd = _open_contained(
                workdir, target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except (ValueError, OSError) as e:
            return f"ERROR: {e}"
        # os.scandir(fd) does NOT take ownership of fd, so close it ourselves.
        try:
            with os.scandir(fd) as it:
                entries = [
                    f"{'d' if entry.is_dir() else '-'} {entry.name}"
                    for entry in sorted(it, key=lambda e: e.name)
                ]
            result = "\n".join(entries) if entries else "(empty)"
        except (PermissionError, OSError) as e:
            result = f"ERROR: {e}"
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - already closed
                pass
        return result

    return Tool(
        name="list_dir",
        description="List files in a directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
        fn=fn,
        parallel_safe=True,
    )
