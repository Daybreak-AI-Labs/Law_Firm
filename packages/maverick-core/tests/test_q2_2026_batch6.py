"""Reflexion storage edge cases retained from the Q2 2026 batch."""
from __future__ import annotations

import time


def test_reflexion_recall_empty_file(tmp_path):
    from maverick.reflexion import recall
    p = tmp_path / "nonexistent.ndjson"
    assert recall("anything", path=p) == []


def test_reflexion_recall_no_query(tmp_path):
    from maverick.reflexion import recall, record
    p = tmp_path / "x.ndjson"
    record("g", "k", "m", "r", path=p)
    assert recall("", path=p) == []


def test_reflexion_list_recent_newest_first(tmp_path):
    from maverick.reflexion import list_recent, record
    p = tmp_path / "x.ndjson"
    record("first", "k", "m", "r", path=p)
    time.sleep(0.01)
    record("second", "k", "m", "r", path=p)
    time.sleep(0.01)
    record("third", "k", "m", "r", path=p)
    entries = list_recent(limit=10, path=p)
    assert [e.goal_text for e in entries] == ["third", "second", "first"]


def test_reflexion_clear(tmp_path):
    from maverick.reflexion import clear, recall, record
    p = tmp_path / "x.ndjson"
    record("g", "k", "m", "r", path=p)
    assert p.exists()
    assert clear(path=p) is True
    assert not p.exists()
    assert recall("g", path=p) == []


def test_reflexion_file_perms_600(tmp_path):
    """File should be created with mode 0600."""
    import os
    import stat

    from maverick.reflexion import record
    p = tmp_path / "perm.ndjson"
    record("g", "k", "m", "r", path=p)
    assert p.exists()
    mode = stat.S_IMODE(p.stat().st_mode)
    # 0o600 OR 0o644 depending on umask + test env; the helper does
    # chmod 600 but we don't fail if the FS quietly refuses. POSIX-only:
    # NTFS reports 0o666 and os.geteuid() doesn't exist on Windows.
    if os.name != "nt":
        assert (mode & 0o077) == 0 or os.geteuid() == 0


def test_reflexion_format_context_renders_sections():
    from maverick.reflexion import Reflexion, format_context
    r = Reflexion(
        ts=time.time(),
        goal_text="Refactor auth",
        failure_class="auth",
        failure_msg="401",
        reflection="Read middleware first next time.",
    )
    out = format_context([(0.85, r)])
    assert "Prior failures on similar goals" in out
    assert "Refactor auth" in out
    assert "Read middleware first" in out


def test_reflexion_format_context_empty_returns_empty():
    from maverick.reflexion import format_context
    assert format_context([]) == ""


def test_reflexion_record_failsafe_on_bad_path(tmp_path):
    """Writing to a path under a file (cannot create) is a soft failure."""
    from maverick.reflexion import record
    blocking = tmp_path / "blocker"
    blocking.write_text("x")
    bad = blocking / "subdir" / "f.ndjson"
    # Recording must NOT raise; returns False.
    ok = record("g", "k", "m", "r", path=bad)
    assert ok is False
