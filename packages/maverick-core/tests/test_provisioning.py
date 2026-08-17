"""Phase 5 — headless provisioning (`init --from-file`) + the runtime-protoc
opt-out for immutable/locked-down images."""
from __future__ import annotations

import pytest
from maverick import grpc_stubs

# ---- runtime-protoc opt-out -----------------------------------------------


def test_runtime_protoc_disabled_env(monkeypatch):
    monkeypatch.setenv("MAVERICK_NO_RUNTIME_PROTOC", "1")
    assert grpc_stubs.runtime_protoc_disabled() is True
    monkeypatch.setenv("MAVERICK_NO_RUNTIME_PROTOC", "0")
    assert grpc_stubs.runtime_protoc_disabled() is False


def test_guard_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("MAVERICK_NO_RUNTIME_PROTOC", "1")
    with pytest.raises(RuntimeError, match="gen-stubs"):
        grpc_stubs.guard_runtime_generation("federation.proto")


def test_guard_noop_when_enabled(monkeypatch):
    monkeypatch.delenv("MAVERICK_NO_RUNTIME_PROTOC", raising=False)
    assert grpc_stubs.guard_runtime_generation("maverick.proto") is None




# ---- headless provisioning -------------------------------------------------










def test_init_from_file_missing(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "out.toml"))
    r = CliRunner().invoke(main, ["init", "--from-file", str(tmp_path / "nope.toml")])
    assert r.exit_code != 0


