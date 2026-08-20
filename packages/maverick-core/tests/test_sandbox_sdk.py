"""Structural contract for the two retained sandbox backends."""
from __future__ import annotations

import pytest
from maverick.sandbox import sdk
from maverick.sandbox.docker import DockerBackend
from maverick.sandbox.local import ExecResult, LocalBackend


@pytest.mark.parametrize("backend_cls", [LocalBackend, DockerBackend])
def test_every_retained_backend_conforms(backend_cls):
    assert sdk.conformance(backend_cls) == []


def test_local_instance_satisfies_protocol(tmp_path):
    sb = LocalBackend(workdir=tmp_path)
    assert isinstance(sb, sdk.SandboxV2)
    assert sdk.conformance(sb) == []
    assert "exec" in sdk.capabilities(sb)


def test_conformance_flags_missing_exec():
    class Bad:
        workdir = "."

    assert any("missing exec" in problem for problem in sdk.conformance(Bad))


def test_conformance_flags_missing_timeout_kwarg():
    class NoTimeout:
        workdir = "."

        def exec(self, cmd):
            return ExecResult(stdout="", stderr="", exit_code=0)

    assert any("timeout" in problem for problem in sdk.conformance(NoTimeout))


def test_conformance_accepts_var_keyword():
    class Kw:
        workdir = "."

        def exec(self, cmd, **kw):
            return ExecResult(stdout="", stderr="", exit_code=0)

    assert sdk.conformance(Kw) == []


def test_conformance_flags_missing_workdir():
    class NoWorkdir:
        def exec(self, cmd, timeout=None):
            return ExecResult(stdout="", stderr="", exit_code=0)

    assert any("workdir" in problem for problem in sdk.conformance(NoWorkdir))


def test_capabilities_reports_optional_methods():
    class WithFiles:
        workdir = "."

        def exec(self, cmd, timeout=None):
            return ExecResult(stdout="", stderr="", exit_code=0)

        def put_file(self, src, dst):
            return None

        def exec_authenticated_tests(self, request, timeout=None):
            return None

    assert sdk.capabilities(WithFiles()) == {
        "exec",
        "put_file",
        "exec_authenticated_tests",
    }


def test_sdk_has_no_external_loader_surface():
    assert not hasattr(sdk, "installed_entry_point_names")
    assert not hasattr(sdk, "load_entry_point_backend")


def test_sdk_exports():
    import maverick.sandbox as sandbox

    assert sandbox.SDK_VERSION == 2
    assert sandbox.SandboxV2 is sdk.SandboxV2
