"""Channel-server integration with the installed goal dispatcher."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.parametrize(
    ("dispatch_result", "response_text", "goal_status"),
    [
        ("queued", "queued for background processing", "pending"),
        (None, "internal error occurred", "blocked"),
    ],
)
def test_channel_submission_uses_dispatcher_and_never_runs_locally(
    monkeypatch, tmp_path, dispatch_result, response_text, goal_status,
):
    from maverick import runner
    from maverick import server as server_mod
    from maverick.world_model import WorldModel

    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    monkeypatch.delenv("MAVERICK_TENANT_BY_USER", raising=False)
    monkeypatch.setattr("maverick.world_model.DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setattr(
        "maverick.compliance.first_turn_disclosure", lambda *a, **k: None,
    )

    world = WorldModel(tmp_path / "world.db")
    submitted = []

    class FakeDispatcher:
        def submit(self, goal_id, **kwargs):
            submitted.append((goal_id, kwargs))
            return dispatch_result

    monkeypatch.setattr(runner, "_dispatcher", FakeDispatcher())
    monkeypatch.setattr(
        runner,
        "run_goal_in_thread",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("channel bypassed installed dispatcher")
        ),
    )

    srv = server_mod.Server.__new__(server_mod.Server)
    srv.world = world
    srv.llm = object()
    srv.sandbox = object()
    srv.max_depth = 3
    srv._channels = []
    srv._tasks = []
    srv._shield = None

    class Message:
        channel = "telegram"
        user_id = "alice"
        text = "review the handoff"
        attachments: list = []

    try:
        response = asyncio.run(srv._handle_message(Message()))
        goal = world.get_goal(submitted[0][0])
    finally:
        world.close()

    assert response_text in response
    assert len(submitted) == 1
    goal_id, kwargs = submitted[0]
    assert goal_id > 0
    assert goal.status == goal_status
    assert kwargs["channel"] == "telegram"
    assert kwargs["user_id"] == "telegram:alice"
    assert kwargs["conversation_id"] > 0
