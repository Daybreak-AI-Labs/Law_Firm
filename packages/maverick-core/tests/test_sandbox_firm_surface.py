"""Negative contract for the bounded two-person-firm sandbox surface."""
from __future__ import annotations

from pathlib import Path

import pytest
from maverick.sandbox import (
    BUILTIN_SANDBOX_BACKENDS,
    SandboxPolicyError,
    build_sandbox,
)

RETIRED_BACKENDS = (
    "devcontainer",
    "firecracker",
    "gvisor",
    "kubernetes",
    "modal",
    "podman",
    "ssh",
    "ep:vendor",
)


def test_exact_sandbox_module_surface():
    sandbox_dir = Path(__file__).parents[1] / "maverick" / "sandbox"
    assert {path.name for path in sandbox_dir.glob("*.py")} == {
        "__init__.py",
        "docker.py",
        "local.py",
        "sdk.py",
    }
    assert BUILTIN_SANDBOX_BACKENDS == ("local", "docker")


@pytest.mark.parametrize("backend", RETIRED_BACKENDS)
def test_retired_and_external_backends_fail_closed(backend, tmp_path):
    with pytest.raises(SandboxPolicyError, match="unsupported sandbox backend"):
        build_sandbox(
            workdir=tmp_path,
            backend=backend,
            sandbox_config={"backend": backend},
        )


def test_cross_run_pool_and_network_policy_are_absent():
    package = Path(__file__).parents[1] / "maverick"
    assert not (package / "sandbox" / "pool.py").exists()
    assert not (package / "sandbox" / "network_policy.py").exists()
    assert not (package / "tenant" / "egress.py").exists()


def test_parser_isolation_remains_default(monkeypatch):
    from maverick import parser_isolation

    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    assert parser_isolation.should_isolate() is True


def test_malformed_integer_boolean_cannot_enable_docker_egress(
    monkeypatch, tmp_path
):
    from maverick.sandbox import DockerBackend

    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    monkeypatch.setattr(DockerBackend, "_verify_docker", lambda self: None)
    backend = build_sandbox(
        workdir=tmp_path,
        backend="docker",
        sandbox_config={"backend": "docker", "allow_network": 2},
    )
    assert backend.allow_network is False


@pytest.mark.parametrize(
    "image",
    ["python:3.12-slim", "python:latest", "python", ""],
)
def test_secure_docker_refuses_mutable_or_default_image(
    image, monkeypatch, tmp_path
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    config = {"backend": "docker"}
    if image:
        config["image"] = image
    with pytest.raises(SandboxPolicyError, match="immutable image reference"):
        build_sandbox(
            workdir=tmp_path,
            backend="docker",
            sandbox_config=config,
        )


def test_require_container_refuses_mutable_image(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    with pytest.raises(SandboxPolicyError, match="immutable image reference"):
        build_sandbox(
            workdir=tmp_path,
            backend="docker",
            sandbox_config={
                "backend": "docker",
                "require_container": True,
                "image": "python:3.12-slim",
            },
        )


def test_secure_docker_accepts_digest_pinned_image(monkeypatch, tmp_path):
    from maverick.sandbox import DockerBackend

    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setattr(DockerBackend, "_verify_docker", lambda self: None)
    image = "python@sha256:" + ("a" * 64)
    backend = build_sandbox(
        workdir=tmp_path,
        backend="docker",
        sandbox_config={"backend": "docker", "image": image},
    )
    assert backend.image == image
