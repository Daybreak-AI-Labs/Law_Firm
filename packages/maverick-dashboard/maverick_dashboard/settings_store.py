"""Dashboard Settings overlay for provider keys + capability/feature toggles.

Writes ``~/.maverick/dashboard-config.toml`` (config.dashboard_overrides_path),
which ``maverick.config.load_config`` deep-merges over config.toml — so a key
or toggle set in the UI takes effect on the next run without touching the
user's config.toml. (Models / budget / tool denials live in the separate
runtime-overrides.toml, read via their own hooks.)

Secrets: provider api_keys are written here at 0600. The kernel resolves
``[providers.<name>].api_key`` from config BEFORE the env vars, so a key set
here unblocks goals immediately. The HTTP layer must NEVER echo a stored key
back — callers get a masked hint only (see ``state()``).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from maverick import config

# Serializes the overlay load-modify-save in-process; cross_process_lock in
# _locked() extends it across processes (multiple dashboard workers edit
# provider keys / toggles).
_SETTINGS_LOCK = threading.Lock()


class SecuritySuiteRevisionConflict(RuntimeError):
    """The admin edited an out-of-date deployment-wide security control."""


class SecuritySuiteConfigUnavailable(RuntimeError):
    """The security-suite overlay cannot be read without risking data loss."""


def _locked():
    from contextlib import ExitStack

    from maverick.file_lock import cross_process_lock
    stack = ExitStack()
    stack.enter_context(_SETTINGS_LOCK)
    stack.enter_context(cross_process_lock(config.dashboard_overrides_path()))
    return stack


def _security_suite_revision(data: dict) -> int:
    metadata = data.get("security_suite_control")
    value = metadata.get("revision") if isinstance(metadata, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def security_suite_revision() -> int:
    """Return the monotonic CAS revision for the four suite control bits."""
    with _locked():
        return _security_suite_revision(_load_security_suite_overlay())

# Providers offered in the UI: name, label, env var(s) that also satisfy it,
# whether a base_url (self-hosted endpoint) is relevant.
PROVIDERS: list[dict] = [
    {"name": "anthropic", "label": "Anthropic (Claude)", "env": ["ANTHROPIC_API_KEY"], "base_url": False},
    {"name": "openai", "label": "OpenAI", "env": ["OPENAI_API_KEY"], "base_url": False},
    {"name": "gemini", "label": "Google Gemini", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "base_url": False},
    {"name": "openrouter", "label": "OpenRouter", "env": ["OPENROUTER_API_KEY"], "base_url": False},
    {"name": "moonshot", "label": "Moonshot", "env": ["MOONSHOT_API_KEY"], "base_url": False},
    {"name": "deepseek", "label": "DeepSeek", "env": ["DEEPSEEK_API_KEY"], "base_url": False},
    {"name": "xai", "label": "xAI (Grok)", "env": ["XAI_API_KEY", "GROK_API_KEY"], "base_url": False},
    {"name": "ollama", "label": "Ollama (self-hosted)", "env": [], "base_url": True},
    {"name": "vllm", "label": "vLLM / OpenAI-compatible", "env": ["VLLM_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL"], "base_url": True},
]
_PROVIDER_NAMES = {p["name"] for p in PROVIDERS}

# Mirror maverick.config.get_capabilities/get_features keys + defaults.
CAPABILITY_DEFAULTS = {
    "computer_use": False, "browser": False, "web_search": False,
    "mobile_tools": False, "ros": False, "code_exec": False,
}
FEATURE_DEFAULTS = {"skills": True, "world_model": True, "streaming": True}

# What each toggle actually turns on -- shown next to the checkbox so an
# admin knows what they are selecting before they select it.
CAPABILITY_INFO = {
    "computer_use": "Agents drive a real desktop inside the sandbox — click, "
                    "type, read the screen. The highest-impact capability; "
                    "enable only for workflows that genuinely need a GUI.",
    "browser": "Headless web browsing for agents: open pages, follow links, "
               "read rendered content. Governed by the egress policy.",
    "web_search": "Live web search for research tasks; results flow into the "
                  "agent's context with their sources.",
    "mobile_tools": "Drive Android/iOS devices through the mobile toolchain "
                    "(taps, swipes, screen reads) for app testing and flows.",
    "ros": "Robot Operating System bridge — agents publish/subscribe to ROS "
           "topics. Only for robotics deployments.",
    "code_exec": "Agents run the code they write, inside the governed "
                 "sandbox (every shell call goes through sandbox.exec with "
                 "its allowlists and audit).",
}
FEATURE_INFO = {
    "skills": "Reusable learned procedures agents can load mid-task — the "
              "skill library built by the learning loop.",
    "world_model": "The shared operating record: goals, episodes, approvals, "
                   "and memory live in one governed store. Turning this off "
                   "disables cross-run state.",
    "streaming": "Live token streaming to the dashboard chat and goal "
                 "timelines (off = responses arrive only when complete).",
}

# The self-learning loop's subsystems -- ONE source of truth shared by the
# one-click toggle (set_learning writes each (section, config_key) to the
# overlay, which load_config deep-merges so every subsystem's enabled() sees it)
# and the /learning status view (which pairs each key with its live enabled()).
# Keeping both off one list stops the button and the view from drifting when a
# subsystem is added. Governed learning defaults ON; DGM is intentionally not
# in this registry and has its own high-risk production control.
LEARNING_SUBSYSTEMS: list[dict] = [
    {"key": "self_learning", "section": "self_learning", "config_key": "enable",
     "label": "Governed capability learning",
     "how": "[self_learning] enable — or MAVERICK_SELF_LEARNING=0 to opt out"},
    {"key": "distill_local", "section": "self_learning", "config_key": "distill_local",
     "label": "Local skill distillation",
     "how": "[self_learning] distill_local — or MAVERICK_DISTILL_LOCAL=0"},
    {"key": "failure_telemetry", "section": "telemetry", "config_key": "failure_modes",
     "label": "Local failure-mode telemetry",
     "how": "[telemetry] failure_modes or MAVERICK_FAILURE_TELEMETRY=0"},
    {"key": "budget_tuning", "section": "budget", "config_key": "self_tuning",
     "label": "Evidence-based budget tuning",
     "how": "[budget] self_tuning or MAVERICK_BUDGET_SELF_TUNING=0"},
    {"key": "consequence", "section": "consequence", "config_key": "enable",
     "label": "Grounded outcomes (reality feedback)",
     "how": "[consequence] enable — or MAVERICK_CONSEQUENCE=0"},
    {"key": "reflexion", "section": "reflexion", "config_key": "enable",
     "label": "Failure memory (learn from mistakes)",
     "how": "[reflexion] enable — or MAVERICK_REFLEXION=0"},
    {"key": "dreaming", "section": "dreaming", "config_key": "enable",
     "label": "Consolidation (distil skills + insights)",
     "how": "[dreaming] enable — or MAVERICK_DREAMING=0"},
    {"key": "experience", "section": "experience", "config_key": "enable",
     "label": "Experience-guided planning",
     "how": "[experience] enable — or MAVERICK_EXPERIENCE_GUIDANCE=0"},
    {"key": "skill_synthesis", "section": "skill_synthesis", "config_key": "enable",
     "label": "Task-specific skill synthesis",
     "how": "[skill_synthesis] enable — or MAVERICK_SKILL_SYNTHESIS=0"},
    {"key": "self_improvement", "section": "self_improvement", "config_key": "enable",
     "label": "Governed improvement promotion",
     "how": "[self_improvement] enable — or MAVERICK_SELF_IMPROVEMENT=0"},
    {"key": "prm_guidance", "section": "self_improvement", "config_key": "prm_guidance",
     "label": "Process-reward guidance",
     "how": "[self_improvement] prm_guidance or MAVERICK_PRM_GUIDANCE=0"},
    {"key": "capture", "section": "self_improvement", "config_key": "capture",
     "label": "Trajectory capture (fuel for the flywheel)",
     "how": "[self_improvement] capture — or MAVERICK_TRAJECTORY_CAPTURE=0"},
    {"key": "causal_promotion", "section": "self_improvement", "config_key": "causal_promotion",
     "label": "Causal promotion evidence",
     "how": "[self_improvement] causal_promotion — or MAVERICK_CAUSAL_PROMOTION=0"},
    {"key": "factory_learning", "section": "self_improvement", "config_key": "factory_learning",
     "label": "Self-improving agent factory",
     "how": "[self_improvement] factory_learning — or MAVERICK_FACTORY_LEARNING=0"},
    {"key": "evaluator_evolution", "section": "self_improvement", "config_key": "evaluator_evolution",
     "label": "Evaluator co-evolution",
     "how": "[self_improvement] evaluator_evolution — or MAVERICK_EVALUATOR_EVOLUTION=0"},
    {"key": "self_harness", "section": "self_harness", "config_key": "enable",
     "label": "Risk-limited self-harness",
     "how": "[self_harness] enable — or MAVERICK_SELF_HARNESS=0"},
    {"key": "data_engine", "section": "data_engine", "config_key": "enable",
     "label": "Improvement flywheel",
     "how": "[data_engine] enable — or MAVERICK_DATA_ENGINE=0"},
    {"key": "operations_scientist", "section": "operations_scientist", "config_key": "enable",
     "label": "Operations scientist",
     "how": "[operations_scientist] enable — or MAVERICK_OPERATIONS_SCIENTIST=0"},
    {"key": "rehearsal", "section": "rehearsal", "config_key": "enable",
     "label": "Pre-execution rehearsal",
     "how": "[rehearsal] enable — or MAVERICK_REHEARSAL=0"},
    {"key": "credit", "section": "credit", "config_key": "enable",
     "label": "Counterfactual agent credit",
     "how": "[credit] enable — or MAVERICK_CREDIT=0"},
    {"key": "reasoning_reward", "section": "reasoning_reward", "config_key": "enable",
     "label": "Structured reasoning rewards",
     "how": "[reasoning_reward] enable or MAVERICK_REASONING_REWARD=0"},
    {"key": "reward_audit", "section": "reasoning_reward", "config_key": "audit_rewards",
     "label": "Tamper-evident reward audit",
     "how": "[reasoning_reward] audit_rewards or MAVERICK_REASONING_REWARD_AUDIT=0"},
    {"key": "jit_rl", "section": "jit_rl", "config_key": "enable",
     "label": "JitRL test-time adaptation",
     "how": "[jit_rl] enable or MAVERICK_JIT_RL=0"},
]
LEARNING_TOGGLES = [(s["section"], s["config_key"]) for s in LEARNING_SUBSYSTEMS]
_LEARNING_SECTIONS = list(dict.fromkeys(s["section"] for s in LEARNING_SUBSYSTEMS))

# Channels offered in the UI: name + per-field spec. Keys MUST match what
# (channel adapters removed; the [channels.*] overlay plumbing is gone with
# load_config() deep-merge. ``secret`` fields are masked + never echoed back;
# ``type: int`` fields are stored as numbers.
CHANNELS: list[dict] = [
    {"name": "telegram", "label": "Telegram", "fields": [
        {"key": "bot_token", "label": "Bot token", "secret": True},
    ]},
    {"name": "discord", "label": "Discord", "fields": [
        {"key": "bot_token", "label": "Bot token", "secret": True},
    ]},
    {"name": "slack", "label": "Slack", "fields": [
        {"key": "app_token", "label": "App-level token", "secret": True},
        {"key": "bot_token", "label": "Bot token", "secret": True},
    ]},
    {"name": "whatsapp_cloud", "label": "WhatsApp (Cloud API)", "fields": [
        {"key": "access_token", "label": "Access token", "secret": True},
        {"key": "phone_number_id", "label": "Phone number ID", "secret": False},
        {"key": "verify_token", "label": "Verify token", "secret": True},
        {"key": "app_secret", "label": "App secret", "secret": True},
        {"key": "port", "label": "Webhook port", "secret": False, "type": "int"},
    ]},
    {"name": "sms", "label": "SMS (Twilio)", "fields": [
        {"key": "account_sid", "label": "Account SID", "secret": False},
        {"key": "auth_token", "label": "Auth token", "secret": True},
        {"key": "from_number", "label": "From number", "secret": False},
        {"key": "port", "label": "Webhook port", "secret": False, "type": "int"},
    ]},
    {"name": "email", "label": "Email (IMAP/SMTP)", "fields": [
        {"key": "imap_host", "label": "IMAP host", "secret": False},
        {"key": "imap_user", "label": "IMAP user", "secret": False},
        {"key": "imap_password", "label": "IMAP password", "secret": True},
        {"key": "smtp_host", "label": "SMTP host", "secret": False},
        {"key": "smtp_user", "label": "SMTP user", "secret": False},
        {"key": "smtp_password", "label": "SMTP password", "secret": True},
        {"key": "smtp_port", "label": "SMTP port", "secret": False, "type": "int"},
    ]},
]
_CHANNELS_BY_NAME = {c["name"]: c for c in CHANNELS}


def _tomllib():
    try:
        import tomllib  # 3.11+
    except ModuleNotFoundError:  # 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib


def load_overlay() -> dict:
    p = config.dashboard_overrides_path()
    if not p.exists():
        return {}
    try:
        with open(p, "rb") as f:
            return _tomllib().load(f)
    except (OSError, ValueError):
        return {}


def _load_overlay_for_update() -> dict:
    """Strict overlay read for every load-modify-write operation.

    The general Settings UI historically treats a malformed optional overlay as
    empty. A mutation cannot: writing a newly constructed table over unreadable
    state would delete unrelated provider, channel, feature, and security-suite
    settings. Missing remains the one legitimate empty state.
    """
    path = config.dashboard_overrides_path()
    try:
        with open(path, "rb") as handle:
            value = _tomllib().load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SecuritySuiteConfigUnavailable(
            "dashboard settings are unreadable; settings were not changed"
        ) from exc
    if not isinstance(value, dict):  # defensive: TOML roots are mappings
        raise SecuritySuiteConfigUnavailable(
            "dashboard settings are unreadable; settings were not changed"
        )
    return value


def _load_security_suite_overlay() -> dict:
    """Compatibility name for the strict security-suite CAS read."""
    return _load_overlay_for_update()


def _toml_str(value: str) -> str:
    return json.dumps(str(value))  # JSON strings are valid TOML basic strings


def _toml_value(value) -> str:
    """Render the bounded TOML value types load_overlay can return.

    Security connector selections and vendor extension settings may be arrays
    or inline tables. Coercing them through ``str()`` silently changed their
    type on every unrelated feature-toggle save.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = (
            f"{_toml_str(str(key))} = {_toml_value(item)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
        return "{ " + ", ".join(entries) + " }"
    raise TypeError(f"unsupported dashboard setting value: {type(value).__name__}")


def _dump_value(lines: list[str], val: dict) -> None:
    """Emit [value] -- the client's cost/value assumptions for the Savings
    page (hourly rate, hours per task, optional per-department overrides).
    Read back by maverick.config.get_value via the same deep-merge as
    everything else."""
    if not val:
        return
    lines.append("[value]")
    if "enable" in val:
        lines.append(f"enable = {'true' if val['enable'] else 'false'}")
    for k in ("hourly_rate", "hours_per_task"):
        if k in val:
            lines.append(f"{k} = {float(val[k])}")
    if val.get("currency"):
        lines.append(f"currency = {_toml_str(val['currency'])}")
    lines.append("")
    for name in sorted(val.get("departments") or {}):
        ov = val["departments"][name] or {}
        if not ov:
            continue
        lines.append(f"[value.departments.{_toml_str(name)}]")
        for k in ("hourly_rate", "hours_per_task"):
            if k in ov:
                lines.append(f"{k} = {float(ov[k])}")
        lines.append("")


def _dump(data: dict) -> str:
    lines = [
        "# Dashboard-managed settings overlay (provider keys + capability/feature",
        "# toggles). Edit via the dashboard Settings page, not by hand. Your",
        "# config.toml is never touched by the dashboard.",
        "",
    ]
    # "flows" carries the autonomous self-improvement knobs (auto_evolve/auto_apply,
    # both booleans) -- persisted here so set_flow_autonomy survives a restart.
    for section in ("capabilities", "features", "flows", "ekko",
                    "security_ops", "threat_hunt", "env_hunt",
                    "security_suite_control",
                    *_LEARNING_SECTIONS):
        tbl = data.get(section) or {}
        if tbl:
            lines.append(f"[{section}]")
            for k in sorted(tbl):
                lines.append(f"{k} = {_toml_value(tbl[k])}")
            lines.append("")
    _dump_value(lines, data.get("value") or {})
    # [model_cost_tiers] -- manager-set cost bands per model. Keys are model
    # ids that can contain '/' or '.', so they are always quoted.
    tiers = data.get("model_cost_tiers") or {}
    if tiers:
        lines.append("[model_cost_tiers]")
        for model in sorted(tiers):
            lines.append(f"{_toml_str(model)} = {_toml_str(str(tiers[model]))}")
        lines.append("")
    # [webhooks] secret -- the signing key the Settings page manages. Written
    # verbatim (never logged or re-displayed).
    whk = data.get("webhooks") or {}
    if whk.get("secret"):
        lines.append("[webhooks]")
        lines.append(f"secret = {_toml_str(whk['secret'])}")
        lines.append("")
    for name in sorted(data.get("providers") or {}):
        pcfg = data["providers"][name] or {}
        fields = [(f, pcfg.get(f)) for f in ("api_key", "base_url") if pcfg.get(f)]
        if not fields:
            continue
        lines.append(f"[providers.{name}]")
        for field, val in fields:
            lines.append(f"{field} = {_toml_str(val)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write(data: dict) -> None:
    # Unique temp + os.replace (0600): the fixed ".toml.tmp" collided between two
    # concurrent workers. RMW serialization is in the mutators via _locked().
    from maverick.file_lock import atomic_write_text
    atomic_write_text(config.dashboard_overrides_path(), _dump(data))


def set_provider(name: str, api_key: str | None = None, base_url: str | None = None) -> None:
    """Set a provider's api_key/base_url. Empty/None values are left unchanged
    (so re-saving a base_url never wipes a key you can't see). Use
    ``clear_provider`` to remove."""
    if name not in _PROVIDER_NAMES:
        raise ValueError("unknown provider")
    with _locked():
        data = _load_overlay_for_update()
        pcfg = data.setdefault("providers", {}).setdefault(name, {})
        if api_key and api_key.strip():
            pcfg["api_key"] = api_key.strip()
        if base_url and base_url.strip():
            pcfg["base_url"] = base_url.strip()
        if not pcfg:
            data["providers"].pop(name, None)
        _write(data)


def clear_provider(name: str) -> None:
    if name not in _PROVIDER_NAMES:
        raise ValueError("unknown provider")
    with _locked():
        data = _load_overlay_for_update()
        (data.get("providers") or {}).pop(name, None)
        _write(data)


def set_webhooks_secret(secret: str | None) -> None:
    """Set (or clear, when empty) the inbound/outbound webhook signing secret
    in the dashboard overlay -- the same [webhooks] secret knob operators set
    in config; the MAVERICK_WEBHOOK_SECRET env var still takes precedence."""
    with _locked():
        data = _load_overlay_for_update()
        cleaned = (secret or "").strip()
        if cleaned:
            data.setdefault("webhooks", {})["secret"] = cleaned
        else:
            (data.get("webhooks") or {}).pop("secret", None)
            if not data.get("webhooks"):
                data.pop("webhooks", None)
        _write(data)


def set_toggle(section: str, name: str, enabled: bool) -> None:
    """Override a [capabilities] or [features] flag in the overlay."""
    defaults = {"capabilities": CAPABILITY_DEFAULTS, "features": FEATURE_DEFAULTS}.get(section)
    if defaults is None or name not in defaults:
        raise ValueError("unknown setting")
    with _locked():
        data = _load_overlay_for_update()
        data.setdefault(section, {})[name] = bool(enabled)
        _write(data)


# Platform systems whose [section] enable flag an admin may switch from
# inside the app -- the single authority the /features/switches API trusts.
SWITCHABLE_SECTIONS: tuple[str, ...] = (
    "dreaming", "self_improvement", "self_harness", "fleet_memory",
    "rehearsal", "flows", "threat_hunt", "env_hunt", "entity_graph",
)


def set_section_enable(section: str, enabled: bool) -> None:
    """Overlay one switchable platform system's ``enable`` flag."""
    if section not in SWITCHABLE_SECTIONS:
        raise ValueError("unknown setting")
    with _locked():
        data = _load_overlay_for_update()
        data.setdefault(section, {})["enable"] = bool(enabled)
        _write(data)


def set_model_cost_tier(model: str, band: str | None) -> None:
    """Persist a manager's cost-band override for a model in
    ``[model_cost_tiers]``. ``band=None`` clears the override (back to the
    price-derived band). Validated against the known bands."""
    from maverick.model_cost_tier import BANDS
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    if band is not None and band not in BANDS:
        raise ValueError(f"band must be one of {BANDS} or null")
    with _locked():
        data = _load_overlay_for_update()
        tiers = data.setdefault("model_cost_tiers", {})
        if band is None:
            tiers.pop(model, None)
            if not tiers:
                data.pop("model_cost_tiers", None)
        else:
            tiers[model] = band
        _write(data)


def set_security_suite(
    *,
    security_ops: bool,
    threat_hunt: bool,
    env_hunt: bool,
    response_execution: bool,
    actor: str,
    expected_revision: int,
) -> int:
    """Atomically publish deployment-wide security-suite enablement.

    Response execution can never be armed without the environment hunter. The
    signed authorization is durable before the overlay is published, so a
    crash or concurrent reader can never observe an unaudited authority bit.
    """
    if response_execution and not env_hunt:
        raise ValueError("response execution requires env_hunt")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision must be a positive integer")
    requested = {
        "security_ops": bool(security_ops),
        "threat_hunt": bool(threat_hunt),
        "env_hunt": bool(env_hunt),
        "response_execution": bool(response_execution),
    }
    with _locked():
        before = _load_security_suite_overlay()
        current_revision = _security_suite_revision(before)
        if current_revision != expected_revision:
            raise SecuritySuiteRevisionConflict(
                f"expected security-suite revision {expected_revision}, "
                f"found {current_revision}"
            )
        effective_before = config.load_global_config() or {}
        if config.config_source_errors(include_tenant=False):
            raise SecuritySuiteConfigUnavailable(
                "an active global config source is unreadable; "
                "security-suite configuration was not changed"
            )
        data = json.loads(json.dumps(before))
        security_section = dict(data.get("security_ops") or {})
        platform_section = dict(data.get("threat_hunt") or {})
        environment_section = dict(data.get("env_hunt") or {})
        security_section["enable"] = requested["security_ops"]
        platform_section["enable"] = requested["threat_hunt"]
        environment_section["enable"] = requested["env_hunt"]
        environment_section["response_execution"] = requested["response_execution"]
        # This endpoint owns only the four booleans above. Preserve connector
        # selections, polling intervals, limits, and vendor extension settings.
        data["security_ops"] = security_section
        data["threat_hunt"] = platform_section
        data["env_hunt"] = environment_section
        next_revision = current_revision + 1
        metadata = dict(data.get("security_suite_control") or {})
        metadata["revision"] = next_revision
        data["security_suite_control"] = metadata
        from maverick.config import reset_config_cache

        effective_security = effective_before.get("security_ops") or {}
        effective_platform = effective_before.get("threat_hunt") or {}
        effective_environment = effective_before.get("env_hunt") or {}
        previous = {
            "security_ops": effective_security.get("enable", True) is True,
            "threat_hunt": effective_platform.get("enable", False) is True,
            "env_hunt": effective_environment.get("enable", False) is True,
            "response_execution": (
                effective_environment.get("response_execution", False) is True
            ),
        }
        import time

        from maverick.audit import AuditEvent, EventKind, global_audit_log
        from maverick.privacy_ops import _actor_label

        accepted = global_audit_log().record(AuditEvent(
            ts=time.time(),
            kind=EventKind.SECURITY_SUITE_CONTROL_CHANGED,
            agent="dashboard",
            payload={
                "actor": _actor_label(actor),
                "previous": previous,
                "requested": requested,
                "revision": next_revision,
                "phase": "authorized",
            },
        ))
        if not accepted:
            raise RuntimeError("security-suite control audit was not accepted")
        _write(data)
        reset_config_cache()
        return next_revision


def set_learning(enabled: bool, *, actor: str = "local") -> None:
    """Turn the whole self-learning loop on or off in one action -- writes each
    of ``LEARNING_TOGGLES`` to the overlay, which ``load_config()`` deep-merges,
    so every subsystem's ``enabled()`` sees it. (An env var like
    ``MAVERICK_CONSEQUENCE`` still overrides the overlay if a deployment set
    one.)"""
    with _locked():
        before = _load_overlay_for_update()
        data = json.loads(json.dumps(before))
        for section, key in LEARNING_TOGGLES:
            data.setdefault(section, {})[key] = bool(enabled)
        # The product-wide control selects the conservative unattended harness
        # profile explicitly. Core config keeps legacy partial [self_harness]
        # tables backward-compatible, so relying on an implicit default here
        # could otherwise reactivate a weaker old policy.
        harness = data.setdefault("self_harness", {})
        harness["risk_limited"] = bool(enabled)
        harness["auto_run"] = bool(enabled)
        # Persist the authorization record before publishing a control bit.
        # Runtime readers do not take the dashboard settings lock, so writing
        # first would create a window where an unaudited state was observable.
        from maverick.audit import EventKind, audit_event
        from maverick.config import reset_config_cache

        audited = audit_event(
            EventKind.LEARNING_CONTROL_CHANGED,
            _global=True,
            control="governed_learning",
            enabled=bool(enabled),
            actor=actor,
            acknowledged=False,
            phase="authorized",
        )
        if not audited:
            raise RuntimeError("global learning control audit could not be persisted")
        _write(data)
        reset_config_cache()


def _higher_precedence_ekko_enable_owner() -> str | None:
    """Return the active operator/tenant source that owns ``ekko.enable``.

    The dashboard overlay intentionally overrides the base config, but the
    optional operator and tenant sources are merged after it.  A non-table
    ``ekko`` value is also ownership for this purpose: it replaces the whole
    dashboard table and is invalid as an Ekko policy, so an expansive request
    must fail closed.
    """
    operator = os.environ.get(config.CONFIG_OVERLAY_ENV)
    sources = []
    if operator:
        sources.append((config.CONFIG_OVERLAY_ENV, Path(operator).expanduser()))
    tenant = config.tenant_config_path()
    if tenant is not None:
        sources.append(("tenant config", tenant))
    for owner, path in sources:
        source = config.load_config(path)
        if "ekko" not in source:
            continue
        section = source.get("ekko")
        if not isinstance(section, dict) or "enable" in section:
            return owner
    return None


def set_ekko(enabled: bool, *, actor: str = "local") -> dict:
    """Set the deployment-global, default-off Ekko request bit.

    The dashboard owns only ``[ekko] enable``.  Capture scope, retention,
    provider egress, device enrollment, and endpoint permissions remain in the
    client's higher-authority configuration and enrollment record.

    As with DGM, the durable global audit authorization is written before the
    atomic overlay commit.  Environment ownership is always rejected.  Before
    an expansive ON commit, active operator/tenant sources are inspected and
    an existing ``ekko.enable`` owner is rejected without publishing a latent
    dashboard ON bit.  A post-commit effective-state check remains as a race
    defense and rolls back a change superseded while this call was in flight.
    """
    from maverick.ekko_control import control_barrier

    # Serialize the complete authorization change with collector commits. A
    # disable call therefore cannot return while an append authorized under
    # the previous deployment policy is still in flight.
    with control_barrier(), _locked():
        from maverick.config import config_source_errors, get_ekko, reset_config_cache

        before_status = get_ekko()
        if "MAVERICK_EKKO" in os.environ:
            raise PermissionError("MAVERICK_EKKO owns the Ekko control")
        if config_source_errors():
            raise PermissionError("an active config source is invalid")
        if enabled:
            owner = _higher_precedence_ekko_enable_owner()
            if owner is not None:
                raise PermissionError(
                    f"higher-precedence {owner} owns the Ekko control"
                )
        before = _load_overlay_for_update()
        data = json.loads(json.dumps(before))
        data.setdefault("ekko", {})["enable"] = bool(enabled)
        from maverick.audit import EventKind, audit_event

        audited = audit_event(
            EventKind.EKKO_CONTROL_CHANGED,
            _global=True,
            control="ekko",
            enabled=bool(enabled),
            actor=actor,
            previous_enabled=bool(before_status.get("enable", False)),
            phase="authorized",
        )
        if not audited:
            raise RuntimeError("global Ekko control audit could not be persisted")

        _write(data)
        reset_config_cache()
        try:
            after = get_ekko()
        except Exception:
            _write(before)
            reset_config_cache()
            raise
        if bool(after.get("enable", False)) != bool(enabled):
            _write(before)
            reset_config_cache()
            raise PermissionError("a higher-precedence deployment policy owns Ekko")
        return after


def set_flow_autonomy(*, auto_evolve: bool | None = None,
                      auto_apply: bool | None = None) -> None:
    """Override the flow self-improvement loop's [flows] knobs in the overlay.

    Kept OFF the blanket ``set_learning`` list on purpose: autonomously rewriting
    a live workflow is a heavier consequence than the passive learning that button
    controls, so it gets its own explicit opt-in. ``auto_apply`` only takes effect
    while ``auto_evolve`` is on (the forward arrow needs the revert safety net);
    only the fields passed are changed. An env var (``MAVERICK_FLOWS_AUTO``) still
    overrides the overlay if a deployment set one."""
    with _locked():
        data = _load_overlay_for_update()
        flows = data.setdefault("flows", {})
        if auto_evolve is not None:
            flows["auto_evolve"] = bool(auto_evolve)
        if auto_apply is not None:
            flows["auto_apply"] = bool(auto_apply)
        if not bool(flows.get("auto_evolve", False)):
            flows["auto_apply"] = False
        _write(data)


def set_value_assumptions(*, hourly_rate: float | None = None,
                          hours_per_task: float | None = None,
                          currency: str | None = None,
                          enable: bool | None = None,
                          departments: dict | None = None) -> None:
    """Persist the client's cost/value assumptions ([value]) in the overlay.

    Only the fields passed are changed. ``departments`` maps department name ->
    {hourly_rate, hours_per_task}; an entry whose dict is empty/None REMOVES
    that department's override (reverts it to the global assumption). Rates and
    hours must be non-negative -- the Savings page is a claim a client will
    repeat to their CFO, so junk never persists."""
    def _pos(v: object, label: str) -> float:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number") from exc
        if f < 0:
            raise ValueError(f"{label} must be >= 0")
        return f

    with _locked():
        data = _load_overlay_for_update()
        val = data.setdefault("value", {})
        if hourly_rate is not None:
            val["hourly_rate"] = _pos(hourly_rate, "hourly_rate")
        if hours_per_task is not None:
            val["hours_per_task"] = _pos(hours_per_task, "hours_per_task")
        if currency is not None and currency.strip():
            val["currency"] = currency.strip().upper()[:8]
        if enable is not None:
            val["enable"] = bool(enable)
        for name, ov in (departments or {}).items():
            dep = val.setdefault("departments", {})
            if not ov:
                dep.pop(str(name), None)
                continue
            row = {}
            if ov.get("hourly_rate") is not None:
                row["hourly_rate"] = _pos(ov["hourly_rate"],
                                          f"{name} hourly_rate")
            if ov.get("hours_per_task") is not None:
                row["hours_per_task"] = _pos(ov["hours_per_task"],
                                             f"{name} hours_per_task")
            if row:
                dep[str(name)] = {**dep.get(str(name), {}), **row}
        if not val.get("departments"):
            val.pop("departments", None)
        if not val:
            data.pop("value", None)
        _write(data)


def _raw_config_providers() -> dict:
    """Providers from config.toml ONLY (no overlay), to attribute the source."""
    try:
        return config._load_config_file(config.config_path()).get("providers", {}) or {}
    except Exception:
        return {}


def _mask(secret: str) -> str:
    s = str(secret)
    return ("•" * 4 + s[-4:]) if len(s) > 4 else "••••"


def state() -> dict:
    """Redacted snapshot for the settings page. NEVER returns a raw key."""
    overlay = load_overlay()
    ov_providers = overlay.get("providers") or {}
    raw_providers = _raw_config_providers()
    providers = []
    for p in PROVIDERS:
        name = p["name"]
        ov = ov_providers.get(name) or {}
        raw = raw_providers.get(name) or {}
        env_set = any(os.environ.get(v) for v in p["env"])
        key = ov.get("api_key") or raw.get("api_key")
        base = ov.get("base_url") or raw.get("base_url")
        if ov.get("api_key") or ov.get("base_url"):
            via = "dashboard"
        elif raw.get("api_key") or raw.get("base_url"):
            via = "config.toml"
        elif env_set:
            via = "environment"
        else:
            via = None
        providers.append({
            "name": name, "label": p["label"], "base_url_field": p["base_url"],
            "configured": bool(key or base or env_set), "via": via,
            "key_hint": _mask(key) if (ov.get("api_key") or raw.get("api_key")) else None,
            "base_url": base or "",
            "env_hint": ", ".join(p["env"]) if p["env"] else "",
            "dashboard_set": bool(ov),
        })
    caps_eff = config.get_capabilities()
    feats_eff = config.get_features()
    ov_caps = overlay.get("capabilities") or {}
    ov_feats = overlay.get("features") or {}
    capabilities = [
        {"name": k, "enabled": bool(caps_eff.get(k, v)),
         "overridden": k in ov_caps, "info": CAPABILITY_INFO.get(k, "")}
        for k, v in CAPABILITY_DEFAULTS.items()
    ]
    features = [
        {"name": k, "enabled": bool(feats_eff.get(k, v)),
         "overridden": k in ov_feats, "info": FEATURE_INFO.get(k, "")}
        for k, v in FEATURE_DEFAULTS.items()
    ]
    return {"providers": providers, "capabilities": capabilities, "features": features}


