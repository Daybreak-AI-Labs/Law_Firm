"""Kernel-level department (suite) grant enforcement.

The grant store moved from the dashboard into the kernel so authenticated
suite checks share one policy source. Safety invariants under test:
  * empty acting principal (host operator / auth off) -> unrestricted;
  * configured dashboard admins -> unrestricted;
  * resolution chain: explicit grant -> registered resolver -> config default.
"""
from __future__ import annotations

import pytest
from maverick import suite_grants as sg


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "")


def test_unrestricted_paths():
    # No acting principal (CLI / auth off) and suite-less (generic) packs are
    # never scoped, even when a grant store exists for other principals.
    sg.set_suites("user:fin", ["finance"])
    assert sg.suite_allowed_for(None, "legal")
    assert sg.suite_allowed_for("", "legal")
    assert sg.suite_allowed_for("user:fin", None)
    # And an ungranted principal is unrestricted by default (opt-in scoping).
    assert sg.suite_allowed_for("user:other", "legal")


def test_admin_principals_never_scoped(monkeypatch):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "user:boss")
    sg.set_suites("user:boss", ["finance"])  # a stale grant must not bite
    assert sg.is_admin_principal("user:boss")
    assert sg.suite_allowed_for("user:boss", "legal")


def test_ensure_raises_department_access_error():
    sg.set_suites("user:fin", ["finance"])
    sg.ensure_suite_allowed("user:fin", "finance")
    with pytest.raises(sg.DepartmentAccessError) as exc:
        sg.ensure_suite_allowed("user:fin", "legal")
    assert "legal" in str(exc.value) and "user:fin" in str(exc.value)


def test_resolution_chain_explicit_then_resolver_then_default(monkeypatch):
    import maverick.config as config
    monkeypatch.setattr(config, "load_config",
                        lambda: {"dashboard": {"default_suites": ["tax"]}})
    calls = []

    def resolver(principal):
        calls.append(principal)
        if principal == "user:grouped":
            return ["finance", "bogus_suite"]  # unknown keys are dropped
        return None

    monkeypatch.setattr(sg, "_EXTRA_RESOLVERS", [resolver])
    # Explicit grant wins without consulting the resolver.
    sg.set_suites("user:explicit", ["legal"])
    assert sg.granted_suites("user:explicit") == frozenset({"legal"})
    assert calls == []
    # Resolver answer wins over the config default, validated to known suites.
    assert sg.granted_suites("user:grouped") == frozenset({"finance"})
    # No explicit, resolver has no opinion -> config default.
    assert sg.granted_suites("user:plain") == frozenset({"tax"})


def test_resolver_error_fails_closed(monkeypatch):
    import maverick.config as config
    monkeypatch.setattr(config, "load_config",
                        lambda: {"dashboard": {"default_suites": ["finance"]}})

    def broken(principal):
        raise RuntimeError("group store unavailable")

    monkeypatch.setattr(sg, "_EXTRA_RESOLVERS", [broken])
    with pytest.raises(sg.SuiteGrantStoreError):
        sg.granted_suites("user:x")


def test_resolver_error_cannot_become_unrestricted_without_default(monkeypatch):
    import maverick.config as config
    monkeypatch.setattr(config, "load_config", dict)

    def broken(principal):
        raise RuntimeError("group store unavailable")

    monkeypatch.setattr(sg, "_EXTRA_RESOLVERS", [broken])
    with pytest.raises(sg.SuiteGrantStoreError):
        sg.ensure_suite_allowed("user:x", "legal")


def test_register_grant_resolver_is_idempotent(monkeypatch):
    monkeypatch.setattr(sg, "_EXTRA_RESOLVERS", [])

    def fn(principal):
        return None

    sg.register_grant_resolver(fn)
    sg.register_grant_resolver(fn)
    assert [fn] == sg._EXTRA_RESOLVERS
