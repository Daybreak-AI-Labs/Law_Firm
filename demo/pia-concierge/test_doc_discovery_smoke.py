"""Security smoke tests for the public PIA demo's mock-only discovery path."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from maverick import doc_discovery
from starlette.requests import Request

HERE = Path(__file__).resolve().parent


def _load_demo_app():
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location("pia_concierge_demo_app", HERE / "app.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HERE))


@pytest.fixture(scope="module")
def demo_app():
    return _load_demo_app()


def test_startup_replaces_real_discovery_environment_with_mock_only_values(
    demo_app,
    monkeypatch,
):
    async def _mailserver(*_args, **_kwargs):
        return object()

    async def _watcher():
        return None

    monkeypatch.setattr(demo_app, "start_mailsink", _mailserver)
    monkeypatch.setattr(demo_app, "_approval_watcher", _watcher)
    monkeypatch.setenv("WORLD_PORT", "18890")
    monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "real-graph-token")
    monkeypatch.setenv("MSGRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
    monkeypatch.setenv("MAVERICK_FETCH_ALLOW_PRIVATE", "0")
    monkeypatch.setenv("SLACK_SEARCH_TOKEN", "real-slack-search-token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "real-slack-token")
    monkeypatch.setenv("SLACK_BASE_URL", "https://slack.com/api")
    monkeypatch.setenv("GDRIVE_ACCESS_TOKEN", "real-drive-token")
    monkeypatch.setenv("GDRIVE_BASE_URL", "https://www.googleapis.com")

    async def _run_startup():
        await demo_app._startup()
        await demo_app.app.state.watcher

    asyncio.run(_run_startup())

    assert demo_app.os.environ["MSGRAPH_ACCESS_TOKEN"] == demo_app.GRAPH_SIM_TOKEN
    assert demo_app.os.environ["MSGRAPH_BASE_URL"] == (
        "http://127.0.0.1:18890/graph-sim"
    )
    assert "MAVERICK_FETCH_ALLOW_PRIVATE" not in demo_app.os.environ
    assert "SLACK_BOT_TOKEN" not in demo_app.os.environ
    assert "GDRIVE_ACCESS_TOKEN" not in demo_app.os.environ


def test_public_discovery_route_returns_mock_hits_from_msgraph_only(
    demo_app,
    monkeypatch,
):
    case_id = "smoke-case"
    demo_app.STORE.cases[case_id] = demo_app.Case(
        id=case_id,
        ticket_number="PRV-SMOKE",
        subject="Acme CRM",
        requester="Alice",
        requester_email="alice@example.test",
        data_types="email",
    )
    observed = {}

    def _discover(subject, **kwargs):
        observed.update({"subject": subject, **kwargs})
        return [
            doc_discovery.DocHit(
                source="msgraph",
                doc_id="mock-doc-1",
                name="Acme CRM DPA.pdf",
                mime="application/pdf",
            ),
        ]

    monkeypatch.setattr(doc_discovery, "discover", _discover)
    monkeypatch.setattr(demo_app, "_audit", lambda *_args, **_kwargs: None)

    response = asyncio.run(demo_app.intake_discover(case_id))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["hits"][0]["doc_id"] == "mock-doc-1"
    assert observed == {
        "subject": "Acme CRM",
        "sources": ["msgraph"],
        "allow_ambient_credentials": True,
    }


def test_real_discovery_path_reaches_only_scoped_loopback_mock(demo_app, monkeypatch):
    class _GraphHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract
            if (
                self.path != "/graph-sim/search/query"
                or self.headers.get("Authorization")
                != f"Bearer {demo_app.GRAPH_SIM_TOKEN}"
            ):
                self.send_error(401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            assert request["requests"][0]["query"]["queryString"]
            body = json.dumps({
                "value": [{
                    "hitsContainers": [{
                        "hits": [{
                            "summary": "mock DPA",
                            "resource": {
                                "id": "mock-network-doc",
                                "name": "Acme CRM DPA.pdf",
                                "webUrl": "http://127.0.0.1/mock",
                                "size": 123,
                                "file": {"mimeType": "application/pdf"},
                                "parentReference": {"driveId": "demo-drive"},
                            },
                        }],
                    }],
                }],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _GraphHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", demo_app.GRAPH_SIM_TOKEN)
    monkeypatch.setenv(
        "MSGRAPH_BASE_URL",
        f"http://127.0.0.1:{server.server_port}/graph-sim",
    )
    monkeypatch.delenv("MAVERICK_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(doc_discovery, "get_assessments_sources", lambda: ["msgraph"])
    monkeypatch.setattr(
        "maverick.enterprise.enterprise_egress_denial",
        lambda *_args, **_kwargs: None,
    )
    try:
        hits = demo_app._discover_mock_documents("Acme CRM")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

    assert [hit.doc_id for hit in hits] == ["mock-network-doc"]
    assert hits[0].source == "msgraph"


def test_public_attach_route_rejects_every_non_mock_source(demo_app, monkeypatch):
    case_id = "attach-smoke-case"
    demo_app.STORE.cases[case_id] = demo_app.Case(
        id=case_id,
        ticket_number="PRV-ATTACH-SMOKE",
        subject="Acme CRM",
        requester="Alice",
        requester_email="alice@example.test",
        data_types="email",
    )
    body = json.dumps({
        "source": "slack",
        "doc_id": "attacker-controlled",
        "ref": {"download_url": "http://127.0.0.1:9/secret"},
    }).encode()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/intake/{case_id}/attach-found",
            "headers": [(b"content-type", b"application/json")],
        },
        _receive,
    )
    monkeypatch.setattr(
        doc_discovery,
        "fetch",
        lambda *_args, **_kwargs: pytest.fail("non-msgraph fetch was attempted"),
    )

    response = asyncio.run(demo_app.intake_attach_found(case_id, request))

    assert response.status_code == 400
    assert json.loads(response.body)["ok"] is False
