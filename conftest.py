"""Repo-root pytest bootstrap: isolate ``~/.maverick`` BEFORE any maverick import.

Many maverick modules freeze ``Path.home()``-derived paths into module-level
constants at import time (``world_model.DEFAULT_DB``, ``skills.SKILLS_DIR``,
``skill_stats.DEFAULT_PATH``, ``self_learning.LEARNED_PATH``, the audit dirs,
...). The per-test ``HOME`` monkeypatch in maverick-core's conftest runs too
late for those: in a full-suite run the first collected module to import
maverick bakes the REAL home into the constants, and every test that then
relies on a default path writes the developer's actual ``~/.maverick`` --
observed as fake swe-bench goals + phantom spend in ``maverick budget``, junk
``good``/``bad`` skills that ``relevant_skills()`` would inject into REAL
future runs, and test rows in the real audit log.

Mutating the environment at root-conftest *import* is the earliest hook pytest
offers: it precedes package conftests and every collection import, so the
frozen constants bake a throwaway session dir instead. It also covers
``benchmarks/`` (no conftest of its own) and ``apps/``. Per-test ``tmp_path``
HOME fixtures still apply on top for call-time resolution, and subprocesses
spawned by tests inherit the redirect.

``test_home_isolation.py`` pins this contract against the OS-level home (via
``pwd``), which environment variables cannot fool.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_session_home = tempfile.mkdtemp(prefix="maverick-test-home-")
os.environ["HOME"] = _session_home
os.environ["USERPROFILE"] = _session_home  # Windows: what Path.home() reads
atexit.register(shutil.rmtree, _session_home, True)

# Secure-by-default ships ON in production (audit signing, at-rest encryption,
# fail-closed high-risk consent, ...). The existing suite asserts each control's
# on/off mechanics under explicit config, so pin the legacy posture here for
# stability (same pattern as the MAVERICK_BUILTIN_SKILLS=0 pin); the secure
# DEFAULT itself is covered by test_secure_defaults.py, which overrides this.
os.environ.setdefault("MAVERICK_SECURE_DEFAULT", "0")

# Voice STT model auto-fetch ships ON so the dashboard mic works out of the
# box, but a test that wanders into the local STT chain must never download a
# ~150 MB Whisper model from Hugging Face. Tests of the auto-fetch mechanics
# set this env themselves (against a mocked urlopen).
os.environ.setdefault("MAVERICK_VOICE_AUTO_FETCH", "0")


@pytest.fixture(autouse=True)
def _isolate_dynamic_maverick_home(tmp_path, monkeypatch):
    """Give every package a fresh dynamic home, including on Windows.

    The core test package already did this locally, but dashboard/channels/MCP
    tests share the repo-root session home. Stores migrated to the documented
    ``MAVERICK_HOME`` resolver then leaked RBAC grants, invites, and UI policy
    across tests. Set every supported home input consistently per test; an
    individual test can still override any of them afterward.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / ".maverick"))


@pytest.fixture(autouse=True)
def _reset_governed_process_authority_pin():
    """Keep process-lifetime production admission state isolated per test."""
    try:
        from maverick import governed_records
    except ImportError:
        yield
        return
    governed_records._reset_process_authority_pin_for_testing()
    try:
        yield
    finally:
        governed_records._reset_process_authority_pin_for_testing()


@pytest.fixture(autouse=True)
def _isolate_default_audit_writers():
    """Do not let a cached deployment-global writer cross test data roots.

    ``default_audit_log`` intentionally caches one writer for a production
    process. Tests change ``MAVERICK_HOME`` for every case, though, and the
    process-level singleton would otherwise keep writing to the first case's
    audit chain. Besides leaking state, stable outbox identities from two
    independent test worlds can then collide and correctly fail closed.
    """
    try:
        from maverick.audit import writer
    except ImportError:
        yield
        return

    def _reset() -> None:
        with writer._default_lock:
            writer._default = None
            writer._defaults.clear()

    _reset()
    try:
        yield
    finally:
        _reset()
