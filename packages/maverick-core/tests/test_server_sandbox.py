from __future__ import annotations

from pathlib import Path


def test_build_from_config_uses_configured_sandbox_backend(monkeypatch, tmp_path):
    from maverick import server as server_mod

    class _FakeWorld:
        pass

    class _FakeLLM:
        def __init__(self, **_kwargs):
            pass

    calls = {}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(server_mod, "load_config", lambda: {
        "sandbox": {"backend": "docker", "workdir": str(tmp_path)},
        "channels": {},
    })
    monkeypatch.setattr(server_mod, "WorldModel", _FakeWorld)
    monkeypatch.setattr(server_mod, "LLM", _FakeLLM)

    def _fake_build_sandbox(*, workdir=None, backend=None):
        calls["workdir"] = workdir
        calls["backend"] = backend

        class _Sandbox:
            pass

        return _Sandbox()

    monkeypatch.setattr(server_mod, "build_sandbox", _fake_build_sandbox)

    try:
        server_mod.build_from_config()
    except RuntimeError as e:
        assert "No channels enabled" in str(e)

    assert calls["backend"] == "docker"
    assert Path(calls["workdir"]) == tmp_path


def _make_bare_server():
    """A Server with just enough state to exercise add_channel (no world/llm)."""
    from maverick import server as server_mod

    srv = object.__new__(server_mod.Server)
    srv._channels = []
    return srv


class _DummyChannel:
    name = "dummy"

    async def start(self):  # pragma: no cover - never run in this test
        pass

    async def send(self, user_id, text):  # pragma: no cover
        pass


def test_add_channel_wraps_when_rich_render_on(monkeypatch):
    from maverick_channels import rich_render

    monkeypatch.setattr(rich_render, "enabled", lambda: True)
    srv = _make_bare_server()
    ch = _DummyChannel()
    srv.add_channel(ch)
    assert isinstance(srv._channels[0], rich_render.RichRenderChannel)
    # The wrapper proxies attribute access to the inner channel.
    assert srv._channels[0].name == "dummy"


def test_add_channel_passthrough_when_rich_render_off(monkeypatch):
    from maverick_channels import rich_render

    monkeypatch.setattr(rich_render, "enabled", lambda: False)
    srv = _make_bare_server()
    ch = _DummyChannel()
    srv.add_channel(ch)
    # Default (knob off): the channel is stored unchanged, no wrapping.
    assert srv._channels[0] is ch
