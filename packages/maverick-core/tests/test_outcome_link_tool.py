"""The remember_outcome_key tool: a run links an external business id to its
episode so a later real-world outcome can ground it. Registered only when the
learning loop ([consequence]) is on."""
from __future__ import annotations

from maverick import consequence
from maverick.tools.outcome_link import remember_outcome_key


class _Ep:
    def __init__(self, id):
        self.id = id


class _World:
    def __init__(self, episodes):
        self._episodes = episodes

    def list_episodes(self, goal_id=None, limit=50):
        return self._episodes[:limit]


def test_tool_links_key_to_latest_episode(tmp_path, monkeypatch):
    monkeypatch.setattr("maverick.paths.data_dir", lambda *p, **k: tmp_path.joinpath(*p))
    consequence.reset_shared()
    tool = remember_outcome_key(_World([_Ep(55)]), goal_id=7)
    msg = tool.fn(key="invoice:INV-3")
    assert "Linked" in msg
    assert consequence.shared_correlation().resolve("invoice:INV-3") == (7, 55)


def test_tool_requires_key():
    tool = remember_outcome_key(_World([_Ep(1)]), goal_id=1)
    assert tool.fn(key="  ").startswith("ERROR")


def test_tool_requires_active_goal():
    tool = remember_outcome_key(None, goal_id=None)
    assert tool.fn(key="x").startswith("ERROR")


def test_tool_handles_no_episode_yet():
    tool = remember_outcome_key(_World([]), goal_id=1)
    assert tool.fn(key="x").startswith("ERROR")


def test_tool_registered_only_when_consequence_on(tmp_path, monkeypatch):
    from maverick.sandbox import LocalBackend
    from maverick.tools import base_registry
    from maverick.world_model import WorldModel
    w = WorldModel(path=tmp_path / "world.db")
    sb = LocalBackend(workdir=tmp_path)

    monkeypatch.delenv("MAVERICK_CONSEQUENCE", raising=False)
    monkeypatch.setattr("maverick.config.get_consequence", lambda: {"enable": False})
    reg = base_registry(w, sb, goal_id=1)
    assert "remember_outcome_key" not in {t.name for t in reg.all()}

    monkeypatch.setenv("MAVERICK_CONSEQUENCE", "1")
    reg2 = base_registry(w, sb, goal_id=1)
    assert "remember_outcome_key" in {t.name for t in reg2.all()}
