"""Goal-execution dispatcher seam (threads now, queue later).

``run_goal_in_background`` routes through a swappable Dispatcher so a future
queue/worker backend is a ``set_dispatcher`` call, not a caller rewrite.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import maverick.runner as runner
import pytest


def test_default_dispatcher_is_local_thread():
    assert isinstance(runner.get_dispatcher(), runner.LocalThreadDispatcher)


def test_run_goal_in_background_delegates_to_active_dispatcher(monkeypatch):
    calls = []

    class FakeDispatcher:
        def submit(self, goal_id, **kw):
            calls.append((goal_id, kw))
            return "done"

    monkeypatch.setattr(runner, "_dispatcher", FakeDispatcher())
    out = runner.run_goal_in_background(
        7, max_dollars=2.0, channel="api", user_id="u1", conversation_id=19,
        allowed_suites=frozenset({"finance"}),
    )
    assert out == "done"
    assert calls[0][0] == 7
    assert calls[0][1]["max_dollars"] == 2.0
    assert calls[0][1]["channel"] == "api"
    assert calls[0][1]["user_id"] == "u1"
    assert calls[0][1]["conversation_id"] == 19
    assert calls[0][1]["allowed_suites"] == frozenset({"finance"})


@pytest.mark.asyncio
async def test_async_dispatcher_wrapper_runs_submission_off_event_loop(monkeypatch):
    calls = []
    event_loop_thread = threading.get_ident()

    class FakeDispatcher:
        def submit(self, goal_id, **kw):
            calls.append((threading.get_ident(), goal_id, kw))
            return "done"

    monkeypatch.setattr(runner, "_dispatcher", FakeDispatcher())
    out = await runner.run_goal_in_background_async(
        8, channel="telegram", user_id="alice", conversation_id=23,
        allowed_suites=frozenset(),
    )

    assert out == "done"
    assert calls[0][0] != event_loop_thread
    assert calls[0][1] == 8
    assert calls[0][2]["conversation_id"] == 23
    assert calls[0][2]["allowed_suites"] == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatcher_result", "expected_result"),
    [(None, "dispatch unavailable"), (RuntimeError("broker down"), "dispatch failed")],
)
async def test_async_dispatch_failure_blocks_pending_goal_without_local_fallback(
    monkeypatch, dispatcher_result, expected_result,
):
    from maverick import world_model

    updates = []

    class FakeWorld:
        def get_goal(self, goal_id):
            return SimpleNamespace(status="pending")

        def set_goal_status(self, goal_id, status, *, result):
            updates.append((goal_id, status, result))

    class FailingDispatcher:
        def submit(self, goal_id, **kw):
            if isinstance(dispatcher_result, Exception):
                raise dispatcher_result
            return dispatcher_result

    monkeypatch.setattr(runner, "_dispatcher", FailingDispatcher())
    monkeypatch.setattr(
        runner,
        "run_goal_in_thread",
        lambda *a, **k: pytest.fail("dispatch failure fell back to local execution"),
    )
    monkeypatch.setattr(world_model, "open_world", lambda: FakeWorld())
    monkeypatch.setattr(world_model, "close_world_if_owned", lambda world: None)

    out = await runner.run_goal_in_background_async(41)

    assert out == ("error" if isinstance(dispatcher_result, Exception) else None)
    assert updates == [(41, "blocked", expected_result)]


def test_local_dispatcher_delegates_to_run_goal_in_thread(monkeypatch):
    seen = {}

    def fake_run(*, goal_id, **kw):
        seen["goal_id"] = goal_id
        seen.update(kw)
        return "blocked"

    monkeypatch.setattr(runner, "run_goal_in_thread", fake_run)
    out = runner.LocalThreadDispatcher().submit(
        42, max_wall_seconds=30.0, conversation_id=17,
        allowed_suites=frozenset({"legal"}),
    )
    assert out == "blocked"
    assert seen["goal_id"] == 42
    assert seen["max_wall_seconds"] == 30.0
    assert seen["conversation_id"] == 17
    assert seen["allowed_suites"] == frozenset({"legal"})


def test_set_dispatcher_swaps_and_is_restorable():
    original = runner.get_dispatcher()
    try:
        sentinel = runner.LocalThreadDispatcher()
        runner.set_dispatcher(sentinel)
        assert runner.get_dispatcher() is sentinel
    finally:
        runner.set_dispatcher(original)
    assert runner.get_dispatcher() is original
