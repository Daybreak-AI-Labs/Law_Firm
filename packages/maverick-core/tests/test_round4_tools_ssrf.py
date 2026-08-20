"""Audit round 4: SSRF-pin REST connectors and strip gold-patch env.

Three fixes under test:
  1. ``scrub_env`` strips ``MAVERICK_GOLD_PATCH`` for *every* sandbox shell,
     not just opaque-benchmark runs (shell.py only popped it for opaque). A
     non-opaque agent that runs ``printenv`` no longer reads the gold answer.
  2. The REST connector routes through ``_ssrf.safe_client`` (resolve-once +
     IP-pin), so a connector host that resolves to a private/metadata address
     can't exfil the bearer token. Surfaced as a clean ``ERROR: blocked host``.
  3. The GraphQL connector takes the same SSRF-safe path.
"""
from __future__ import annotations

import pytest

# --- fix 1: gold patch stripped from the non-opaque sandbox shell ----------

def test_scrub_env_strips_gold_patch_outside_opaque_mode():
    from maverick.sandbox.local import scrub_env
    # No MAVERICK_BENCHMARK_OPAQUE here: the shell tool only pops the var for
    # opaque runs, so without scrub_env covering it a normal-mode `printenv`
    # in the agent's shell would echo the SWE-bench gold patch.
    src = {
        "MAVERICK_GOLD_PATCH": "diff --git a/x b/x\n+the answer\n",
        "PATH": "/usr/bin",
    }
    out = scrub_env(src)
    assert "MAVERICK_GOLD_PATCH" not in out
    assert out["PATH"] == "/usr/bin"


def test_gold_patch_in_always_strip_list():
    from maverick.sandbox import local
    # GOLD_PATCH matches no keyword in _SECRET_ENV_RE, so it must be named
    # explicitly -- guard the regression in case the tuple is refactored.
    assert "MAVERICK_GOLD_PATCH" in local._ALWAYS_STRIP_ENV
    assert not local._SECRET_ENV_RE.search("MAVERICK_GOLD_PATCH")


# --- fix 2/3: REST + GraphQL connectors pin the host IP --------------------

@pytest.fixture(autouse=True)
def _no_allow_private(monkeypatch):
    # The SSRF guard honours MAVERICK_FETCH_ALLOW_PRIVATE=1 (on-prem opt-in);
    # make sure these tests run with the guard armed regardless of host env.
    monkeypatch.delenv("MAVERICK_FETCH_ALLOW_PRIVATE", raising=False)


def test_rest_connector_blocks_private_host(monkeypatch):
    from maverick.tools._rest_connector import make_rest_tool
    # A literal link-local IP avoids any DNS lookup: getaddrinfo returns it
    # verbatim and the guard rejects it as non-public (cloud metadata range).
    monkeypatch.setenv("RB_BASE", "http://169.254.169.254")
    monkeypatch.setenv("RB_TOKEN", "secret-bearer")  # pragma: allowlist secret
    tool = make_rest_tool(
        name="rb", base_url_env="RB_BASE", token_env="RB_TOKEN",
        description="test connector",
    )
    out = tool.fn({"op": "get", "path": "/latest/meta-data/"})
    assert "blocked host (SSRF guard)" in out
    # The bearer token must not leak into the error surface.
    assert "secret-bearer" not in out
def test_graphql_connector_blocks_private_host(monkeypatch):
    from maverick.tools._rest_connector import make_graphql_tool
    monkeypatch.setenv("GB_BASE", "http://169.254.169.254/graphql")
    monkeypatch.setenv("GB_TOKEN", "secret-bearer")  # pragma: allowlist secret
    tool = make_graphql_tool(
        name="gb", base_url_env="GB_BASE", token_env="GB_TOKEN",
        description="test graphql connector",
    )
    out = tool.fn({"query": "{ viewer { id } }"})
    assert "blocked host (SSRF guard)" in out
    assert "secret-bearer" not in out

# End retained connector SSRF tests.
