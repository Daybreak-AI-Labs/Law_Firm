"""The egress lock, enforced where requests actually leave.

``enterprise.py`` provides an application-layer egress control. An earlier
version documented the mechanism as ``enterprise_egress_denial`` applied *"at
each tool's request"*. That parenthetical was the load-bearing false clause.
The check existed at **11** call sites; a census of production code found
**194 direct outbound HTTP call sites across 96 modules**, of which **87 had no
egress gate of any kind** -- including ``gmail_tool``,
``salesforce_tool``, ``slack_bot``, ``replicate_tool`` (a second third-party
inference API), and ``maverick_knowledge/embed.py``, which ships document text
to an embedding vendor.

The per-tool design could not have worked. It required every author of every
connector, forever, to remember an unenforced convention, and nothing failed
when they did not -- the test suite validated the policy *function* and a
handful of hand-picked wrappers, so it was structurally incapable of noticing a
bypass. Sixty connectors under ``tools/`` drifted out of coverage exactly that
way.

So the check moves to where requests are actually issued. 86 of those 88
modules use ``httpx`` and one uses ``requests``; both funnel every call --
module-level ``httpx.get``, a hand-built ``Client``, a pooled ``Session`` --
through a one-request network seam. Wrapping those methods covers 87 of 88
modules without touching a single connector, covers connectors not yet
written, and re-checks every followed redirect hop.

**This does not weaken SSRF pinning.** ``tools/_ssrf`` installs a custom
*transport*, which sits below the one-hop client method; the guard runs first
and the pinned transport still runs after. They compose.

**Default posture is unchanged.** ``egress_permitted`` returns True whenever
enterprise mode is off, so on a default install this is one predicate per
request and nothing else. It is not a general firewall: it is defense in depth
for supported Python HTTP paths. A hard no-egress boundary additionally
requires sandbox network isolation and host/OS/VPC firewall policy.

**What it still does not cover**, stated because an overclaim here is the
original defect: a tool that shells out to ``curl``, opens a raw socket, or
uses an HTTP library the guard does not wrap. ``sandbox/network_policy`` says
the same about itself -- there is no packet-level backend. The
``egress_contract`` CI gate enumerates every module that makes direct outbound
HTTP and fails when one uses a library outside the wrapped set, so that gap is
a recorded number rather than a discovery.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

#: HTTP client libraries this guard wraps. The egress_contract gate refuses a
#: production module that reaches the network through anything else.
COVERED_LIBRARIES = ("httpx", "requests", "urllib")

_installed = False
_originals: dict[str, Any] = {}
_compatibility: dict[str, dict[str, Any]] = {}

#: Distinguishes "caller omitted timeout" from "caller passed None", which
#: urllib treats differently (None means no timeout; omitted means the global
#: default socket timeout).
_SENTINEL = object()


def _denied(url: str) -> str | None:
    """Denial reason for an outbound URL, or None to allow.

    Resolved at request time rather than at install time: enterprise mode, the
    compliance floor, and the allow-list are all live config, and a deployment
    that turns the boundary on must not have to restart to get it.
    """
    try:
        from .enterprise import egress_permitted
        if egress_permitted(url):
            return None
    # failure-policy: fail_closed
    except Exception:
        log.exception("egress guard: policy unavailable; refusing egress to %r", url)
        return ("egress policy could not be evaluated, so the boundary stays "
                "closed")
    host = ""
    try:
        from .enterprise import _host_of
        host = _host_of(url)
    except Exception:  # failure-policy: best_effort
        host = ""
    # Deliberate, narrow exception to the audit-refusal contract.
    #
    # That contract exists so "we could not record this action" never becomes
    # "we did it anyway". Here the action is being REFUSED either way -- the
    # caller gets EgressBlocked whether or not this write lands -- so the
    # concern the contract protects against cannot arise. Letting AuditRefused
    # propagate would only replace an accurate egress reason with a confusing
    # one: under enterprise mode with no off-host signing key, every blocked
    # request would surface as OffHostSigningRequiredError, and an operator
    # would go hunting for a signing problem instead of reading the allow-list.
    #
    # The refusal is logged at error level rather than dropped, because a
    # deployment that cannot record blocked exfiltration attempts has a real
    # problem -- just not one worth mistranslating this exception for.
    from .audit import EventKind
    from .audit.errors import AuditRefused
    from .audit.writer import audit_event
    try:
        audit_event(EventKind.EGRESS_BLOCKED, provider="http-client",
                    host=host or (url or "?"))
    except AuditRefused:
        log.error(
            "egress guard: blocked egress to %r but the audit subsystem "
            "refused to record it; the request is still refused",
            host or url)
    return (
        f"enterprise mode: refusing egress to {host or url!r} -- not a local "
        "endpoint and not in [enterprise] allowed_hosts. The application "
        "egress boundary blocks this request."
    )


def _check(url: str) -> None:
    reason = _denied(url)
    if reason is None:
        return
    from .enterprise import EgressBlocked
    raise EgressBlocked(reason)


def _is_network_transport(transport: Any) -> bool:
    """Whether a transport must pass the outbound policy check.

    An ASGI/WSGI/Mock transport hands the request to an in-process object: no
    socket opens, nothing crosses the host boundary, and there is nothing for
    an egress lock to prevent. Blocking those is a false positive with teeth --
    it refuses in-process ASGI calls under enterprise mode, which is how this
    was found (every TestClient request in the suite blocked on "testserver").

    The exemption is deliberately an exact-type allowlist. A custom
    ``BaseTransport`` can open a socket, shell out, or delegate elsewhere, so
    unknown transports -- including subclasses and wrappers around a mock --
    are treated as network-capable. Starlette's in-process TestClient transport
    is admitted only when its exact class object is available from the already
    imported module; a matching class name is not enough.

    A mock handler that itself makes a real request is checked when that request
    goes back through a guarded library.
    """
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx absent
        return True

    transport_type = type(transport)
    if any(
        transport_type is known_type
        for known_type in (
            getattr(httpx, "ASGITransport", None),
            getattr(httpx, "WSGITransport", None),
            getattr(httpx, "MockTransport", None),
        )
    ):
        return False

    starlette_testclient = sys.modules.get("starlette.testclient")
    if starlette_testclient is not None:
        test_transport = getattr(
            starlette_testclient,
            "_TestClientTransport",
            None,
        )
        if transport_type is test_transport:
            return False

    return True


def _reaches_the_network(client: Any, request: Any) -> bool:
    """Whether this request will actually leave the process."""
    try:
        transport = client._transport_for_url(request.url)
    except Exception:  # pragma: no cover - unknown client shape; fail closed
        return True
    return _is_network_transport(transport)


def _httpx_degraded_check(request: Any) -> None:
    """Deny every HTTPX send when the per-hop guard cannot be installed.

    A public ``send`` wrapper cannot observe a redirect chain's later hops.
    Therefore, when HTTPX no longer exposes the one-hop seam, enterprise and
    compliance deployments must refuse the whole request rather than apply an
    incomplete first-URL check. Default deployments retain their existing
    behavior so an unsupported optional client version does not become a
    platform-wide outage outside the asserted data boundary.
    """
    try:
        from .enterprise import enterprise_enabled

        boundary_required = enterprise_enabled()
    except Exception:  # pragma: no cover - enterprise_enabled is fail-closed
        boundary_required = True
    if not boundary_required:
        return

    url = str(getattr(request, "url", "") or "")
    host = url
    try:
        from .enterprise import _host_of

        host = _host_of(url) or url
    except Exception:  # pragma: no cover - diagnostic only
        pass

    # Record the compatibility refusal when possible. Failure to write this
    # event never opens the data boundary; the request is refused below.
    try:
        from .audit import EventKind
        from .audit.writer import audit_event

        audit_event(
            EventKind.EGRESS_BLOCKED,
            provider="httpx-compatibility-fallback",
            host=host or "?",
        )
    except Exception:  # failure-policy: best_effort
        log.exception(
            "egress guard: could not audit degraded HTTPX refusal for %r",
            host or url,
        )

    from .enterprise import EgressBlocked

    raise EgressBlocked(
        "enterprise/compliance mode: installed HTTPX cannot be guarded at "
        "every redirect hop, so all HTTPX sends are refused until a compatible "
        "version is installed."
    )


def _patch_httpx_fail_closed(mod: Any, reason: str) -> bool:
    """Install the degraded public-send boundary; return whether it succeeded."""
    sync_method = getattr(mod.Client, "send", None)
    async_method = getattr(mod.AsyncClient, "send", None)
    if not callable(sync_method) or not callable(async_method):
        _compatibility["httpx"] = {
            "compatible": False,
            "reason": f"{reason}; public send fallback is unavailable",
            "seam": "_send_single_request",
            "fail_closed_fallback": False,
        }
        return False

    sync_key = "httpx.Client.send"
    async_key = "httpx.AsyncClient.send"
    _originals[sync_key] = sync_method
    _originals[async_key] = async_method

    def _sync_send(self, request, *args, **kwargs):
        _httpx_degraded_check(request)
        return sync_method(self, request, *args, **kwargs)

    async def _async_send(self, request, *args, **kwargs):
        _httpx_degraded_check(request)
        return await async_method(self, request, *args, **kwargs)

    mod.Client.send = _sync_send
    mod.AsyncClient.send = _async_send
    _compatibility["httpx"] = {
        "compatible": False,
        "reason": reason,
        "seam": "_send_single_request",
        "fallback_seam": "send",
        "fail_closed_fallback": True,
    }
    return True


def _patch_httpx(mod: Any) -> None:
    """Wrap httpx at the one-network-request seam.

    ``Client.send`` owns a complete redirect chain, so wrapping it checks only
    the first URL. ``_send_single_request`` is entered once for every actual
    network hop, after HTTPX has built the next redirect request and selected
    its stream type. Wrapping the sync and async implementations independently
    preserves their respective byte-stream contracts while re-evaluating live
    policy (including the new URL's host) on every hop.

    In-process ASGI/WSGI/TestClient and mock transports are still exempt through
    :func:`_reaches_the_network`; a request that opens no socket is not egress.
    """
    sync_method = getattr(mod.Client, "_send_single_request", None)
    async_method = getattr(mod.AsyncClient, "_send_single_request", None)
    if not callable(sync_method) or not callable(async_method):
        reason = (
            "installed HTTPX does not expose compatible sync and async "
            "_send_single_request seams"
        )
        if _patch_httpx_fail_closed(mod, reason):
            return
        raise RuntimeError(_compatibility["httpx"]["reason"])

    async_key = "httpx.AsyncClient._send_single_request"
    sync_key = "httpx.Client._send_single_request"
    _originals[sync_key] = sync_method
    _originals[async_key] = async_method

    def _sync_send_single_request(self, request, *args, **kwargs):
        if _reaches_the_network(self, request):
            _check(str(request.url))
        return sync_method(self, request, *args, **kwargs)

    async def _async_send_single_request(self, request, *args, **kwargs):
        if _reaches_the_network(self, request):
            _check(str(request.url))
        return await async_method(self, request, *args, **kwargs)

    mod.Client._send_single_request = _sync_send_single_request
    mod.AsyncClient._send_single_request = _async_send_single_request
    _compatibility["httpx"] = {
        "compatible": True,
        "reason": "",
        "seam": "_send_single_request",
    }


def _patch_requests(mod: Any) -> None:
    original_send = mod.Session.send
    _originals["requests.Session.send"] = original_send

    def _send(self, request, **kwargs):
        _check(str(getattr(request, "url", "")))
        return original_send(self, request, **kwargs)

    mod.Session.send = _send


def _patch_urllib(mod: Any) -> None:
    """Nine production modules reach the network through urllib.

    ``urlopen`` and ``build_opener().open()`` both funnel through
    ``OpenerDirector.open``, so one wrapper covers every form.
    """
    original_open = mod.OpenerDirector.open
    _originals["urllib.OpenerDirector.open"] = original_open

    def _open(self, fullurl, data=None, timeout=_SENTINEL):
        # `fullurl` is a str or a Request; Request.full_url holds the target.
        url = getattr(fullurl, "full_url", None) or (
            fullurl if isinstance(fullurl, str) else "")
        _check(str(url))
        if timeout is _SENTINEL:
            return original_open(self, fullurl, data)
        return original_open(self, fullurl, data, timeout)

    mod.OpenerDirector.open = _open


def _unpatch(name: str, mod: Any) -> None:
    if name == "httpx":
        sync_key = "httpx.Client._send_single_request"
        async_key = "httpx.AsyncClient._send_single_request"
        if sync_key in _originals:
            mod.Client._send_single_request = _originals.pop(sync_key)
            mod.AsyncClient._send_single_request = _originals.pop(async_key)
        fallback_sync_key = "httpx.Client.send"
        fallback_async_key = "httpx.AsyncClient.send"
        if fallback_sync_key in _originals:
            mod.Client.send = _originals.pop(fallback_sync_key)
            mod.AsyncClient.send = _originals.pop(fallback_async_key)
    elif name == "requests":
        mod.Session.send = _originals.pop(
            "requests.Session.send", mod.Session.send)
    elif name == "urllib.request":
        mod.OpenerDirector.open = _originals.pop(
            "urllib.OpenerDirector.open", mod.OpenerDirector.open)


#: Module name -> patcher. Keyed on the importable name so the hook below can
#: match it exactly.
_TARGETS = {
    "httpx": _patch_httpx,
    "requests": _patch_requests,
    "urllib.request": _patch_urllib,
}

_patched: set[str] = set()


class _PatchOnImport:
    """Patch a client library the moment it is first imported.

    Importing httpx eagerly from ``import maverick`` costs real time and breaks
    the CLI cold-start guarantee -- ``maverick --help`` is pinned not to load
    httpx, the provider SDKs, numpy, or fastapi, and a stray module-level
    import taxes every invocation. So the guard arms itself instead: it patches
    what is already loaded, and registers this finder to catch the rest.

    ``find_spec`` delegates to the remaining finders for the real spec, then
    wraps that spec's ``exec_module`` so the patch lands immediately after the
    module body runs. The loader instance is created per-spec, so setting the
    attribute here does not leak into unrelated imports.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _TARGETS or fullname in _patched:
            return None
        spec = None
        for finder in sys.meta_path:
            if finder is self:
                continue
            found = getattr(finder, "find_spec", None)
            if found is None:
                continue
            try:
                spec = found(fullname, path, target)
            except Exception:  # pragma: no cover - a broken finder is not ours
                spec = None
            if spec is not None:
                break
        if spec is None or spec.loader is None:
            return None
        real_exec = spec.loader.exec_module

        def exec_module(module, _real=real_exec, _name=fullname):
            _real(module)
            try:
                _TARGETS[_name](module)
                _patched.add(_name)
            # failure-policy: fail_closed
            except Exception as exc:
                # An imported-but-unwrapped client would remain usable if
                # enterprise mode were enabled later at runtime. Readiness
                # metadata alone cannot close that socket path, so refuse the
                # import when no enforceable wrapper could be installed.
                _compatibility.setdefault(
                    _name,
                    {
                        "compatible": False,
                        "reason": "egress guard installation failed",
                        "fail_closed_fallback": False,
                    },
                )
                log.exception("egress guard: could not wrap %s", _name)
                raise RuntimeError(
                    f"refusing to import {_name}: the process-wide egress "
                    "boundary could not be installed"
                ) from exc

        try:
            spec.loader.exec_module = exec_module
        except (AttributeError, TypeError):  # pragma: no cover - exotic loader
            return None
        return spec


_hook: _PatchOnImport | None = None


def install() -> bool:
    """Arm the guard. Idempotent; returns whether it is active.

    Patches every target already imported, and hooks the rest so they are
    wrapped on first import. Deliberately does NOT import httpx: see
    :class:`_PatchOnImport`.
    """
    global _installed, _hook
    if _installed:
        return True
    for name, patcher in _TARGETS.items():
        mod = sys.modules.get(name)
        if mod is not None and name not in _patched:
            patcher(mod)
            _patched.add(name)
    if _hook is None:
        _hook = _PatchOnImport()
        sys.meta_path.insert(0, _hook)
    _installed = True
    return True


def uninstall() -> None:
    """Restore the unwrapped network paths. For tests and controlled teardown."""
    global _installed, _hook
    if not _installed:
        return
    for name in list(_patched):
        mod = sys.modules.get(name)
        if mod is not None:
            _unpatch(name, mod)
    _patched.clear()
    if _hook is not None:
        try:
            sys.meta_path.remove(_hook)
        except ValueError:  # pragma: no cover
            pass
        _hook = None
    _originals.clear()
    _compatibility.clear()
    _installed = False


def active() -> bool:
    return _installed and all(
        status.get("compatible") is True
        for status in _compatibility.values()
    )


def compatibility_status() -> dict[str, Any]:
    """Return an operator-readable snapshot of guarded-client compatibility.

    HTTPX's one-hop seam is intentionally checked at runtime rather than
    assumed from a package version. ``active`` becomes false when a loaded
    client cannot be wrapped, giving readiness/doctor integrations a
    fail-closed signal instead of silently claiming the boundary is installed.
    """
    return {
        "installed": _installed,
        "active": active(),
        "clients": {
            name: dict(status)
            for name, status in sorted(_compatibility.items())
        },
    }


__all__ = [
    "COVERED_LIBRARIES",
    "active",
    "compatibility_status",
    "install",
    "uninstall",
]
