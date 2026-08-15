"""The enterprise boundary, tested at the layer where requests leave.

``enterprise.py`` sells one property -- *"Sensitive data physically cannot
reach a third-party API"* -- and documented the mechanism as
``enterprise_egress_denial`` applied *"at each tool's request"*. A census found
**94 production modules making direct outbound HTTP and 87 with no egress gate
at all**, including ``gmail_tool``, ``salesforce_tool``, ``slack_bot``,
``replicate_tool``, and ``maverick_knowledge/embed.py``.

The reason it drifted that far unnoticed is visible in the old tests, and it is
the thing this file is written against. Every existing egress test is one of
two shapes: a pure-policy test asserting ``egress_permitted(...)`` returns
False, or a per-tool test that calls one tool's ``_run`` and greps its returned
error string. Neither can detect a bypass, because neither ever issues a
request. Sixty connectors under ``tools/`` were added over time with raw
``httpx`` calls and every check stayed green.

So these tests make real calls through real client libraries and assert on what
the network layer does. The blocked cases never reach a socket -- the guard
raises above the transport -- and the permitted cases are asserted by their
*connection* failing, which proves the guard let them through.
"""

from __future__ import annotations

import urllib.request

import pytest
from maverick import egress_guard
from maverick.enterprise import EgressBlocked

httpx = pytest.importorskip("httpx")

#: Refused by DNS/connect rather than by policy, so a connection error proves
#: the guard permitted the request instead of blocking it.
UNROUTABLE = "http://127.0.0.1:9/probe"


@pytest.fixture(autouse=True)
def _leave_the_guard_installed():
    """Restore the process-wide guard after every test in this file.

    Several tests here uninstall it deliberately. Leaving it off would silently
    disarm the boundary for every later test in the session -- the same
    order-dependent pollution this suite already suffers from, introduced by
    the file that tests the fix.
    """
    yield
    egress_guard.uninstall()
    # Direct compatibility probes patch synthetic client classes while the
    # process guard is intentionally uninstalled.  They do not participate in
    # the runtime ``_patched`` registry, so discard their synthetic restore
    # entries before reinstalling the real libraries.
    egress_guard._originals.clear()
    egress_guard._compatibility.clear()
    egress_guard.install()


@pytest.fixture
def guarded(monkeypatch):
    """Enterprise mode on, guard installed, torn down cleanly."""
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    egress_guard.uninstall()
    egress_guard.install()
    yield
    egress_guard.uninstall()


@pytest.fixture
def unguarded_mode(monkeypatch):
    """Guard installed but enterprise mode OFF -- the default posture."""
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "0")
    egress_guard.uninstall()
    egress_guard.install()
    yield
    egress_guard.uninstall()


def _allowed(monkeypatch, *hosts):
    monkeypatch.setattr(
        "maverick.enterprise._allowed_egress_hosts", lambda: frozenset(hosts))


# -- the test that did not exist -------------------------------------------

def test_a_raw_httpx_call_is_blocked(guarded) -> None:
    """No tool, no wrapper, no gate call. Just httpx, as 60 connectors use it.

    This is the assertion the suite never had. Before the guard it fails: the
    request goes out.
    """
    with pytest.raises(EgressBlocked):
        httpx.get("https://api.example.com/v1/customers", timeout=1.0)


def test_a_raw_httpx_client_is_blocked(guarded) -> None:
    with pytest.raises(EgressBlocked), httpx.Client(timeout=1.0) as c:
        c.post("https://api.example.com/upload", json={"pii": "x"})


@pytest.mark.asyncio
async def test_a_raw_async_httpx_client_is_blocked(guarded) -> None:
    async with httpx.AsyncClient(timeout=1.0) as c:
        with pytest.raises(EgressBlocked):
            await c.get("https://api.example.com/v1/me")


def test_a_raw_urllib_call_is_blocked(guarded) -> None:
    """Nine production modules use urllib, including http_fetch itself."""
    with pytest.raises(EgressBlocked):
        urllib.request.urlopen("https://api.example.com/data", timeout=1.0)


def test_a_urllib_opener_is_blocked(guarded) -> None:
    """build_opener bypasses urlopen, so the wrapper sits on OpenerDirector."""
    opener = urllib.request.build_opener()
    with pytest.raises(EgressBlocked):
        opener.open("https://api.example.com/data", timeout=1.0)


def test_a_urllib_request_object_is_blocked(guarded) -> None:
    """urlopen also accepts a Request, whose URL lives on .full_url."""
    req = urllib.request.Request(
        "https://api.example.com/data", headers={"X": "1"})
    with pytest.raises(EgressBlocked):
        urllib.request.urlopen(req, timeout=1.0)


def test_a_raw_requests_call_is_blocked(guarded) -> None:
    requests = pytest.importorskip("requests")
    with pytest.raises(EgressBlocked):
        requests.get("https://api.example.com/data", timeout=1.0)


def test_without_the_guard_the_same_call_escapes(monkeypatch) -> None:
    """Proof the guard is load-bearing, not incidental.

    Enterprise mode ON, guard NOT installed -- exactly the state the platform
    shipped in. The request is attempted and fails on the network rather than
    on policy, which is the 87-module hole reproduced in one assertion. If this
    ever starts raising EgressBlocked, something else is enforcing the boundary
    and the tests above no longer prove what they claim.
    """
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    egress_guard.uninstall()
    try:
        with pytest.raises(httpx.HTTPError):
            httpx.get(UNROUTABLE, timeout=0.5)
    finally:
        egress_guard.install()


def test_a_real_previously_ungated_connector_is_blocked(guarded, monkeypatch) -> None:
    """End-to-end on shipped code, not a synthetic httpx call.

    ``asana_tool`` is one of the 60 connectors under ``tools/`` that reached
    the network with a bare ``httpx.get`` and no gate. Nothing about the
    connector changed; it is covered because the guard sits under it. This is
    the assertion that generalises to the other 86.
    """
    monkeypatch.setenv("ASANA_TOKEN", "t-fake")
    from maverick.tools import asana_tool

    with pytest.raises(EgressBlocked):
        asana_tool._get("/workspaces")


# -- what must still be allowed --------------------------------------------

def test_the_guard_is_a_noop_when_enterprise_mode_is_off(unguarded_mode) -> None:
    """The load-bearing negative control.

    A guard that blocked by default would "pass" every test above while
    breaking every default install -- the zero-config path calls a cloud LLM.
    A connection error here proves the request was permitted and simply had
    nowhere to go.
    """
    with pytest.raises(httpx.HTTPError):
        httpx.get(UNROUTABLE, timeout=0.5)


def test_a_local_endpoint_is_permitted_under_enterprise_mode(guarded) -> None:
    """Local LLMs and sidecars must keep working inside the boundary."""
    with pytest.raises(httpx.HTTPError):
        httpx.get(UNROUTABLE, timeout=0.5)


def test_an_allowlisted_host_is_permitted(guarded, monkeypatch) -> None:
    _allowed(monkeypatch, "allowed.internal")
    try:
        httpx.get("https://allowed.internal/x", timeout=0.5)
    except EgressBlocked:  # pragma: no cover - the failure this pins
        pytest.fail("an allow-listed host must not be blocked")
    except Exception:
        pass  # DNS/connect failure is the expected outcome


def test_a_non_allowlisted_host_is_still_blocked(guarded, monkeypatch) -> None:
    """Negative control for the allow-list: it must not become allow-all."""
    _allowed(monkeypatch, "allowed.internal")
    with pytest.raises(EgressBlocked):
        httpx.get("https://other.example.com/x", timeout=0.5)


# -- redirects: every network hop gets a fresh policy decision --------------

def test_a_redirect_to_a_denied_host_is_blocked(guarded, monkeypatch) -> None:
    """An allow-listed host must not be able to hand off to a denied one.

    The first endpoint is genuinely reached and returns a 302. The second URL
    is then checked as a new request and refused before DNS/connect.
    """
    import http.server
    import socketserver
    import threading

    ports: dict[str, int] = {}

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://denied.test:{ports['b']}/x")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _Redirector)
    ports["a"] = srv.server_address[1]
    ports["b"] = 9
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # Permit the first hop only, and prove policy is invoked again with the
        # rebuilt redirect URL rather than reusing the first-hop decision.
        checked_urls = []

        def local_only_for_first_hop(url):
            checked_urls.append(url)
            return f":{ports['a']}" in url

        monkeypatch.setattr(
            "maverick.enterprise._is_local_endpoint",
            local_only_for_first_hop,
        )
        _allowed(monkeypatch)
        with pytest.raises(EgressBlocked):
            httpx.get(f"http://127.0.0.1:{ports['a']}/start",
                      follow_redirects=True, timeout=3.0)
        assert any(f":{ports['a']}/start" in url for url in checked_urls)
        assert any("denied.test" in url for url in checked_urls)
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_first_hop_is_still_permitted(guarded, monkeypatch) -> None:
    """Positive control for the test above.

    If the guard simply blocked everything once redirects were involved, the
    redirect test would pass while the feature was broken.
    """
    monkeypatch.setattr("maverick.enterprise._is_local_endpoint",
                        lambda url: True)
    with pytest.raises(httpx.HTTPError):
        httpx.get(UNROUTABLE, timeout=0.5)


@pytest.mark.asyncio
async def test_async_permitted_hop_preserves_async_stream_contract(
    guarded,
) -> None:
    """The async wrapper must delegate to AsyncClient's own one-hop method."""
    async with httpx.AsyncClient(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError) as caught:
            await client.get(UNROUTABLE)
    assert "sync Client instance" not in str(caught.value)


# -- in-process transports are not egress ----------------------------------

def test_an_in_process_asgi_request_is_not_blocked(guarded) -> None:
    """A request that never opens a socket cannot leak, so it must not be
    refused.

    Found the hard way: the guard blocked every TestClient request in the suite
    (host "testserver") under enterprise mode, taking down five unrelated
    security tests. That is not a test artifact -- it would refuse any
    in-process ASGI call a deployment makes.
    """
    starlette = pytest.importorskip("starlette.testclient")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    # Starlette's TestClient, which is the exact shape that broke: it ships its
    # own `_TestClientTransport`, not httpx's ASGITransport.
    # No `with`: entering the client runs the ASGI lifespan, which this bare
    # app does not implement. The request path is what is under test.
    assert starlette.TestClient(app).get("/x").status_code == 200


def test_a_mock_transport_is_not_blocked(guarded) -> None:
    def handler(request):
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert c.get("https://denied.example.com/x").status_code == 200


def test_an_unknown_sync_transport_is_checked_before_it_can_run(guarded) -> None:
    """A custom BaseTransport is network-capable unless explicitly proven not."""

    class SocketLikeTransport(httpx.BaseTransport):
        reached = False

        def handle_request(self, request):
            self.reached = True
            return httpx.Response(200, text="escaped", request=request)

    transport = SocketLikeTransport()
    with httpx.Client(transport=transport) as client:
        with pytest.raises(EgressBlocked):
            client.get("https://denied.example.com/x")
    assert transport.reached is False


def test_an_unknown_transport_redirect_is_checked_before_second_hop(
    guarded,
    monkeypatch,
) -> None:
    """Custom transports cannot use an allowed first hop to bypass policy."""
    calls = []

    class RedirectingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://denied.example.com/steal"},
                request=request,
            )

    _allowed(monkeypatch, "allowed.example.com")
    with httpx.Client(transport=RedirectingTransport()) as client:
        with pytest.raises(EgressBlocked):
            client.get(
                "https://allowed.example.com/start",
                follow_redirects=True,
            )
    assert calls == ["https://allowed.example.com/start"]


@pytest.mark.asyncio
async def test_an_unknown_async_transport_is_checked_before_it_can_run(
    guarded,
) -> None:
    """The same fail-closed classification applies to AsyncBaseTransport."""

    class AsyncSocketLikeTransport(httpx.AsyncBaseTransport):
        reached = False

        async def handle_async_request(self, request):
            self.reached = True
            return httpx.Response(200, text="escaped", request=request)

    transport = AsyncSocketLikeTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressBlocked):
            await client.get("https://denied.example.com/x")
    assert transport.reached is False


def test_a_custom_mock_subclass_is_not_allowed_to_claim_in_process(
    guarded,
) -> None:
    """Only the exact known mock type is exempt; overrides can perform I/O."""

    class SocketLikeMock(httpx.MockTransport):
        reached = False

        def handle_request(self, request):
            self.reached = True
            return httpx.Response(200, text="escaped", request=request)

    transport = SocketLikeMock(lambda request: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(EgressBlocked):
            client.get("https://denied.example.com/x")
    assert transport.reached is False


def test_a_wrapped_network_transport_is_still_checked(guarded, monkeypatch) -> None:
    """The negative control that makes the rule above safe.

    `tools/_ssrf._PinnedTransport` WRAPS an httpx network transport rather than
    being one, so a rule of "only HTTPTransport counts as network" would skip
    the check for exactly the requests most worth checking. Unknown transports
    fail closed, and this pins that behavior.
    """
    from maverick.tools import _ssrf

    inner = httpx.HTTPTransport()
    pinned = _ssrf._PinnedTransport("denied.example.com",
                                    "denied.example.com", "203.0.113.1", inner)
    from maverick.egress_guard import _is_network_transport
    assert _is_network_transport(pinned) is True
    assert _is_network_transport(httpx.MockTransport(lambda r: None)) is False


def test_an_unresolvable_transport_fails_closed() -> None:
    """"We cannot tell what this is" must mean "check it"."""
    from maverick.egress_guard import _reaches_the_network

    class _Odd:
        def _transport_for_url(self, url):
            raise RuntimeError("no idea")

    assert _reaches_the_network(_Odd(), type("R", (), {"url": "x"})()) is True


# -- composition with SSRF pinning -----------------------------------------

def test_the_guard_does_not_disable_ssrf_pinning(guarded, monkeypatch) -> None:
    """The pinned transport sits below the one-hop seam; both must still apply.

    If the guard had replaced the transport rather than wrapping the client
    method, this would silently drop SSRF protection.
    """
    from maverick.tools import _ssrf

    _allowed(monkeypatch, "localhost")
    with pytest.raises(_ssrf.BlockedHost):
        _ssrf.safe_client("http://127.0.0.1/x")


# -- fail closed -----------------------------------------------------------

def test_an_unevaluable_policy_blocks_rather_than_allows(guarded, monkeypatch) -> None:
    """"We cannot tell" must not resolve to "let it out"."""
    monkeypatch.setattr(
        "maverick.enterprise.egress_permitted",
        lambda url: (_ for _ in ()).throw(RuntimeError("config on fire")))
    with pytest.raises(EgressBlocked):
        httpx.get("https://api.example.com/x", timeout=0.5)


def test_a_block_is_audited(guarded, monkeypatch) -> None:
    """A blocked exfiltration attempt is exactly what an auditor needs to see."""
    seen = []
    monkeypatch.setattr("maverick.audit.record",
                        lambda kind, **kw: seen.append((kind, kw)) or True)
    with pytest.raises(EgressBlocked):
        httpx.get("https://api.example.com/x", timeout=0.5)
    assert seen, "a blocked egress attempt must be recorded"
    assert any("api.example.com" in str(kw) for _, kw in seen), seen


# -- install/uninstall mechanics -------------------------------------------

#: The attribute the httpx wrapper actually replaces. Named here so these
#: mechanics tests fail loudly if the seam moves again, rather than quietly
#: asserting nothing about the method that carries the check.
_SEAM = "_send_single_request"


def test_the_seam_is_recorded() -> None:
    """Pins WHICH method carries the check, not merely that one does.

    ``_send_single_request`` is entered once per actual redirect hop; wrapping
    ``send`` would inspect only the first URL in the chain.
    """
    assert hasattr(httpx.Client, _SEAM)
    status = egress_guard.compatibility_status()
    assert status["active"] is True
    assert status["clients"]["httpx"] == {
        "compatible": True,
        "reason": "",
        "seam": _SEAM,
    }


@pytest.mark.asyncio
async def test_one_hop_wrappers_forward_signature_extensions(
    monkeypatch,
) -> None:
    """Future optional seam parameters must reach the matching client method."""
    egress_guard.uninstall()
    calls = []
    checked = []

    class SyncClient:
        def _send_single_request(self, request, *args, **kwargs):
            calls.append(("sync", request, args, kwargs))
            return "sync-result"

    class AsyncClient:
        async def _send_single_request(self, request, *args, **kwargs):
            calls.append(("async", request, args, kwargs))
            return "async-result"

    FakeHttpx = type(
        "FakeHttpx",
        (),
        {"Client": SyncClient, "AsyncClient": AsyncClient},
    )

    request = type("Request", (), {"url": "https://allowed.example/x"})()
    monkeypatch.setattr(egress_guard, "_reaches_the_network", lambda *a: True)
    monkeypatch.setattr(egress_guard, "_check", lambda url: checked.append(url))

    egress_guard._patch_httpx(FakeHttpx)

    assert (
        SyncClient()._send_single_request(request, "future", option=1)
        == "sync-result"
    )
    assert (
        await AsyncClient()._send_single_request(request, "future", option=2)
        == "async-result"
    )
    assert calls == [
        ("sync", request, ("future",), {"option": 1}),
        ("async", request, ("future",), {"option": 2}),
    ]
    assert checked == [
        "https://allowed.example/x",
        "https://allowed.example/x",
    ]


def test_incompatible_httpx_seam_is_visible_in_readiness(monkeypatch) -> None:
    egress_guard.uninstall()

    class MissingSeam:
        pass

    class IncompatibleHttpx:
        Client = MissingSeam
        AsyncClient = MissingSeam

    with pytest.raises(RuntimeError, match="_send_single_request"):
        egress_guard._patch_httpx(IncompatibleHttpx)

    status = egress_guard.compatibility_status()
    assert status["active"] is False
    assert status["clients"]["httpx"]["compatible"] is False
    assert status["clients"]["httpx"]["fail_closed_fallback"] is False
    assert "_send_single_request" in status["clients"]["httpx"]["reason"]


@pytest.mark.asyncio
async def test_incompatible_httpx_fails_closed_without_delegating(
    monkeypatch,
) -> None:
    """A future HTTPX cannot silently open the enterprise data boundary."""
    egress_guard.uninstall()
    calls = []

    class SyncClient:
        def send(self, request, *args, **kwargs):
            calls.append(("sync", request, args, kwargs))
            return "network"

    class AsyncClient:
        async def send(self, request, *args, **kwargs):
            calls.append(("async", request, args, kwargs))
            return "network"

    FakeHttpx = type(
        "FakeHttpx",
        (),
        {"Client": SyncClient, "AsyncClient": AsyncClient},
    )
    request = type("Request", (), {"url": "https://api.example.com/x"})()
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    monkeypatch.setattr("maverick.audit.writer.audit_event", lambda *a, **k: True)

    egress_guard._patch_httpx(FakeHttpx)

    status = egress_guard.compatibility_status()
    assert status["active"] is False
    assert status["clients"]["httpx"]["compatible"] is False
    assert status["clients"]["httpx"]["fail_closed_fallback"] is True
    assert status["clients"]["httpx"]["fallback_seam"] == "send"
    with pytest.raises(EgressBlocked, match="every redirect hop"):
        SyncClient().send(request, "future", option=1)
    with pytest.raises(EgressBlocked, match="every redirect hop"):
        await AsyncClient().send(request, "future", option=2)
    assert calls == []


def test_incompatible_httpx_fallback_preserves_default_mode(
    monkeypatch,
) -> None:
    """The degraded guard is fail-closed only where the boundary is asserted."""
    egress_guard.uninstall()
    calls = []

    class SyncClient:
        def send(self, request, *args, **kwargs):
            calls.append((request, args, kwargs))
            return "delegated"

    class AsyncClient:
        async def send(self, request, *args, **kwargs):
            return "delegated"

    FakeHttpx = type(
        "FakeHttpx",
        (),
        {"Client": SyncClient, "AsyncClient": AsyncClient},
    )
    request = type("Request", (), {"url": "https://api.example.com/x"})()
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "0")
    egress_guard._patch_httpx(FakeHttpx)

    assert SyncClient().send(request, "future", option=1) == "delegated"
    assert calls == [(request, ("future",), {"option": 1})]


def test_install_is_idempotent() -> None:
    egress_guard.uninstall()
    assert egress_guard.install() is True
    first = getattr(httpx.Client, _SEAM)
    assert egress_guard.install() is True
    assert getattr(httpx.Client, _SEAM) is first, (
        "double-wrapping would stack checks")
    egress_guard.uninstall()


def test_uninstall_restores_the_original_method() -> None:
    egress_guard.uninstall()
    original = getattr(httpx.Client, _SEAM)
    egress_guard.install()
    assert getattr(httpx.Client, _SEAM) is not original
    egress_guard.uninstall()
    assert getattr(httpx.Client, _SEAM) is original
    assert egress_guard.active() is False


def test_importing_maverick_does_not_import_httpx() -> None:
    """Arming the guard must not cost cold-start time.

    The first version imported httpx eagerly and broke the CLI's cold-start
    guarantee -- `maverick --help` is pinned not to load httpx, the provider
    SDKs, numpy or fastapi, because a stray module-level import taxes every
    invocation. The guard now hooks the import instead of forcing it.
    """
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import maverick, sys; print('httpx' in sys.modules)"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "importing maverick pulled in httpx"


def test_a_library_imported_after_the_guard_is_still_wrapped() -> None:
    """The other half: lazy must not mean unguarded.

    Most connectors do ``import httpx`` inside the function body, so the module
    loads long after ``import maverick``. If the hook only covered
    already-loaded libraries, exactly those connectors would stay unprotected
    -- which is the population the whole change exists to cover.

    Exercise this in a subprocess. Evicting and re-importing HTTPX in the
    pytest process creates two incompatible ``Request``/``SyncByteStream``
    class families and can corrupt already-collected Starlette TestClient
    classes even when the guard restores every method correctly.
    """
    import subprocess
    import sys

    script = (
        "import os, sys; "
        "os.environ['MAVERICK_ENTERPRISE']='1'; "
        "import maverick; "
        "assert 'httpx' not in sys.modules; "
        "import httpx; "
        "from maverick.enterprise import EgressBlocked; "
        "\ntry:\n"
        " httpx.get('https://api.example.com/x', timeout=1.0)\n"
        "except EgressBlocked:\n"
        " print('blocked')\n"
        "else:\n"
        " raise SystemExit('late HTTPX import was not guarded')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "blocked"


def test_late_client_import_is_refused_when_no_guard_seam_exists() -> None:
    """A future client shape cannot remain imported and silently unguarded."""
    import subprocess
    import sys

    script = """
import pathlib
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp())
(root / "httpx.py").write_text(
    "class Client:\\n    pass\\nclass AsyncClient:\\n    pass\\n",
    encoding="utf-8",
)
sys.path.insert(0, str(root))
import maverick
try:
    import httpx
except RuntimeError as exc:
    assert "egress boundary could not be installed" in str(exc)
    print("refused")
else:
    raise SystemExit("incompatible HTTPX remained importable without a guard")
"""
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "refused"


def test_importing_maverick_installs_the_guard() -> None:
    """The wiring that cannot be forgotten.

    Explicit per-entry-point installation is the same shape of mistake as
    per-tool gating: it works until someone adds an entry point.
    """
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import maverick; from maverick import egress_guard;"
         " print(egress_guard.active())"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True", out.stdout
