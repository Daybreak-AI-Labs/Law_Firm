"""`maverick doctor`: end-to-end health check with remediation.

v0.1.6: every red/yellow row now ends with an actionable verb so users
aren't told "something's wrong" without knowing what to do (council UX
review).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import click

from .sandbox_names import BUILTIN_SANDBOX_BACKENDS

GREEN = click.style("✓", fg="green")
YELLOW = click.style("!", fg="yellow")
RED = click.style("✗", fg="red")

# diagnose() accumulates the labels of failed (✗) checks here so the CLI can
# exit nonzero when the install is actually broken. doctor used to print red
# rows but always exit 0, so `maverick doctor && deploy` and CI health gates
# couldn't tell a broken deployment from a healthy one (user-testing finding).
_FAILURES: list[str] = []


def _is_outage(exc: BaseException) -> bool:
    """True if ``exc`` signals the provider is actually unreachable/down --
    a connection error, a timeout, or a 5xx server response -- as opposed to
    a local reason the probe couldn't run (SDK quirk, unexpected shape).

    Matched by class name so this works for both the anthropic and openai
    SDKs (and their httpx-level timeouts) without importing either eagerly.
    Both SDKs share these exception names (APIConnectionError / APITimeoutError
    / APIStatusError, with the 5xx InternalServerError as an APIStatusError
    subclass).
    """
    names = {c.__name__ for c in type(exc).__mro__}
    if names & {"APIConnectionError", "APITimeoutError", "ConnectError",
                "ConnectTimeout", "ReadTimeout", "TimeoutException"}:
        return True
    # 5xx server-side outage. InternalServerError subclasses APIStatusError and
    # carries a status_code; treat any >=500 status as a real outage.
    if "APIStatusError" in names:
        code = getattr(exc, "status_code", None)
        return isinstance(code, int) and code >= 500
    return "InternalServerError" in names


def _row(marker: str, label: str, detail: str = "", fix: str = "") -> None:
    if marker == RED:
        _FAILURES.append(label)
    line = f"  {marker} {label}"
    if detail:
        line += click.style(f"  ({detail})", fg="bright_black")
    click.echo(line)
    if fix:
        click.echo(click.style(f"      → {fix}", fg="cyan"))


def _check_config() -> dict:
    # tomllib (with config.py's 3.10 tomli fallback) is reused for the
    # validity probe below.
    from .config import config_path, load_config, tomllib
    p = config_path()
    if not p.exists():
        _row(RED, "config", f"{p} not found",
             fix="run  maverick init")
        return {}
    # Parse directly: load_config() fails SOFT (returns {} + logs a warning) on
    # a syntax error, so checking validity through it always reported GREEN --
    # a corrupt config that silently drops every user setting went unflagged by
    # the very tool meant to catch it.
    try:
        with open(p, "rb") as f:
            tomllib.load(f)
    except Exception as e:
        _row(RED, "config", f"invalid TOML -- your settings are being IGNORED ({e})",
             fix=f"edit {p} -- fix the TOML syntax, or back it up + re-run `maverick init`")
        return {}
    _row(GREEN, "config", str(p))
    return load_config(p)


def _check_config_lint(cfg: dict) -> None:
    """Schema-lint the loaded config so a mistyped section/key -- e.g. a budget
    cap typo (`[budget] max_dollarss`) that would otherwise silently run
    UNCAPPED -- surfaces in `maverick doctor`, not only in the dedicated
    `maverick config-lint`. Advisory: findings are warnings, never failures."""
    if not cfg:
        return  # no config / corrupt -- _check_config already reported it
    try:
        from .config_lint import lint_config
        findings = lint_config(cfg)
    except Exception:  # pragma: no cover -- linting must never break the doctor
        return
    if not findings:
        _row(GREEN, "config-lint", "no unknown or mistyped keys")
        return
    for i, f in enumerate(findings[:8]):
        _row(YELLOW, "config-lint", getattr(f, "message", str(f)),
             fix="run `maverick config-lint` for the full report" if i == 0 else "")


def _check_anthropic() -> None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        # Not a failure when some OTHER provider is configured (env key,
        # base-url env, or a [providers.<name>] table): a self-hosted
        # Ollama/vLLM deployment is healthy with no Anthropic key at all.
        # Doctor used to hard-RED here regardless (predicate split-brain,
        # platform-test finding).
        try:
            from .config import any_provider_configured
            other = any_provider_configured()
        except Exception:
            other = False
        if other:
            _row(YELLOW, "anthropic",
                 "ANTHROPIC_API_KEY not set (another provider is configured)",
                 fix="fine unless a [models] role routes to an anthropic model")
        else:
            _row(RED, "anthropic", "ANTHROPIC_API_KEY not set",
                 fix="add to ~/.maverick/.env or `export ANTHROPIC_API_KEY=sk-ant-...`")
        return
    if not key.startswith("sk-ant-"):
        _row(YELLOW, "anthropic", "key doesn't start with sk-ant-",
             fix="re-check the key at https://console.anthropic.com/settings/keys")
        return
    try:
        import anthropic
    except ImportError:
        _row(YELLOW, "anthropic", "SDK not installed",
             fix="pip install anthropic")
        return
    try:
        client = anthropic.Anthropic(api_key=key)
        list(client.models.list(limit=1))
        _row(GREEN, "anthropic", "key validated")
    except anthropic.AuthenticationError:
        _row(RED, "anthropic", "API rejected the key",
             fix="generate a new key at https://console.anthropic.com/settings/keys, then `maverick init` to update .env")
    except Exception as e:
        # A real outage (no connection / timeout / 5xx) is RED -- the agent
        # cannot reach the API, so reporting it as a benign YELLOW "skipped"
        # hid genuine downtime. Anything else (unexpected SDK shape) stays
        # YELLOW: we just couldn't run the probe.
        if _is_outage(e):
            _row(RED, "anthropic", f"API unreachable: {type(e).__name__}",
                 fix="check network / proxy / api.anthropic.com status; key format looks right")
        else:
            _row(YELLOW, "anthropic", f"validation skipped: {type(e).__name__}",
                 fix="check network / proxy; key format looks right")


def _check_openai() -> None:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return
    try:
        from openai import AuthenticationError, OpenAI
    except ImportError:
        _row(
            YELLOW,
            "openai",
            "SDK not installed",
            fix=("install the OpenAI extra from the same reviewed Maverick "
                 "checkout; public-index lookup is disabled"),
        )
        return
    try:
        client = OpenAI(api_key=key)
        list(client.models.list().data[:1])
        _row(GREEN, "openai", "key validated")
    except AuthenticationError:
        _row(RED, "openai", "API rejected the key",
             fix="regenerate at https://platform.openai.com/api-keys, then `maverick init`")
    except Exception as e:
        # Real outage (no connection / timeout / 5xx) is RED; everything else
        # stays YELLOW "skipped" -- see _check_anthropic.
        if _is_outage(e):
            _row(RED, "openai", f"API unreachable: {type(e).__name__}",
                 fix="check network / proxy / status.openai.com")
        else:
            _row(YELLOW, "openai", f"validation skipped: {type(e).__name__}")


def _check_sandbox_docker() -> None:
    if not shutil.which("docker"):
        _row(RED, "sandbox", "docker not on PATH",
             fix="install Docker Desktop (https://docker.com/products/docker-desktop) or change [sandbox] backend to 'local' in ~/.maverick/config.toml")
        return
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True, timeout=5, check=True,
        )
        _row(GREEN, "sandbox", "docker daemon responding")
    except subprocess.CalledProcessError:
        _row(RED, "sandbox", "docker daemon not running",
             fix="start Docker Desktop, or `sudo systemctl start docker` on Linux")
    except subprocess.TimeoutExpired:
        _row(RED, "sandbox", "docker version timed out",
             fix="docker is installed but unresponsive -- restart Docker Desktop")


def _check_sandbox(cfg: dict) -> None:
    """Report health for the exact retained backend catalog."""
    backend = str(
        cfg.get("sandbox", {}).get("backend", "local") or "local"
    ).strip().lower()
    if backend == "local":
        _row(GREEN, "sandbox", "local subprocess")
        return
    if backend == "docker":
        _check_sandbox_docker()
        return
    _row(
        RED,
        "sandbox",
        f"backend={backend} is not supported by the firm runtime",
        fix=f"supported: {', '.join(BUILTIN_SANDBOX_BACKENDS)}",
    )


CHANNEL_DEPS = {
    "telegram": ("telegram", "python-telegram-bot"),
    "discord":  ("discord", "discord.py"),
    "slack":    ("slack_sdk", "slack_sdk"),
    "matrix":   ("nio", "matrix-nio"),
    "whatsapp": ("twilio", "twilio + fastapi"),
    "sms":      ("twilio", "twilio + fastapi"),
}


def _check_channels(cfg: dict) -> None:
    channels = cfg.get("channels", {})
    if not channels:
        return
    for name, ch_cfg in channels.items():
        if not ch_cfg.get("enabled"):
            continue
        dep = CHANNEL_DEPS.get(name)
        if dep:
            mod, friendly = dep
            try:
                __import__(mod)
                _row(GREEN, f"channel:{name}", f"{friendly} installed")
            except ImportError:
                _row(
                    YELLOW,
                    f"channel:{name}",
                    f"{friendly} not installed",
                    fix=("install this channel extra from the same reviewed "
                         "Maverick checkout; public-index lookup is disabled"),
                )
                continue
        elif name == "signal":
            if not shutil.which("signal-cli"):
                _row(YELLOW, "channel:signal", "signal-cli not on PATH",
                     fix="install signal-cli per https://github.com/AsamK/signal-cli, then register your number")
                continue
            _row(GREEN, "channel:signal", "signal-cli present")
        elif name == "imessage":
            if sys.platform != "darwin":
                _row(RED, "channel:imessage", f"requires macOS (you're on {sys.platform})",
                     fix="disable in config or run Maverick from a Mac")
                continue
            _row(GREEN, "channel:imessage", "macOS")
        elif name == "email":
            _row(GREEN, "channel:email", "stdlib only")


def _check_world_db() -> None:
    from .workspace import Workspace
    from .world_model import close_world_if_owned, open_world

    label = str(Workspace.current().db_path)
    try:
        w = open_world()
        try:
            _row(GREEN, "world-db", f"{label} (schema v{w.schema_version})")
        finally:
            close_world_if_owned(w)
    except Exception as e:
        _row(RED, "world-db", f"open failed: {e}",
             fix="check world-model backend credentials, connectivity, and storage permissions")


def _check_shield() -> None:
    try:
        from maverick_shield import Shield
    except ImportError:
        from .shield_policy import shield_required
        if shield_required():
            _row(RED, "shield",
                 "shield REQUIRED (enterprise / [safety] require_shield) but "
                 "maverick-shield is not installed — external traffic is refused",
                 fix=("install Shield from the same reviewed Maverick checkout; "
                      "public-index lookup is disabled"))
            return
        _row(
            YELLOW,
            "shield",
            "maverick-shield not installed",
            fix=("install Shield from the same reviewed Maverick checkout; "
                 "public-index lookup is disabled"),
        )
        return
    # warn_if_missing=False: doctor renders the shield row (with remediation)
    # itself, so the raw "SDK not installed" log line would just bleed into the
    # health-check output mid-table.
    s = Shield.from_config(warn_if_missing=False)
    backend_label = {
        "agent-shield": "agent-shield SDK (full ~115 patterns)",
        "builtin": "builtin rules (~20 high-impact patterns)",
        "none": "DISABLED -- [safety] profile=off in config",
    }.get(s.backend, s.backend)
    if s.backend == "agent-shield":
        _row(GREEN, "shield", backend_label)
    elif s.backend == "builtin":
        _row(YELLOW, "shield", backend_label,
             fix="Use the built-in layer; the full SDK is not on public PyPI")
    else:
        _row(RED, "shield", backend_label,
             fix="set [safety] profile = \"balanced\" in ~/.maverick/config.toml to re-enable")


def _check_profile() -> None:
    """Surface the active deployment profile + security posture so an operator
    can confirm which posture is live (the single ``MAVERICK_PROFILE`` /
    ``[profile] name`` switch). Informational; a misconfigured enterprise
    boundary is reported in depth by ``maverick enterprise verify``."""
    try:
        from .enterprise import enterprise_enabled
        from .profile import active_profile
        from .security_defaults import secure_by_default
    except Exception:  # pragma: no cover - never break doctor
        return
    prof = active_profile()
    ent = enterprise_enabled()
    sec = secure_by_default()
    posture = []
    posture.append("egress lock ON" if ent else "egress lock off (cloud-capable)")
    posture.append("hardened defaults ON" if sec else "hardened defaults OFF")
    if prof == "enterprise":
        _row(GREEN, "profile",
             f"deployment profile = enterprise ({', '.join(posture)})")
    else:
        _row(GREEN, "profile",
             f"deployment profile = standard ({', '.join(posture)})",
             fix="set MAVERICK_PROFILE=enterprise (or [profile] name) for the "
                 "regulated, data-boundary posture")


def _check_config_perms() -> None:
    """Config may hold tokens/secrets — warn if it's group/world-accessible."""
    try:
        from .config import config_path
        p = config_path()
        if not p.exists():
            return
        mode = p.stat().st_mode & 0o777
    except Exception:  # pragma: no cover - never break doctor
        return
    if mode & 0o077:
        _row(YELLOW, "config-perms",
             f"{p} is group/world-accessible (mode {oct(mode)})",
             fix=f"chmod 600 {p} — it may hold tokens/secrets")
    else:
        _row(GREEN, "config-perms", "config.toml is owner-only (0600)")


def _check_client_binding() -> None:
    """One Maverick per enterprise client — surface the binding and fail loudly
    when it's enforced but unset (the deployment would otherwise serve from the
    shared root)."""
    try:
        from .client import status as client_status
        st = client_status()
    except Exception as e:  # pragma: no cover - never break doctor
        _row(YELLOW, "client", f"binding status unavailable: {e}")
        return
    cid = st.get("client_id")
    if cid:
        _row(GREEN, "client", f"bound to {cid!r} — data root {st['data_root']}")
    elif st.get("enforced"):
        _row(RED, "client",
             "client binding ENFORCED but no client id set — refusing to serve "
             "unbound",
             fix="set MAVERICK_CLIENT_ID (service unit) or [client] id in config")
    else:
        _row(YELLOW, "client",
             "no client binding (shared root) — single-tenant/legacy mode",
             fix="for an enterprise deployment set [client] id + enforce = true")


def _check_proxy_auth() -> None:
    """Flag an insecure reverse-proxy-SSO config: proxy auth enabled, but no
    `trusted_proxies` pin and the loopback fallback still active -- any
    co-located loopback process (a sidecar, a pod-netns neighbour, an SSRF pivot
    to 127.0.0.1) could then spoof the forwarded identity header. Advisory only;
    off-by-default proxy auth and a pinned/enterprise config are silent."""
    try:
        from .proxy_auth import (
            _section,
            _trust_loopback_fallback,
            proxy_auth_enabled,
        )
    except Exception:  # pragma: no cover -- never break the doctor
        return
    if not proxy_auth_enabled():
        return  # off by default -> nothing to flag
    trusted = _section().get("trusted_proxies")
    if isinstance(trusted, (list, tuple)) and trusted:
        _row(GREEN, "proxy-auth", "trusted_proxies pinned")
    elif _trust_loopback_fallback():
        _row(YELLOW, "proxy-auth",
             "enabled with no trusted_proxies pin; any loopback process can spoof "
             "the identity header",
             fix="pin [auth.proxy] trusted_proxies, or set trust_loopback = false "
                 "(enterprise mode disables the fallback automatically)")
    else:
        _row(GREEN, "proxy-auth", "loopback fallback disabled")


def diagnose() -> int:
    """Run every health check, print the report, and return the number of
    failed (✗) checks. 0 == healthy. The CLI exits nonzero when this is
    nonzero so a deploy gate or CI can detect a broken install."""
    _FAILURES.clear()
    click.echo(click.style("Maverick health check\n", bold=True))
    cfg = _check_config()
    _check_config_lint(cfg)
    _check_config_perms()
    _check_profile()
    _check_client_binding()
    _check_proxy_auth()
    _check_anthropic()
    _check_openai()
    _check_sandbox(cfg)
    _check_channels(cfg)
    _check_world_db()
    _check_shield()
    click.echo("")
    if _FAILURES:
        click.echo(click.style(
            f"{len(_FAILURES)} check(s) need attention: " + ", ".join(_FAILURES),
            fg="red") + "   Re-run after fixing:  maverick doctor")
        return len(_FAILURES)
    click.echo(click.style("Done.", fg="bright_black") + "  Re-run any time:  maverick doctor")
    return 0
