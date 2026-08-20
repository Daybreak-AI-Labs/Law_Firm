"""Maverick interactive installer.

Configures Maverick for a fresh install. Sets up:
  - one explicitly pinned AI provider and model
  - safety profile
  - sandbox backend
  - budget caps
  - API keys (stored in ~/.maverick/.env, referenced from config.toml via ${VAR})

Writes ~/.maverick/config.toml and ~/.maverick/.env. The agent reads from there.

v0.1.1 additions (council UX feedback):
  - Preflight: Python version, write perms, optional docker check
  - API key validation: pings Anthropic with the entered key before save
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover -- Py 3.10 CI matrix
    import tomli as tomllib  # type: ignore[no-redef]

from rich.console import Console
from rich.panel import Panel

try:
    import questionary
except ImportError:  # pragma: no cover
    questionary = None  # type: ignore

from . import models as catalog

CONFIG_DIR = Path.home() / ".maverick"
CONFIG_FILE = CONFIG_DIR / "config.toml"
ENV_FILE = CONFIG_DIR / ".env"

console = Console()


# Channel catalog: (id, label, env_vars_needed)


# Ordered advanced-flow steps, mirroring the pick_* sequence in run().
# Purely a progress-bar aid: changing this list never changes the config.
STEPS: list[tuple[str, str]] = [
    ("deployment", "Deployment"),
    ("providers", "Providers"),
    ("run_model", "Model"),
    ("safety", "Safety"),
    ("signed_skills", "Signed skills"),
    ("budget", "Budget"),
    ("sandbox", "Sandbox"),
    ("self_learning", "Self-learning"),
    ("flows", "Flow engine"),
    ("knowledge", "Knowledge RAG"),
    ("oauth_vault", "OAuth token vault"),
    ("durable", "Durable execution"),
    ("assessments", "Assessment assists"),
    ("security_suite", "Security & GRC"),
    ("advanced", "Advanced reasoning"),
    ("web_search", "Web search"),
    ("tool_acl", "Tool ACL"),
    ("rate_limits", "Rate limits"),
    ("retention", "Retention"),
    ("persona", "Persona"),
]


def _step_indicator(index: int, *, done: list[str] | None = None) -> str:
    """Format the ``Step N/M`` progress line for the ``index``-th step
    (1-based), optionally trailed by a breadcrumb of completed labels.

    Returns plain text (no Rich markup): styling is applied by the caller
    via ``console.print(..., style=...)`` so the literal "Step N/M" text
    stays contiguous in rendered output instead of being fragmented by
    inline ANSI codes. Defined as a pure helper so tests can assert the
    formatting without driving the whole wizard.
    """
    total = len(STEPS)
    label = STEPS[index - 1][1] if 1 <= index <= total else ""
    line = f"Step {index}/{total} {label}"
    if done:
        line += f"  ({' > '.join(done)})"
    return line


def _safe_int(s: str, *, default: int) -> int:
    """``int()`` that doesn't crash on whitespace, empty, or junk input."""
    try:
        return int(str(s or "").strip())
    except (TypeError, ValueError):
        return default


def _csv_list(raw: str, *, lower: bool = False) -> list[str]:
    """Split a comma-separated prompt answer into trimmed, non-empty items."""
    items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    return [x.lower() for x in items] if lower else items


def _safe_float(s: str, *, default: float) -> float:
    """``float()`` that doesn't crash on whitespace, empty, or junk input."""
    try:
        return float(str(s or "").strip())
    except (TypeError, ValueError):
        return default


# ---------- prompt primitives ----------

def _ask(question: Any) -> Any:
    """Run a questionary prompt, treating a ``None`` answer as an abort.

    questionary's ``.ask()`` returns ``None`` when the user presses
    Ctrl-C / Ctrl-D or when there's no interactive TTY (e.g. stdin is a
    pipe). Every call site then did ``.split()`` / ``.strip()`` on the
    result and crashed with an opaque ``AttributeError``. Convert that
    to ``KeyboardInterrupt`` so the entry point prints a clean "Aborted"
    message and exits 130 instead of dumping a traceback.
    """
    answer = question.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _q_select(message: str, choices: list[str], default: str | None = None) -> str:
    if questionary is None:
        print(message)
        for i, c in enumerate(choices):
            marker = "*" if default == c else " "
            print(f"  {marker} {i+1}) {c}")
        while True:
            choice = input("> ").strip()
            if not choice and default:
                return default
            if choice.isdigit() and 1 <= int(choice) <= len(choices):
                return choices[int(choice) - 1]
    return _ask(questionary.select(message, choices=choices, default=default))


def _q_text(message: str, default: str = "") -> str:
    if questionary is None:
        val = input(f"{message} [{default}]: ").strip()
        return val or default
    return _ask(questionary.text(message, default=default))


def _q_secret(message: str) -> str:
    if questionary is None:
        import getpass

        return getpass.getpass(f"{message}: ").strip()
    # Route through _ask so Ctrl-C / Ctrl-D / non-TTY (questionary returns
    # None) raises KeyboardInterrupt and aborts the wizard, like every other
    # prompt. The old `.ask() or ""` swallowed the abort into "", which
    # callers read as "skip this key" -- so Ctrl-C silently continued.
    return _ask(questionary.password(message)) or ""


def _q_checkbox(message: str, choices: list[str], default: list[str] | None = None) -> list[str]:
    if questionary is None:
        print(f"{message} (comma-separated numbers, blank = none)")
        for i, c in enumerate(choices):
            marker = "*" if default and c in default else " "
            print(f"  {marker} {i+1}) {c}")
        raw = input("> ").strip()
        if not raw:
            return default or []
        picks = [c.strip() for c in raw.split(",")]
        return [choices[int(p) - 1] for p in picks if p.isdigit() and 1 <= int(p) <= len(choices)]
    # questionary.checkbox ignores `default` (it's documented "not used by
    # checkbox"). To actually pre-select the defaults, wrap them as
    # pre-checked Choice objects whose value is the title string -- so
    # callers still get back the same strings they passed in.
    default_set = set(default or [])
    q_choices: Any = (
        [questionary.Choice(c, checked=c in default_set) for c in choices]
        if default_set else choices
    )
    return _ask(questionary.checkbox(message, choices=q_choices))


def _q_confirm(message: str, default: bool = True) -> bool:
    if questionary is None:
        val = input(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not val:
            return default
        return val.startswith("y")
    return _ask(questionary.confirm(message, default=default))


# ---------- preflight ----------

def preflight() -> bool:
    """Check the environment before asking any questions.

    Returns True if all critical checks pass. Warnings are shown but
    don't block the wizard.
    """
    console.print("\n[dim]Checking your environment...[/dim]")
    all_ok = True

    # Python version
    if sys.version_info < (3, 10):
        console.print(f"[red]✗[/red] Python 3.10+ required (you have {sys.version.split()[0]})")
        all_ok = False
    else:
        console.print(f"[green]✓[/green] Python {sys.version.split()[0]}")

    # Config dir writable
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        test_file = CONFIG_DIR / ".write-test"
        test_file.write_text("ok")
        test_file.unlink()
        console.print(f"[green]✓[/green] {CONFIG_DIR} is writable")
    except (PermissionError, OSError) as e:
        console.print(f"[red]✗[/red] Can't write to {CONFIG_DIR}: {e}")
        all_ok = False

    # Docker (advisory only -- only matters if user picks docker sandbox)
    if shutil.which("docker"):
        try:
            subprocess.run(
                ["docker", "version"],
                capture_output=True, timeout=5, check=True,
            )
            console.print("[green]✓[/green] Docker is running")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            console.print(
                "[yellow]![/yellow] Docker installed but daemon isn't responding "
                "(only matters if you pick the docker sandbox)"
            )
    else:
        console.print(
            "[yellow]![/yellow] Docker not installed "
            "(only matters if you pick the docker sandbox)"
        )

    return all_ok


# ---------- validators ----------

def _validate_anthropic_key(key: str) -> tuple[bool, str]:
    """Ping Anthropic with the key. Returns (ok, message).

    Skip the prefix check: Anthropic now ships admin keys, batch keys,
    and several legacy formats. The API ping handles whatever shape
    the key takes.
    """
    if not key.strip():
        return False, "empty key"
    try:
        import anthropic
    except ImportError:
        return True, "anthropic SDK not installed -- skipping validation"
    try:
        client = anthropic.Anthropic(api_key=key, timeout=5.0)
        # Minimal call -- list available models is enough to verify auth.
        list(client.models.list(limit=1))
        return True, "validated"
    except anthropic.AuthenticationError:
        return False, "API rejected the key"
    except Exception as e:
        return True, f"validation skipped: {type(e).__name__}"


def _validate_openai_key(key: str) -> tuple[bool, str]:
    if not key.strip():
        return False, "empty key"
    # Azure OpenAI keys are 32-char hex with no prefix; OpenAI ships
    # sk-, sk-proj-, sk-svcacct-. Just ping the API and let it tell us.
    try:
        from openai import AuthenticationError, OpenAI
    except ImportError:
        return True, "openai SDK not installed -- skipping validation"
    try:
        client = OpenAI(api_key=key, timeout=5.0)
        list(client.models.list().data[:1])
        return True, "validated"
    except AuthenticationError:
        return False, "API rejected the key"
    except Exception as e:
        return True, f"validation skipped: {type(e).__name__}"


def _validate_openai_compat_key(key: str, base_url: str, label: str) -> tuple[bool, str]:
    """For openai-compatible endpoints (Moonshot, DeepSeek, xAI, Gemini)."""
    if not key:
        return False, "empty key"
    try:
        from openai import AuthenticationError, OpenAI
    except ImportError:
        return True, "openai SDK not installed -- skipping validation"
    try:
        client = OpenAI(api_key=key, base_url=base_url, timeout=5.0)
        list(client.models.list().data[:1])
        return True, f"validated against {label}"
    except AuthenticationError:
        return False, f"{label} rejected the key"
    except Exception as e:
        # Network / route errors are non-fatal -- saving still useful.
        return True, f"validation skipped: {type(e).__name__}"


def _validate_moonshot_key(key: str) -> tuple[bool, str]:
    return _validate_openai_compat_key(
        key, "https://api.moonshot.ai/v1", "Moonshot",
    )


def _validate_deepseek_key(key: str) -> tuple[bool, str]:
    return _validate_openai_compat_key(
        key, "https://api.deepseek.com/v1", "DeepSeek",
    )


def _validate_xai_key(key: str) -> tuple[bool, str]:
    return _validate_openai_compat_key(
        key, "https://api.x.ai/v1", "xAI",
    )


def _validate_gemini_key(key: str) -> tuple[bool, str]:
    return _validate_openai_compat_key(
        key, "https://generativelanguage.googleapis.com/v1beta/openai/", "Gemini",
    )


def _validation_marker(ok: bool, msg: str) -> str:
    """Status marker for a key-validation result. A SKIPPED check (SDK missing /
    couldn't reach the API) is NOT a confirmed key -- render it distinctly so a
    green "ok" never appears next to "validation skipped"."""
    if not ok:
        return "[red]x[/red]"
    if "skipped" in msg.lower():
        return "[yellow]?[/yellow]"
    return "[green]ok[/green]"


_VALIDATORS = {
    "ANTHROPIC_API_KEY": _validate_anthropic_key,
    "OPENAI_API_KEY":    _validate_openai_key,
    "MOONSHOT_API_KEY":  _validate_moonshot_key,
    "DEEPSEEK_API_KEY":  _validate_deepseek_key,
    "XAI_API_KEY":       _validate_xai_key,
    "GEMINI_API_KEY":    _validate_gemini_key,
    # Channel tokens validated when 'maverick serve' starts (less time-critical).
}


# ---------- validation cache ----------

VALIDATION_CACHE_PATH = CONFIG_DIR / "validation-cache.json"
_VALIDATION_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _key_fingerprint(env_name: str, key: str) -> str:
    import hashlib
    digest = hashlib.sha256(f"{env_name}\x00{key}".encode()).hexdigest()
    return digest[:32]


def _load_validation_cache() -> dict[str, Any]:
    try:
        import json as _json
        return _json.loads(VALIDATION_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_validation_cache(cache: dict[str, Any]) -> None:
    import json as _json
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        VALIDATION_CACHE_PATH.write_text(_json.dumps(cache, default=str))
        try:
            os.chmod(VALIDATION_CACHE_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _cached_validation(env_name: str, key: str) -> tuple[bool, str] | None:
    """Return cached (ok, msg) if the same key was validated within the TTL."""
    import time as _time
    if not key.strip():
        return None
    cache = _load_validation_cache()
    fp = _key_fingerprint(env_name, key)
    entry = cache.get(fp)
    if not entry:
        return None
    ts = float(entry.get("ts", 0))
    if (_time.time() - ts) > _VALIDATION_TTL_SECONDS:
        return None
    return bool(entry.get("ok", False)), str(entry.get("msg", "cached"))


def _remember_validation(env_name: str, key: str, ok: bool, msg: str) -> None:
    import time as _time
    if not key.strip():
        return
    cache = _load_validation_cache()
    cache[_key_fingerprint(env_name, key)] = {
        "ts": _time.time(),
        "ok": ok,
        "msg": msg,
    }
    _save_validation_cache(cache)


# ---------- error UI ----------

def show_bad_key_error(env_name: str, msg: str) -> None:
    """Council UX seat error screen #1: provider rejected the key."""
    console.print()
    console.print(Panel.fit(
        f"[bold]That {env_name.split('_')[0].title()} key didn't work.[/bold]\n\n"
        f"{msg}\n\n"
        "Common causes:\n"
        "  1. Typo (the secret is long; copy/paste, don't retype).\n"
        "  2. The key was deleted from your account.\n"
        "  3. Billing isn't set up on the provider.",
        border_style="red",
    ))


def show_network_error(provider: str, exception_type: str) -> None:
    """Council UX seat error screen #2: validator couldn't reach the provider."""
    console.print()
    console.print(Panel.fit(
        f"[bold]Couldn't reach {provider} to check the key ({exception_type}).[/bold]\n\n"
        "Usually a network block or proxy. Your key is saved either way;\n"
        "if it's wrong the first goal you run will say so.",
        border_style="yellow",
    ))


def show_install_failure(exc: BaseException) -> None:
    """Council UX seat error screen #3: catch-all for unexpected setup failures."""
    console.print()
    console.print(Panel.fit(
        "[bold]Setup hit a problem and stopped.[/bold]\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Nothing was changed. Try again, or report the issue with the\n"
        "diagnostic output of [bold]maverick doctor[/bold].",
        border_style="red",
    ))


# ---------- wizard steps ----------

def welcome() -> None:
    console.print(Panel.fit(
        "[bold]Maverick installer[/bold]\n\n"
        "Next you'll pick a setup mode: a quick consumer flow (a few\n"
        "questions, firm-safe defaults) or advanced (configure models,\n"
        "matter safeguards, safety, and budget). Re-run any time with\n"
        "[bold]maverick init[/bold].",
        border_style="cyan",
    ))


def pick_deployment() -> str:
    choices = [
        "local  - Reviewed virtual environment on this host",
        "docker - Operator-built local image (immutable digest required in secure mode)",
        "vps    - Firm-controlled server with authenticated HTTPS",
    ]
    # Default to the previously recorded target on a re-run of `maverick init`
    # (what [deployment] type was written for).
    default = None
    try:
        from maverick.config import get_deployment
        prior = get_deployment().get("type", "")
        default = next((c for c in choices if c.split()[0] == prior), None)
    except Exception:  # pragma: no cover -- never block the wizard
        default = None
    pick = _q_select("Where will Maverick run?", choices, default=default)
    deployment = pick.split()[0]
    if deployment == "docker":
        console.print(
            "[dim]No public Maverick image is published or trusted. Build from "
            "the reviewed commit with a digest-pinned base image; secure mode "
            "also requires [sandbox] image to be an immutable sha256 reference.[/dim]"
        )
    return deployment


def pick_providers() -> list[str]:
    choices = []
    for prov_id, info in catalog.PROVIDERS.items():
        tag = "[ready]" if info["status"] == "ready" else "[v0.2]"
        choices.append(f"{prov_id:10} {tag} - {info['label']}")

    pick = _q_select(
        "Which AI provider may receive this firm's model requests?",
        choices,
        default=None,
    )
    return [pick.split()[0]]


def _model_choices(providers: list[str]) -> list[str]:
    """Return exact ``provider:model`` choices for one selected provider."""
    if len(providers) != 1 or providers[0] not in catalog.PROVIDERS:
        return []
    provider = providers[0]
    info = catalog.PROVIDERS[provider]
    tag = "" if info["status"] == "ready" else " [v0.2]"
    return [
        f"{provider}:{model['id']}{tag}  - {model['notes']}"
        for model in info["models"]
    ]


def _validate_run_model(run_model: str, providers: list[str]) -> str:
    """Validate the single run-wide model pin emitted by the installer.

    Secure firm execution has no implicit provider or role-specific routing.
    The installer therefore accepts exactly one selected provider and one
    catalogued ``provider:model`` value for every setup path.
    """
    spec = str(run_model or "").strip()
    if len(providers) != 1:
        raise ValueError("exactly one AI provider must be selected")
    if ":" not in spec or any(char.isspace() for char in spec):
        raise ValueError("models.default must be an exact provider:model value")
    provider, model = spec.split(":", 1)
    if provider != providers[0] or not model:
        raise ValueError("models.default must use the selected provider")
    configured = catalog.PROVIDERS.get(provider)
    if configured is None:
        raise ValueError(f"unknown AI provider: {provider}")
    valid_models = {str(item["id"]) for item in configured.get("models", [])}
    if model not in valid_models:
        raise ValueError(f"unknown model for {provider}: {model}")
    return spec


def pick_run_model(providers: list[str]) -> str:
    """Require one explicit run-wide model pin, with no vendor default."""
    choices = _model_choices(providers)
    if not choices:
        raise ValueError("select exactly one supported AI provider first")
    console.print()
    console.print(
        "[bold]Pick the one model this installation may use.[/bold] "
        "Secure firm runs do not silently reroute to another provider.\n"
    )
    pick = _q_select("Run-wide model (provider:model):", choices, default=None)
    return _validate_run_model(pick.split()[0], providers)


def pick_safety() -> dict[str, Any]:
    pick = _q_select(
        "Safety profile:",
        [
            "strict     - Block on any medium+ threat. Best for sensitive use.",
            "balanced   - Block on high+ threats. Recommended default.",
            "permissive - Block only on critical threats. For research/experimentation.",
            "off        - No safety scanning. NOT recommended.",
        ],
        default="balanced   - Block on high+ threats. Recommended default.",
    )
    profile = pick.split()[0]
    threshold = {
        "strict": "medium",
        "balanced": "high",
        "permissive": "critical",
        "off": "critical",
    }[profile]
    # Agent compartments: when one agent's scan blocks a threat, record its
    # signature so the rest of the swarm is immune to the same attack for the
    # run. Moot with safety off (nothing scans). Off by default.
    compartments = False
    if profile != "off":
        compartments = _q_confirm(
            "  Enable agent compartments (one agent's blocked threat immunizes "
            "the rest of the swarm for the run)?",
            default=False,
        )
    return {
        "profile": profile,
        "block_threshold": threshold,
        "scan_input": profile != "off",
        "scan_tool_calls": profile != "off",
        "scan_output": profile != "off",
        "compartments": compartments,
    }


def pick_signed_skills() -> dict[str, Any]:
    """Optional Ed25519 signing policy for installed skills.

    Returns a dict written under ``[skills]``. Defaults keep current
    behavior (no trusted publishers, unsigned skills allowed)."""
    console.print()
    console.print(
        "[dim]Signed skills: a publisher can sign a SKILL.md with an Ed25519 "
        "key. Paste trusted publisher public keys (hex) to verify against; "
        "leave blank to skip.[/dim]"
    )
    raw = _q_text("  Trusted skill publisher pubkeys (comma-separated hex)", default="")
    trusted = _csv_list(raw)
    require = _q_confirm(
        "  Reject unsigned skills (only install signed + trusted ones)?",
        default=False,
    )
    return {
        "trusted_pubkeys": trusted,
        "require_signed": require,
    }


def pick_budget() -> dict[str, Any]:
    console.print()
    console.print("[dim]Per-run caps. Edit later in ~/.maverick/config.toml.[/dim]")
    budget: dict[str, Any] = {
        "max_dollars": _safe_float(
            _q_text("  Max $ per run", default="5.0"), default=5.0,
        ),
        "max_wall_seconds": _safe_float(
            _q_text("  Max wall-clock seconds per run", default="3600"),
            default=3600.0,
        ),
        "max_tool_calls": _safe_int(
            _q_text("  Max tool calls per run", default="500"), default=500,
        ),
    }
    # Billing-grade accounting is the safe default. Preserve an explicit false
    # for legacy/custom gateways, where estimates remain provenance-labelled.
    budget["strict_pricing"] = _q_confirm(
        "  Strict pricing? Fail closed if a model has no verified price "
        "(recommended; choose No only for estimate-only custom gateways)",
        default=True,
    )
    return budget


def pick_self_learning() -> dict[str, Any]:
    """Configure the retained governed local-improvement policy."""
    console.print()
    console.print(
        "[dim]Local learning retains governed reflexion, rehearsal, and "
        "distillation. It never installs remote skills or executable tools. "
        "Extra provider calls remain a separate opt-in.[/dim]"
    )
    enable = _q_confirm("Enable governed self-learning?", default=True)
    if not enable:
        return {"enable": False}
    allow_provider_egress = _q_confirm(
        "  Allow learning helpers to make EXTRA model calls with redacted task "
        "or result text (may use another configured provider)?",
        default=False,
    )
    distill_local = _q_confirm(
        "  Distill successful runs into local skills? After a successful run, "
        "save a reusable skill under ~/.maverick/learned-skills.",
        default=True,
    )
    return {
        "enable": True,
        "allow_provider_egress": allow_provider_egress,
        "distill_local": distill_local,
    }


def pick_flows() -> dict[str, Any]:
    """Opt-in to the flow engine (multi-step visual workflows).

    Off by default. When on, the dashboard's visual designer can build and run
    flows -- a deterministic graph of agent/action/branch/loop/approval nodes,
    schedulable on a cron, with self-rewrite + rollback. When a flow pauses for
    approval it can post signed Approve/Reject links to a channel; those links
    need the dashboard's externally-reachable base URL, collected here and
    written as ``[flows] public_url``. Optionally lets the loop self-correct
    (``[flows] auto_evolve``). Returns a dict written under ``[flows]``.
    """
    console.print()
    console.print(
        "[dim]The flow engine runs multi-step visual workflows (agent + action + "
        "branch + loop + approval), schedulable on a cron, with self-rewrite and "
        "rollback. OFF by default.[/dim]"
    )
    if not _q_confirm("Enable the flow engine?", default=False):
        return {"enable": False}
    out: dict[str, Any] = {"enable": True}
    console.print(
        "[dim]Retries can recover transient agent/tool failures, but every retry "
        "can spend provider budget or repeat side effects. Keep the per-node cap "
        "small unless operations explicitly need more.[/dim]"
    )
    retry_cap = _q_text("Max retries per flow node", default="5").strip()
    try:
        out["max_node_retries"] = max(0, min(50, int(retry_cap or "5")))
    except ValueError:
        out["max_node_retries"] = 5
    console.print(
        "[dim]For signed browser approval links, the dashboard needs its "
        "externally-reachable base URL. Leave blank to keep approvals inside "
        "the dashboard.[/dim]"
    )
    url = _q_text("Public base URL for approval links (e.g. https://ops.acme.com)",
                  default="").strip()
    if url:
        out["public_url"] = url
    console.print(
        "[dim]Self-correction lets a scheduled pass autonomously revert a node "
        "rewrite whose grounded outcomes measurably regressed -- the loop undoes "
        "its own bad changes. Off by default; surfaced proposals + human apply "
        "work without it.[/dim]"
    )
    if _q_confirm("Let flows self-correct (autonomous revert of a regressed rewrite)?",
                  default=False):
        out["auto_evolve"] = True
        # Full autonomy: also APPLY a proposal forward (soften a failing action to
        # an agent), not just revert. Strictly opt-in on top of self-correction,
        # since a bad apply is auto-reverted only if the revert side is on.
        if _q_confirm("Also let flows autonomously APPLY a learned rewrite (not just revert)?",
                      default=False):
            out["auto_apply"] = True
    return out


def pick_knowledge() -> dict[str, Any]:
    """Opt-in to per-domain vector RAG (the maverick-knowledge package).

    Off by default; the kernel never requires maverick-knowledge. When on,
    ``embedder`` selects an on-box provider (local or deterministic); the
    vector store is the embedded SQLite one.
    Returns a dict written under ``[knowledge]``.
    """
    console.print()
    console.print(
        "[dim]Knowledge RAG gives the agent per-domain document recall via a "
        "vector store (the knowledge_search tool). Needs the maverick-knowledge "
        "package + an embedder. OFF by default.[/dim]"
    )
    if not _q_confirm("Enable per-domain knowledge RAG?", default=False):
        return {"enable": False}
    out: dict[str, Any] = {"enable": True}
    embedder = _q_select(
        "  Embedder:",
        [
            "local         - on-box sentence-transformers (no API key)",
            "deterministic - hashing stub (offline; low quality, for testing)",
        ],
        default="local         - on-box sentence-transformers (no API key)",
    ).split()[0]
    out["embedder"] = embedder
    return out


def pick_oauth_vault() -> dict[str, Any]:
    """Opt-in to sealing captured OAuth tokens in the per-tenant vault.

    Off by default. When on, the OAuth helper seals access/refresh tokens
    encrypted-at-rest under the tenant's DEK (one data key per tenant) instead
    of writing them to a plaintext file. Returns a dict written under
    ``[oauth]``.
    """
    console.print()
    console.print(
        "[dim]The OAuth token vault seals captured access/refresh tokens "
        "encrypted-at-rest under each tenant's own key (no plaintext token "
        "files, no cross-tenant readability). Recommended for hosted/multi-"
        "tenant deploys. OFF by default.[/dim]"
    )
    return {"vault": _q_confirm("Seal OAuth tokens in the per-tenant vault?",
                                default=False)}


def pick_durable() -> dict[str, Any]:
    """Opt-in to durable execution (crash-resume via checkpoints).

    Off by default. When on, a long-running goal checkpoints its loop state at
    each step so ``maverick resume`` continues from where a crash left off
    instead of re-running from the start. Returns a dict written under
    ``[durable]``.
    """
    console.print()
    console.print(
        "[dim]Durable execution checkpoints a goal's progress so a crash or "
        "restart resumes from the last step instead of starting over. Adds a "
        "small write per step; OFF by default.[/dim]"
    )
    if not _q_confirm("Enable durable execution (crash-resume)?", default=False):
        return {"enabled": False}
    return {"enabled": True, "keep_last": 5}


def pick_assessments() -> dict[str, Any]:
    """Configure retained assessment, privacy, and vendor-paper helpers."""
    console.print()
    console.print(
        "[dim]Assessment learning may suggest answers from the firm's own "
        "reviewed past assessments. A human still reviews every answer.[/dim]"
    )
    learn = _q_confirm("Enable assessment learning (suggest from past "
                       "assessments)?", default=True)
    ops = _q_confirm("Enable the privacy ops record types (DPA review, AI "
                     "registry, RoPA, DSAR tracker)?", default=True)
    console.print()
    console.print(
        "[dim]Vendor-paper review compares a vendor's own DPA or privacy "
        "addendum against your standard positions and produces a Word "
        "tracked-changes redline. Which clauses fall short is always decided "
        "deterministically from the clause checklists; a model is used only to "
        "re-word YOUR required clause to match the vendor's drafting, and any "
        "draft that weakens your position is discarded. Say no and the redline "
        "uses your template language verbatim -- no model sees the "
        "contract.[/dim]"
    )
    draft = _q_confirm("Let a model re-word your standard clauses to match "
                       "vendor drafting?", default=True)
    console.print()
    console.print(
        "[dim]When a vendor signs YOUR paper, Maverick drafts your template "
        "DPA/addendum as a Word document with the vendor-specific values "
        "filled in red for counsel to verify. Your legal entity name goes on "
        "the party line; leave blank to keep a red placeholder.[/dim]"
    )
    org_name = _q_text("Legal entity name for drafted agreements (blank = "
                       "placeholder)", default="").strip()
    console.print()
    console.print(
        "[dim]The entity graph links your records into one lineage: every "
        "vendor, reviewer, document, and clause becomes one identity, and "
        "every review/decision becomes a dated, citable edge — so you can ask "
        "'why did we approve this vendor?', 'what did we know on the day we "
        "signed?', and 'this clause turned out bad — what relied on it?'. "
        "Derived and read-only: it is rebuilt from your records and never "
        "stores anything they don't say.[/dim]"
    )
    graph = _q_confirm("Enable the entity graph (lineage & dossier queries)?",
                       default=True)
    out: dict[str, Any] = {}
    if not learn:
        out["learn"] = False
    if not ops:
        out["privacy_ops"] = False
    if not draft:
        out["paper_review_use_model"] = False
    if org_name:
        out["paper_review_org"] = org_name
    if not graph:
        out["entity_graph"] = False
    return out


def pick_security_suite() -> dict[str, Any]:
    """Configure the surviving security surfaces.

    The GRC self-certification cluster (records workspace, Model Risk officer,
    AI evidence gateway, environment hunter) was deleted; what remains is the
    review-gated evidence graph and the read-only platform threat hunter.
    """
    console.print()
    console.print(
        "[dim]The evidence graph stores bounded evidence metadata and hashes "
        "over the audit chain. The platform threat hunter defensively scans "
        "Maverick's own signed telemetry. Both are OFF by default.[/dim]"
    )
    evidence_graph = _q_confirm(
        "Enable the review-gated evidence graph? It stores bounded evidence "
        "metadata and hashes, never raw telemetry.",
        default=False,
    )
    threat_hunt = _q_confirm(
        "Enable the defensive Maverick platform threat hunter?", default=False
    )
    return {
        "evidence_graph": evidence_graph,
        "threat_hunt": threat_hunt,
    }

def pick_oidc() -> dict[str, Any]:
    """Opt-in to OIDC ID-token verification for `maverick serve` (SSO).

    Off by default. When enabled, the server verifies an OpenID-Connect ID
    token (RS256/ES256 only — never HMAC/none) against the issuer + audience
    you configure, and maps the verified ``sub`` to a ``user:<sub>`` principal.
    Returns a dict written under ``[auth.oidc]``; ``{"enabled": False}`` when
    declined (so the writer emits nothing).
    """
    console.print(
        "[dim]OIDC SSO lets users authenticate to `maverick serve` with your "
        "identity provider (Okta, Auth0, Entra, Google, ...). Tokens are "
        "verified with the IdP's public keys; only RS256/ES256 are accepted "
        "(HMAC/none are rejected to prevent algorithm-confusion). OFF by "
        "default.[/dim]"
    )
    if not _q_confirm("Enable OIDC SSO token verification?", default=False):
        return {"enabled": False}
    issuer = _q_text(
        "  Issuer URL (the IdP's 'iss', e.g. https://example.okta.com)",
        default="",
    ).strip()
    audience = _q_text(
        "  Audience (your app's client_id / API audience)", default="",
    ).strip()
    jwks_uri = _q_text(
        "  JWKS URI (the IdP's signing-key endpoint, "
        "e.g. https://example.okta.com/oauth2/v1/keys)",
        default="",
    ).strip()
    result: dict[str, Any] = {
        "enabled": True,
        "issuer": issuer,
        "audience": audience,
        "jwks_uri": jwks_uri,
    }
    # Optional: the built-in browser-login (authorization-code) flow. The
    # default path is bearer-token verification only (API clients) or a reverse
    # proxy for browser SSO; this self-contained flow is for deployments that
    # can't run an auth proxy. OFF unless the operator opts in and supplies the
    # OAuth client + a session-signing secret.
    console.print(
        "[dim]Optional: built-in browser login. Lets the dashboard run the "
        "OAuth2 authorization-code flow itself (browser SSO without a separate "
        "auth proxy). Needs an OAuth client_id/secret registered with your IdP "
        "and a redirect URI of <dashboard-url>/auth/callback. OFF by "
        "default.[/dim]"
    )
    if _q_confirm("  Also enable the built-in browser login flow?", default=False):
        client_id = _q_text(
            "    OAuth client_id (this dashboard's registered client)", default="",
        ).strip()
        client_secret = _q_text(
            "    OAuth client_secret", default="",
        ).strip()
        redirect_uri = _q_text(
            "    Redirect URI (must be <dashboard-url>/auth/callback)", default="",
        ).strip()
        session_secret = _q_text(
            "    Session-signing secret (a long random string; keep it secret)",
            default="",
        ).strip()
        result.update(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "session_secret": session_secret,
            }
        )
    return result


def pick_advanced() -> dict[str, Any]:
    """Configure advanced reasoning, learning, and governance features.

    Governed learning choices default on; higher-authority or infrastructure
    features retain explicit opt-ins. All are editable later in
    ~/.maverick/config.toml.
    """
    console.print()
    advanced: dict[str, Any] = {
        "tree_of_thought": _q_confirm(
            "Tree-of-thought planning? Draft a few plans and let a critic pick the "
            "best before working (more tokens up front, fewer dead ends).",
            default=False,
        ),
        "compact_history": _q_confirm(
            "Compact long conversations? Keep the most relevant older turns under a "
            "token budget instead of just the last few.",
            default=False,
        ),
        "model_scaled_context": _q_confirm(
            "Model-scaled context? Size history windows, compaction targets, and "
            "routing thresholds to the driving model's real context window (a "
            "1M-token model gets 1M-token bounds) instead of fixed constants. "
            "Recommended; ON by default.",
            default=True,
        ),
        "attach_any_mime": _q_confirm(
            "Accept attachments of ANY declared type? Text, images, audio, video, "
            "and document formats are always accepted; this also admits anything "
            "else EXCEPT executables/archives (magic-byte deny stays). OFF by "
            "default.",
            default=False,
        ),
        "attachment_understanding": _q_confirm(
            "Attachment understanding? Extract text from supported Office and "
            "PDF documents into the goal context. ON by default.",
            default=True,
        ),
        "compaction_strategy": _q_select(
            "  Compaction strategy? (default = simple shrink)",
            ["default     - keep recent + trim big tool outputs",
             "learned     - LLM summary, self-tuning prompt picker",
             "multimodal  - stub heavy image/audio blocks to text",
             "streaming   - incremental running summary (long chats)",
             "graph       - entity-relation digest"],
            default="default     - keep recent + trim big tool outputs",
        ).split()[0],
        "reflexion": _q_confirm(
            "Reflexion learning? Remember lessons from failed runs and recall them "
            "on the next similar goal.",
            default=True,
        ),
        "self_harness": _q_confirm(
            "Self-harness? Learn a MODEL-SPECIFIC operating-guidance addendum from "
            "recurring failures -- mined, validated on held-out cases, and gated "
            "through the same promotion ladder as every other learned change -- "
            "then recalled into that model's system prompt. Inspect or roll it "
            "back any time from the dashboard. ON by default.",
            default=True,
        ),
        "memory_guard": _q_confirm(
            "Memory Guard (OWASP ASI06)? Screen every stored fact for prompt-"
            "injection/poisoning, stamp it with provenance + a trust tier, and "
            "keep low-trust memory out of the agent's standing brief (trust-aware "
            "retrieval). Every decision is audited. OFF by default.",
            default=False,
        ),
        "temporal_memory": _q_confirm(
            "Temporal memory? Keep a bitemporal history of every fact (validity "
            "windows) instead of overwriting -- answer 'what did we believe on "
            "date X, and why' for the Operating Record. OFF by default.",
            default=False,
        ),
        "fairness_monitor": _q_confirm(
            "Continuous fairness monitoring (ISO 42001 A.6.2.6)? Watch decision "
            "outcomes over a rolling window and raise a signed FAIRNESS_ALERT when "
            "the four-fifths rule is breached or fairness drifts below baseline. A "
            "deployment opts in by feeding the monitor its outcomes. OFF by default.",
            default=False,
        ),
        "specialist_discipline": _q_confirm(
            "Specialist operating discipline? Append the firm's legal "
            "privilege, confidentiality, citation, and counsel-review guardrails "
            "to every domain pack's persona at "
            "spawn. Recommended; prompts only, hard limits stay enforced by "
            "capabilities/governance.",
            default=True,
        ),
        "allow_pack_editing": _q_confirm(
            "Allow editing agents (domain packs) from the dashboard? Operators "
            "can fork/tweak a specialist's persona, tools, and workflow per "
            "client; an edit is validated before it saves, so it can never "
            "weaken the safety envelope. Turn off to lock the agent roster.",
            default=True,
        ),
        "allow_role_editing": _q_confirm(
            "Allow editing the core roles (orchestrator, coder, ...) from the "
            "dashboard? Operators can add a per-client system-prompt addendum "
            "to a role. Turn off to lock role behavior. Per-role reasoning "
            "effort remains a separate control; the run model is global.",
            default=True,
        ),
        "dreaming_llm_consolidation": _q_confirm(
            "  └ LLM-enriched consolidation? Have the selected run model "
            "rewrite each consolidated failure into a transferable lesson "
            "(instead of the deterministic template). Inputs+output are "
            "shield-scanned and the spend is budgeted; fails open to the "
            "deterministic text. Costs a few tokens per dream cycle.",
            default=False,
        ),
        "dreaming": _q_confirm(
            "Dreaming (offline consolidation)? `maverick dream` replays recent "
            "runs while idle, distills recurring wins into skills per department, "
            "turns repeated failures into recalled insights (promoting patterns "
            "shared across departments), retires skills whose track record "
            "decayed, and prunes stale lessons. Deterministic by default "
            "(opt-in LLM enrichment + rehearsal runs are separate and budgeted).",
            default=True,
        ),
        "risk_proportional_verify": _q_confirm(
            "Risk-proportional verification? Skip the verifier on trivial, low-risk "
            "answers (short, prose-only, no tools or code) to save tokens and latency.",
            default=False,
        ),
        "autonomy_gate": _q_confirm(
            "Autonomy gate? When sub-agents disagree, tighten the risk ceiling "
            "and hold irreversible actions until the disagreement is resolved "
            "or a human approves.",
            default=False,
        ),
        "headless_assume": _q_confirm(
            "Autonomous (headless) mode? When no human is available to answer, the "
            "agent states a reasonable assumption and continues instead of stalling "
            "on a clarifying question. Best for batch / unattended runs.",
            default=False,
        ),
        "governed_actions": _q_confirm(
            "Governed actions? Record a tamper-evident lineage of every consequential "
            "agent action (writes, shell) so a run's actions are auditable end-to-end.",
            default=False,
        ),
        "calibration_enforce": _q_confirm(
            "Calibration interlock? Freeze self-improvement "
            "if the verifier stops telling correct answers from incorrect ones on "
            "your labeled set, so the system never learns from a drifted evaluator.",
            default=False,
        ),
        "adaptive_compute": _q_confirm(
            "Adaptive test-time compute? Concentrate effort on uncertain sub-tasks "
            "and spend less when the swarm agrees (cheaper, focused).",
            default=False,
        ),
        "best_of_n": _q_confirm(
            "Best-of-N answers? Sample a few candidate answers and keep the one the "
            "verifier scores highest (stronger, more tokens).",
            default=False,
        ),
        "structured_verifier_off": _q_confirm(
            "Turn OFF the structured rubric verifier? It's ON by default: the "
            "verifier scores each FINAL on a rubric (correctness/completeness/"
            "grounding/safety) and a failing safety facet vetoes even a high score, "
            "with the rubric recorded for the learning audit (maverick.reasoning_reward). "
            "Costs ~more tokens per verify; say yes to fall back to the scalar verifier.",
            default=False,
        ),
        "jit_rl_off": _q_confirm(
            "Turn OFF JitRL test-time adaptation? It's ON by default but a no-op until "
            "experience accumulates: it records verifier-guided best-of-N outcomes and "
            "steers candidate selection toward what has verified well (maverick.jit_rl, "
            "no weight updates). Say yes to disable it entirely.",
            default=False,
        ),
        "audit_rewards": _q_confirm(
            "Sign verification rewards into the audit chain? Provable learning: write a "
            "tamper-evident, signed row for every structured reward the system learns "
            "from -- the per-dimension rubric, the score, and where a facet vetoed "
            "(maverick.reasoning_reward). On by default (adds audit volume); recommended "
            "for regulated deployments.",
            default=False,
        ),
        "reward_laundering": _q_confirm(
            "Enforce reward-laundering resistance? Beyond freezing on verifier DRIFT, "
            "also freeze learning when the judge stays sharp on normal traffic but its "
            "edge on adversarial probes collapses -- i.e. it's being GAMED "
            "(maverick.calibration). Off by default; enables the calibration interlock "
            "and needs adversarial probes recorded to bite.",
            default=False,
        ),
        "signed_approval": _q_confirm(
            "Require cryptographic approval for code/weights self-changes? Replace the "
            "self-settable approval boolean with an Ed25519 signature over the exact "
            "change, verified against operator-held keys, so a self-modifying agent "
            "cannot approve itself (maverick.approval_signing). Off by default; you "
            "provide approver public keys (fails closed until you do).",
            default=False,
        ),
        "experience_guidance": _q_confirm(
            "Experience-guided orchestration? Steer planning with how similar past "
            "goals turned out (what worked, what failed).",
            default=True,
        ),
        "credit_assignment": _q_confirm(
            "Counterfactual credit assignment? After a swarm answers, work out which "
            "sub-agent actually helped (ablate + re-verify) to improve learning and "
            "routing. Costs extra verifier calls per swarm.",
            default=True,
        ),
        "causal_promotion": _q_confirm(
            "Counterfactual promotion? When governed self-improvement is on, promote a "
            "learned change (tool/prompt/policy) only if its confounder-adjusted CAUSAL "
            "effect on outcomes clears the bar -- not just a correlation that co-occurred "
            "with success. Each promotion records the effect, its confidence interval, and "
            "what it adjusted for. Requires self-improvement enabled.",
            default=True,
        ),
        "evaluator_evolution": _q_confirm(
            "Evaluator co-evolution? When the learned judge stops discriminating, don't "
            "just FREEZE learning -- promote a better judge: a challenger evaluator "
            "replaces the incumbent only when its agreement with a fixed, checksum-locked "
            "ground-truth ANCHOR beats it (on the dedicated 'evaluator' rung). The anchor "
            "is immutable so a weak judge can't launder drift. Requires self-improvement "
            "enabled; an evaluator swap needs human approval until you raise max_auto_rung "
            "to 'evaluator'.",
            default=True,
        ),
        "rehearsal": _q_confirm(
            "Pre-execution rehearsal? Before a risky plan runs, simulate it against the "
            "learned world-model of your environment and gate on the prediction: proceed "
            "when the model is confident it's safe, BLOCK a confidently-poor outcome, and "
            "ESCALATE to a human when the model is unsure or has never seen the move. "
            "Governance that lets agents be bolder where it's earned; on by default.",
            default=True,
        ),
        "data_engine": _q_confirm(
            "Cognitive Data Engine? The Tesla-style improvement flywheel: production "
            "failures are triaged by CAUSAL impact on real outcomes (fix what moves "
            "reality most, not what's merely frequent), then mined, validated in the "
            "world-model, and promoted through the safety ladder. The firm improves "
            "from its own experience; on by default.",
            default=True,
        ),
        "operations_scientist": _q_confirm(
            "Operations Scientist? An agent that DISCOVERS a better process and proves it: "
            "it pairs a harmful action with the beneficial habit that should replace it, "
            "validates the swap in the world-model, then runs a real causal experiment and "
            "ships the proven win. Discovery, not just labour; on by default.",
            default=True,
        ),
        "consequence": _q_confirm(
            "Consequence Engine? Ground the firm's learning in REAL outcomes instead "
            "of a model's self-graded proxy: when a downstream result lands (an invoice "
            "paid, a ticket reopened), it overrides the proxy reward so the data engine "
            "learns from reality. Reality is the reward signal; on by default.",
            default=True,
        ),
        "flows": _q_confirm(
            "Flow engine? A deterministic skeleton -- branch, switch, loop, parallel, "
            "approval, delay, wait-for-event, try/catch -- over steps that are either a "
            "fixed tool call or a full agentic goal. Migrated workflows keep their "
            "structure and auditability, while any node can still be an agent; each "
            "node's outcome trains which steps should be deterministic vs. agentic. "
            "Off by default.",
            default=False,
        ),
        "connections": _q_confirm(
            "Named connections? Store a connector's base URL + API token under a name, "
            "sealed at rest, so a connector can be wired from the dashboard without "
            "setting <NAME>_TOKEN env vars (an existing env var still wins). Off by "
            "default.",
            default=False,
        ),
        "emergent_protocol": _q_confirm(
            "Emergent coordination shorthand? Swarms evolve short codes for the boilerplate "
            "they repeat (cheaper coordination), while every code decodes EXACTLY back to "
            "English -- the auditable translation layer, so nothing is ever hidden from the "
            "Shield or a human. Off by default; a no-op until a codebook is learned.",
            default=False,
        ),
        "emergent_codec": _q_confirm(
            "Measure the token-aware codec on live coordination? The codec that saves "
            "actual frontier TOKENS (not just bytes), via byte-stuffed cheap codes. When "
            "on, the blackboard measures -- never changes -- what it would compress real "
            "traffic to, so you can confirm the savings before agents ever read codes. "
            "Off by default; pure telemetry, the audit/Shield path is untouched.",
            default=False,
        ),
        "enforce_capabilities": _q_confirm(
            "Enforce agent capabilities? Each agent runs under a scoped grant and "
            "spawned sub-agents can only narrow it, never exceed it (least privilege).",
            default=False,
        ),
        "per_call_token_exchange": _q_confirm(
            "Per-call token exchange? Each tool call trades the run-long grant for "
            "a freshly minted, single-tool, short-lived signed token (zero-trust; "
            "shrinks the blast radius of a mid-run compromise). Needs capability "
            "enforcement.",
            default=False,
        ),
        "enforce_quotas": _q_confirm(
            "Enforce per-principal usage quotas? Track spend (dollars + tokens) per "
            "user per day and refuse to start a new goal once the daily cap is hit "
            "(chargeback / cost governance across runs, beyond the per-run budget).",
            default=False,
        ),
        "tenant_by_user": _q_confirm(
            "Isolate each user into their own tenant? Per-user cross-session memory "
            "is kept separate — recommended for multi-user servers.",
            default=False,
        ),
        "client_id": _q_text(
            "Client/tenant id for THIS deployment (one Maverick per enterprise "
            "client). All data (world DB, audit, memory, queue) is isolated under "
            "this id — leave blank only for a personal/single-user install. "
            "Letters/digits/._- e.g. \"acme-corp\".",
            default="",
        ),
        "enterprise": _q_confirm(
            "Enterprise mode (private/sensitive data)? Pin every LLM call to a "
            "local/self-hosted model so data never leaves your boundary, gate "
            "destructive actions, and enforce per-agent capabilities. Recommended "
            "when the agent handles PHI/PII/financial data.",
            default=False,
        ),
        "anonymous_logs": _q_confirm(
            "Anonymous mode? Scrub user-identifying content (goal text, user/channel "
            "ids, home paths, emails/phones) from logs and audit events — hashes or "
            "sentinels instead of raw values. Good for shared/regulated environments.",
            default=False,
        ),
        "encrypt_at_rest": _q_confirm(
            "Encrypt sensitive local stores at rest? Seals the cross-session "
            "memory store with AES-256-GCM (key in ~/.maverick/keys, chmod 600). "
            "Implied by enterprise mode; recommended for PHI/PII/financial data.",
            default=False,
        ),
        "encrypt_per_tenant": _q_confirm(
            "Per-tenant encryption keys? Each tenant gets its own data key "
            "(wrapped by a KMS KEK) so one tenant's key never opens another's "
            "data — the posture a hosted multi-tenant store needs. Requires "
            "at-rest encryption; reads of existing data stay transparent. "
            "Off by default (single-tenant boxes don't need it).",
            default=False,
        ),
        "audit_sign": _q_confirm(
            "Sign the audit log for tamper-evidence? Ed25519 hash-chains every "
            "audit row (plus a signed cross-file ledger) so `maverick audit verify` "
            "can prove the log was not altered — the basis for SOC 2 evidence. "
            "Needs the [audit-signing] extra; falls back to unsigned if absent.",
            default=False,
        ),
        "audit_worm": _q_confirm(
            "Export closed audit day-files to a write-once (WORM) store? Beyond "
            "tamper-EVIDENCE, this makes the historical log un-alterable: each "
            "closed day-file is shipped with a retention lock so it can't be "
            "rewritten or deleted (S3 Object-Lock for regulator-grade WORM, or a "
            "local read-only mirror). Run `maverick audit worm push` (e.g. nightly "
            "cron). Defaults to a local mirror; edit [audit.worm] for S3.",
            default=False,
        ),
        "security_autofix": _q_confirm(
            "Let the security assessor auto-fix low-risk gaps? With enterprise mode "
            "on, the assessor may auto-apply reversible, in-boundary "
            "config fixes (enable audit signing, set retention); anything "
            "behaviour-changing stays gated for a human. Off by default; every fix "
            "is audited and reversible.",
            default=False,
        ),
        "dual_approval": _q_confirm(
            "Require two-person approval for risky actions (N-of-M dual control)? "
            "A high/critical-risk action then needs 2 DISTINCT approvers in the "
            "dashboard queue, and the requester can't approve their own request — "
            "the segregation-of-duties control SOX / SOC 2 / HIPAA auditors test. "
            "Writes [security] approvals_required = 2. Off by default.",
            default=False,
        ),
        "local_runtime": _q_confirm(
            "Manage a local model server (vLLM / TGI / llama.cpp)? Writes "
            "[local_runtime] so the local-runtime planner composes the "
            "right batching / KV-cache / precision flags for your engine; "
            "configure the engine + model in config.toml after the wizard. "
            "Off by default.",
            default=False,
        ),
        "output_cache": _q_confirm(
            "Cache tool outputs? Memoize side-effect-free (read-only) tool calls "
            "within a run so a repeated read isn't re-done. Off by default.",
            default=False,
        ),
        "effort": _q_confirm(
            "Per-role reasoning effort? Keep the orchestrator/coder at high effort "
            "but run bulk roles (researcher/verifier/writer) at lower effort — the "
            "biggest cost/latency lever on Opus 4.7/4.8. Off by default.",
            default=False,
        ),
        "cache_prewarm": _q_confirm(
            "Pre-warm the prompt cache at start? A max_tokens=0 prefill writes the "
            "system+tools cache so the first turn doesn't pay the cold-write "
            "latency (best for interactive use). Off by default.",
            default=False,
        ),
    }
    # Autonomous self-correction only makes sense once flows are on. Human apply
    # + surfaced proposals work without it; this governs whether the loop may
    # UNDO a change on its own.
    if advanced.get("flows") and _q_confirm(
        "  Let flows self-correct? A scheduled pass reverts a node rewrite whose "
        "grounded outcomes measurably regressed -- the loop undoes its own bad "
        "changes. Off by default.",
        default=False,
    ):
        advanced["flows_auto_evolve"] = True
    # Signed browser approval links need the dashboard's externally reachable
    # base URL; blank keeps approvals on the dashboard itself.
    if advanced.get("flows"):
        flows_url = _q_text(
            "  Public base URL for flow approval links (e.g. https://ops.acme.com; "
            "blank = approve from the dashboard)", default="").strip()
        if flows_url:
            advanced["flows_public_url"] = flows_url
    # Self-harness has a handful of optional paths beyond the validation floors
    # written by default. Only worth asking once the loop itself is on; each
    # stays at its historical default unless the operator opts in here.
    if advanced.get("self_harness") and _q_confirm(
        "  Tune self-harness advanced paths? (eval corpus, per-department "
        "scoping, semantic mining, best-of-N, efficacy review, auto-retire)",
        default=False,
    ):
        corpus = _q_text(
            "  Eval-corpus path for the live A/B (lets a scheduled pass actually "
            "promote; blank = dry pass)", default="").strip()
        if corpus:
            advanced["self_harness_eval_corpus"] = corpus
            # A live A/B spends real provider dollars unattended, so cap it by
            # default; explicit 0 opts out.
            advanced["self_harness_eval_budget"] = max(0.0, _safe_float(_q_text(
                "    Spend cap per auto-evaluated cycle, in dollars (0 = uncapped)",
                default="5"), default=5.0))
        if _q_confirm(
            "    Mine and scope guidance per department (domain)?", default=False):
            advanced["self_harness_bucket_domain"] = True
        advanced["self_harness_semantic_mining"] = _q_confirm(
            "    Semantic mining? Cluster failures by embedding similarity when "
            "embeddings are available (else deterministic Jaccard).", default=False)
        advanced["self_harness_efficacy_review"] = _q_confirm(
            "    Efficacy review? Re-measure each line's live A/B lift every cycle "
            "and demote dead weight (needs the eval corpus above).", default=False)
        advanced["self_harness_canary"] = _q_confirm(
            "    Canary rollout? Stage each newly learned line on probation and "
            "graduate or pull it from real run outcomes.", default=False)
        advanced["self_harness_auto_run"] = _q_confirm(
            "    Auto-run? Run the governed learning cycle for the pinned run model "
            "as part of `maverick dream` (no second cron entry).", default=True)
        advanced["self_harness_metamorphic"] = _q_confirm(
            "    Metamorphic validation? Paraphrase held-out cases and require a "
            "candidate's lift to survive the rewording (needs the live A/B).",
            default=False)
        advanced["self_harness_relapse"] = _q_confirm(
            "    Relapse watch? Put a learned line back on probation when half "
            "or more of its recent run outcomes are failures.", default=False)
        advanced["self_harness_calibrate_judge"] = _q_confirm(
            "    Judge calibration? Feed the evaluation judge's verdicts into "
            "the verifier-drift freeze so a drifting judge pauses learning.",
            default=False)
        harvest_mode = _q_select(
            "    Corpus bootstrapping from run history?",
            ["off     - don't harvest",
             "propose - stage mined cases for your review",
             "auto    - merge mined cases straight into the corpus"],
            default="off     - don't harvest").split()[0]
        if harvest_mode in ("propose", "auto"):
            advanced["self_harness_corpus_harvest"] = harvest_mode
        # Harvesting rides the dream beat; without auto_run it is configured
        # but never executed, so say so now rather than silently at 3am.
        if not advanced.get("self_harness_auto_run") and harvest_mode in (
            "propose", "auto"
        ):
            console.print(
                "    [yellow]Note: nightly harvesting only runs when "
                "auto-run is on -- enable it above.[/yellow]")
        if harvest_mode in ("propose", "auto") and not corpus:
            console.print(
                "    [yellow]Note: corpus bootstrapping needs the eval-corpus "
                "path above; without one the harvest is a no-op.[/yellow]")
        store_pick = _q_select(
            "    Learning-store backend?",
            ["files - local JSON under ~/.maverick (default)",
             "world - durable world database"],
            default="files - local JSON under ~/.maverick (default)",
        ).split()[0]
        if store_pick == "world":
            advanced["self_harness_store"] = "world"
        advanced["self_harness_candidates"] = max(1, _safe_int(_q_text(
            "    Candidate lines per weakness (best-of-N; 1 = single)",
            default="1"), default=1))
        advanced["self_harness_retire_days"] = max(0, _safe_int(_q_text(
            "    Auto-retire lines unused this many days (0 = never)",
            default="0"), default=0))
    # Regulated-deployment posture: a compliance disclosure line. Maps to the
    # independent [compliance] scalar table; previously only hand-editable.
    if _q_confirm(
        "  Set a compliance disclosure line shown to users?",
        default=False,
    ):
        disclosure = _q_text(
            "  Compliance disclosure line shown to users (blank = none)",
            default="").strip()
        if disclosure:
            advanced["compliance_disclosure_text"] = disclosure
    # OIDC SSO is a string-bearing toggle (issuer/audience/jwks_uri), so it has
    # its own prompt; the result is nested under the "oidc" key and the writer
    # emits a single [auth.oidc] table for it.
    advanced["oidc"] = pick_oidc()
    # Department scoping only applies to a named signed-in principal.
    advanced["department_access"] = pick_department_access(
        bool(advanced["oidc"].get("enabled")))
    return advanced


def _split_csv(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def pick_department_access(sso_enabled: bool) -> dict[str, Any]:
    """Opt-in department (job-function) scoping for authenticated users.

    Only meaningful once sign-in is on (it keys on the authenticated
    principal), so it is skipped entirely when SSO is off. Collects the
    deny-by-default department set under ``[dashboard]``. Returns ``{}`` when
    declined, so a default install is unchanged.
    """
    if not sso_enabled:
        return {}
    console.print(
        "[dim]Department scoping limits which specialist teams each signed-in "
        "user can see and run — a member of one practice group does not "
        "automatically gain access to another. OFF by default (everyone sees "
        "every enabled legal group); you can "
        "always assign per-user access later on the dashboard Users page.[/dim]"
    )
    if not _q_confirm("Restrict users to specific departments by default?",
                      default=False):
        return {}
    return {
        "default_suites": _split_csv(_q_text(
            "  Departments a new user may use until granted more (comma-separated "
            "suite keys; blank = none)", default="")),
    }


def _docker_available() -> bool:
    """Return True iff the `docker` binary is on PATH AND the daemon
    responds. Used to pick a safe sandbox default in consumer mode and
    to choose the wizard's default in dev mode."""
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True, timeout=2, check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


# Docker picks its image from the operator-selected toolchain language (see
# ``sandbox._IMAGE_BY_LANGUAGE``). The local backend uses the host toolchain.
_LANGUAGE_BACKENDS = {"docker"}
_IMMUTABLE_DOCKER_IMAGE_RE = re.compile(
    r"^(?:[^\s@]+@)?sha256:[0-9a-fA-F]{64}$"
)


def _pick_immutable_docker_image() -> str:
    """Require the immutable Docker reference enforced by the firm runtime."""
    while True:
        image = _q_text(
            "  Immutable Docker sandbox image "
            "(repository@sha256:<64-hex> or local sha256:<64-hex>)",
            default="",
        ).strip()
        if _IMMUTABLE_DOCKER_IMAGE_RE.fullmatch(image):
            return image
        console.print(
            "[red]A full immutable sha256 Docker reference is required; mutable "
            "tags and the empty default are refused in secure mode.[/red]"
        )


def pick_sandbox() -> dict[str, Any]:
    # Security-first default: keep Docker selected by default regardless
    # of current daemon reachability to avoid silently falling back to
    # the least isolated local backend.
    docker_default = "docker - Throwaway Docker container (recommended)"
    pick = _q_select(
        "Sandbox backend (for retained local subprocess helpers):",
        [
            "local  - Subprocess on this machine (fastest, least isolated)",
            "docker - Throwaway Docker container (recommended)",
        ],
        default=docker_default,
    )
    backend = pick.split()[0]
    workdir = _q_text("  Workspace directory", default=str(Path.home() / "maverick-workspace"))
    cfg: dict[str, Any] = {"backend": backend, "workdir": workdir, "timeout": 60}
    if backend == "docker":
        cfg["image"] = _pick_immutable_docker_image()
    # Non-Python coders get a toolchain image that can actually run their tests
    # (cargo/go test, the JS runner, ...). Python is the default image, so we
    # only write [sandbox] language when it's something else -- existing and
    # Python configs stay byte-identical.
    if backend in _LANGUAGE_BACKENDS:
        languages = [
            "python     - python:3.12-slim (default)",
            "javascript - node:22 (JavaScript / TypeScript)",
            "go         - golang:1",
            "rust       - rust:1",
            "java       - eclipse-temurin:21 (Java / Kotlin)",
            "ruby       - ruby:3",
        ]
        lang = _q_select(
            "  What do you mostly code in? (sets the container's toolchain)",
            languages,
            default=languages[0],
        ).split()[0]
        if lang != "python":
            cfg["language"] = lang
    return cfg


# ---------- new wizard steps (council parity pass) ----------

def pick_web_search() -> tuple[bool, list[str]]:
    """Enable web search + pick a default backend. Returns (enabled, env_vars_needed)."""
    if not _q_confirm(
        "Enable web search? (Tavily / Brave / SerpAPI / DuckDuckGo)",
        default=True,
    ):
        return False, []
    pick = _q_select(
        "  Default backend:",
        [
            "tavily   - best quality, free tier ~1k/mo, BYOK",
            "brave    - generous free tier, BYOK",
            "serpapi  - paid, covers more engines",
            "ddg      - no key, rate-limited",
        ],
        default="tavily   - best quality, free tier ~1k/mo, BYOK",
    )
    backend = pick.split()[0]
    envs = {
        "tavily":  ["TAVILY_API_KEY"],
        "brave":   ["BRAVE_API_KEY"],
        "serpapi": ["SERPAPI_API_KEY"],
        "ddg":     [],
    }[backend]
    os.environ["MAVERICK_SEARCH_BACKEND"] = backend  # picked up by web_search tool
    return True, envs


def pick_tool_acl() -> dict[str, Any]:
    """Optional global tool deny list."""
    if not _q_confirm(
        "Restrict tools the agent may run? (skip for full access)",
        default=False,
    ):
        return {}
    acl: dict[str, Any] = {}
    common = ["write_file", "web_search"]
    denied = _q_checkbox(
        "Deny these tools globally (rare; usually empty):",
        common,
        default=[],
    )
    if denied:
        acl["denied_tools"] = denied
    return acl


def pick_rate_limits() -> dict[str, str]:
    """Per-tool sliding-window rate caps."""
    if not _q_confirm(
        "Cap call rate per tool?",
        default=False,
    ):
        return {}
    limits: dict[str, str] = {}
    proposed = [
        ("web_search", "10/60"),
        ("knowledge_search", "30/60"),
    ]
    for name, spec_default in proposed:
        spec = _q_text(
            f"  {name} (N/seconds, blank to skip)",
            default=spec_default,
        ).strip()
        if spec:
            limits[name] = spec
    return limits


def pick_retention() -> dict[str, int]:
    """Auto-prune audit logs and world-model rows."""
    if not _q_confirm(
        "Auto-prune audit logs + old episodes after N days?",
        default=True,
    ):
        return {}
    return {
        "audit_days":    _safe_int(_q_text("  Audit log retention days",   default="90"),  default=90),
        "episodes_days": _safe_int(_q_text("  Episode retention days",     default="365"), default=365),
        "events_days":   _safe_int(_q_text("  Goal-event retention days",  default="180"), default=180),
    }


def pick_persona() -> dict[str, str]:
    """Agent identity: name + writing style."""
    if not _q_confirm(
        "Customise the agent's name and style? (skip for defaults)",
        default=False,
    ):
        return {}
    name = _q_text("  Agent name", default="Maverick").strip() or "Maverick"
    style_pick = _q_select(
        "  Style:",
        [
            "concise   - terse, direct",
            "balanced  - default",
            "verbose   - explains its reasoning",
        ],
        default="balanced  - default",
    )
    return {"name": name, "style": style_pick.split()[0]}


def pick_persona_roles() -> dict[str, Any]:
    """Bind the operator to persona consumer role(s) for the deliverables inbox.

    Sets ``[personas] default`` so the signed-in user lands on their own
    deliverables ("my forecasts") instead of the full list. Per-user mappings
    (``[personas]`` keyed by principal) are added later like RBAC roles; this is
    the single-user default."""
    if not _q_confirm(
        "Default the deliverables inbox to your own role(s)?",
        default=False,
    ):
        return {}
    raw = _q_text(
        "  Your primary role(s) (e.g. fpa_analyst, controller; space/comma separated)",
        default="",
    ).strip()
    roles = [r.strip() for r in raw.replace(",", " ").split() if r.strip()]
    return {"default": roles} if roles else {}


def pick_connectors() -> dict[str, str]:
    """Collect credentials for the retained GET-only legal-system connectors.

    Connectors are always registered in the kernel; they only need their
    BASE_URL/TOKEN env vars set to work. Returns ``{ENV_NAME: value}`` for the
    systems the user chose to connect now, merged into ~/.maverick/.env. The
    catalog (and ``docs/connectors.md``) come from ``connector_catalog()`` in
    maverick-core, so this stays in sync as connectors are added. Secrets
    collected here are never persisted to the partial-state file.
    """
    try:
        from maverick.tools.enterprise_connectors import connector_catalog
        entries = connector_catalog()
    except Exception as e:  # maverick-core not importable / catalog moved
        console.print(f"[yellow]Connector catalog unavailable: {e}[/yellow]")
        return {}
    if not entries:
        return {}
    console.print()
    console.print(
        f"[dim]The firm build ships {len(entries)} reviewed, GET-only legal-system "
        "connectors. Full list: docs/connectors.md. Add credentials now, or "
        "add them later in "
        "~/.maverick/.env.[/dim]"
    )
    if not _q_confirm("Configure any legal-system connectors now?", default=False):
        return {}
    by_name = {e["name"]: e for e in entries}
    raw = _q_text(
        "  Which tools? (comma-separated names, e.g. clio_read, docusign_read)",
        default="",
    )
    picked = _csv_list(raw, lower=True)
    keys: dict[str, str] = {}
    for name in dict.fromkeys(picked):  # dedupe, preserve order
        entry = by_name.get(name)
        if entry is None:
            console.print(
                f"  [yellow]unknown connector '{name}' — skipped "
                "(see docs/connectors.md for valid names)[/yellow]"
            )
            continue
        console.print(f"  [bold]{entry['label']}[/bold]")
        for env_name, is_secret in entry["env"]:
            current = os.environ.get(env_name, "")
            if is_secret:
                masked = (current[:4] + "...") if current else "(none)"
                val = _q_secret(
                    f"    {env_name} [current: {masked}] (blank = keep current)"
                )
                if not val and current:
                    val = current
            else:
                val = _q_text(f"    {env_name}", default=current)
            if val:
                keys[env_name] = val
    return keys


def collect_api_keys(providers: list[str], extra_envs: set[str]) -> dict[str, str]:
    keys: dict[str, str] = {}
    needed: list[str] = []

    for prov in providers:
        info = catalog.PROVIDERS.get(prov, {})
        env_name = info.get("env")
        if env_name:
            needed.append(env_name)
        needed.extend(info.get("env_vars", []))

    needed.extend(sorted(extra_envs))

    if not needed:
        return keys

    console.print()
    console.print("[bold]API keys / tokens[/bold] (stored in ~/.maverick/.env, chmod 600)")
    for env_name in dict.fromkeys(needed):  # dedupe preserving order
        current = os.environ.get(env_name, "")
        masked = (current[:7] + "...") if current else "(none)"
        val = _q_secret(f"  {env_name} [current: {masked}] (leave blank to keep current)")
        if not val:
            if current:
                keys[env_name] = current
            continue

        # Validate when we know how, with a 7-day cache so re-runs of
        # the wizard don't burn an API round-trip on every key.
        validator = _VALIDATORS.get(env_name)
        if validator:
            cached = _cached_validation(env_name, val)
            if cached is not None:
                ok, msg = cached
                console.print(f"    {_validation_marker(ok, msg)} {msg} (cached)")
            else:
                ok, msg = validator(val)
                console.print(f"    {_validation_marker(ok, msg)} {msg}")
                _remember_validation(env_name, val, ok, msg)
            if not ok and not _q_confirm("Save anyway?", default=False):
                continue
        keys[env_name] = val
    return keys


# ---------- write + verify ----------

def _toml_str(v: Any) -> str:
    """Render a value as a TOML basic string with proper escaping.

    Windows paths (e.g. a sandbox workdir ``C:\\Users\\x\\ws``) contain
    backslashes; emitted raw into a ``"..."`` basic string, ``\\U`` is parsed
    as a unicode escape and the config.toml the wizard just wrote can't be read
    back (``TOMLDecodeError: Invalid hex value``). Escape backslashes and
    double-quotes so the round-trip holds on every platform.
    """
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _emit_kv(lines: list[str], k: str, v: Any) -> None:
    """Append one TOML key=value line, type-dispatched."""
    if isinstance(v, bool):
        lines.append(f"{k} = {str(v).lower()}")
    elif isinstance(v, (int, float)):
        lines.append(f"{k} = {v}")
    elif isinstance(v, list):
        rendered = ", ".join(_toml_str(x) for x in v)
        lines.append(f"{k} = [{rendered}]")
    else:
        lines.append(f"{k} = {_toml_str(v)}")


# Business-function agent suites the factory can spawn from (domain packs under
# maverick/domains/). The kernel's enabled_domains() honors the [suites] table
# this writes; suites are ON by default (opt-out), so writing nothing keeps all.
AGENT_SUITES: list[tuple[str, str]] = [
    # Only suites with packs behind them in this fork. Verified against
    # packages/maverick-core/maverick/domains/: upstream offered 53 industry
    # suites, of which 42 (healthcare, banking, aerospace, mining, maritime,
    # oil & gas, semiconductors, ...) toggle nothing here after the prune to
    # 31 legal packs. A prompt that configures nothing is worse than no prompt.
    ("legal", "Legal — the practice (31 packs)"),
]


def pick_suites() -> dict[str, bool]:
    """Which business-function agent suites to enable. All on unless customized.

    Returns a ``suite -> bool`` map for the ``[suites]`` config table (empty when
    the operator keeps the default, so the kernel enables every suite)."""
    console.print()
    console.print("[bold]Practice suites[/bold] — the legal specialists the "
                  "firm runtime may use.")
    console.print("[dim]All enabled by default. A disabled suite's agents can't be "
                  "spawned. Editable later in ~/.maverick/config.toml under "
                  "[suites].[/dim]")
    if not _q_confirm("  Customize which suites are enabled? (No = keep all on)",
                      default=False):
        return {}
    out: dict[str, bool] = {}
    for key, label in AGENT_SUITES:
        out[key] = _q_confirm(f"    Enable the {label} suite?", default=True)
    return out


def _cfg_deployment(deployment: str | None) -> list[str]:
    if not deployment:
        return []
    # Record the chosen deployment topology (laptop / vps / ...) for
    # provenance + so a later `maverick init` can default to it.
    return [
        "[deployment]",
        f"type = {_toml_str(str(deployment))}",
        "",
    ]


def _cfg_governance(profile: str | None) -> list[str]:
    if not profile:
        return []
    # Record the governance level chosen at onboarding. This is a label for
    # humans + dashboards: the enforceable knobs it expanded to ([audit],
    # [quotas], [self_improvement], retention, ...) are written explicitly
    # elsewhere in this file, so hand-edits keep working. Re-run
    # `maverick init` to switch levels.
    return [
        "[governance]",
        f"profile = {_toml_str(str(profile))}   # essentials | standard | regulated | custom",
        "",
    ]


def _cfg_providers(providers: list[str]) -> list[str]:
    lines: list[str] = []
    for prov in providers:
        info = catalog.PROVIDERS.get(prov, {})
        lines.append(f"[providers.{prov}]")
        env_name = info.get("env")
        if env_name:
            lines.append(f'api_key = "${{{env_name}}}"')
        if prov == "ollama":
            lines.append('base_url = "http://localhost:11434"')
        if prov == "openai_compatible":
            lines.append('base_url = "${OPENAI_COMPATIBLE_BASE_URL}"')
        lines.append("")
    return lines


def _cfg_run_model(run_model: str, providers: list[str]) -> list[str]:
    spec = _validate_run_model(run_model, providers)
    return ["[models]", f"default = {_toml_str(spec)}", ""]


def _cfg_core(
    budget: dict[str, Any],
    safety: dict[str, Any],
    sandbox: dict[str, Any],
) -> list[str]:
    lines = ["[budget]"]
    for k, v in budget.items():
        _emit_kv(lines, k, v)
    lines.append("")
    lines.append("[safety]")
    for k, v in safety.items():
        _emit_kv(lines, k, v)
    lines.append("")
    lines.append("[sandbox]")
    for k, v in sandbox.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_skills(skills: dict[str, Any] | None) -> list[str]:
    if not skills:
        return []
    # Signed-skill policy. trusted_pubkeys = hex Ed25519 publisher keys
    # a signed SKILL.md must match; require_signed rejects unsigned ones.
    lines = ["", "[skills]"]
    for k, v in skills.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_self_learning(self_learning: dict[str, Any] | None) -> list[str]:
    if not self_learning:
        return []
    # Self-learning. enable gates the whole feature; sub-toggles let the
    # agent install skills and generate local reusable guidance.
    lines = ["", "[self_learning]"]
    for k, v in self_learning.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_flows(flows: dict[str, Any] | None) -> list[str]:
    if not flows:
        return []
    # Flow engine. enable gates the visual multi-step workflow designer/runner;
    # public_url (optional) is the base URL for signed browser approval links.
    lines = ["", "[flows]"]
    for k, v in flows.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_knowledge(knowledge: dict[str, Any] | None) -> list[str]:
    if not knowledge:
        return []
    # Per-domain vector RAG (maverick-knowledge). enable gates the whole
    # feature; embedder/store/dsn are read by config.get_knowledge.
    lines = ["", "[knowledge]"]
    for k, v in knowledge.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_oauth(oauth: dict[str, Any] | None) -> list[str]:
    if not (oauth and oauth.get("vault")):
        return []
    # Seal captured OAuth tokens in the per-tenant vault (encrypted at rest).
    return ["", "[oauth]", "vault = true"]


def _cfg_durable(durable: dict[str, Any] | None) -> list[str]:
    if not (durable and durable.get("enabled")):
        return []
    # Durable execution: checkpoint loop state so `maverick resume`
    # continues from the last step after a crash. Off unless opted in.
    lines = ["", "[durable]"]
    for k, v in durable.items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_assessments(assessments: dict[str, Any] | None) -> list[str]:
    if not assessments:
        return []
    # Assessment assists + privacy ops are default-on; write tables only when
    # the operator opted OUT of something (write-only-on-divergence).
    body = {k: v for k, v in assessments.items()
            if k not in ("privacy_ops", "paper_review_use_model",
                         "paper_review_org", "entity_graph")}
    lines: list[str] = []
    if body:
        lines += ["", "[assessments]"]
        for k, v in body.items():
            _emit_kv(lines, k, v)
    if "privacy_ops" in assessments:
        lines += ["", "[privacy_ops]"]
        _emit_kv(lines, "enable", assessments["privacy_ops"])
    if "paper_review_use_model" in assessments \
            or "paper_review_org" in assessments:
        lines += ["", "[paper_review]"]
        if "paper_review_use_model" in assessments:
            _emit_kv(lines, "use_model", assessments["paper_review_use_model"])
        if "paper_review_org" in assessments:
            _emit_kv(lines, "org_name", assessments["paper_review_org"])
    if "entity_graph" in assessments:
        lines += ["", "[entity_graph]"]
        _emit_kv(lines, "enable", assessments["entity_graph"])
    return lines


def _cfg_security_suite(security_suite: dict[str, Any] | None) -> list[str]:
    if security_suite is None:
        return []
    return [
        "",
        "[evidence_graph]",
        f"enable = {'true' if security_suite.get('evidence_graph', False) else 'false'}",
        "",
        "[threat_hunt]",
        f"enable = {'true' if security_suite.get('threat_hunt', False) else 'false'}",
    ]

def _cfg_capabilities(
    capability_config: dict[str, Any],
) -> list[str]:
    lines: list[str] = []
    if capability_config:
        lines.append("")
        lines.append("[capabilities]")
        for k, v in capability_config.items():
            lines.append(f"{k} = {str(v).lower()}")
    return lines


def _cfg_suites(suites: dict[str, bool] | None) -> list[str]:
    if not suites:
        return []
    # Per-suite enable/disable; the kernel's enabled_domains() reads this.
    lines = ["", "[suites]"]
    for k, v in suites.items():
        lines.append(f"{k} = {str(v).lower()}")
    return lines


def _cfg_advanced(  # noqa: C901 - flat sequence of independent feature toggles
    advanced: dict[str, Any] | None,
) -> list[str]:
    if not advanced:
        return []
    lines: list[str] = []
    # These governed-learning sections are default-on in the kernel. Preserve a
    # deliberate "no" from the advanced wizard instead of silently omitting the
    # table (which would now mean on). True choices are emitted with their richer
    # comments/settings in the existing blocks below.
    _learning_sections = {
        "reflexion": "reflexion",
        "self_harness": "self_harness",
        "dreaming": "dreaming",
        "experience_guidance": "experience",
        "credit_assignment": "credit",
        "rehearsal": "rehearsal",
        "data_engine": "data_engine",
        "operations_scientist": "operations_scientist",
        "consequence": "consequence",
    }
    for choice, section in _learning_sections.items():
        if choice in advanced and advanced[choice] is False:
            lines.extend(["", f"[{section}]", "enable = false"])
    if advanced.get("risk_proportional_verify"):
        lines.append("")
        lines.append("[verification]")
        lines.append("risk_proportional = true")
    if advanced.get("autonomy_gate") or advanced.get("headless_assume"):
        lines.append("")
        lines.append("[autonomy]")
        if advanced.get("autonomy_gate"):
            lines.append("enable = true")
        # Independent axis: assume-and-proceed instead of blocking on ask_user.
        if advanced.get("headless_assume"):
            lines.append("headless_assume = true")
    if advanced.get("governed_actions"):
        lines.append("")
        lines.append("[actions]")
        lines.append("enable = true")
    if advanced.get("calibration_enforce") or advanced.get("reward_laundering"):
        lines.append("")
        lines.append("[calibration]")
        lines.append("enforce = true")
        if advanced.get("reward_laundering"):
            lines.append("# Reward-laundering interlock: also freeze learning when the")
            lines.append("# verifier's edge on adversarial probes falls below this fraction")
            lines.append("# of its edge on natural traffic (the judge is being gamed). Needs")
            lines.append("# adversarial probes recorded (calibration.record_probe) to bite.")
            lines.append("min_resistance = 0.5")
    if advanced.get("adaptive_compute"):
        lines.append("")
        lines.append("[adaptive_compute]")
        lines.append("enable = true")
    if advanced.get("best_of_n"):
        lines.append("")
        lines.append("[search]")
        lines.append("enable = true")
    if advanced.get("structured_verifier_off") or advanced.get("audit_rewards"):
        lines.append("")
        lines.append("[reasoning_reward]")
        if advanced.get("structured_verifier_off"):
            lines.append("# The structured rubric verifier is ON by default (per-dimension")
            lines.append("# scoring + safety veto); disable it to fall back to the scalar")
            lines.append("# verifier verdict (maverick.reasoning_reward).")
            lines.append("enable = false")
        if advanced.get("audit_rewards"):
            lines.append("# Sign each structured reward into the tamper-evident audit")
            lines.append("# chain (provable learning) -- the evidence the system learns")
            lines.append("# from, not just a mutable verdict. Enabled by default.")
            lines.append("audit_rewards = true")
    if advanced.get("jit_rl_off"):
        lines.append("")
        lines.append("[jit_rl]")
        lines.append("# JitRL test-time adaptation is ON by default (a no-op until it has")
        lines.append("# learned something); disable it entirely here (maverick.jit_rl).")
        lines.append("enable = false")
    if advanced.get("experience_guidance"):
        lines.append("")
        lines.append("[experience]")
        lines.append("enable = true")
    if advanced.get("credit_assignment"):
        lines.append("")
        lines.append("[credit]")
        lines.append("enable = true")
    # The [self_improvement] block carries several default-on sub-toggles; emit
    # explicit values when the wizard records a choice.
    has_learning_policy = any(
        key in advanced
        for key in ("causal_promotion", "evaluator_evolution")
    )
    if has_learning_policy or advanced.get("signed_approval"):
        lines.append("")
        lines.append("[self_improvement]")
        lines.append("enable = true")
        lines.append("capture = true")
        lines.append("prm_guidance = true")
        if "causal_promotion" in advanced:
            lines.append("# Promote learned changes on their confounder-adjusted causal")
            lines.append("# effect (maverick.promotion_effect), not a correlation. Applies")
            lines.append("# when self-improvement is enabled.")
            lines.append(
                f"causal_promotion = {str(bool(advanced.get('causal_promotion'))).lower()}"
            )
        if "evaluator_evolution" in advanced:
            lines.append("# Promote a better judge instead of only freezing when the")
            lines.append("# evaluator drifts: a challenger replaces the incumbent only when")
            lines.append("# its agreement with a checksum-locked ground-truth anchor beats")
            lines.append("# it (maverick.evaluator_evolution, 'evaluator' rung).")
            lines.append(
                f"evaluator_evolution = {str(bool(advanced.get('evaluator_evolution'))).lower()}"
            )
        if advanced.get("signed_approval"):
            lines.append("# Require a cryptographic (Ed25519) human approval for the")
            lines.append("# code/weights rungs instead of a self-settable boolean, bound to")
            lines.append("# the exact change (maverick.approval_signing). Fails closed until")
            lines.append("# you provide approver PUBLIC keys (hex) or a dir of *.pub.")
            lines.append("require_signed_approval = true")
            lines.append('# approver_keys = ["<ed25519-pubkey-hex>"]')
            lines.append('# approver_keys_dir = "/etc/maverick/approvers"')
    if advanced.get("rehearsal"):
        lines.append("")
        lines.append("[rehearsal]")
        lines.append("# Simulate a risky plan against the learned world-model before it")
        lines.append("# runs; proceed when confidently safe, block a poor outcome, escalate")
        lines.append("# the unknown (maverick.rehearsal). Fail-open while disabled.")
        lines.append("enable = true")
    if advanced.get("data_engine"):
        lines.append("")
        lines.append("[data_engine]")
        lines.append("# Triage production failures by causal impact on real outcomes, then")
        lines.append("# mine + validate + promote fixes (maverick.data_engine). The Tesla")
        lines.append("# governed firm-improvement loop; reads the trajectory store.")
        lines.append("enable = true")
    if advanced.get("operations_scientist"):
        lines.append("")
        lines.append("[operations_scientist]")
        lines.append("# Discover a better process and prove it: pair a harmful action with")
        lines.append("# the beneficial habit that should replace it, validate the swap in the")
        lines.append("# world-model, then experiment for real (maverick.operations_scientist).")
        lines.append("enable = true")
    if advanced.get("consequence"):
        lines.append("")
        lines.append("[consequence]")
        lines.append("# Ground learning in REAL downstream outcomes: a recorded consequence")
        lines.append("# (invoice paid, ticket reopened) overrides the model's self-graded")
        lines.append("# proxy reward, so the data engine learns from reality (maverick.consequence).")
        lines.append("enable = true")
    if advanced.get("flows"):
        lines.append("")
        lines.append("[flows]")
        lines.append("# Deterministic control-flow skeleton (branch/foreach/parallel/approval/")
        lines.append("# delay) over agentic + tool steps; migrated workflows keep their shape")
        lines.append("# while any node can still be a full agentic goal (maverick.flow).")
        lines.append("enable = true")
        lines.append("# Per-node retry cap; bounds repeated provider/tool calls.")
        lines.append("max_node_retries = 5")
        if advanced.get("flows_auto_evolve"):
            lines.append("# Autonomously revert a node rewrite whose grounded outcomes")
            lines.append("# regressed (the loop undoes its own bad changes).")
            lines.append("auto_evolve = true")
        if advanced.get("flows_public_url"):
            lines.append("# Externally-reachable dashboard base URL for signed channel")
            lines.append("# Approve/Reject links (actionable approvals).")
            _emit_kv(lines, "public_url", advanced["flows_public_url"])
    if advanced.get("connections"):
        lines.append("")
        lines.append("[connections]")
        lines.append("# Named SaaS connections (base URL + sealed token) so a connector")
        lines.append("# can be wired from the dashboard without <NAME>_TOKEN env vars; an")
        lines.append("# existing env var still takes precedence (maverick.connections).")
        lines.append("enable = true")
    if advanced.get("emergent_protocol"):
        lines.append("")
        lines.append("[emergent_protocol]")
        lines.append("# Learn short codes for the swarm's repeated coordination boilerplate;")
        lines.append("# every code decodes exactly back to English, so nothing is hidden from")
        lines.append("# the Shield/audit (maverick.emergent_protocol). No-op until learned.")
        lines.append("enable = true")
    if advanced.get("emergent_codec"):
        lines.append("")
        lines.append("[emergent_codec]")
        lines.append("# Measure the token-aware codec (maverick.emergent_tokens) on the live")
        lines.append("# coordination stream: byte-stuffed cheap codes that save real tokens.")
        lines.append("# Telemetry only -- the rendered text agents/Shield see is unchanged.")
        lines.append("enable = true")
    if advanced.get("enforce_quotas"):
        lines.append("")
        lines.append("[quotas]")
        lines.append("enforce = true")
        # Starter daily caps per principal; edit or set to 0 to disable a
        # dimension. The kernel also reads MAVERICK_QUOTA_* env overrides.
        lines.append("max_dollars_per_day = 25.0")
        lines.append("max_tokens_per_day = 5000000")
    if advanced.get("tenant_by_user"):
        lines.append("")
        lines.append("[tenancy]")
        lines.append("by_user = true")
    _client_id = str(advanced.get("client_id") or "").strip()
    if _client_id:
        lines.append("")
        lines.append("[client]")
        lines.append(f'id = "{_client_id}"')
        # Enforced: refuse to start unbound so this client's data can never land
        # in the shared root. Also set MAVERICK_CLIENT_ID in the service unit.
        lines.append("enforce = true")
    if advanced.get("enterprise"):
        lines.append("")
        lines.append("[enterprise]")
        lines.append("mode = true")
    if advanced.get("compliance_disclosure_text"):
        lines.append("")
        lines.append("# AI-disclosure line surfaced to users (maverick.compliance).")
        lines.append("[compliance]")
        _emit_kv(lines, "disclosure_text", advanced["compliance_disclosure_text"])
    if advanced.get("anonymous_logs"):
        lines.append("")
        lines.append("[privacy]")
        lines.append("anonymous = true")
    if advanced.get("encrypt_at_rest"):
        lines.append("")
        lines.append("[encryption]")
        lines.append("at_rest = true")
        if advanced.get("encrypt_per_tenant"):
            lines.append("per_tenant = true")
    if advanced.get("audit_sign"):
        lines.append("")
        lines.append("[audit]")
        lines.append("sign = true")
    if advanced.get("audit_worm"):
        lines.append("")
        # WORM export of closed audit day-files. Defaults to a local read-only
        # mirror (best-effort, tamper-evident); switch provider to "s3" + an
        # Object-Lock bucket for regulator-grade immutability. Ship with
        # `maverick audit worm push` (see docs/security-hardening.md).
        lines.append("[audit.worm]")
        lines.append('provider = "local"   # or "s3" for S3 Object-Lock')
        lines.append("retention_days = 2555   # lock duration (~7y)")
        lines.append('# bucket = "my-audit-worm"   # s3: Object-Lock + versioning enabled')
        lines.append('# prefix = "maverick/audit/"')
        lines.append('# mode = "COMPLIANCE"        # COMPLIANCE | GOVERNANCE')
        lines.append('# region = "us-east-1"')
    # Dashboard editing locks. Both default on, so we only emit the disables --
    # and as ONE [features] table (two tables would be a duplicate-key TOML
    # error). The kernel reads these via config.get_features.
    _feature_locks = []
    if advanced.get("allow_pack_editing") is False:
        _feature_locks.append("pack_editing = false")
    if advanced.get("allow_role_editing") is False:
        _feature_locks.append("role_editing = false")
    if _feature_locks:
        lines.append("")
        lines.append("[features]")
        lines.extend(_feature_locks)
    if advanced.get("tree_of_thought"):
        lines.append("")
        lines.append("[planning]")
        lines.append('mode = "tree_of_thought"')
    _context_lines = []
    if advanced.get("compact_history"):
        _context_lines.append("compact = true")
    strat = advanced.get("compaction_strategy")
    if strat and strat != "default":
        _context_lines.append(f'compaction_strategy = "{strat}"')
    if advanced.get("model_scaled_context") is False:
        # Default-on; only a decline needs a line.
        _context_lines.append("model_scaled = false")
    if _context_lines:
        lines.append("")
        lines.append("[context]")
        lines.extend(_context_lines)
    _attach_lines = []
    if advanced.get("attach_any_mime"):
        _attach_lines.append("allow_any_mime = true")
    if advanced.get("attachment_understanding") is False:
        # Default-on; only a decline needs a line.
        _attach_lines.append("extract_text = false")
    if _attach_lines:
        lines.append("")
        lines.append("[attachments]")
        lines.extend(_attach_lines)
    if advanced.get("reflexion"):
        lines.append("")
        lines.append("[reflexion]")
        lines.append("enable = true")
    if advanced.get("self_harness"):
        lines.append("")
        lines.append("[self_harness]")
        lines.append("enable = true")
        lines.append("risk_limited = true")
        # Ship production-safe validation floors by default (an operator can
        # relax them): never promote on only the mined examples, require an
        # unseen-sample floor, and an effect-size floor so noisy lifts don't
        # promote. No effect until a promotion pass actually runs.
        lines.append("require_held_out = true")
        lines.append("min_held_out = 5")
        lines.append("min_delta = 0.02")
        # Optional advanced paths, written only when the operator opted in above;
        # each is at its historical default otherwise, so the block stays minimal.
        if advanced.get("self_harness_eval_corpus"):
            # Free-text path: escape via _toml_str so a path with a backslash or
            # quote can't corrupt the config the wizard writes.
            _emit_kv(lines, "eval_corpus", advanced["self_harness_eval_corpus"])
            eval_budget = advanced.get("self_harness_eval_budget")
            if eval_budget and eval_budget > 0:
                lines.append(f"eval_budget_dollars = {float(eval_budget)}")
        if advanced.get("self_harness_bucket_domain"):
            lines.append('mine_bucket_by = ["domain"]')
        if advanced.get("self_harness_semantic_mining"):
            lines.append("semantic_mining = true")
        if advanced.get("self_harness_efficacy_review"):
            lines.append("efficacy_review = true")
        if advanced.get("self_harness_canary"):
            lines.append("promote_as_canary = true")
        if advanced.get("self_harness_auto_run"):
            lines.append("auto_run = true")
        elif "self_harness_auto_run" in advanced:
            lines.append("auto_run = false")
        if advanced.get("self_harness_metamorphic"):
            lines.append("metamorphic = true")
        if advanced.get("self_harness_relapse"):
            lines.append("relapse_failure_share = 0.5")
        if advanced.get("self_harness_calibrate_judge"):
            lines.append("calibrate_judge = true")
        if advanced.get("self_harness_corpus_harvest"):
            _emit_kv(lines, "corpus_harvest", advanced["self_harness_corpus_harvest"])
        if advanced.get("self_harness_store"):
            _emit_kv(lines, "store", advanced["self_harness_store"])
        cands = advanced.get("self_harness_candidates")
        if cands and cands > 1:
            lines.append(f"candidates_per_signature = {int(cands)}")
        retire_days = advanced.get("self_harness_retire_days")
        if retire_days and retire_days > 0:
            lines.append(f"retire_after_days = {int(retire_days)}")
    if advanced.get("memory_guard"):
        lines.append("")
        lines.append("[memory_guard]")
        lines.append("enable = true")
    if advanced.get("temporal_memory"):
        lines.append("")
        lines.append("[memory]")
        lines.append("temporal = true")
    if advanced.get("fairness_monitor"):
        lines.append("")
        lines.append("[fairness_monitor]")
        lines.append("enable = true")
    # Discipline defaults ON; only an explicit decline is written.
    if advanced.get("specialist_discipline") is False:
        lines.append("")
        lines.append("[domains]")
        lines.append("discipline = false")
    if advanced.get("dreaming"):
        lines.append("")
        lines.append("[dreaming]")
        lines.append("enable = true")
        if advanced.get("dreaming_llm_consolidation"):
            lines.append("llm_consolidation = true")
    if advanced.get("effort"):
        lines.append("")
        lines.append("[effort]")
        lines.append("enabled = true")
    if advanced.get("cache_prewarm"):
        lines.append("")
        lines.append("[cache]")
        lines.append("prewarm = true")
    tool_lines: list[str] = []
    if advanced.get("output_cache"):
        tool_lines.append("output_cache = true")
    if tool_lines:
        lines.append("")
        lines.append("[tools]")
        lines.extend(tool_lines)
    if advanced.get("local_runtime"):
        lines.append("")
        lines.append("[local_runtime]")
        lines.append("enabled = true")
        lines.append('# engine = "vllm"  # vllm | tgi | llamacpp')
        lines.append('# model  = "..."   # REQUIRED for the local-runtime planner')
    oidc = advanced.get("oidc") or {}
    if isinstance(oidc, dict) and oidc.get("enabled"):
        # SSO ID-token verification for `maverick serve`. Its own table
        # (written once), so no duplicate-[auth.oidc] bug. The kernel reads
        # it via maverick.oidc.oidc_enabled() / load_oidc_config().
        lines.append("")
        lines.append("[auth.oidc]")
        lines.append("enabled = true")
        _emit_kv(lines, "issuer", oidc.get("issuer", ""))
        _emit_kv(lines, "audience", oidc.get("audience", ""))
        _emit_kv(lines, "jwks_uri", oidc.get("jwks_uri", ""))
        # Built-in browser-login fields, written ONLY when the operator
        # opted into that flow (so a bearer-only OIDC config is unchanged).
        # The kernel's login_enabled() additionally gates the routes.
        for key in (
            "client_id", "client_secret", "redirect_uri", "session_secret",
        ):
            val = oidc.get(key)
            if val:
                _emit_kv(lines, key, val)
    dept = advanced.get("department_access") or {}
    if isinstance(dept, dict) and dept.get("default_suites"):
        # Department scoping for signed-in users. default_suites is
        # deny-by-default for users with no explicit grant.
        lines.append("")
        lines.append("[dashboard]")
        lines.append("# Departments a signed-in user may use until an admin "
                     "grants more.")
        _emit_kv(lines, "default_suites", list(dept["default_suites"]))
    return lines


def _cfg_security(tool_acl: dict[str, Any] | None, autofix: bool,
                  dual_approval: bool = False) -> list[str]:
    if not (tool_acl or autofix or dual_approval):
        return []
    lines = ["", "[security]"]
    if autofix:
        lines.append("auto_fix = true")
    if dual_approval:
        # N-of-M dual control (two-person rule): high/critical-risk actions need
        # 2 distinct approvers and the requester can't self-approve. See
        # docs/security-hardening.md. Use a [security.approvals_required] table
        # to vary N per risk band.
        lines.append("approvals_required = 2")
        lines.append("allow_self_approval = false")
    for k, v in (tool_acl or {}).items():
        _emit_kv(lines, k, v)
    return lines


def _cfg_rate_limits(rate_limits: dict[str, str] | None) -> list[str]:
    if not rate_limits:
        return []
    lines = ["", "[rate_limits]"]
    for name, spec in rate_limits.items():
        # Quote names that aren't bare identifiers (e.g. "http_*").
        key = name if name.replace("_", "").isalnum() else f'"{name}"'
        # spec is free-text ("N/seconds"); escape via _toml_str so a stray
        # quote/backslash can't corrupt the config the wizard writes.
        lines.append(f'{key} = {_toml_str(spec)}')
    return lines


def _cfg_table(header: str, mapping: dict[str, Any] | None) -> list[str]:
    if not mapping:
        return []
    lines = ["", f"[{header}]"]
    for k, v in mapping.items():
        _emit_kv(lines, k, v)
    return lines


def _backup_wizard_file(path: Path) -> None:
    """Best-effort private backup of a file the wizard is about to replace."""

    try:
        if not path.exists():
            return
        backup = Path(f"{path}.bak")
        temporary = Path(f"{backup}.tmp")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with path.open("rb") as source, os.fdopen(fd, "wb") as destination:
                fd = -1
                shutil.copyfileobj(source, destination)
            try:
                stat = path.stat()
                os.utime(temporary, (stat.st_atime, stat.st_mtime))
            except OSError:
                pass
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
        finally:
            if fd != -1:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        pass


def _wizard_file_unchanged(path: Path, data: bytes) -> bool:
    try:
        if not path.is_file():
            return False
        # Text-mode writes use CRLF on Windows. Compare logical content so the
        # same wizard answers are idempotent on every supported OS.
        existing = path.read_bytes().replace(b"\r\n", b"\n")
        expected = data.replace(b"\r\n", b"\n")
        return existing == expected
    except OSError:
        return False


def _tighten_wizard_file(path: Path) -> None:
    # A true no-op re-run still repairs custody if an older install left a
    # matching file with broad permissions.
    try:
        from maverick.file_lock import ensure_private_file

        ensure_private_file(path, 0o600)
    except ImportError:  # pragma: no cover -- installer normally depends on core
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _write_wizard_file(path: Path, body: str) -> None:
    """Write one private wizard file, preserving a logically equivalent retry."""

    if _wizard_file_unchanged(path, body.encode("utf-8")):
        _tighten_wizard_file(path)
        console.print(f"[green]ok[/green] unchanged {path} (chmod 600)")
        return
    _backup_wizard_file(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            destination.write(body)
    finally:
        # If the file already existed at a wider mode, tighten it.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    console.print(f"[green]ok[/green] wrote {path} (chmod 600)")


def write_config(
    providers: list[str],
    run_model: str,
    safety: dict[str, Any],
    budget: dict[str, Any],
    sandbox: dict[str, Any],
    keys: dict[str, str],
    capabilities: dict[str, bool] | None = None,
    *,
    advanced: dict[str, Any] | None = None,
    tool_acl: dict[str, Any] | None = None,
    rate_limits: dict[str, str] | None = None,
    retention: dict[str, int] | None = None,
    persona: dict[str, str] | None = None,
    personas: dict[str, Any] | None = None,
    web_search_enabled: bool = False,
    skills: dict[str, Any] | None = None,
    self_learning: dict[str, Any] | None = None,
    flows: dict[str, Any] | None = None,
    knowledge: dict[str, Any] | None = None,
    oauth: dict[str, Any] | None = None,
    durable: dict[str, Any] | None = None,
    assessments: dict[str, Any] | None = None,
    security_suite: dict[str, Any] | None = None,
    deployment: str | None = None,
    suites: dict[str, bool] | None = None,
    governance_profile: str | None = None,
) -> None:
    # Validate the confidentiality-bearing provider/model authority before any
    # directory, config, or credential write. A malformed pin must not leave a
    # partial installation behind.
    run_model = _validate_run_model(run_model, providers)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Re-running the wizard truncates config.toml / .env. The loader explicitly
    # supports hand-editing, so back up any existing file first (0o600) instead
    # of silently destroying a user's manual edits.
    if keys:
        # Atomic + perm-from-creation: previous version was
        # ``write_text(...)`` followed by ``chmod(0o600)``, which left
        # the file world-readable (0o644) for one syscall. Open with
        # ``O_CREAT | O_WRONLY | O_TRUNC`` and mode 0o600 so the file
        # never exists at any other permission.
        body = "\n".join(f"{k}={v}" for k, v in keys.items()) + "\n"
        _write_wizard_file(ENV_FILE, body)

    # [flows] can be requested by two independent wizard steps: the dedicated
    # flow-engine step (the `flows` arg) and the advanced-reasoning "Flow
    # engine?" toggle (`advanced["flows"]`). Emitting the table from both paths
    # writes [flows] twice, which is invalid TOML -- tomllib rejects the WHOLE
    # file and load_config() silently falls back to {} (every wizard choice
    # lost; the smoke test then reports "sandbox backend missing"). Fold the
    # advanced flow extras into the single dedicated [flows] table so it is
    # emitted exactly once. Copy `advanced` so we never mutate the caller's dict.
    advanced = dict(advanced or {})
    if advanced.pop("flows", False):
        flows = dict(flows or {})
        flows.setdefault("enable", True)
        if advanced.pop("flows_auto_evolve", False):
            flows.setdefault("auto_evolve", True)
        _flows_url = advanced.pop("flows_public_url", None)
        if _flows_url:
            flows.setdefault("public_url", _flows_url)

    lines = [
        "# Maverick config. Regenerate with:  maverick init",
        "",
    ]
    lines += _cfg_deployment(deployment)
    lines += _cfg_governance(governance_profile)
    lines += _cfg_providers(providers)
    lines += _cfg_run_model(run_model, providers)
    lines += _cfg_core(budget, safety, sandbox)
    lines += _cfg_skills(skills)
    lines += _cfg_self_learning(self_learning)
    lines += _cfg_flows(flows)
    lines += _cfg_knowledge(knowledge)
    lines += _cfg_oauth(oauth)
    lines += _cfg_durable(durable)
    lines += _cfg_assessments(assessments)
    lines += _cfg_security_suite(security_suite)

    # The firm build has no browser/computer/code-exec/deferred-tool switches.
    # Preserve only the retained policy flags if an older caller passes a
    # legacy capability mapping.
    capability_config = {
        key: value
        for key, value in dict(capabilities or {}).items()
        if key in {"enforce", "per_call_tokens", "web_search"}
    }
    if web_search_enabled and not capability_config.get("web_search"):
        # web_search is wired through enable_web_search at kernel
        # boot; reflect the wizard's pick under [capabilities].
        capability_config["web_search"] = True
    if advanced and advanced.get("enforce_capabilities"):
        capability_config["enforce"] = True
    if advanced and advanced.get("per_call_token_exchange"):
        # Per-call token exchange only makes sense atop capability enforcement
        # (a token minted from no grant has nothing to scope), so turning it on
        # implies enforcement.
        capability_config["enforce"] = True
        capability_config["per_call_tokens"] = True

    lines += _cfg_capabilities(capability_config)
    lines += _cfg_suites(suites)
    lines += _cfg_advanced(advanced)
    lines += _cfg_security(tool_acl, bool((advanced or {}).get("security_autofix")),
                           dual_approval=bool((advanced or {}).get("dual_approval")))
    lines += _cfg_rate_limits(rate_limits)
    lines += _cfg_table("retention", retention)
    lines += _cfg_table("persona", persona)
    lines += _cfg_table("personas", personas)

    # SECURITY: config.toml is NOT secret-free. Unlike API keys (which live in
    # ~/.maverick/.env and are referenced via ${VAR}), the OIDC browser-login
    # client_secret and session_secret (HMAC session-cookie signing key) are
    # written here as literal values. The 0600 mode below is therefore load-
    # bearing, not just tidiness -- never relax it, and treat this file as
    # secret-bearing in backups/log redaction. chmod 600 so multi-user hosts
    # don't leak it to other accounts.
    config_body = "\n".join(lines) + "\n"
    _write_wizard_file(CONFIG_FILE, config_body)


def smoke_test() -> bool:
    console.print()
    console.print("[dim]Running smoke test...[/dim]")
    try:
        from maverick.config import load_config
        cfg = load_config()
        assert cfg.get("sandbox", {}).get("backend"), "sandbox backend missing"
        console.print("[green]✓[/green] Config readable")
    except Exception as e:
        console.print(f"[red]✗[/red] Config read failed: {e}")
        return False

    try:
        import maverick_shield  # noqa: F401
        console.print("[green]✓[/green] Maverick Shield available")
    except ImportError:
        console.print("[yellow]⚠[/yellow] maverick-shield not installed (safety will be disabled)")

    try:
        import anthropic  # noqa: F401
        console.print("[green]✓[/green] Anthropic SDK available")
    except ImportError:
        console.print("[yellow]⚠[/yellow] anthropic not installed; install with: pip install anthropic")

    return True


PARTIAL_STATE_PATH = CONFIG_DIR / "wizard-partial.json"


def _save_partial(state: dict[str, Any]) -> None:
    """Persist wizard progress so --resume can pick up later."""
    try:
        import json as _json
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PARTIAL_STATE_PATH.write_text(_json.dumps(state, default=str))
        os.chmod(PARTIAL_STATE_PATH, 0o600)
    except OSError:
        pass


def _load_partial() -> dict[str, Any] | None:
    """Return persisted partial state, or None if absent."""
    if not PARTIAL_STATE_PATH.exists():
        return None
    try:
        import json as _json
        return _json.loads(PARTIAL_STATE_PATH.read_text())
    except (OSError, ValueError):
        return None


def _clear_partial() -> None:
    try:
        if PARTIAL_STATE_PATH.exists():
            PARTIAL_STATE_PATH.unlink()
    except OSError:
        pass


def run_fast() -> int:
    """``maverick init --fast``: zero-question setup with safe defaults.

    Fast mode cannot ask which provider may receive client material, so the
    operator must explicitly set ``MAVERICK_MODEL_OVERRIDE=provider:model``.
    The provider is derived from that pin; no vendor is guessed.
    """
    welcome()
    if not preflight():
        console.print(
            "[red]Preflight failed.[/red] Fix the issues above and re-run."
        )
        return 1
    console.print(
        "[bold]Fast setup:[/bold] using the explicitly pinned run model and "
        "safe defaults. "
        "Run `maverick init` (no --fast) anytime to customize.\n"
    )
    run_model = os.environ.get("MAVERICK_MODEL_OVERRIDE", "").strip()
    provider = run_model.partition(":")[0]
    providers = [provider] if provider else []
    try:
        run_model = _validate_run_model(run_model, providers)
    except ValueError as exc:
        console.print(
            "[red]Fast setup requires an explicit model pin.[/red] Set "
            "MAVERICK_MODEL_OVERRIDE to a supported provider:model value "
            f"before retrying ({exc})."
        )
        return 1
    safety = {
        "profile": "balanced",
        "block_threshold": "high",
        "scan_input": True,
        "scan_tool_calls": True,
        "scan_output": True,
        "compartments": False,
    }
    budget = {
        "max_dollars": 5.0,
        "max_wall_seconds": 3600.0,
        "max_tool_calls": 500,
    }
    # Non-interactive setup cannot obtain and verify the operator's immutable
    # Docker image digest, so it never guesses a mutable image tag. Advanced
    # setup is the explicit path for Docker.
    backend = "local"
    sandbox = {
        "backend": backend,
        "workdir": str(Path.home() / "maverick-workspace"),
        "timeout": 60,
    }
    console.print(
        "[yellow]![/yellow] Fast setup uses the [bold]local[/bold] backend. "
        "The firm registry does not expose host-mutating model tools. Run "
        "[bold]maverick init[/bold] interactively to configure Docker with an "
        "immutable image digest."
    )
    # Copy only credentials and endpoint settings for the explicitly selected
    # provider. Fast mode never prompts or configures a standby provider.
    keys: dict[str, str] = {}
    provider_info = catalog.PROVIDERS[provider]
    env_names = [provider_info.get("env"), *provider_info.get("env_vars", [])]
    for env_name in env_names:
        if env_name and os.environ.get(env_name):
            keys[env_name] = os.environ[env_name]
    write_config(
        providers, run_model, safety, budget,
        sandbox, keys, deployment="local",
    )
    # smoke_test() returns a bool and it was discarded, so `maverick init
    # --fast` printed "Fast setup finished" and exited 0 over a broken install
    # -- the one command whose entire promise is that you can trust the result
    # without answering questions. An installer that cannot fail is an
    # installer whose success means nothing.
    if not smoke_test():
        console.print()
        console.print(Panel.fit(
            "[bold red]Fast setup failed its smoke test.[/bold red]\n\n"
            "The config was written, but the checks above did not pass, so "
            "this install is not ready to run.\n"
            "Fix the failures listed above, then re-run "
            "[bold]maverick init --fast[/bold] (or `maverick doctor`).",
            border_style="red",
        ))
        return 1
    console.print()
    console.print(Panel.fit(
        "[bold green]Fast setup finished.[/bold green]\n\n"
        "Try: [bold]maverick dashboard[/bold]  # then compose your first goal in the web UI\n"
        "(If the selected provider needs credentials, add them to ~/.maverick/.env.)\n",
        border_style="green",
    ))
    return 0


CONSUMER_DEMO_GOAL = (
    "Draft a source-cited research memo for qualified-attorney review."
)


def pick_mode() -> str:
    """First-screen picker: consumer vs advanced.

    Council round-2 design: every launch starts here so a non-technical
    user lands in a four-question flow with safe defaults, and a power
    user can opt straight into the full wizard.
    """
    console.print()
    console.print(Panel.fit(
        "[bold]How do you want to set this up?[/bold]\n\n"
        "  consumer  Four questions, safe defaults. About a minute.\n"
        "  express   A few questions, then turn ON all safe features.\n"
        "  advanced  Pick models, controls, safety level, and budget.",
        border_style="cyan",
    ))
    pick = _q_select(
        "Pick a mode:",
        [
            "consumer - just get me running",
            "express  - turn on all features, few questions",
            "advanced - let me configure everything",
        ],
        default="consumer - just get me running",
    )
    return pick.split()[0]


def _consumer_budget() -> dict[str, float]:
    """Single-question budget chip picker for consumer mode."""
    pick = _q_select(
        "Stop after spending how much per task?",
        ["$1", "$5", "$20", "custom"],
        default="$5",
    )
    if pick == "custom":
        dollars = _safe_float(_q_text("  Custom cap ($)", default="5.0"), default=5.0)
    else:
        dollars = float(pick.lstrip("$"))
    return {
        "max_dollars": dollars,
        "max_wall_seconds": 600.0,
        "max_tool_calls": 100,
    }


def write_consumer_config(
    *,
    user_name: str,
    providers: list[str],
    run_model: str,
    keys: dict[str, str],
    workdir: str,
    budget: dict[str, float],
    profile: str | None = None,
) -> None:
    """Write a consumer-mode config with the safety-seat safe defaults.

    Single source of truth for the CLI consumer flow.
    ``profile`` is the governance level from :data:`GOVERNANCE_PROFILES`
    (unknown values fall back to the default) -- it layers the learning
    lifecycle + governance posture on top of the safety-seat base, which
    is identical at every level. Creates the workspace dir. Raises on
    write failure (caller renders the branded error).
    """
    preset = GOVERNANCE_PROFILES.get(profile or "")
    if preset is None:
        profile, preset = (
            DEFAULT_GOVERNANCE_PROFILE,
            GOVERNANCE_PROFILES[DEFAULT_GOVERNANCE_PROFILE],
        )
    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    write_config(
        providers,
        run_model,
        {
            "profile": "strict",          # strictest shield
            "block_threshold": "medium",  # block medium+ threats
            "scan_input": True,
            "scan_tool_calls": True,
            "scan_output": True,
        },
        budget,
        {
            "backend": "local",
            "workdir": str(Path(workdir).expanduser()),
            "timeout": 60,
        },
        keys,
        advanced=dict(preset["advanced"]),
        self_learning=dict(preset["self_learning"]),
        rate_limits={
            "web_search": "5/60",
        },
        retention=dict(preset["retention"]),
        persona={"name": "Maverick", "style": "balanced", "user_name": user_name},
        web_search_enabled=True,
        governance_profile=profile,
        deployment="local",
    )


def run_consumer() -> int:
    """Consumer flow. Writes a minimal config with
    consumer-grade safe defaults, then prints a one-line demo command."""
    console.print()
    console.print(Panel.fit(
        "[bold]Maverick setup[/bold]\n\n"
        "A few required questions. About a minute. You can change anything later\n"
        "by running [bold]maverick init[/bold] again.",
        border_style="cyan",
    ))

    if not preflight():
        console.print(
            "[red]Setup can't continue.[/red] Fix the issues above and try again."
        )
        return 1

    user_name = _q_text(
        "What should we call you?",
        default=os.environ.get("USER") or os.environ.get("USERNAME") or "",
    ).strip() or "you"

    profile = pick_governance_profile()

    providers = pick_providers()
    run_model = pick_run_model(providers)
    keys = collect_api_keys(providers, set())

    workdir = _q_text(
        "Where can Maverick work?",
        default=str(Path.home() / "Documents" / "Maverick"),
    ).strip() or str(Path.home() / "Documents" / "Maverick")

    budget = _consumer_budget()

    try:
        write_consumer_config(
            user_name=user_name, providers=providers, run_model=run_model,
            keys=keys, workdir=workdir, budget=budget, profile=profile,
        )
    except Exception as e:
        show_install_failure(e)
        return 1

    # First-goal nudge. Don't run the goal here (the kernel doesn't
    # stream into a wizard window today, and shelling out from inside
    # the installer is ugly); print a legal, matter-bound nudge instead.
    console.print()
    required_key = catalog.PROVIDERS[providers[0]].get("env")
    if keys or not required_key:
        console.print(Panel.fit(
            f"[bold green]Setup complete, {user_name}.[/bold green]\n\n"
            "Try your first goal in the web UI:\n"
            "  [bold]maverick dashboard[/bold]   web UI at http://127.0.0.1:8765\n"
            f"  (a good starter goal: \"{CONSUMER_DEMO_GOAL}\")\n\n",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            f"[bold yellow]Setup saved without an API key, {user_name}.[/bold yellow]\n\n"
            f"Add one later by exporting {required_key} or by running\n"
            "[bold]maverick init[/bold] again.",
            border_style="yellow",
        ))
    _clear_partial()
    return 0


# Express mode turns on the reviewed firm + self-improvement features with
# sane defaults -- everything that makes the retained runtime fully configured
# WITHOUT the deployment-specific controls that must stay explicit opt-in
# (operator key custody and public provider/host allow-lists).
# Kept as data so a test can assert exactly what express enables
# without driving the prompts. Every key here is one `_cfg_advanced` recognises.
_EXPRESS_ADVANCED: dict[str, Any] = {
    # stronger reasoning on the single operator-selected model
    "compact_history": True,
    "adaptive_compute": True,
    "risk_proportional_verify": True,
    "autonomy_gate": True,
    "effort": True,
    "cache_prewarm": True,
    "output_cache": True,
    # the closed self-improvement lifecycle
    "reflexion": True,
    "dreaming": True,
    "experience_guidance": True,
    "credit_assignment": True,
    "self_harness": True,
    "data_engine": True,
    "operations_scientist": True,
    "consequence": True,
    "rehearsal": True,
    "evaluator_evolution": True,
    # governed memory + provable-learning audit (non-blocking)
    "memory_guard": True,
    "temporal_memory": True,
    "fairness_monitor": True,
    "governed_actions": True,
    "calibration_enforce": True,
    "audit_sign": True,
    "audit_rewards": True,
    # product surface: named SaaS connections (flows go via the dedicated arg)
    "connections": True,
}

# Self-learning ON, but without generate-and-run-new-code autonomy.
_EXPRESS_SELF_LEARNING: dict[str, Any] = {
    "enable": True,
    "allow_provider_egress": False,
    "distill_local": True,
}


# --- Governance profiles: firm onboarding levels -------------------------------
#
# One early question maps to a preset
# bundle of ALREADY-EXISTING knobs. Every level gets the self-learning /
# self-improvement lifecycle (that's the product); what escalates is the
# governance posture around it: signed audit, enforced budgets, immutable
# retention, and signed approvals. Kept as data (same
# convention as _EXPRESS_ADVANCED) so tests can assert exactly what each level
# enables without driving prompts. Budget caps are NOT part of a profile: they
# are collected separately and are never optional at any level.

# The closed learning lifecycle every level turns on. Safe-by-construction
# subset: in-process learning + consolidation only -- no generate-and-run-code
# autonomy.
_LEARNING_LIFECYCLE: dict[str, Any] = {
    "reflexion": True,
    "dreaming": True,
    "experience_guidance": True,
    "credit_assignment": True,
    "self_harness": True,
    "data_engine": True,
    "evaluator_evolution": True,
    "consequence": True,
    "rehearsal": True,
}

_PROFILE_SELF_LEARNING: dict[str, Any] = dict(_EXPRESS_SELF_LEARNING)

# Governance additions by level (cumulative: standard ⊃ essentials,
# regulated ⊃ standard). Every key is one _cfg_advanced recognises.
_STANDARD_GOVERNANCE: dict[str, Any] = {
    "memory_guard": True,         # governed memory writes
    "governed_actions": True,     # side-effectful actions logged + gated
    "calibration_enforce": True,  # confidence calibration gate
    "fairness_monitor": True,
    "audit_sign": True,           # Ed25519-signed audit chain
    "enforce_quotas": True,       # per-principal daily spend/token caps
}
_REGULATED_GOVERNANCE: dict[str, Any] = {
    **_STANDARD_GOVERNANCE,
    "audit_rewards": True,        # provable-learning reward audit
    "audit_worm": True,           # WORM export of closed audit day-files
    "signed_approval": True,      # human Ed25519 sign-off for code/weights rungs
}

GOVERNANCE_PROFILES: dict[str, dict[str, Any]] = {
    "essentials": {
        "advanced": dict(_LEARNING_LIFECYCLE),
        "self_learning": dict(_PROFILE_SELF_LEARNING),
        "retention": {"audit_days": 30, "episodes_days": 90, "events_days": 30},
    },
    "standard": {
        "advanced": {**_LEARNING_LIFECYCLE, **_STANDARD_GOVERNANCE},
        "self_learning": dict(_PROFILE_SELF_LEARNING),
        "retention": {"audit_days": 90, "episodes_days": 180, "events_days": 90},
    },
    "regulated": {
        "advanced": {**_LEARNING_LIFECYCLE, **_REGULATED_GOVERNANCE},
        "self_learning": dict(_PROFILE_SELF_LEARNING),
        "retention": {"audit_days": 365, "episodes_days": 365, "events_days": 365},
    },
}

DEFAULT_GOVERNANCE_PROFILE = "essentials"

# Menu strings start with the profile name so the picker can split()[0] them,
# same convention as pick_mode().
GOVERNANCE_CHOICES: list[str] = [
    "essentials - firm baseline: exact-matter work and counsel review",
    "standard   - signed audit and enforced firm budgets",
    "regulated  - signed approvals, immutable audit, and long retention",
]


def _prior_governance_profile() -> str | None:
    """The [governance] profile recorded by a previous `maverick init`, if any.

    Same pattern as pick_deployment's prior-choice defaulting: a re-run of the
    wizard must default to what the operator ALREADY chose. Without this, an
    operator on a regulated install re-running init to change a name would
    silently downgrade to essentials -- dropping audit signing, quotas, and
    signed approvals -- by accepting the default. Reads CONFIG_FILE (the file
    this wizard writes) rather than the kernel loader so it honors the same
    path every other wizard read/write uses.
    """
    try:
        cfg = tomllib.loads(CONFIG_FILE.read_text())
        prior = (cfg.get("governance") or {}).get("profile", "")
        return prior if prior in GOVERNANCE_PROFILES else None
    except Exception:  # pragma: no cover -- absent/invalid config: no prior
        return None


def pick_governance_profile() -> str:
    """One onboarding question: how much governance does this business need?

    Defaults to the previously configured level on a re-run, else
    :data:`DEFAULT_GOVERNANCE_PROFILE`; falls back to the default on anything
    it doesn't recognise, so an <Enter> or a stubbed answer never lands
    outside the three supported levels.
    """
    console.print()
    console.print(
        "[dim]Every level gets self-learning and self-improvement. The level "
        "sets how much governance wraps it -- you can change it any time by "
        "re-running maverick init.[/dim]"
    )
    prior = _prior_governance_profile()
    default = next(
        (c for c in GOVERNANCE_CHOICES if c.split()[0] == prior),
        GOVERNANCE_CHOICES[0],
    )
    pick = _q_select(
        "What governance posture does this firm require?",
        list(GOVERNANCE_CHOICES),
        default=default,
    )
    name = pick.split()[0]
    return name if name in GOVERNANCE_PROFILES else (prior or DEFAULT_GOVERNANCE_PROFILE)


def run_express() -> int:
    """Express setup: a few user-specific questions, then turn on every safe
    single-user feature at once. The shortcut for "I want everything on"
    without sitting through the full advanced wizard. Host-dangerous and
    infra-shaped toggles stay off (and are listed for the user)."""
    console.print()
    console.print(Panel.fit(
        "[bold]Maverick express setup[/bold]\n\n"
        "A few questions, then every safe single-user feature is turned on:\n"
        "the flow engine, named connections, the self-improvement lifecycle\n"
        "(reflexion, dreaming, experience, self-harness, data engine),\n"
        "governed memory, web search, durable execution, and 31 legal profiles.\n\n"
        "[dim]Left off: operator-custodied key configuration and public\n"
        "provider/host allow-lists. Configure those deliberately for the firm's\n"
        "deployment after reviewing its confidentiality policy.[/dim]",
        border_style="cyan",
    ))

    if not preflight():
        console.print(
            "[red]Setup can't continue.[/red] Fix the issues above and try again."
        )
        return 1

    user_name = _q_text(
        "What should we call you?",
        default=os.environ.get("USER") or os.environ.get("USERNAME") or "",
    ).strip() or "you"
    profile = pick_governance_profile()
    providers = pick_providers()
    run_model = pick_run_model(providers)
    keys = collect_api_keys(providers, set())
    workdir = _q_text(
        "Where can Maverick work?",
        default=str(Path.home() / "Documents" / "Maverick"),
    ).strip() or str(Path.home() / "Documents" / "Maverick")
    budget = _consumer_budget()

    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    # Express turns on every safe single-user feature; the governance level
    # layers its posture (signed audit, quotas, WORM, signed approvals) on
    # top. Union of two all-True dicts, so profile keys can only ADD.
    preset = GOVERNANCE_PROFILES.get(
        profile, GOVERNANCE_PROFILES[DEFAULT_GOVERNANCE_PROFILE])
    try:
        write_config(
            providers=providers,
            run_model=run_model,
            safety={"profile": "balanced", "scan_input": True,
                    "scan_tool_calls": True, "scan_output": True},
            budget=budget,
            sandbox={"backend": "local",
                     "workdir": str(Path(workdir).expanduser()), "timeout": 60},
            keys=keys,
            advanced={**_EXPRESS_ADVANCED, **preset["advanced"]},
            persona={"name": "Maverick", "style": "balanced", "user_name": user_name},
            web_search_enabled=True,
            self_learning=dict(_EXPRESS_SELF_LEARNING),
            flows={"enable": True},
            oauth={"vault": True},
            durable={"enabled": True},
            deployment="local",
            # Express historically wrote NO [retention] table, and absent
            # means keep-forever (retention is opt-in pruning). Preserve that
            # at the default level; only an explicitly-picked governance
            # upgrade may introduce pruning windows.
            retention=(dict(preset["retention"])
                       if profile != DEFAULT_GOVERNANCE_PROFILE else None),
            governance_profile=profile,
        )
    except Exception as e:
        show_install_failure(e)
        return 1

    console.print()
    required_key = catalog.PROVIDERS[providers[0]].get("env")
    if keys or not required_key:
        console.print(Panel.fit(
            f"[bold green]Everything's on, {user_name}.[/bold green]\n\n"
            "Open the dashboard:\n"
            "  [bold]maverick dashboard[/bold]   web UI at http://127.0.0.1:8765\n\n"
            "[dim]Change anything in ~/.maverick/config.toml, or re-run "
            "maverick init.[/dim]",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            f"[bold yellow]Everything's on, but no API key yet, {user_name}.[/bold yellow]\n\n"
            f"Add one by exporting {required_key} or re-running "
            "[bold]maverick init[/bold].",
            border_style="yellow",
        ))
    _clear_partial()
    return 0


# (surface, one-line description) shown after a regulated-posture setup, so a
# non-technical operator discovers the verification + GDPR/EU AI Act
# documentation surfaces they'd otherwise never find. Dashboard pages (start the
# dashboard with `maverick dashboard`) plus the audit CLI. Defined as data so a
# test can assert the set without rendering the Rich panel.
_COMPLIANCE_COMMANDS: list[tuple[str, str]] = [
    ("/compliance", "GDPR + EU AI Act control coverage"),
    ("/safety", "live safety + governance posture"),
    ("/assessments", "run a PIA / AIRA / vendor-risk assessment"),
    ("/audit/binder", "auditor-ready evidence binder"),
    ("maverick audit verify", "verify the tamper-evident audit log"),
]


def _regulated_deployment(advanced: dict[str, Any]) -> bool:
    """True if the operator turned on a sensitive-data control, so the wizard
    should point them at the compliance + documentation commands."""
    advanced = advanced or {}
    return bool(
        advanced.get("enterprise")
        or advanced.get("encrypt_at_rest")
        or advanced.get("audit_sign")
        or advanced.get("audit_worm")
        or advanced.get("dual_approval")
        or advanced.get("security_autofix")
    )


def show_compliance_commands(advanced: dict[str, Any]) -> None:
    """Print the compliance/documentation command panel after a regulated setup.

    No-op unless the deployment enabled enterprise mode, at-rest encryption, or
    audit signing -- otherwise it's just noise for a personal install.
    """
    if not _regulated_deployment(advanced):
        return
    rows = "\n".join(
        f"  [bold]{cmd}[/bold]{' ' * max(1, 28 - len(cmd))}# {desc}"
        for cmd, desc in _COMPLIANCE_COMMANDS
    )
    console.print()
    console.print(Panel.fit(
        "[bold]You enabled a regulated-data posture.[/bold] Prove and document it\n"
        "in the dashboard (`maverick dashboard`):\n\n"
        f"{rows}\n\n"
        "[dim]See docs/regulated-deployment.md. Control coverage, not legal advice.[/dim]",
        border_style="cyan",
        title="Compliance & documentation",
    ))


def _run_simple_picks(state: dict[str, Any], _announce) -> dict[str, Any]:
    """Run the contiguous block of single-answer ``state.get(x) or pick_x()``
    steps (safety through advanced), persisting each. Returns the answers."""
    _announce()
    safety = state.get("safety") or pick_safety()
    state["safety"] = safety
    _save_partial(state)

    _announce()
    signed_skills = state.get("signed_skills") or pick_signed_skills()
    state["signed_skills"] = signed_skills
    _save_partial(state)

    _announce()
    budget = state.get("budget") or pick_budget()
    state["budget"] = budget
    _save_partial(state)

    _announce()
    sandbox = state.get("sandbox") or pick_sandbox()
    state["sandbox"] = sandbox
    _save_partial(state)

    # Retired high-impact capability prompts are deliberately ignored even
    # when resuming a partial file written by an older installer.
    state.pop("capabilities", None)
    capabilities: dict[str, bool] = {}

    _announce()
    self_learning = state.get("self_learning") or pick_self_learning()
    state["self_learning"] = self_learning
    _save_partial(state)

    _announce()
    flows = state.get("flows") or pick_flows()
    state["flows"] = flows
    _save_partial(state)

    _announce()
    knowledge = state.get("knowledge") or pick_knowledge()
    state["knowledge"] = knowledge
    _save_partial(state)

    _announce()
    oauth = state.get("oauth") or pick_oauth_vault()
    state["oauth"] = oauth
    _save_partial(state)

    _announce()
    durable = state.get("durable") or pick_durable()
    state["durable"] = durable
    _save_partial(state)

    # `is None` sentinel: keeping both defaults legitimately returns {}.
    _announce()
    assessments = state.get("assessments")
    if assessments is None:
        assessments = pick_assessments()
        state["assessments"] = assessments
        _save_partial(state)

    _announce()
    security_suite = state.get("security_suite")
    if security_suite is None:
        security_suite = pick_security_suite()
        state["security_suite"] = security_suite
        _save_partial(state)

    _announce()
    advanced = state.get("advanced") or pick_advanced()
    state["advanced"] = advanced
    _save_partial(state)

    return {
        "safety": safety,
        "signed_skills": signed_skills,
        "budget": budget,
        "sandbox": sandbox,
        "capabilities": capabilities,
        "self_learning": self_learning,
        "flows": flows,
        "knowledge": knowledge,
        "oauth": oauth,
        "durable": durable,
        "assessments": assessments,
        "security_suite": security_suite,
        "advanced": advanced,
    }


def _run_policy_picks(state: dict[str, Any], _announce) -> dict[str, Any]:
    """Run the ACL and local policy block.

    Uses the ``is None`` sentinel for steps whose legitimate answer is falsy.
    Returns the answers needed downstream by ``write_config``.
    """
    # NOTE: these steps use the `is None` sentinel (not `or`) because a
    # legitimately-declined answer is falsy ({}/[]); the `or` pattern treated
    # "I chose nothing" as "unanswered" and re-prompted it on --resume.
    _announce()
    tool_acl = state.get("tool_acl")
    if tool_acl is None:
        tool_acl = pick_tool_acl()
        state["tool_acl"] = tool_acl
        _save_partial(state)

    _announce()
    rate_limits = state.get("rate_limits")
    if rate_limits is None:
        rate_limits = pick_rate_limits()
        state["rate_limits"] = rate_limits
        _save_partial(state)

    _announce()
    retention = state.get("retention")
    if retention is None:
        retention = pick_retention()
        state["retention"] = retention
        _save_partial(state)

    return {
        "tool_acl": tool_acl,
        "rate_limits": rate_limits,
        "retention": retention,
    }


def run(fast: bool = False, resume: bool = False) -> int:
    if fast:
        return run_fast()
    # A non-interactive stdin (CI, Docker build, `... | maverick init`) can't
    # answer prompts -- questionary just prints "Input is not a terminal" and
    # the first prompt aborts with a terse "Aborted!". Detect it up front and
    # point at the paths that DO work without a TTY.
    if not sys.stdin.isatty():
        console.print(
            "[yellow]maverick init needs an interactive terminal.[/yellow]\n"
            "  - run it in a terminal, or\n"
            "  - use  [bold]maverick init --fast[/bold]  for recommended defaults, or\n"
            "  - edit  ~/.maverick/config.toml  by hand (see docs/configuration.md)."
        )
        return 1
    welcome()
    # Council round-2: mode picker on every launch. Consumer is default.
    # Skip the picker on --resume since it implies an in-progress
    # advanced flow.
    if not resume:
        mode = pick_mode()
        if mode == "consumer":
            return run_consumer()
        if mode == "express":
            return run_express()
    if not preflight():
        console.print(
            "[red]Preflight failed.[/red] Fix the issues above and re-run `maverick init`."
        )
        return 1

    # --resume: load any persisted partial state and only ask
    # questions the user hasn't answered yet.
    state: dict[str, Any] = {}
    if resume:
        loaded = _load_partial()
        if loaded:
            state = loaded
            console.print(
                f"[dim]Resuming from {PARTIAL_STATE_PATH}: "
                f"{len(state)} answers already on file.[/dim]\n"
            )
        else:
            console.print(
                f"[yellow]⚠[/yellow] No partial state at {PARTIAL_STATE_PATH}; "
                "starting fresh.\n"
            )

    # Progress bar: announce "Step N/M <label>" before each pick_*, with a
    # breadcrumb of steps already behind us. Purely cosmetic.
    _done: list[str] = []
    _step = [0]

    def _announce() -> None:
        _step[0] += 1
        console.print(_step_indicator(_step[0], done=_done), style="bold cyan")
        _done.append(STEPS[_step[0] - 1][1])

    _announce()
    deployment = state.get("deployment") or pick_deployment()
    state["deployment"] = deployment
    _save_partial(state)

    _announce()
    providers = state.get("providers") or pick_providers()
    while not providers:
        # Aborting on empty selection forced the user to restart the
        # whole wizard (UX seat finding). Re-ask instead.
        console.print(
            "[yellow]Pick at least one provider; Maverick needs an LLM.[/yellow]"
        )
        providers = pick_providers()
    state["providers"] = providers
    _save_partial(state)

    _announce()
    run_model = state.get("run_model")
    if run_model is None:
        run_model = pick_run_model(providers)
        state["run_model"] = run_model
        _save_partial(state)

    _simple = _run_simple_picks(state, _announce)
    safety = _simple["safety"]
    signed_skills = _simple["signed_skills"]
    budget = _simple["budget"]
    sandbox = _simple["sandbox"]
    capabilities = _simple["capabilities"]
    self_learning = _simple["self_learning"]
    flows = _simple["flows"]
    knowledge = _simple["knowledge"]
    oauth = _simple["oauth"]
    durable = _simple["durable"]
    assessments = _simple["assessments"]
    security_suite = _simple["security_suite"]
    advanced = _simple["advanced"]

    _announce()
    web_search_enabled, web_search_envs = (
        state.get("_web_search_pair") or pick_web_search()
    )
    state["_web_search_pair"] = [web_search_enabled, web_search_envs]
    _save_partial(state)

    _policy_block = _run_policy_picks(state, _announce)
    tool_acl = _policy_block["tool_acl"]
    rate_limits = _policy_block["rate_limits"]
    retention = _policy_block["retention"]

    _announce()
    persona = state.get("persona")
    if persona is None:
        persona = pick_persona()
        state["persona"] = persona
        _save_partial(state)

    personas = state.get("_personas") or pick_persona_roles()
    state["_personas"] = personas
    _save_partial(state)

    # Keys/sessions are never persisted to disk in the partial state
    # (they're secrets; the only safe place is ~/.maverick/.env).
    extra_envs = set(web_search_envs)
    keys = collect_api_keys(providers, extra_envs)
    # Enterprise connectors are always registered; collect any credentials the
    # user wants to wire up now (merged into ~/.maverick/.env, never persisted
    # to partial state). Editable later in the .env file.
    keys.update(pick_connectors())

    suites = pick_suites()

    console.print()
    if not _q_confirm("Write config and finish?", default=True):
        # Be honest about where the state lives and what restore does.
        console.print(
            f"Stopped. Partial answers saved to {PARTIAL_STATE_PATH}.\n"
            "Resume with: maverick init --resume"
        )
        return 0

    write_config(
        providers, run_model, safety, budget, sandbox,
        keys, capabilities,
        advanced=advanced,
        tool_acl=tool_acl,
        rate_limits=rate_limits,
        retention=retention,
        persona=persona,
        personas=personas,
        web_search_enabled=web_search_enabled,
        skills=signed_skills if (signed_skills.get("trusted_pubkeys") or signed_skills.get("require_signed")) else None,
        self_learning=self_learning if self_learning.get("enable") else None,
        flows=flows if flows.get("enable") else None,
        knowledge=knowledge if knowledge.get("enable") else None,
        oauth=oauth if oauth.get("vault") else None,
        durable=durable if durable.get("enabled") else None,
        assessments=assessments or None,
        security_suite=security_suite,
        deployment=deployment,
        suites=suites,
        # The advanced flow applies no preset -- the operator hand-picked
        # every knob -- so record "custom" rather than omitting the label
        # (dashboards keyed on [governance].profile would otherwise read
        # advanced-configured tenants, the most governance-sensitive ones,
        # as tier-less) or borrowing a level name the knobs may not match.
        governance_profile="custom",
    )
    _clear_partial()
    ok = smoke_test()
    if ok:
        console.print()
        console.print(Panel.fit(
            "[bold green]Setup complete.[/bold green]\n\n"
            "Try:\n"
            "  [bold]maverick dashboard[/bold]    # web UI at http://127.0.0.1:8765\n"
            "  [bold]maverick doctor[/bold]       # health check\n\n",
            border_style="green",
        ))
        show_compliance_commands(advanced)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
