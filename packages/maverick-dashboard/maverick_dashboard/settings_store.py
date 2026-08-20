"""Encrypted provider credentials for the compact firm settings page.

Model allow-lists, role pins, and per-goal budget caps live in the signed
runtime-overrides store.  This module owns only the provider table in
``dashboard-config.toml``; unsupported historical product toggles cannot be
written through the dashboard.
"""
from __future__ import annotations

import json
import os
import threading

from maverick import config

_SETTINGS_LOCK = threading.Lock()


class SecuritySuiteConfigUnavailable(RuntimeError):
    """The provider overlay cannot be read or sealed without risking data."""


PROVIDERS: list[dict] = [
    {
        "name": "anthropic",
        "label": "Anthropic (Claude)",
        "env": ["ANTHROPIC_API_KEY"],
        "base_url": False,
    },
    {
        "name": "openai",
        "label": "OpenAI",
        "env": ["OPENAI_API_KEY"],
        "base_url": False,
    },
    {
        "name": "gemini",
        "label": "Google Gemini",
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "base_url": False,
    },
    {
        "name": "openrouter",
        "label": "OpenRouter",
        "env": ["OPENROUTER_API_KEY"],
        "base_url": False,
    },
    {
        "name": "moonshot",
        "label": "Moonshot",
        "env": ["MOONSHOT_API_KEY"],
        "base_url": False,
    },
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "env": ["DEEPSEEK_API_KEY"],
        "base_url": False,
    },
    {
        "name": "xai",
        "label": "xAI (Grok)",
        "env": ["XAI_API_KEY", "GROK_API_KEY"],
        "base_url": False,
    },
    {
        "name": "ollama",
        "label": "Ollama (self-hosted)",
        "env": [],
        "base_url": True,
    },
    {
        "name": "vllm",
        "label": "vLLM / OpenAI-compatible",
        "env": ["VLLM_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL"],
        "base_url": True,
    },
]
_PROVIDER_NAMES = {provider["name"] for provider in PROVIDERS}


def _locked():
    from contextlib import ExitStack

    from maverick.file_lock import cross_process_lock

    stack = ExitStack()
    stack.enter_context(_SETTINGS_LOCK)
    stack.enter_context(cross_process_lock(config.dashboard_overrides_path()))
    return stack


def _tomllib():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib


def _reveal_provider_secrets(value: dict) -> dict:
    providers = value.get("providers")
    if not isinstance(providers, dict):
        return value
    from maverick.crypto_at_rest import unseal_from_str

    for provider in providers.values():
        if isinstance(provider, dict) and "api_key" in provider:
            provider["api_key"] = unseal_from_str(provider.get("api_key"))
    return value


def _seal_secret(value: str) -> str:
    from maverick.crypto_at_rest import at_rest_enabled, seal_to_str

    if not at_rest_enabled():
        raise SecuritySuiteConfigUnavailable(
            "refusing to persist dashboard secrets while at-rest encryption is disabled"
        )
    return seal_to_str(value)


def _read_overlay(*, strict: bool) -> dict:
    path = config.dashboard_overrides_path()
    try:
        with open(path, "rb") as handle:
            value = _tomllib().load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if not strict:
            return {}
        raise SecuritySuiteConfigUnavailable(
            "dashboard settings are unreadable; settings were not changed"
        ) from exc
    if not isinstance(value, dict):
        if not strict:
            return {}
        raise SecuritySuiteConfigUnavailable(
            "dashboard settings are unreadable; settings were not changed"
        )
    try:
        return _reveal_provider_secrets(value)
    except Exception as exc:
        if not strict:
            return {}
        raise SecuritySuiteConfigUnavailable(
            "dashboard secrets could not be decrypted; settings were not changed"
        ) from exc


def load_overlay() -> dict:
    return _read_overlay(strict=False)


def _load_overlay_for_update() -> dict:
    return _read_overlay(strict=True)


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def _dump(data: dict) -> str:
    """Serialize only the retained provider table, dropping retired controls."""
    lines = [
        "# Firm dashboard provider credentials. Managed in Settings.",
        "# Keys are sealed at rest and are never rendered back to a browser.",
        "",
    ]
    for name in sorted(data.get("providers") or {}):
        provider = (data.get("providers") or {}).get(name)
        if name not in _PROVIDER_NAMES or not isinstance(provider, dict):
            continue
        api_key = str(provider.get("api_key") or "").strip()
        base_url = str(provider.get("base_url") or "").strip()
        if not api_key and not base_url:
            continue
        lines.append(f"[providers.{name}]")
        if api_key:
            lines.append(f"api_key = {_toml_str(_seal_secret(api_key))}")
        if base_url:
            lines.append(f"base_url = {_toml_str(base_url)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write(data: dict) -> None:
    from maverick.file_lock import atomic_write_text

    atomic_write_text(config.dashboard_overrides_path(), _dump(data))


def set_provider(
    name: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> None:
    if name not in _PROVIDER_NAMES:
        raise ValueError("unknown provider")
    with _locked():
        data = _load_overlay_for_update()
        provider = data.setdefault("providers", {}).setdefault(name, {})
        if api_key and api_key.strip():
            provider["api_key"] = api_key.strip()
        if base_url and base_url.strip():
            provider["base_url"] = base_url.strip()
        if not provider:
            data["providers"].pop(name, None)
        _write(data)


def clear_provider(name: str) -> None:
    if name not in _PROVIDER_NAMES:
        raise ValueError("unknown provider")
    with _locked():
        data = _load_overlay_for_update()
        (data.get("providers") or {}).pop(name, None)
        _write(data)


def _raw_config_providers() -> dict:
    try:
        return config._load_config_file(config.config_path()).get("providers", {}) or {}
    except Exception:
        return {}


def _mask(secret: str) -> str:
    value = str(secret)
    return ("•" * 4 + value[-4:]) if len(value) > 4 else "••••"


def state() -> dict:
    """Return provider status with raw credentials permanently redacted."""
    overlay = load_overlay()
    overlay_providers = overlay.get("providers") or {}
    raw_providers = _raw_config_providers()
    providers: list[dict] = []
    for spec in PROVIDERS:
        name = spec["name"]
        saved = overlay_providers.get(name) or {}
        raw = raw_providers.get(name) or {}
        env_set = any(os.environ.get(variable) for variable in spec["env"])
        key = saved.get("api_key") or raw.get("api_key")
        base_url = saved.get("base_url") or raw.get("base_url")
        if saved.get("api_key") or saved.get("base_url"):
            via = "dashboard"
        elif raw.get("api_key") or raw.get("base_url"):
            via = "config.toml"
        elif env_set:
            via = "environment"
        else:
            via = None
        providers.append(
            {
                "name": name,
                "label": spec["label"],
                "base_url_field": spec["base_url"],
                "configured": bool(key or base_url or env_set),
                "via": via,
                "key_hint": _mask(key) if key and not env_set else None,
                "base_url": base_url or "",
                "env_hint": ", ".join(spec["env"]),
                "dashboard_set": bool(saved),
            }
        )
    return {"providers": providers}
