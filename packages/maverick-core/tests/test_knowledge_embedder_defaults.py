"""Firm knowledge configuration never implies a remote model acquisition."""
from __future__ import annotations

import pytest
from maverick import config


def _knowledge(monkeypatch, section: dict) -> dict:
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {"knowledge": section})
    return config.get_knowledge()


@pytest.mark.parametrize(
    ("embedder", "model", "digest", "dim"),
    [
        ("local", "", "", 384),
        ("deterministic", "", "", 256),
    ],
)
def test_model_metadata_follows_selected_embedder(
    monkeypatch,
    embedder: str,
    model: str,
    digest: str,
    dim: int,
):
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": embedder})
    assert resolved["model"] == model
    assert resolved["model_digest"] == digest
    assert resolved["dim"] == dim


def test_local_never_defaults_to_a_repository_id(monkeypatch):
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": "local"})
    assert resolved["model"] == ""
    assert resolved["model_digest"] == ""


def test_explicit_pinned_local_model_metadata_wins(monkeypatch, tmp_path):
    model_dir = str(tmp_path.resolve())
    digest = "sha256:" + "a" * 64
    resolved = _knowledge(
        monkeypatch,
        {
            "enable": True,
            "embedder": "local",
            "model": model_dir,
            "model_digest": digest,
            "dim": 512,
        },
    )
    assert resolved["model"] == model_dir
    assert resolved["model_digest"] == digest
    assert resolved["dim"] == 512


def test_unset_embedder_defaults_to_unconfigured_local(monkeypatch):
    resolved = _knowledge(monkeypatch, {"enable": True})
    assert resolved["embedder"] == "local"
    assert (resolved["model"], resolved["model_digest"], resolved["dim"]) == (
        "",
        "",
        384,
    )


def test_unknown_embedder_uses_non_remote_metadata_before_builder_rejects(monkeypatch):
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": "nonsense"})
    assert (resolved["model"], resolved["model_digest"], resolved["dim"]) == (
        "",
        "",
        384,
    )
