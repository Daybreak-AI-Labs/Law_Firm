"""Enterprise mode — hardened defaults for working with private / sensitive data.

The kernel ships **fail-open and cloud-capable** by design (CLAUDE.md rule 1): the
shield fails open, consent auto-approves, capabilities are opt-in, and any configured
LLM provider — including a third-party cloud API — sees the prompt. That is the right
default for a personal agent. It is the *wrong* default the moment the agent is pointed
at PHI / PII / financial / regulated data, where the data leaving the boundary or an
unsupervised destructive action is exactly the exposure an enterprise cannot accept.

**Enterprise mode is one opt-in switch that makes those application defaults
fail-closed.** It blocks known LLM dispatch and supported Python HTTP client
paths; it is defense in depth, not a packet-level firewall. A deployment that
requires a hard no-egress boundary must also enforce sandbox network isolation
and host/OS/VPC egress policy.

Turn it on with ``MAVERICK_ENTERPRISE=1``, ``[enterprise] mode = true`` in
``~/.maverick/config.toml``, or the installer wizard. **Off by default** — behaviour is
exactly as before.

When on, it enforces:

- **Egress lock.** Every governed LLM call is pinned to a local / self-hosted provider
  (``ollama`` / ``vllm`` / ``tgi``, or an endpoint you allow-list under
  ``[enterprise] local_providers``). A call routed to a cloud provider
  (``anthropic`` / ``openai`` / ...) raises :class:`EgressBlocked` **before any prompt
  is sent**, and the denial is audited. This protects the governed LLM dispatch
  path. (Enforced in :func:`maverick.llm.LLM.complete`.)
- **Tool egress lock.** Outbound HTTP is held to local/private endpoints or hosts
  allow-listed under ``[enterprise] allowed_hosts`` (default-deny) on the
  supported HTTP paths. (Enforced in :mod:`maverick.egress_guard`, which wraps
  the request path of every HTTP client library the codebase uses -- ``httpx``,
  ``requests`` and ``urllib`` -- so a connector is covered whether or not its
  author called anything. Tools that surface a denial as an error string still
  call :func:`enterprise_egress_denial` directly for the better message.)

  This clause used to read "enforced at each tool's request", which was false: the
  check existed at 11 call sites while 87 of the 94 modules that make outbound HTTP
  had no gate at all, including ``gmail``, ``salesforce``, ``slack_bot`` and the
  knowledge-store embedder. It is stated here because the boundary is the property
  this mode is sold on, and the gap between the sentence and the code was three
  years wide. The remaining hole is recorded, not hidden: see
  :mod:`maverick.egress_contract`, whose CI gate fails when a module reaches the
  network through a library the guard does not wrap.

  What it still does not cover, stated so the sentence above stays honest:

  * A process that shells out to ``curl`` or opens a raw socket. Hard
    process-level egress is bounded by Docker and deployment network policy,
    not by this in-process lock.
  * An HTTP library outside the wrapped set. The static egress-contract CI gate
    catches known production imports, but it cannot substitute for an OS/VPC
    deny rule at runtime.

  HTTPX is checked at its one-request seam, including each followed redirect
  hop. Known in-process test/mock transports are exempted by exact type; every
  unknown or custom transport is treated as network-capable and checked.
- **Consent fail-closed.** Destructive-action consent defaults to ``ask`` (and therefore
  *deny* in non-interactive contexts) instead of ``auto-approve``.
  (Enforced in :mod:`maverick.safety.consent`.)
- **Capabilities enforced.** Per-agent capability scoping + attenuating propagation is
  turned on, so a sub-agent can never exceed its grant.
  (Enforced in :func:`maverick.capability.capability_enforced`.)

An explicit env/config setting still wins for each individual control (so an operator
can, e.g., allow-list one extra self-hosted endpoint), but the *defaults* are safe and
the egress lock can never be satisfied by a cloud provider.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Mapping
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Built-in self-hosted providers: data stays on infrastructure the operator runs.
# ``ollama`` (localhost:11434), ``vllm`` and ``tgi`` (self-hosted inference servers).
# An operator can declare additional local endpoints via ``[enterprise]
# local_providers`` (e.g. a custom in-process provider). Known cloud providers are
# never accepted as local, even if listed in config.
LOCAL_PROVIDERS = frozenset({"ollama", "vllm", "tgi"})

# Providers whose canonical implementations route to third-party/cloud APIs. They
# must not become enterprise-local through the operator-provided provider-name list.
CLOUD_PROVIDERS = frozenset({
    "anthropic",
    "azure",
    "bedrock",
    "deepseek",
    "gemini",
    "moonshot",
    "openai",
    "openrouter",
    "xai",
})

# The generic OpenAI-compatible provider can point at either a local/self-hosted
# endpoint or an arbitrary public gateway. In enterprise mode it is only treated as
# local when its configured endpoint is provably local/private.
_ENDPOINT_VALIDATED_PROVIDERS = frozenset({"openai_compatible"})

# Built-in local providers can be redirected off-box via [providers.<name>]
# base_url or these env vars. A configured non-local endpoint must NOT satisfy the
# egress lock just because the provider *name* is "local" (ollama has no env
# override -- it reads [providers.ollama] base_url).
_LOCAL_PROVIDER_ENDPOINT_ENV = {
    "vllm": "VLLM_BASE_URL",
    "tgi": "TGI_BASE_URL",
}


class EgressBlocked(RuntimeError):
    """Raised before governed data can cross a disallowed egress boundary."""

    def __init__(self, provider: str, *, message: str | None = None):
        super().__init__(message or (
            "enterprise mode: refusing to send data to non-local provider "
            f"{provider!r}. Sensitive data must stay in your boundary — route this "
            "role to a self-hosted model (ollama / vllm / tgi) or another "
            "validated local provider."
        ))
        self.provider = provider


_TRUE_WORDS = {"1", "true", "yes", "on", "enable", "enabled", "y", "t"}
_FALSE_WORDS = {"0", "false", "no", "off", "disable", "disabled", "n", "f", ""}
_UNSET = object()


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_tristate(value: str) -> bool | None:
    """Parse an env flag: True/False for a recognized affirmative/negative, None
    for an unrecognized value -- so an ambiguous *security* flag is never silently
    treated as 'off' (the ``MAVERICK_ENTERPRISE=enabled`` footgun)."""
    v = value.strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    return None


def deployment_enterprise_enabled(
    *,
    config: object = _UNSET,
    source_errors: object = _UNSET,
) -> bool:
    """Resolve the deployment-global enterprise floor without tenant overlays.

    Authority-bearing shared ingress and deployment admission must not change
    with the tenant active in the current request.  Callers that already loaded
    global policy pass that snapshot and its source errors here so compliance,
    the enterprise knob, and the deployment profile are evaluated atomically
    from the same policy view.  Omitted arguments are loaded deployment-globally.

    Any unreadable or malformed security policy fails closed to enterprise-on.
    """
    if config is _UNSET or source_errors is _UNSET:
        try:
            from .config import config_source_errors, load_global_config

            if config is _UNSET:
                config = load_global_config()
            if source_errors is _UNSET:
                source_errors = config_source_errors(include_tenant=False)
        except Exception:
            log.exception(
                "enterprise: deployment-global policy is unavailable; failing closed",
            )
            return True
    if source_errors:
        log.error(
            "enterprise: deployment-global config source is unreadable; failing closed",
        )
        return True
    if not isinstance(config, Mapping):
        log.error(
            "enterprise: deployment-global config root is malformed; failing closed",
        )
        return True

    # Compliance is evaluated directly from the supplied snapshot.  Calling
    # requires_floor() here would re-read load_config() and admit a tenant overlay.
    try:
        from .compliance_profiles import FLOOR_EGRESS_LOCK, required_floors

        compliance = config.get("compliance") or {}
        profiles = compliance.get("profiles") if isinstance(compliance, Mapping) else ()
        configured_profiles = profiles if isinstance(profiles, (list, tuple)) else ()
        if FLOOR_EGRESS_LOCK in required_floors(configured_profiles):
            return True
    except Exception:
        log.exception(
            "enterprise: deployment-global compliance floor is unavailable; "
            "failing closed",
        )
        return True

    env = os.environ.get("MAVERICK_ENTERPRISE")
    if env is not None and env.strip() != "":
        decided = _env_tristate(env)
        if decided is not None:
            return decided
        log.error(
            "MAVERICK_ENTERPRISE=%r is not a recognized boolean; failing "
            "closed to enterprise mode",
            env,
        )
        return True

    section = config.get("enterprise")
    if section is not None and not isinstance(section, Mapping):
        log.error("enterprise.mode policy is malformed; failing closed")
        return True
    value = (section or {}).get("mode")
    if value is not None:
        if isinstance(value, bool):
            return value
        log.error("enterprise.mode must be a boolean; failing closed")
        return True

    # Resolve the named deployment profile from the same snapshot instead of
    # is_enterprise_profile(), whose load_config() call includes a tenant overlay.
    try:
        from .profile import ENTERPRISE, _normalize

        profile_env = os.environ.get("MAVERICK_PROFILE")
        decided_profile = None
        if profile_env is not None and profile_env.strip() != "":
            decided_profile = _normalize(profile_env)
            if decided_profile is None:
                log.warning(
                    "MAVERICK_PROFILE=%r is not a recognized profile; reading "
                    "deployment-global config",
                    profile_env,
                )
        if decided_profile is None:
            profile_section = config.get("profile") or {}
            profile_name = (
                profile_section.get("name")
                if isinstance(profile_section, Mapping)
                else None
            )
            decided_profile = _normalize(profile_name)
        return decided_profile == ENTERPRISE
    except Exception:
        log.exception(
            "enterprise: deployment-global profile is unavailable; failing closed",
        )
        return True


def enterprise_enabled() -> bool:
    """True if enterprise mode is on or required by an active compliance profile.

    Compliance floors are mandatory and strictest-wins: a HIPAA profile that
    requires the egress lock keeps the enterprise boundary closed even if the
    standalone enterprise knob is absent or false. Otherwise, a recognized
    ``MAVERICK_ENTERPRISE`` env value wins over ``[enterprise] mode`` in config;
    an *unrecognized* security flag, unreadable source, or malformed present
    policy fails closed to enterprise-on. When neither is set, the named
    deployment profile decides (``profile = "enterprise"`` turns it on; see
    :mod:`maverick.profile`). Off by default only after the policy sources are
    read successfully (the default profile is ``standard``).
    """
    try:
        from .compliance_profiles import FLOOR_EGRESS_LOCK, requires_floor
        if requires_floor(FLOOR_EGRESS_LOCK):
            return True
    except Exception:
        log.exception(
            "enterprise: compliance floor is unavailable; failing closed",
        )
        return True
    env = os.environ.get("MAVERICK_ENTERPRISE")
    if env is not None and env.strip() != "":
        decided = _env_tristate(env)
        if decided is not None:
            return decided
        log.error(
            "MAVERICK_ENTERPRISE=%r is not a recognized boolean; failing "
            "closed to enterprise mode",
            env,
        )
        return True
    try:
        from .config import config_source_errors, load_config

        cfg = load_config() or {}
        if config_source_errors():
            log.error("enterprise: config source is unreadable; failing closed")
            return True
    except Exception:
        log.exception("enterprise: config policy is unavailable; failing closed")
        return True
    if not isinstance(cfg, dict):
        log.error("enterprise: config root is malformed; failing closed")
        return True
    section = cfg.get("enterprise")
    if section is not None and not isinstance(section, dict):
        log.error("enterprise: policy section is malformed; failing closed")
        return True
    val = (section or {}).get("mode")
    if val is not None:
        if isinstance(val, bool):
            return val
        log.error("enterprise.mode must be a boolean; failing closed")
        return True
    # No explicit [enterprise] mode and no env override: the named deployment
    # profile decides. profile="enterprise" turns the boundary on by default;
    # "standard" (the default) leaves it off. Any explicit knob above still wins.
    try:
        from .profile import is_enterprise_profile
        return is_enterprise_profile()
    except Exception:
        log.exception("enterprise: deployment profile is unavailable; failing closed")
        return True


def _configured_openai_compatible_base_url() -> str | None:
    """Return the configured OpenAI-compatible endpoint, matching provider setup."""
    try:
        from .config import get_provider_config

        cfg = get_provider_config("openai_compatible")
    except Exception:
        cfg = {}
    url = cfg.get("base_url") or os.environ.get("OPENAI_COMPATIBLE_BASE_URL")
    return str(url).strip() if url else None


# Cloud instance-metadata endpoints are link-local but are NOT operator-controlled
# local services -- they expose instance role credentials. In the enterprise egress
# boundary they must be treated as off-limits, not "local" (user-testing finding;
# defense-in-depth -- retained REST connectors also use the shared SSRF guard,
# but the egress boundary must enforce this independently). Compared as parsed IPs so IPv6
# normalization can't sneak an equivalent form past a string match.
_IMDS_IPS = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "fd00:ec2::254")
)


def _is_local_endpoint(url: str | None) -> bool:
    """True only for endpoints that are syntactically local/private.

    Enterprise egress is fail-closed: public hostnames such as Groq/Together and
    ambiguous DNS names are not considered local here because this check runs before
    prompt dispatch and must not rely on network lookups. Cloud IMDS addresses are
    explicitly NOT local (they leak instance credentials).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "ip6-localhost", "ip6-loopback"} or host.endswith(
        ".localhost"
    ):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip in _IMDS_IPS:
        return False  # IMDS is cloud infra, not a local service -> deny via egress
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _is_firm_loopback_endpoint(url: str | None) -> bool:
    """Firm-local means loopback/``.localhost`` only, never the whole LAN."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_imds_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in _IMDS_IPS
    except ValueError:
        return False


def _refresh_bound_matter_context():
    """Resolve live membership/policy; uncertainty is a dispatch denial."""
    from .matter_context import refresh_matter_context

    return refresh_matter_context()


def _endpoint_validated_provider_is_local(provider: str) -> bool:
    if provider != "openai_compatible":
        return False
    return _is_local_endpoint(_configured_openai_compatible_base_url())


def _provider_config_base_url(provider: str) -> str | None:
    from .config import get_provider_config

    try:
        cfg = get_provider_config(provider)
    except Exception:
        cfg = {}
    url = cfg.get("base_url")
    return str(url).strip() if url else None


def _configured_base_url(provider: str) -> str | None:
    """Return the endpoint that ``provider`` will use, if explicitly configured.

    This mirrors the dispatch precedence: an explicit ``[providers.<name>]
    base_url`` is passed into the provider client and wins over provider-specific
    endpoint environment variables such as ``VLLM_BASE_URL`` and ``TGI_BASE_URL``.
    """
    cfg_url = _provider_config_base_url(provider)
    if cfg_url:
        return cfg_url

    env_var = _LOCAL_PROVIDER_ENDPOINT_ENV.get(provider)
    env_url = os.environ.get(env_var) if env_var else None
    return str(env_url).strip() if env_url else None


def _explicit_local_provider_endpoints(provider: str) -> tuple[str, ...]:
    """Return every explicit endpoint source for a built-in local provider.

    The egress guard must never let one local-looking source mask another
    non-local source. Dispatch currently prefers config over env, but checking all
    explicit sources keeps enterprise mode fail-closed if precedence changes again.
    """
    urls: list[str] = []
    cfg_url = _provider_config_base_url(provider)
    if cfg_url:
        urls.append(cfg_url)
    env_var = _LOCAL_PROVIDER_ENDPOINT_ENV.get(provider)
    env_url = os.environ.get(env_var) if env_var else None
    if env_url and str(env_url).strip():
        urls.append(str(env_url).strip())
    return tuple(urls)


def _builtin_local_provider_is_local(provider: str) -> bool:
    """A built-in local provider (ollama/vllm/tgi) is local unless it has been
    pointed at a non-local endpoint. No configured endpoint -> the built-in
    localhost default -> local. Any public base_url/env URL -> NOT local: this is
    the egress-lock bypass (a "local" provider name aimed off-box) that we close."""
    urls = _explicit_local_provider_endpoints(provider)
    if not urls:
        return True
    return all(_is_local_endpoint(url) for url in urls)


def _extra_local_providers() -> frozenset[str]:
    """Operator-declared additional self-hosted providers (``[enterprise]
    local_providers``), canonicalized and fail-closed. Known cloud providers are
    ignored even if listed, and base-url-driven providers must have a configured
    local/private endpoint before they count as local. Empty / unreadable config ->
    no extras."""
    try:
        from .config import load_config
        from .providers import _canonical

        raw = ((load_config() or {}).get("enterprise") or {}).get("local_providers") or []
        providers: set[str] = set()
        for item in raw:
            canon = _canonical(str(item))
            if not canon:
                continue
            if canon in LOCAL_PROVIDERS:
                providers.add(canon)
            elif canon in CLOUD_PROVIDERS:
                continue
            elif canon in _ENDPOINT_VALIDATED_PROVIDERS:
                if _endpoint_validated_provider_is_local(canon):
                    providers.add(canon)
            else:
                # Operator-vouched custom provider: admit it (a bare name is a
                # deliberate vouch), but never when it carries an explicitly
                # non-local base_url -- a public endpoint can't be "local".
                url = _configured_base_url(canon)
                if url is None or _is_local_endpoint(url):
                    providers.add(canon)
        return frozenset(providers)
    except Exception:
        return frozenset()


def is_local_provider(provider: str) -> bool:
    """True if ``provider`` is self-hosted AND its endpoint is provably local.

    The name is canonicalized first (so the ``ollama`` alias ``local`` and any
    capitalization resolve). A built-in local provider (ollama/vllm/tgi) only
    counts as local when its resolved endpoint is local/private -- a ``base_url``
    or ``VLLM_BASE_URL``/``TGI_BASE_URL`` pointing off-box does NOT satisfy the
    egress lock just because the provider name is "local".
    """
    try:
        from .providers import _canonical
        canon = _canonical(provider)
    except Exception:
        canon = (provider or "").strip().lower()
    if canon in LOCAL_PROVIDERS:
        return _builtin_local_provider_is_local(canon)
    return canon in _extra_local_providers()


def _canonical_provider(provider: str) -> str:
    try:
        from .providers import _canonical

        return _canonical(provider)
    except Exception:
        return (provider or "").strip().lower()


def _active_matter_context():
    """Return the bound matter context without inventing ambient authority."""
    from .matter_context import current_matter_context

    return current_matter_context()


def matter_egress_boundary_active() -> bool:
    """Whether the current task carries privileged matter data."""
    try:
        return _active_matter_context() is not None
    except Exception:
        # A broken context resolver must make degraded HTTP clients refuse,
        # never silently turn a matter boundary into the legacy open posture.
        return True


def _firm_allowlist(key: str) -> frozenset[str]:
    """Exact deployment allow-list from ``[firm]``; malformed means empty."""
    try:
        from .config import load_config

        config = load_config() or {}
        if not isinstance(config, Mapping):
            return frozenset()
        section = config.get("firm") or {}
        if not isinstance(section, Mapping):
            return frozenset()
        raw = section.get(key) or []
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return frozenset()
        values = {
            str(item).strip().lower().rstrip(".")
            for item in raw
            if isinstance(item, str) and item.strip()
        }
        return frozenset(values)
    except Exception:
        return frozenset()


def _approved_matter_providers() -> frozenset[str]:
    return frozenset(
        _canonical_provider(item) for item in _firm_allowlist("approved_providers")
    )


def _matter_provider_permitted(context, provider: str) -> bool:
    # Local inference is still privileged matter processing.  Refresh the
    # durable membership before *every* provider authorization, including a
    # loopback Ollama/vLLM/TGI endpoint; otherwise a run that outlives an
    # attorney's revocation can keep sending client content to the local model.
    try:
        context = _refresh_bound_matter_context()
    except Exception:
        return False
    if is_local_provider(provider):
        endpoint = _configured_base_url(provider)
        if endpoint:
            # A provider name does not make an on-prem/LAN endpoint local. The
            # exact HTTP policy below requires approved_local_hosts + live ACL.
            return _matter_http_permitted(endpoint, context)
        # Only built-in providers have a known loopback default. Custom
        # operator-declared names without a concrete endpoint are ambiguous.
        return provider in LOCAL_PROVIDERS
    return (
        getattr(context, "egress_mode", None) == "approved_services"
        and provider in _approved_matter_providers()
        and not enterprise_enabled()
    )


def _audit_matter_egress_denial(*, provider: str, host: str = "") -> None:
    from .audit import EventKind, audit_event

    context = _active_matter_context()
    payload = {"provider": provider}
    if host:
        payload["host"] = host
    if context is not None:
        payload.update(
            matter_id=context.matter_id,
            purpose=context.purpose,
            egress_mode=context.egress_mode,
        )
    audit_event(EventKind.EGRESS_BLOCKED, **payload)


def assert_provider_allowed(provider: str) -> None:
    """Central provider egress guard for enterprise and client-matter data.

    A bound matter is local-only unless its responsible attorney selected
    ``approved_services`` *and* the canonical provider is in the deployment's
    exact ``[firm] approved_providers`` list. Enterprise mode remains a stricter
    floor and therefore still permits local providers only.

    Raises :class:`EgressBlocked` before the governed dispatch path can call a
    forbidden provider, and records an ``egress_blocked`` audit event.
    Called at the single LLM dispatch chokepoint
    (:func:`maverick.llm.LLM.complete`) so it covers every agent, role, and
    tool-driven model call that uses that chokepoint.
    """
    canon = _canonical_provider(provider)
    context = _active_matter_context()
    if context is not None:
        if _matter_provider_permitted(context, canon):
            return
        _audit_matter_egress_denial(provider=canon)
        raise EgressBlocked(
            canon,
            message=(
                "matter egress policy: refusing provider "
                f"{canon!r} for matter {context.matter_id}. Local providers are "
                "allowed; a contracted service additionally requires the matter "
                "mode 'approved_services' and an exact [firm] "
                "approved_providers entry."
            ),
        )
    if not enterprise_enabled() or is_local_provider(canon):
        return
    # A refusal still denies egress (the caller cannot reach dispatch), while
    # making it impossible to claim the denial was recorded when it was not.
    from .audit import EventKind, audit_event

    audit_event(EventKind.EGRESS_BLOCKED, provider=canon)
    raise EgressBlocked(canon)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return ""


def _allowed_egress_hosts() -> frozenset[str]:
    """Hosts an operator allows *tool* egress to in enterprise mode
    (``[enterprise] allowed_hosts``). Empty by default -> default-deny."""
    try:
        from .config import load_config
        raw = ((load_config() or {}).get("enterprise") or {}).get("allowed_hosts") or []
    except Exception:
        return frozenset()
    return frozenset(str(h).strip().lower().rstrip(".") for h in raw if str(h).strip())


def _matter_http_permitted(url: str, context) -> bool:
    """Evaluate the firm transport/host policy against live authority."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in {"http", "https"} or not host or _is_imds_host(host):
        return False
    # Loopback is exempt from the destination allow-list, not from the matter
    # ACL.  Resolve the current durable authority before any host class is
    # admitted so revocation/matter reassignment takes effect at the next
    # actual HTTP request, including local-model streaming follow-ups.
    try:
        fresh = _refresh_bound_matter_context()
    except Exception:
        return False
    if _is_firm_loopback_endpoint(url):
        # Explicit loopback runtimes are the only firm HTTP exception.
        return True

    # Everything leaving loopback is encrypted in transit. Certificate
    # verification is enforced by egress_guard at the actual client seam.
    if scheme != "https":
        return False

    if host in _firm_allowlist("approved_local_hosts"):
        return True

    # Literal private/link-local IPs never inherit locality merely from their
    # address class. They require the exact approved_local_hosts branch above.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_link_local):
        return False

    if getattr(fresh, "egress_mode", None) != "approved_services":
        return False
    if host not in _firm_allowlist("approved_hosts"):
        return False
    if enterprise_enabled():
        return host in _allowed_egress_hosts()
    return True


def egress_permitted(url: str) -> bool:
    """Is outbound HTTP egress permitted by matter and enterprise policy?

    Local/private endpoints pass. A matter in ``local_only`` denies every public
    host; ``approved_services`` additionally requires an exact ``[firm]
    approved_hosts`` entry. Enterprise mode then applies as a strict
    intersection using its own exact allow-list.
    """
    context = _active_matter_context()
    if context is not None:
        return _matter_http_permitted(url, context)
    if _is_local_endpoint(url):
        return True
    host = _host_of(url)
    if not enterprise_enabled():
        return True
    return bool(host) and host in _allowed_egress_hosts()


def enterprise_egress_denial(url: str, *, tool: str = "") -> str | None:
    """Tool-egress guard for string-returning tools: returns a denial reason (and
    audits an ``egress_blocked`` event) when enterprise mode forbids an outbound
    request to ``url``, else ``None``. Lets a tool surface the block as an error
    string without having to catch :class:`EgressBlocked`."""
    if egress_permitted(url):
        return None
    host = _host_of(url) or (url or "?")
    context = _active_matter_context()
    provider = f"tool:{tool}" if tool else "tool"
    _audit_matter_egress_denial(provider=provider, host=host)
    if context is not None:
        return (
            f"matter egress policy: refusing tool egress to {host!r} for matter "
            f"{context.matter_id}. Public HTTP requires mode 'approved_services' "
            "and an exact [firm] approved_hosts entry."
        )
    return (
        f"enterprise mode: refusing tool egress to {host!r} -- not a local endpoint "
        "and not in [enterprise] allowed_hosts. The application egress boundary "
        "blocks this request."
    )


__all__ = [
    "CLOUD_PROVIDERS",
    "LOCAL_PROVIDERS",
    "EgressBlocked",
    "egress_permitted",
    "matter_egress_boundary_active",
    "enterprise_egress_denial",
    "deployment_enterprise_enabled",
    "enterprise_enabled",
    "is_local_provider",
    "assert_provider_allowed",
]
