"""Health and containment checks for the retained sandbox catalog."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from maverick import config, health, sandbox
from maverick.sandbox.docker import DockerBackend


def _health_details(cfg, monkeypatch, *, which=True) -> tuple[str, list[str]]:
    rows: list[str] = []
    markers: list[str] = []

    def capture(marker, label, detail="", fix=""):
        markers.append(marker)
        rows.append(f"{detail} || {fix}")

    monkeypatch.setattr(health, "_row", capture)
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if which else None
    )
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: MagicMock(returncode=0)
    )
    health._check_sandbox(cfg)
    return "\n".join(rows), markers


def test_health_recognizes_exact_retained_catalog(monkeypatch):
    local, _ = _health_details({"sandbox": {"backend": "local"}}, monkeypatch)
    assert "local subprocess" in local

    docker, _ = _health_details({"sandbox": {"backend": "docker"}}, monkeypatch)
    assert "docker daemon responding" in docker


@pytest.mark.parametrize(
    "backend",
    [
        "devcontainer",
        "firecracker",
        "gvisor",
        "kubernetes",
        "modal",
        "podman",
        "ssh",
        "ep:vendor",
    ],
)
def test_health_rejects_retired_or_external_backend(backend, monkeypatch):
    details, markers = _health_details(
        {"sandbox": {"backend": backend}}, monkeypatch
    )
    assert "not supported by the firm runtime" in details
    assert health.RED in markers


def _argv(monkeypatch, tmp_path, **kwargs):
    monkeypatch.setattr(DockerBackend, "_verify_docker", lambda self: None)
    captured: dict = {}

    class Result:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **run_kwargs: captured.update(args=args) or Result(),
    )
    DockerBackend(workdir=tmp_path, **kwargs).exec("echo hi")
    return captured["args"]


def test_docker_memory_capped_and_containment_preserved(tmp_path, monkeypatch):
    args = _argv(monkeypatch, tmp_path)
    assert args[args.index("--memory") + 1] == "4g"
    assert args[args.index("--memory-swap") + 1] == "4g"
    assert "--cpus" not in args
    assert "--cap-drop" in args and "ALL" in args
    assert "no-new-privileges" in args
    assert args[args.index("--network") + 1] == "none"


def test_docker_cpu_cap_applied_when_configured(tmp_path, monkeypatch):
    args = _argv(monkeypatch, tmp_path, cpus="2", memory="1g")
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--memory") + 1] == "1g"


def test_build_sandbox_passes_resource_config(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerBackend, "_verify_docker", lambda self: None)
    monkeypatch.setattr(
        config,
        "get_sandbox",
        lambda: {"backend": "docker", "workdir": str(tmp_path)},
    )
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"sandbox": {"backend": "docker", "memory": "8g", "cpus": "4"}},
    )
    backend = sandbox.build_sandbox()
    assert backend.memory == "8g"
    assert backend.cpus == "4"


def test_read_only_paths_fail_closed_on_local(tmp_path):
    with pytest.raises(ValueError, match="read_only_paths requires"):
        sandbox.build_sandbox(
            workdir=tmp_path,
            backend="local",
            sandbox_config={"backend": "local", "read_only_paths": ["evidence"]},
        )
