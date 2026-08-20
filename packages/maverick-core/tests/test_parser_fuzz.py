"""Deterministic fuzz harness for the untrusted-input parsers.

Every verifier/decoder that handles data from an external party (federation
peers, A2A clients, webhook senders, share-link holders, the math tool) documents
a fail-closed contract: on malformed input it returns an error / ``None`` / a
rejection tuple, or raises ONLY its one documented exception — it must never leak
an unexpected exception (``RecursionError``, ``KeyError``, ``TypeError`` …) or
hang. An unhandled exception on this surface is at least a DoS.

This is a seed-based fuzzer (stdlib ``random``, fixed seeds) rather than
Hypothesis so it needs no new dependency and is byte-for-byte reproducible in CI.
The corpus deliberately includes recursion bombs and oversized values.
"""
from __future__ import annotations

import logging
import random

import pytest


@pytest.fixture(autouse=True)
def _quiet_logs():
    """Parser fail-closed contracts are about return values, not log volume; the
    reject paths log at WARNING/INFO and would otherwise flood the output. Scope
    the silencing to THIS module's tests and restore after — a module-level
    ``logging.disable`` would leak globally and break tests that assert on logs.
    """
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


_SCALARS = [
    None, True, False, 0, 1, -1, 1 << 70, -(1 << 70), 3.14, -2.5,
    float("nan"), float("inf"), float("-inf"),
    "", "x", "x" * 20000, "\x00\x01\x02", "  ", "\n\t", "💥🔥", "café",
    "../../etc/passwd", "a.b.c", "a.b", "=", ".", "//", "\\", "sha256=",
    "Bearer ", "share://", "{}", "[]", "ab" * 32, "deadbeef", "0x1",
    b"", b"\xff\xfe\x00", b"abc",
]

# Keys that appear across the envelope / token / card schemas, so generated
# dicts look "almost valid" and exercise the post-shape-check code paths.
_KEYS = [
    "schema", "origin", "audience", "created_at", "correlation_id", "sig",
    "pubkey", "key_id", "to", "from", "channel", "user_id", "text", "listings",
    "goal_title", "goal_description", "exp", "sub", "nonce", "protocolVersion",
    "name", "url", "version", "capabilities", "skills", "id", "taskId",
    "pushNotificationConfig", "max_risk", "deadline_ms", "requested_tools",
]


def _rand_string(rng: random.Random) -> str:
    return "".join(
        chr(rng.randint(0, 0x2FFFF)) for _ in range(rng.randint(0, 48))
    )


def _bomb(rng: random.Random):
    """A deeply nested dict/list — the json.dumps / recursion DoS vector."""
    depth = rng.choice([200, 1500, 4000])
    root = cur = {} if rng.random() < 0.5 else []
    for _ in range(depth):
        nxt: object = {} if rng.random() < 0.5 else []
        if isinstance(cur, dict):
            cur["x"] = nxt
        else:
            cur.append(nxt)
        cur = nxt
    return root


def _rand_value(rng: random.Random, depth: int = 0):
    r = rng.random()
    if r < 0.45 or depth > 3:
        return rng.choice(_SCALARS)
    if r < 0.60:
        return _rand_string(rng)
    if r < 0.80:
        return {
            rng.choice(_KEYS): _rand_value(rng, depth + 1)
            for _ in range(rng.randint(0, 5))
        }
    return [_rand_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]


def corpus(seed: int, n: int = 600) -> list:
    """A reproducible mix of scalars, near-valid dicts, mutations, and bombs."""
    rng = random.Random(seed)
    out: list = []
    for i in range(n):
        if i % 50 == 0:
            out.append(_bomb(rng))
        else:
            out.append(_rand_value(rng))
    return out


def _assert_fail_closed(name, fn, value, allowed: tuple):
    try:
        fn(value)
    except allowed:
        pass
    except Exception as e:  # noqa: BLE001 - the whole point is to catch the leak
        raise AssertionError(
            f"{name} leaked {type(e).__name__} on {value!r:.80}: {e}"
        ) from e


def _run(name, fn, *, allowed: tuple = (), seed: int = 1):
    for value in corpus(seed):
        _assert_fail_closed(name, fn, value, allowed)


# ---- federation signed envelopes (the recursion-bomb regression) -------------

def test_fuzz_web_session():
    from maverick.web_session import verify_session
    _run("verify_session", lambda v: verify_session(v, "secret"))
    _run("verify_session(bytes-secret-guard)",
         lambda v: verify_session("a.b", v if isinstance(v, str) else "s"))


def test_fuzz_share_link(monkeypatch):
    monkeypatch.setenv("MAVERICK_SHARE_SECRET", "s3cr3t")
    from maverick import share_link
    _run("verify_share_link",
         lambda v: share_link.verify_share_link(v if isinstance(v, str) else "a.b.c"),
         allowed=(ValueError,))


def test_fuzz_claim_handoff(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_SHARE_SECRET", "s3cr3t")
    from maverick import share_link
    nonces = tmp_path / "nonces.json"
    _run("claim_handoff",
         lambda v: share_link.claim_handoff(
             v if isinstance(v, str) else "a.b", nonce_path=nonces),
         allowed=(ValueError,))


# ---- OIDC + the math evaluator -----------------------------------------------

def test_fuzz_oidc_verify():
    from maverick.oidc import OIDCConfig, OIDCError, verify_oidc_token
    cfg = OIDCConfig(enabled=True, issuer="https://i", audience="aud",
                     jwks_uri="https://j", algorithms=["RS256"])
    _run("verify_oidc_token",
         lambda v: verify_oidc_token(
             v if isinstance(v, str) else "a.b.c", config=cfg,
             signing_key="not-a-key"),
         allowed=(OIDCError,))
