"""Graceful-shutdown drain: in-flight goal accounting + bounded drain (audit H12)."""
from __future__ import annotations

import time

from maverick import runner


def test_inflight_reflects_held_slots():
    assert runner.inflight_goals() == 0
    runner._mark_inflight_started()
    try:
        assert runner.inflight_goals() == 1
    finally:
        runner._mark_inflight_finished()
    assert runner.inflight_goals() == 0


def test_drain_returns_immediately_when_idle():
    t0 = time.monotonic()
    left = runner.drain_inflight(timeout=5.0)
    assert left == 0
    assert time.monotonic() - t0 < 1.0  # didn't wait out the timeout


def test_drain_times_out_with_a_held_slot():
    runner._mark_inflight_started()
    try:
        t0 = time.monotonic()
        left = runner.drain_inflight(timeout=0.3, poll=0.05)
        elapsed = time.monotonic() - t0
        assert left == 1                # slot never freed -> reported still in-flight
        assert 0.25 <= elapsed < 2.0    # waited ~the timeout, then gave up
    finally:
        runner._mark_inflight_finished()


def test_drain_unblocks_when_slot_released(monkeypatch):
    import threading

    runner._mark_inflight_started()

    def _release_soon():
        time.sleep(0.2)
        runner._mark_inflight_finished()

    threading.Thread(target=_release_soon, daemon=True).start()
    left = runner.drain_inflight(timeout=5.0, poll=0.05)
    assert left == 0  # drained once the slot was released, well under the timeout


def test_inflight_accounting_never_goes_negative(caplog):
    assert runner.inflight_goals() == 0
    runner._mark_inflight_finished()
    assert runner.inflight_goals() == 0
    assert "underflow" in caplog.text
