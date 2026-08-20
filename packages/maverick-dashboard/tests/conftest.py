"""Shared firm-policy defaults for dashboard request tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _trusted_testserver_origin(monkeypatch):
    """Treat Starlette's synthetic host as the configured firm origin.

    Individual host-policy and startup tests override this seam adversarially.
    Production has no fallback: it still loads the canonical HTTPS origin and
    exact trusted-host list from global configuration.
    """
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.public_origin_policy",
        lambda: (True, "https://testserver", frozenset({"testserver"})),
    )
