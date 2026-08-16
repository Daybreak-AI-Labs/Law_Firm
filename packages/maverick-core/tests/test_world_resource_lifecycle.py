"""World-backend ownership at long-lived runtime boundaries."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick import tools as tools_mod
from maverick import world_model as world_model_mod
from maverick.grpc_api import server as grpc_server_mod
from maverick.grpc_api.service import EventDTO


class _Closable:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Channel:
    name = "test"

    def __init__(self) -> None:
        self.stop_calls = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stop_calls += 1


def test_base_tool_names_closes_temporary_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _Closable()
    registry = SimpleNamespace(
        all=lambda: [SimpleNamespace(name="shell"), SimpleNamespace(name="recall")],
    )
    monkeypatch.setattr(world_model_mod, "open_world", lambda _path=None: world)
    monkeypatch.setattr(tools_mod, "base_registry", lambda *_a, **_kw: registry)

    assert tools_mod.base_tool_names() == {"recall", "shell"}
    assert world.close_calls == 1


def test_grpc_disconnect_explicitly_closes_episode_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Codes:
        UNAUTHENTICATED = "UNAUTHENTICATED"

    class _Pb2Grpc:
        class MaverickServicer:
            pass

    class _Pb2:
        Event = SimpleNamespace

    class _Stream:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.sent:
                raise StopIteration
            self.sent = True
            return EventDTO(1, 2, "agent", "kind", "content", 3.0)

        def close(self) -> None:
            self.closed = True

    stream = _Stream()
    service = SimpleNamespace(stream_episode=lambda *_a, **_kw: stream)
    monkeypatch.setattr(grpc_server_mod, "_grpc_code", lambda: _Codes)
    servicer = grpc_server_mod._servicer(
        service,
        _Pb2,
        _Pb2Grpc,
        bearer_token="secret",
    )
    context = SimpleNamespace(
        invocation_metadata=lambda: (("authorization", "Bearer secret"),),
        is_active=lambda: False,
    )
    request = SimpleNamespace(goal_id=2, since_id=0, max_seconds=1.0)

    assert list(servicer.StreamEpisode(request, context)) == []
    assert stream.closed is True
