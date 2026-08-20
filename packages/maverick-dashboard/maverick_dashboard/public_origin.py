"""Canonical firm URL and exact Host boundary for security-bearing links."""
from __future__ import annotations

from urllib.parse import urlsplit


def public_origin_policy() -> tuple[bool, str, frozenset[str]]:
    try:
        from maverick.config import config_source_errors, load_global_config

        if config_source_errors(include_tenant=False):
            return False, "", frozenset()
        config = load_global_config() or {}
    except Exception:
        return False, "", frozenset()
    if not isinstance(config, dict):
        return False, "", frozenset()
    dashboard = config.get("dashboard")
    if not isinstance(dashboard, dict):
        return False, "", frozenset()
    raw_url = dashboard.get("public_base_url")
    raw_hosts = dashboard.get("trusted_hosts")
    if not isinstance(raw_url, str) or not isinstance(raw_hosts, (list, tuple)):
        return False, "", frozenset()
    try:
        parsed = urlsplit(raw_url.strip())
    except ValueError:
        return False, "", frozenset()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return False, "", frozenset()
    hosts: set[str] = set()
    for value in raw_hosts:
        if not isinstance(value, str):
            return False, "", frozenset()
        host = value.strip().lower()
        if not host or host != value.strip() or any(c in host for c in "*/\\@,?#"):
            return False, "", frozenset()
        hosts.add(host)
    if not hosts or parsed.netloc.lower() not in hosts:
        return False, "", frozenset()
    return True, f"https://{parsed.netloc}", frozenset(hosts)


def canonical_url(path: str) -> str:
    valid, base, _hosts = public_origin_policy()
    clean = "/" + str(path or "").lstrip("/")
    if not valid or clean.startswith("//"):
        raise RuntimeError("firm public URL policy is unavailable")
    return base + clean


def request_host_allowed(request) -> bool:
    valid, _base, hosts = public_origin_policy()
    if not valid:
        return False
    host = str(request.headers.get("host") or "").strip().lower()
    peer = request.client.host if request.client else ""
    try:
        from maverick.proxy_auth import proxy_auth_enabled, proxy_trusts

        if proxy_auth_enabled() and proxy_trusts(peer):
            forwarded = str(request.headers.get("x-forwarded-host") or "").strip()
            if forwarded:
                # Multiple proxy hops are ambiguous and therefore refused.
                if "," in forwarded:
                    return False
                host = forwarded.lower()
    except Exception:
        return False
    return host in hosts


__all__ = ["canonical_url", "public_origin_policy", "request_host_allowed"]
