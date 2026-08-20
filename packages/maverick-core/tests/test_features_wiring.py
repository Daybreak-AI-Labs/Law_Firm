"""The [features] config section actually gates behavior.

These keys were documented in configuration.md but nothing read them:
  - skills      -> inject skills into agent prompts (env var still overrides)
  - streaming   -> live progress poller (covered by the config getter test;
                   the poller branch also depends on TTY/MAVERICK_NO_PROGRESS)
  - pack_editing-> allow editing/overriding agents (domain packs) from the
                   dashboard editor (mutating /api/v1/agents endpoints)
"""
from __future__ import annotations

# ---------- config.get_features ----------

def test_get_features_defaults_all_on(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")  # no [features] section at all
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    from maverick.config import get_features
    assert get_features() == {
        "skills": True, "streaming": True,
        "pack_editing": True, "role_editing": True,
    }


def test_get_features_reads_overrides(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[features]\nskills = false\nstreaming = false\n"
        "pack_editing = false\nrole_editing = false\n"
    )
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    from maverick.config import get_features
    assert get_features() == {
        "skills": False, "streaming": False,
        "pack_editing": False, "role_editing": False,
    }


# ---------- swarm._default_use_skills precedence ----------

def test_use_skills_env_wins_over_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[features]\nskills = false\n")
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    from maverick.swarm import _default_use_skills
    # env explicitly on -> on, despite config saying off
    monkeypatch.setenv("MAVERICK_USE_SKILLS", "1")
    assert _default_use_skills() is True
    # env explicitly off -> off
    monkeypatch.setenv("MAVERICK_USE_SKILLS", "0")
    assert _default_use_skills() is False


def test_use_skills_falls_back_to_config_when_env_unset(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[features]\nskills = false\n")
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    monkeypatch.delenv("MAVERICK_USE_SKILLS", raising=False)
    from maverick.swarm import _default_use_skills
    assert _default_use_skills() is False


def test_use_skills_default_on_when_nothing_set(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    monkeypatch.delenv("MAVERICK_USE_SKILLS", raising=False)
    from maverick.swarm import _default_use_skills
    assert _default_use_skills() is True
