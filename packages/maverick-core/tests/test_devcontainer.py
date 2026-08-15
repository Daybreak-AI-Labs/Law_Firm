"""Regression tests for devcontainer spec parsing security.

The `image` field is read from a repo-supplied devcontainer.json and placed
as the IMAGE positional in `docker run`. A leading-dash value is parsed by
docker's CLI as a flag (e.g. `--privileged`), which would negate the
--cap-drop ALL / no-new-privileges hardening. `_parse_devcontainer` must
reject option-like image values.
"""
from __future__ import annotations

import json
import os

import pytest
from maverick.sandbox.devcontainer import (
    DevcontainerBackend,
    DevcontainerSpec,
    _parse_devcontainer,
)


def _write_devcontainer(tmp_path, image):
    p = tmp_path / "devcontainer.json"
    p.write_text(json.dumps({"image": image}), encoding="utf-8")
    return p


def test_parse_rejects_option_like_image(tmp_path):
    # `"image": "--privileged"` would inject a docker run flag that defeats the
    # sandbox's --cap-drop ALL / no-new-privileges hardening.
    p = _write_devcontainer(tmp_path, "--privileged")
    with pytest.raises(RuntimeError, match="option-like"):
        _parse_devcontainer(p)


@pytest.mark.parametrize(
    "bad",
    ["-v/etc:/host", "--mount=type=bind,src=/,dst=/host", "--network=host"],
)
def test_parse_rejects_other_flag_shapes(tmp_path, bad):
    p = _write_devcontainer(tmp_path, bad)
    with pytest.raises(RuntimeError, match="option-like"):
        _parse_devcontainer(p)


@pytest.mark.parametrize(
    "good",
    [
        "python:3.12-slim",
        "ubuntu:22.04",
        "ghcr.io/org/repo:tag",
        "registry.example.com:5000/team/img@sha256:" + "a" * 64,
    ],
)
def test_parse_accepts_normal_image(tmp_path, good):
    p = _write_devcontainer(tmp_path, good)
    spec = _parse_devcontainer(p)
    assert spec.image == good


# ---- isolation parity with DockerBackend (finding #5) ----------------------
#
# The devcontainer backend claims parity with DockerBackend but used to ship
# WEAKER defaults: network ON (every other container backend defaults
# --network none) and, with the spec-default remoteUser="root", the agent ran
# as root against the writable {project_dir}:{workspace} host bind-mount. These
# pin the corrected defaults: network off, and the container user pinned to the
# invoking uid:gid unless a non-root remoteUser or an explicit allow_root opt-in.


def _devc_args(monkeypatch, tmp_path, *, spec, **kw):
    """Capture the ``docker run`` argv one exec would produce, with the docker
    daemon check and the actual subprocess stubbed out."""
    monkeypatch.setattr(DevcontainerBackend, "_verify_docker", lambda self: None)
    cap: dict = {}

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        "subprocess.run", lambda args, **k: cap.update(args=args) or _R(),
    )
    be = DevcontainerBackend(project_dir=tmp_path, spec_override=spec, **kw)
    be.exec("echo hi")
    return cap["args"]


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uid/gid only")
def test_default_network_off_and_user_pinned_for_root_remoteuser(tmp_path, monkeypatch):
    monkeypatch.delenv("MAVERICK_SANDBOX_ALLOW_ROOT", raising=False)
    # Spec default remoteUser is "root"; default allow_network is now False.
    spec = DevcontainerSpec(image="python:3.12-slim")
    args = _devc_args(monkeypatch, tmp_path, spec=spec)
    # Network disabled by default -- parity with docker/podman/kubernetes.
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    # A `remoteUser: root` spec is NOT silently honored against the writable
    # host mount: the container user is pinned to the invoking uid:gid.
    assert "--user" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    # The DockerBackend-parity hardening stays intact.
    assert "--cap-drop" in args and "no-new-privileges" in args


def test_allow_network_true_omits_network_none(tmp_path, monkeypatch):
    spec = DevcontainerSpec(image="python:3.12-slim")
    args = _devc_args(monkeypatch, tmp_path, spec=spec, allow_network=True)
    assert "--network" not in args


def test_non_root_remoteuser_is_honored(tmp_path, monkeypatch):
    # An explicit non-root remoteUser from the spec is a safe choice: honor it.
    spec = DevcontainerSpec(image="python:3.12-slim", remote_user="vscode")
    args = _devc_args(monkeypatch, tmp_path, spec=spec)
    assert args[args.index("--user") + 1] == "vscode"


def test_allow_root_opts_back_into_root(tmp_path, monkeypatch):
    # Explicit opt-in: no --user pinning, so the container runs as root.
    monkeypatch.delenv("MAVERICK_SANDBOX_ALLOW_ROOT", raising=False)
    spec = DevcontainerSpec(image="python:3.12-slim")  # remoteUser root
    args = _devc_args(monkeypatch, tmp_path, spec=spec, allow_root=True)
    assert "--user" not in args


def test_build_sandbox_devcontainer_defaults_isolated(tmp_path, monkeypatch):
    # The build_sandbox default (not just the class field) defaults network OFF
    # and root OFF -- matching docker/podman.
    from maverick import config, sandbox

    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text(
        json.dumps({"image": "python:3.12-slim"}), encoding="utf-8",
    )
    monkeypatch.setattr(
        sandbox.DevcontainerBackend, "_verify_docker", lambda self: None,
    )
    monkeypatch.setattr(
        config, "get_sandbox",
        lambda: {"backend": "devcontainer", "workdir": str(tmp_path)},
    )
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"sandbox": {"backend": "devcontainer", "project_dir": str(tmp_path)}},
    )
    be = sandbox.build_sandbox()
    assert be.allow_network is False
    assert be.allow_root is False
