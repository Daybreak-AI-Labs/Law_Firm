"""Air-gapped preflight verification (roadmap: 2028 H2 safety — "air-gapped mode").

A regulated / classified deployment needs to *prove* there is no outbound path
before it trusts the box. Full OS-level air-gapping (firewall, no NIC) is the
operator's job; this is the **application-layer audit** that catches the ways
Maverick's own config would still reach the network: a remote model provider, a
non-deny-all egress policy, or a sandbox allowed network access. ``maverick
airgap check`` runs it and exits non-zero on any finding, so it can gate a
deployment.

Pure config inspection — a config dict in, a list of violations out — so it's
deterministic and tested without touching the network. The runtime *enforcement*
(the egress chokepoint and explicit provider pin) ships separately; this verifies the
deployment is actually configured for them.
"""
from __future__ import annotations


def audit(*, config=None) -> dict:
    """Audit a config for outbound paths. Returns ``{clean, violations}``."""
    if config is None:
        try:
            from .config import load_config
            config = load_config() or {}
        except Exception:  # pragma: no cover -- never block the check
            config = {}
    violations: list[str] = []
    violations += _audit_providers(config)
    violations += _audit_egress(config)
    violations += _audit_sandbox(config)
    violations += _audit_telemetry(config)
    return {"clean": not violations, "violations": violations}


def _endpoint_is_local(provider: str, config: dict) -> bool:
    """Is ``provider``'s *resolved endpoint* local, for the audited ``config``?

    The name-only check trusts the provider prefix, so a built-in "local" provider
    (ollama / vllm / tgi / the ``local`` alias) pointed off-box via
    ``[providers.<name>] base_url`` was reported clean while every prompt left the
    box. This resolves the effective endpoint from the audited config and validates
    it with the enterprise egress-lock locality check (loopback/private only).

    Compliance posture: this is a proof-of-no-egress tool, so any failure to prove
    an endpoint local (a cloud provider, a non-local base_url, or an unevaluable
    strict check) returns ``False`` — fail toward FLAGGING, never toward clean.
    """
    try:
        from . import enterprise
        from .providers import _canonical
    except Exception:
        return False
    try:
        canon = _canonical(provider)
        # Known cloud providers are never local, whatever base_url is set.
        if canon in enterprise.CLOUD_PROVIDERS:
            return False
        # Resolve the endpoint the audited config pins for this provider; the dict
        # under audit is the source of truth (accept the canonical or raw key).
        providers = config.get("providers") or {}
        pcfg = providers.get(canon) or providers.get(provider) or {}
        base_url = pcfg.get("base_url") if isinstance(pcfg, dict) else None
        if isinstance(base_url, str) and base_url.strip():
            return enterprise._is_local_endpoint(base_url.strip())
        # No endpoint pinned in the audited config -> defer to the enterprise
        # endpoint-validating check (built-in localhost default + env-var URLs).
        return enterprise.is_local_provider(canon)
    except Exception:
        return False


def _audit_providers(config: dict) -> list[str]:
    def _provider_of(spec: str) -> str:
        return spec.split(":", 1)[0].strip().lower() if ":" in spec else "anthropic"

    spec = (config.get("models") or {}).get("default")
    if not isinstance(spec, str) or ":" not in spec or not all(
        part.strip() for part in spec.split(":", 1)
    ):
        return [
            "[models] default is not one exact provider:model — pin one local "
            "model for the whole run"
        ]
    if not _endpoint_is_local(_provider_of(spec), config):
        return [f"remote model in use: {spec} — pin a local provider "
                "(ollama / vllm / tgi) whose endpoint is loopback/private"]
    return []


def _audit_egress(config: dict) -> list[str]:
    egress = config.get("egress") or {}
    deny = egress.get("deny") or []
    if "*" not in deny:
        return ['egress is not deny-all — set [egress] deny = ["*"] to block '
                "all outbound hosts"]
    return []


def _truthy(val) -> bool:
    return val is True or (isinstance(val, str) and val.strip().lower() in
                           {"1", "true", "yes", "on"})


def _audit_sandbox(config: dict) -> list[str]:
    sb = config.get("sandbox") or {}
    backend = str(sb.get("backend") or "local").strip().lower()
    val = sb.get("allow_network")
    if _truthy(val):
        return ["[sandbox] allow_network is on — the sandbox can reach the network"]
    if backend == "docker":
        return []
    if backend == "local":
        return ["[sandbox] backend=local uses the host network — choose a sandbox "
                "backend with network disabled"]
    return [f"[sandbox] backend={backend!r} is not retained by the firm runtime; "
            "air-gap status cannot be proven"]


def _audit_telemetry(config: dict) -> list[str]:
    """Flag config-visible telemetry / forwarding sinks that open outbound sockets.

    These reach the network directly, not through the ``[egress]`` deny list the
    LLM/tool paths honor, so the other passes are blind to them. Sentry error
    reporting can ship data off-box. Prometheus is an inbound 127.0.0.1
    listener, not egress, and OTEL is env-var driven rather than config-visible.
    """
    violations: list[str] = []
    dsn = str((config.get("observability") or {}).get("sentry_dsn") or "").strip()
    if dsn:
        violations.append(
            "[observability] sentry_dsn is set — Sentry ships errors/traces off-box; "
            "clear it (and unset MAVERICK_SENTRY_DSN) for an air-gapped deployment")
    return violations


__all__ = ["audit"]
