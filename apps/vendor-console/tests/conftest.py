"""Test fixtures for the vendor console.

Pins a deterministic signing key + a scratch DAYBREAK_HOME so no real key file is
written, and puts maverick-core + the app package on the path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for p in (Path(__file__).parent,                       # so tests can `import conftest`
          _ROOT / "apps" / "vendor-console", _ROOT / "packages" / "maverick-core"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# If the web deps are absent (e.g. a lint-only environment), ignore this dir's
# test files CLEANLY rather than raising a collection error that would abort the
# whole repo-wide `pytest` run (see CLAUDE.md: one collection error stops all).
collect_ignore_glob: list[str] = []
try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    try:
        import python_multipart  # noqa: F401  (newer import name)
    except ImportError:
        import multipart  # noqa: F401  (older python-multipart)
except ImportError:  # pragma: no cover
    collect_ignore_glob = ["test_*.py"]

# A fixed publisher private key so signatures + public_key_hex are deterministic.
TEST_SIGNING_KEY = "1" * 64


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDOR_CONSOLE_SIGNING_KEY", TEST_SIGNING_KEY)
    monkeypatch.setenv("VENDOR_CONSOLE_SECRET", "test-session-secret")
    monkeypatch.setenv("VENDOR_CONSOLE_INSECURE", "1")   # TestClient rides http
    monkeypatch.setenv("DAYBREAK_HOME", str(tmp_path / "daybreak"))
    from vendor_console import ratelimit
    ratelimit.reset_all()                                # no cross-test lockout leakage


@pytest.fixture
def conn(tmp_path):
    from vendor_console import db
    c = db.connect(str(tmp_path / "console.db"))
    db.init_db(c)
    return c


@pytest.fixture
def app(tmp_path):
    from vendor_console.app import create_app
    return create_app(str(tmp_path / "app.db"))


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


# Helpers are exposed as FIXTURES (not module functions imported via
# `from conftest import …`) — a top-level `conftest` import collides across the
# repo's many conftest.py files under `pytest --import-mode=importlib` and would
# abort collection. Fixtures are directory-scoped and safe.

@pytest.fixture
def make_staff(app):
    """Factory: create a staff row directly (bypasses the UI setup), return id."""
    from vendor_console import security, store

    def _make(*, email="owner@daybreak.co", role="owner"):
        return store.create_staff(
            app.state.conn, email=email, name="O", role=role,
            pw_hash=security.hash_password("password-1234"),  # pragma: allowlist secret
            totp_secret=security.new_totp_secret())
    return _make


@pytest.fixture
def login_cookie():
    """Factory: a full-session cookie for a staff id (skips password+TOTP)."""
    from vendor_console import security
    from vendor_console.auth import SESSION_COOKIE

    def _cookie(staff_id: int, role: str = "owner", epoch: int = 0) -> dict:
        token = security.sign_session(
            {"sid": staff_id, "stage": "full", "role": role, "ep": epoch},
            security.session_key())
        return {SESSION_COOKIE: token}
    return _cookie


@pytest.fixture
def admin(client, make_staff, login_cookie):
    """A TestClient authenticated as an owner (full session)."""
    sid = make_staff(email="owner@daybreak.co", role="owner")
    client.cookies.update(login_cookie(sid, "owner"))
    return client
