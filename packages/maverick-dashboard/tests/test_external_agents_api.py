"""Bring-your-own-agent gateway over HTTP: admin enrollment + credential
minting on the dashboard surface, and the self-authenticated agent-facing
routes (minted bearer or platform-native credential — no session, no CSRF
surface, 404 while disabled)."""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import time

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

# Crypto availability gate for the signed-identity tests (same probe as
# test_oidc / test_external_identity): if this sandbox's cryptography/PyJWT
# is broken they SKIP — CI has working crypto and is the real gate.
_CRYPTO_OK = True
_CRYPTO_SKIP_REASON = ""
try:  # noqa: SIM105
    import jwt  # noqa: F401
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    _probe = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _probe.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
except Exception as _e:  # pragma: no cover - env-dependent
    _CRYPTO_OK = False
    _CRYPTO_SKIP_REASON = (
        f"working cryptography/PyJWT unavailable: {type(_e).__name__}: {_e}")

requires_crypto = pytest.mark.skipif(not _CRYPTO_OK,
                                     reason=_CRYPTO_SKIP_REASON)

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from maverick import agent_trust, config, external_agents, world_model
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MAVERICK_EXTERNAL_AGENTS", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setattr(agent_trust, "managed_path",
                        lambda: tmp_path / "agent_trust.json")
    monkeypatch.setattr(external_agents, "registry_path",
                        lambda: tmp_path / "external_agents.json")
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _enroll(**overrides):
    body = {"id": "sf-quotebot", "platform": "agentforce",
            "description": "quoting agent", "owner": "jordan@corp.test",
            "department": "sales", "max_risk": "medium", "max_dollars": 5.0}
    body.update(overrides)
    return client.post("/api/v1/external-agents", json=body)


def _mint() -> str:
    r = client.post("/api/v1/external-agents/sf-quotebot/credentials",
                    json={"surface": "rest"})
    assert r.status_code == 200
    return r.json()["token"]


def test_enroll_mint_and_roster_roundtrip():
    r = _enroll()
    assert r.status_code == 201
    assert r.json()["trust"] == "registered"
    token = _mint()
    assert token.startswith("lw-rest-")
    listing = client.get("/api/v1/external-agents").json()
    (row,) = listing["agents"]
    assert row["platform_label"] == "Salesforce Agentforce"
    assert row["credentials"] == ["rest"]
    # The token value never reappears on any read surface.
    assert token not in str(listing)
    detail = client.get("/api/v1/external-agents/sf-quotebot").json()
    assert detail["department"] == "sales"
    assert client.get("/api/v1/external-agents/ghost").status_code == 404


def test_enroll_validates_platform_and_surface():
    assert _enroll(platform="skynet").status_code == 400
    _enroll()
    r = client.post("/api/v1/external-agents/sf-quotebot/credentials",
                    json={"surface": "carrier-pigeon"})
    assert r.status_code == 422


def test_mint_step_up_approval_gate_over_http(tmp_path):
    _enroll()
    (tmp_path / "config.toml").write_text(
        "[external_agents]\nmint_approval = true\n", encoding="utf-8")
    from maverick import config
    config.reset_config_cache()
    # First call parks the approval and answers 409 — no token minted.
    r = client.post("/api/v1/external-agents/sf-quotebot/credentials",
                    json={"surface": "rest"})
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "approval_required"
    assert "approval" in body["detail"]
    approval_id = body["approval_id"]
    row = client.get("/api/v1/external-agents").json()["agents"][0]
    assert row["credentials"] == []
    from maverick.world_model import WorldModel
    assert WorldModel().decide_approval(approval_id, "approved",
                                        decided_by="admin@corp.test") is True
    # Replaying the approved id mints; the approval is then spent (one-shot).
    r = client.post("/api/v1/external-agents/sf-quotebot/credentials",
                    json={"surface": "rest", "approval_id": approval_id})
    assert r.status_code == 200
    assert r.json()["token"].startswith("lw-rest-")
    r = client.post("/api/v1/external-agents/sf-quotebot/credentials",
                    json={"surface": "rest", "approval_id": approval_id})
    assert r.status_code == 400
    assert "one-shot" in r.json()["detail"]


def test_agent_facing_routes_require_the_rest_bearer():
    _enroll()
    token = _mint()
    # No session, no Origin — the bearer alone authenticates (self-auth path).
    bare = TestClient(app)
    r = bare.post("/api/v1/external/runs",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"title": "Quote for Northwind", "outcome": "success",
                        "summary": "sent", "cost_dollars": 1.25})
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True and body["over_budget"] is False
    # The run is on the Operating Record, visible from the admin detail.
    detail = client.get("/api/v1/external-agents/sf-quotebot").json()
    assert detail["runs"] == 1
    assert detail["record"]["dollars"] == pytest.approx(1.25)
    # Missing/garbage bearer -> 401, never a fallback to ambient auth.
    assert bare.post("/api/v1/external/runs", json={}).status_code == 401
    r = bare.post("/api/v1/external/screen",
                  headers={"Authorization": "Bearer lw-rest-wrong"},
                  json={"tool": "crm_update"})
    assert r.status_code == 401


def test_screen_flow_and_approval_poll_over_http():
    _enroll(max_risk="high")
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    ok = bare.post("/api/v1/external/screen", headers=hdr,
                   json={"tool": "crm_update", "detail": "update stage",
                         "risk": "low"}).json()
    assert ok["allowed"] is True
    parked = bare.post("/api/v1/external/screen", headers=hdr,
                       json={"tool": "send_contract", "risk": "high"}).json()
    assert parked["requires_approval"] is True
    poll = bare.get(f"/api/v1/external/approvals/{parked['approval_id']}",
                    headers=hdr)
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"
    assert bare.get("/api/v1/external/approvals/999999",
                    headers=hdr).status_code == 404


def test_gateway_404s_while_disabled(monkeypatch):
    _enroll()
    token = _mint()
    monkeypatch.delenv("MAVERICK_EXTERNAL_AGENTS", raising=False)
    bare = TestClient(app)
    r = bare.post("/api/v1/external/screen",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"tool": "crm_update"})
    assert r.status_code == 404
    assert bare.get("/api/v1/external/openapi.json").status_code == 404


def test_openapi_artifact_describes_every_gateway_operation():
    spec = TestClient(app).get("/api/v1/external/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    assert set(spec["paths"]) == {
        "/api/v1/external/screen", "/api/v1/external/runs",
        "/api/v1/external/runs/start",
        "/api/v1/external/runs/{goal_id}/heartbeat",
        "/api/v1/external/runs/{goal_id}/finish",
        "/api/v1/external/memory/ingest",
        "/api/v1/external/memory/recall",
        "/api/v1/external/approvals/{approval_id}",
        "/api/v1/external/execute",
        "/api/v1/external/executions/{execution_id}",
        "/api/v1/external/executions/{execution_id}/commit"}
    assert spec["security"] == [{"bearer": []}]


def test_memory_routes_over_http(monkeypatch):
    monkeypatch.setenv("MAVERICK_FLEET_MEMORY", "1")
    from maverick import fleet_memory
    monkeypatch.setattr(fleet_memory, "registry_path",
                        lambda: fleet_memory.inbox_dir().parent
                        / "agents.ndjson")
    _enroll()
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    r = bare.post("/api/v1/external/memory/ingest", headers=hdr,
                  json={"kind": "success",
                        "goal_text": "Renewal quote accepted",
                        "reflection": "Bundling closed it"})
    assert r.status_code == 201, r.text
    r = bare.post("/api/v1/external/memory/recall", headers=hdr,
                  json={"query": "renewal quote"})
    assert r.status_code == 200
    assert "context" in r.json() and "reason" in r.json()
    # The scorecard surfaces the contribution.
    page = client.get("/external-agents/sf-quotebot").text
    assert "Learning contributions" in page
    # Bad kind is schema-gated; no bearer is 401.
    assert bare.post("/api/v1/external/memory/ingest", headers=hdr,
                     json={"kind": "gossip", "goal_text": "x"}
                     ).status_code == 422
    assert bare.post("/api/v1/external/memory/recall",
                     json={"query": "x"}).status_code == 401


def test_console_page_renders_roster_and_forms():
    _enroll()
    page = client.get("/external-agents").text
    assert "Enroll an external agent" in page
    assert 'id="xa-add"' in page
    assert "data-xa-mint" in page
    assert "Salesforce Agentforce" in page
    assert "Connect your platform" in page


def test_admin_mutations_are_csrf_gated_but_gateway_is_not():
    # The ADMIN surface keeps the same-origin gate in no-token mode…
    bare = TestClient(app)
    assert bare.post("/api/v1/external-agents",
                     json={"id": "x", "platform": "custom"}).status_code == 403
    # …while the agent-facing prefix authenticates by bearer alone (asserted
    # in test_agent_facing_routes_require_the_rest_bearer).


def test_unenroll_over_http():
    _enroll()
    assert client.delete("/api/v1/external-agents/sf-quotebot").status_code == 204
    assert client.delete("/api/v1/external-agents/sf-quotebot").status_code == 404
    assert client.get("/api/v1/external-agents").json()["agents"] == []


def test_live_run_flow_over_http():
    _enroll()
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    start = bare.post("/api/v1/external/runs/start", headers=hdr,
                      json={"title": "Live quote", "summary": "working"})
    assert start.status_code == 201
    gid = start.json()["goal_id"]
    assert start.json()["heartbeat_seconds"] > 0
    hb = bare.post(f"/api/v1/external/runs/{gid}/heartbeat", headers=hdr)
    assert hb.status_code == 200 and hb.json()["continue"] is True
    fin = bare.post(f"/api/v1/external/runs/{gid}/finish", headers=hdr,
                    json={"outcome": "success", "summary": "sent",
                          "cost_dollars": 1.5, "steps": ["a"]})
    assert fin.status_code == 200
    assert fin.json()["over_budget"] is False
    # Closed run: heartbeat says stop, finish refuses.
    hb = bare.post(f"/api/v1/external/runs/{gid}/heartbeat", headers=hdr)
    assert hb.json()["continue"] is False
    fin = bare.post(f"/api/v1/external/runs/{gid}/finish", headers=hdr,
                    json={"outcome": "success", "summary": "again"})
    assert fin.status_code == 403
    detail = client.get("/api/v1/external-agents/sf-quotebot").json()
    assert detail["record"]["dollars"] == pytest.approx(1.5)


def test_scorecard_page_renders_and_404s_unknown():
    _enroll()
    page = client.get("/external-agents/sf-quotebot")
    assert page.status_code == 200
    assert "Spend and budget" in page.text
    assert "Salesforce Agentforce" in page.text
    assert client.get("/external-agents/ghost").status_code == 404


def test_boards_carry_platform_segments():
    _enroll()
    token = _mint()
    bare = TestClient(app)
    bare.post("/api/v1/external/runs",
              headers={"Authorization": f"Bearer {token}"},
              json={"title": "Quote", "outcome": "success", "summary": "s",
                    "cost_dollars": 2.0})
    spend = client.get("/api/v1/dashboards/spend").json()
    assert any(r["label"] == "Salesforce Agentforce"
               for r in spend["by_platform"])
    wf = client.get("/api/v1/dashboards/workforce").json()
    (row,) = [r for r in wf["platforms"]
              if r["label"] == "Salesforce Agentforce"]
    assert row["completed"] == 1 and row["spend"] == pytest.approx(2.0)


def test_idempotency_key_over_http():
    _enroll()
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"title": "Quote", "outcome": "success", "summary": "s",
            "cost_dollars": 1.0, "idempotency_key": "k-1"}
    first = bare.post("/api/v1/external/runs", headers=hdr, json=body).json()
    replay = bare.post("/api/v1/external/runs", headers=hdr, json=body).json()
    assert replay["goal_id"] == first["goal_id"]
    assert replay["duplicate"] is True
    detail = client.get("/api/v1/external-agents/sf-quotebot").json()
    assert detail["runs"] == 1


def test_gateway_rate_limit_trips_and_names_the_action(monkeypatch):
    from maverick_dashboard import external_gateway as gw
    _enroll()
    token = _mint()
    monkeypatch.setitem(gw._RATE_LIMITS, "screen", 3)
    monkeypatch.setattr(gw, "_rate_times", {})
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"tool": "research", "risk": "low"}
    for _ in range(3):
        assert bare.post("/api/v1/external/screen", headers=hdr,
                         json=body).status_code == 200
    r = bare.post("/api/v1/external/screen", headers=hdr, json=body)
    assert r.status_code == 429
    assert "screen" in r.json()["detail"]
    assert r.headers["Retry-After"] == "60"


def test_admin_release_and_reset_budget_routes():
    _enroll(max_risk="medium", max_dollars=1.0)
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    # Trip the containment via repeated ceiling breaches.
    for _ in range(5):
        bare.post("/api/v1/external/screen", headers=hdr,
                  json={"tool": "wire_transfer", "risk": "high"})
    verdict = bare.post("/api/v1/external/screen", headers=hdr,
                        json={"tool": "research", "risk": "low"}).json()
    assert verdict["rule"] == "contained"
    r = client.post("/api/v1/external-agents/sf-quotebot/release", json={})
    assert r.status_code == 200
    verdict = bare.post("/api/v1/external/screen", headers=hdr,
                        json={"tool": "research", "risk": "low"}).json()
    assert verdict["rule"] == "allow"
    # Over-budget -> reset restores service.
    bare.post("/api/v1/external/runs", headers=hdr,
              json={"title": "Big", "outcome": "success", "summary": "s",
                    "cost_dollars": 2.0})
    verdict = bare.post("/api/v1/external/screen", headers=hdr,
                        json={"tool": "research", "risk": "low"}).json()
    assert verdict["rule"] == "budget"
    r = client.post("/api/v1/external-agents/sf-quotebot/reset-budget",
                    json={})
    assert r.status_code == 200
    verdict = bare.post("/api/v1/external/screen", headers=hdr,
                        json={"tool": "research", "risk": "low"}).json()
    assert verdict["rule"] == "allow"
    assert client.post("/api/v1/external-agents/ghost/release",
                       json={}).status_code == 404


# -- governed execution over HTTP ---------------------------------------------

class _FakeConn:
    """Recording governed-REST connector double — no network, no creds."""

    def __init__(self, write_result: str = "updated 1 record"):
        self.reads: list[dict] = []
        self.writes: list[dict] = []
        self.write_result = write_result

    def read(self, params):
        self.reads.append(dict(params))
        return "42 open opportunities"

    def preview_write(self, params):
        return f"would {params['op'].upper()} fakecrm{params['path']}"

    def write(self, params):
        self.writes.append(dict(params))
        return self.write_result


def _wire_fakecrm(monkeypatch):
    """Enable one fake connector for the execute tier (the factory is looked
    up at call time, so a setitem patch is enough)."""
    from maverick import governed_rest
    conn = _FakeConn()
    monkeypatch.setitem(governed_rest.GOVERNED_REST_FACTORIES, "fakecrm",
                        lambda: conn)
    monkeypatch.setenv("MAVERICK_EXTERNAL_CONNECTORS", "fakecrm")
    return conn


def _write_body(**kw):
    body = {"connector": "fakecrm", "op": "post",
            "path": "/opportunities/42", "body": {"stage": "closed-won"}}
    body.update(kw)
    return body


def test_execute_requires_bearer_before_validation():
    _enroll()
    bare = TestClient(app)
    # Even a body that could never validate gets 401, not 422 — the bearer
    # dependency resolves before the schema, so nothing leaks to strangers.
    assert bare.post("/api/v1/external/execute",
                     json={"op": "teleport"}).status_code == 401
    r = bare.post("/api/v1/external/execute",
                  headers={"Authorization": "Bearer lw-rest-wrong"},
                  json={"op": "teleport"})
    assert r.status_code == 401


def test_execute_routes_404_while_disabled(monkeypatch):
    _enroll()
    token = _mint()
    monkeypatch.delenv("MAVERICK_EXTERNAL_AGENTS", raising=False)
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    assert bare.post("/api/v1/external/execute", headers=hdr,
                     json=_write_body()).status_code == 404
    assert bare.get("/api/v1/external/executions/x",
                    headers=hdr).status_code == 404
    assert bare.post("/api/v1/external/executions/x/commit", headers=hdr,
                     json=_write_body()).status_code == 404


def test_execute_verdict_when_connector_not_enabled(monkeypatch):
    _enroll()
    token = _mint()
    monkeypatch.delenv("MAVERICK_EXTERNAL_CONNECTORS", raising=False)
    bare = TestClient(app)
    r = bare.post("/api/v1/external/execute",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"connector": "fakecrm", "op": "get",
                        "path": "/accounts"})
    # Denials are 200-with-verdict, never HTTP errors — typed rules travel.
    assert r.status_code == 200
    assert r.json()["allowed"] is False
    assert r.json()["rule"] == "connector_not_enabled"


def test_execute_read_end_to_end_over_http(monkeypatch):
    conn = _wire_fakecrm(monkeypatch)
    _enroll()
    token = _mint()
    bare = TestClient(app)
    r = bare.post("/api/v1/external/execute",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"connector": "fakecrm", "op": "get",
                        "path": "/opportunities"})
    assert r.status_code == 200
    verdict = r.json()
    assert verdict["allowed"] is True and verdict["rule"] == "executed"
    assert verdict["status"] == "executed"
    assert "42 open opportunities" in verdict["result"]
    assert conn.reads == [{"path": "/opportunities"}]


def test_write_park_poll_approve_and_commit_over_http(monkeypatch):
    conn = _wire_fakecrm(monkeypatch)
    _enroll(max_risk="high")
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    parked = bare.post("/api/v1/external/execute", headers=hdr,
                       json=_write_body()).json()
    assert parked["allowed"] is False
    assert parked["rule"] == "approval_required"
    assert parked["requires_approval"] is True
    eid = parked["execution_id"]
    assert conn.writes == []
    poll = bare.get(f"/api/v1/external/executions/{eid}", headers=hdr)
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"
    assert poll.json()["approval_status"] == "pending"
    from maverick.world_model import WorldModel
    assert WorldModel().decide_approval(parked["approval_id"], "approved",
                                        decided_by="admin@corp.test") is True
    done = bare.post(f"/api/v1/external/executions/{eid}/commit",
                     headers=hdr, json=_write_body()).json()
    assert done["allowed"] is True and done["rule"] == "executed"
    assert done["status"] == "executed"
    assert done["execution_id"] == eid
    assert conn.writes == [{"op": "post", "path": "/opportunities/42",
                            "body": {"stage": "closed-won"}}]


def test_commit_digest_mismatch_over_http(monkeypatch):
    conn = _wire_fakecrm(monkeypatch)
    _enroll(max_risk="high")
    token = _mint()
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    parked = bare.post("/api/v1/external/execute", headers=hdr,
                       json=_write_body()).json()
    eid = parked["execution_id"]
    r = bare.post(f"/api/v1/external/executions/{eid}/commit", headers=hdr,
                  json=_write_body(body={"stage": "closed-lost"}))
    assert r.status_code == 200
    assert r.json()["allowed"] is False
    assert r.json()["rule"] == "digest_mismatch"
    assert conn.writes == []


def test_execution_status_unknown_id_404s():
    _enroll()
    token = _mint()
    bare = TestClient(app)
    r = bare.get("/api/v1/external/executions/nope",
                 headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_execute_rate_limit_trips_and_commit_shares_the_bucket(monkeypatch):
    from maverick_dashboard import external_gateway as gw
    _wire_fakecrm(monkeypatch)
    _enroll()
    token = _mint()
    monkeypatch.setitem(gw._RATE_LIMITS, "execute", 2)
    monkeypatch.setattr(gw, "_rate_times", {})
    bare = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"connector": "fakecrm", "op": "get", "path": "/opportunities"}
    for _ in range(2):
        assert bare.post("/api/v1/external/execute", headers=hdr,
                         json=body).status_code == 200
    r = bare.post("/api/v1/external/execute", headers=hdr, json=body)
    assert r.status_code == 429
    assert "execute" in r.json()["detail"]
    assert r.headers["Retry-After"] == "60"
    # A commit can fire a write, so it draws from the same bucket.
    r = bare.post("/api/v1/external/executions/x/commit", headers=hdr,
                  json=_write_body())
    assert r.status_code == 429


# -- platform-native identity over HTTP ---------------------------------------

_ISSUER = "https://partner-idp.example.com"
_AUDIENCE = "maverick-gateway"
_HMAC_REF = "XA_GATEWAY_TEST_SECRET"
_HMAC_SECRET = "gw-test-secret"  # pragma: allowlist secret
_SCREEN_BODY = b'{"tool": "crm_update", "risk": "low"}'


def _identity(**fields):
    """Attach platform-identity fields to the enrolled agent's trust entry
    (the same read-modify-write mint_token uses, so bearers survive)."""
    from maverick import external_agents as xa
    from maverick.agent_trust import lookup, put_agent
    entry = xa._entry_from_agent(lookup("sf-quotebot"))
    entry.update(fields)
    put_agent(entry)


def _ed25519_pair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub_hex


def _envelope_headers(priv, body: bytes, *, nonce: str) -> dict:
    from maverick.external_identity import envelope_message
    ts = str(int(time.time()))
    sig = priv.sign(envelope_message("sf-quotebot", ts, nonce, body)).hex()
    return {"X-Maverick-Agent-Id": "sf-quotebot",
            "X-Maverick-Timestamp": ts,
            "X-Maverick-Nonce": nonce,
            "X-Maverick-Request-Signature": sig,
            "Content-Type": "application/json"}


@requires_crypto
def test_envelope_auth_over_http():
    _enroll()
    priv, pub_hex = _ed25519_pair()
    _identity(pubkey=pub_hex)
    r = TestClient(app).post(
        "/api/v1/external/screen",
        headers=_envelope_headers(priv, _SCREEN_BODY, nonce="n-1"),
        content=_SCREEN_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


@requires_crypto
def test_envelope_replayed_nonce_is_401():
    _enroll()
    priv, pub_hex = _ed25519_pair()
    _identity(pubkey=pub_hex)
    hdr = _envelope_headers(priv, _SCREEN_BODY, nonce="n-replay")
    bare = TestClient(app)
    assert bare.post("/api/v1/external/screen", headers=hdr,
                     content=_SCREEN_BODY).status_code == 200
    # A byte-identical replay carries a valid signature; the single-use
    # nonce is what refuses it — with the same generic 401.
    r = bare.post("/api/v1/external/screen", headers=hdr,
                  content=_SCREEN_BODY)
    assert r.status_code == 401


def _hmac_headers(body: bytes, *, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    mac = hmac_mod.new(_HMAC_SECRET.encode(), f"{ts}.".encode() + body,
                       hashlib.sha256)
    return {"X-Maverick-Agent-Id": "sf-quotebot",
            "X-Maverick-Timestamp": ts,
            "X-Maverick-Signature": "sha256=" + mac.hexdigest(),
            "Content-Type": "application/json"}


def test_hmac_auth_over_http(monkeypatch):
    monkeypatch.setenv(_HMAC_REF, _HMAC_SECRET)
    _enroll()
    _identity(hmac_secret_ref=_HMAC_REF)
    r = TestClient(app).post("/api/v1/external/screen",
                             headers=_hmac_headers(_SCREEN_BODY),
                             content=_SCREEN_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_hmac_stale_timestamp_is_401(monkeypatch):
    monkeypatch.setenv(_HMAC_REF, _HMAC_SECRET)
    _enroll()
    _identity(hmac_secret_ref=_HMAC_REF)
    hdr = _hmac_headers(_SCREEN_BODY, ts=str(int(time.time()) - 4000))
    r = TestClient(app).post("/api/v1/external/screen", headers=hdr,
                             content=_SCREEN_BODY)
    assert r.status_code == 401


def _jwt_setup(tmp_path) -> bytes:
    """Pin issuer/audience + a PEM key file on the enrolled agent; return
    the matching private key for minting tokens."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    key_file = tmp_path / "partner.pem"
    key_file.write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    _identity(jwt_issuer=_ISSUER, jwt_audience=_AUDIENCE,
              jwks_file=str(key_file))
    return priv_pem


def _jwt_token(priv_pem: bytes, **overrides) -> str:
    now = int(time.time())
    claims = {"sub": "sf-quotebot", "iss": _ISSUER, "aud": _AUDIENCE,
              "iat": now, "exp": now + 600}
    claims.update(overrides)
    return jwt.encode(claims, priv_pem, algorithm="RS256")


@requires_crypto
def test_jwt_auth_over_http(tmp_path):
    _enroll()
    priv_pem = _jwt_setup(tmp_path)
    r = TestClient(app).post(
        "/api/v1/external/screen",
        headers={"Authorization": f"Bearer {_jwt_token(priv_pem)}"},
        json={"tool": "crm_update", "risk": "low"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


@requires_crypto
def test_jwt_for_another_subject_is_401(tmp_path):
    """A validly-signed token whose sub names another agent never
    authenticates this one."""
    _enroll()
    priv_pem = _jwt_setup(tmp_path)
    token = _jwt_token(priv_pem, sub="other-bot")
    r = TestClient(app).post(
        "/api/v1/external/screen",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "crm_update", "risk": "low"})
    assert r.status_code == 401


def test_bearer_still_works_alongside_strong_credentials():
    """require_signed defaults OFF: enrolling a pubkey does not strand an
    agent still calling with its minted bearer."""
    _enroll()
    token = _mint()
    _identity(pubkey="ab" * 32)
    r = TestClient(app).post("/api/v1/external/screen",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"tool": "crm_update", "risk": "low"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_require_signed_refuses_bearer_for_strong_credential_agents(tmp_path):
    from maverick import config
    _enroll()
    token = _mint()
    _identity(pubkey="ab" * 32)
    _enroll(id="langbot", platform="custom")
    r = client.post("/api/v1/external-agents/langbot/credentials",
                    json={"surface": "rest"})
    plain_token = r.json()["token"]
    (tmp_path / "config.toml").write_text(
        "[external_agents]\nrequire_signed = true\n", encoding="utf-8")
    config.reset_config_cache()
    bare = TestClient(app)
    body = {"tool": "crm_update", "risk": "low"}
    # The pubkey-bearing agent must present its strong credential…
    r = bare.post("/api/v1/external/screen",
                  headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 401
    # …while a token-only agent keeps working.
    r = bare.post("/api/v1/external/screen",
                  headers={"Authorization": f"Bearer {plain_token}"},
                  json=body)
    assert r.status_code == 200, r.text


def test_require_signed_covers_hmac_only_agents(tmp_path):
    """An agent whose only strong credential is HMAC must also lose its
    bearer under require_signed — otherwise a leaked bearer keeps working
    against the very switch that was meant to retire it."""
    from maverick import config
    _enroll()
    token = _mint()
    _identity(hmac_secret_ref="SF_QUOTEBOT_HMAC")  # pragma: allowlist secret
    (tmp_path / "config.toml").write_text(
        "[external_agents]\nrequire_signed = true\n", encoding="utf-8")
    config.reset_config_cache()
    bare = TestClient(app)
    r = bare.post("/api/v1/external/screen",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"tool": "crm_update", "risk": "low"})
    assert r.status_code == 401
