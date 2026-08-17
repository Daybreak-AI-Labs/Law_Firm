"""Maverick CLI."""
from __future__ import annotations

import functools
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import click

# Council round-2 perf-seat fix: keep the top-level import surface
# minimal so `maverick --help` and `maverick version` don't pay for
# heavy submodules (orchestrator, agent, swarm, skills, sandbox) they
# never use. Submodules import lazily inside the command bodies that
# actually need them. ``world_model`` must be lazy too: importing it performs
# storage admission for its compatibility ``DEFAULT_DB`` hook, while diagnostic
# commands such as ``config-lint`` must be able to report a corrupt client
# configuration before any storage path is admitted.


def open_world(*args, **kwargs):
    """Lazy compatibility facade for the CLI's many world-model commands."""
    from ..world_model import open_world as _open_world

    return _open_world(*args, **kwargs)

_TERMINAL_CONTROL_RE = re.compile(
    r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$))"
    r"|(?:\x1b\[[0-?]*[ -/]*[@-~])"
    r"|(?:\x1b[@-Z\\-_])"
    r"|[\x00-\x1f\x7f-\x9f]"
)


def _strip_terminal_control(text: str) -> str:
    """Remove terminal control bytes before rendering untrusted text."""
    return _TERMINAL_CONTROL_RE.sub("", text)


def _default_model() -> str:
    """Lazy resolver so the click default callback doesn't pull `.llm`
    (and the anthropic SDK) at module import time."""
    from ..llm import DEFAULT_MODEL
    return DEFAULT_MODEL


def _fact_subject_token(channel: str, user: str) -> str:
    """Stable, delimiter-safe token for explicitly user-scoped facts."""
    return f"{quote(channel, safe='')}:{quote(user, safe='')}"


def _model_route_configuration_missing(
    model_spec: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return the selected model provider and its missing prerequisites.

    A global "some credential exists" probe cannot answer whether the route
    this command will actually instantiate is usable. It also rejects valid
    keyless routes such as Ollama/vLLM/TGI localhost defaults and authenticated
    Codex CLI installs. Delegate to the operator preflight's provider contract
    so the CLI and preflight cannot drift.
    """
    from ..config import load_config
    from ..operator_preflight import _route_configuration_missing
    from ..providers import _canonical

    spec = str(model_spec or _default_model()).strip()
    provider = _canonical(spec.split(":", 1)[0] if ":" in spec else "anthropic")
    return provider, _route_configuration_missing(provider, load_config())


def _require_llm_key(model_spec: str | None = None) -> str:
    """Refuse cleanly when a selected route (or any swarm route) is incomplete."""
    try:
        if model_spec is None:
            from ..config import load_config
            from ..operator_preflight import _routed_configuration_missing

            routed = _routed_configuration_missing(load_config())
            missing_routes = {
                provider: fields
                for provider, fields in routed.items()
                if fields
            }
        else:
            provider, missing = _model_route_configuration_missing(model_spec)
            missing_routes = {provider: missing} if missing else {}
    except Exception:
        missing_routes = {"selected": ("configuration",)}
    if not missing_routes:
        return "config"
    from ..operator_preflight import _format_route_missing

    detail = _format_route_missing(missing_routes)
    click.echo(
        "Maverick can't reach an LLM through every selected model route. "
        f"Missing: {detail}.\n"
        "\n"
        "Configure that route with:  maverick init\n"
        "Then verify it with:       maverick preflight",
        err=True,
    )
    sys.exit(2)


def _humanize_run_error(e: Exception) -> str:
    """Map an operational run failure to a one-line, actionable message.

    Sandbox/provider errors used to reach the user as a raw traceback. The
    failures a consumer actually hits -- no Docker daemon, a rejected or
    typo'd key, a dropped connection, exhausted credits -- each get a plain
    sentence and a next step instead.
    """
    name = type(e).__name__.lower()
    msg = str(e).strip()
    low = msg.lower()
    from ..runtime_overrides import RuntimeOverridesSecurityError

    if isinstance(e, RuntimeOverridesSecurityError):
        return (
            "Maverick stopped because the operator policy is unavailable.\n"
            "  Restore or repair runtime-overrides.toml, then run "
            "`maverick doctor` before retrying."
        )
    # Sandbox backends already raise an actionable RuntimeError, e.g.
    # "Docker not available. ... change [sandbox] backend to 'local'".
    if isinstance(e, RuntimeError) and (
        "not available" in low or "docker" in low or "podman" in low
        or "sandbox" in low
    ):
        return f"Couldn't start the sandbox.\n  {msg}"
    if "authentication" in name or "invalid x-api-key" in low or "401" in msg:
        return (
            "Your LLM API key was rejected (401).\n"
            "  Check the key in ~/.maverick/.env or your shell, then retry.\n"
            "  Diagnose with:  maverick doctor"
        )
    # A typo'd / unavailable model id surfaces as a provider 404 (Anthropic/
    # OpenAI raise NotFoundError). Point at `maverick config`, where [models]
    # are set -- NOT `maverick doctor`, which only validates the API key and
    # would send the user chasing a non-existent auth problem.
    if "notfound" in name or "404" in msg or ("model" in low and "not found" in low):
        return (
            "The model id wasn't found by the provider (404).\n"
            "  This usually means a typo'd or unavailable model id.\n"
            "  Check the model in [models]:  maverick config"
        )
    if "ratelimit" in name or "429" in msg:
        return ("The LLM provider rate-limited this run (429). "
                "Wait a moment and retry.")
    if ("connection" in name or "timeout" in name
            or "connect" in low or "network" in low):
        return ("Couldn't reach the LLM provider. Check your network "
                "connection, then retry.")
    if "quota" in low or "credit" in low or "insufficient" in low or "billing" in low:
        return (f"The LLM provider refused the request: {msg}\n"
                "  Check your plan / billing, then retry.")
    # Anything unanticipated: a short line, not a 20-frame stack trace.
    return (
        "The run stopped on an unexpected error.\n"
        f"  {type(e).__name__}: {msg}\n"
        "  Re-run with MAVERICK_DEBUG=1 for the full traceback, "
        "or check `maverick doctor`."
    )


def _humane_errors(fn):
    """Wrap a run-driving command so operational failures print a friendly
    message and exit non-zero instead of dumping a traceback (and exiting 0).

    Set ``MAVERICK_DEBUG=1`` to bypass and re-raise the original exception.
    Use as the innermost decorator (closest to ``def``).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SystemExit, click.ClickException, click.Abort):
            raise
        except KeyboardInterrupt:
            click.echo("\nInterrupted.", err=True)
            sys.exit(130)
        except Exception as e:  # noqa: BLE001 -- top-level humane boundary
            if os.environ.get("MAVERICK_DEBUG"):
                raise
            click.echo(_humanize_run_error(e), err=True)
            sys.exit(1)
    return wrapper


def _kernel():
    """Lazy-import the agent-runtime modules into a single namespace.

    Importing ``.orchestrator`` transitively pulls agent + swarm +
    blackboard + sandbox + skills + tools (~30 ms). Commands that
    don't drive the agent (``version``, ``doctor``, ``config``,
    ``audit``, ``cache``, ``retention``, ``skill *``, ``template *``)
    never need any of it. Call this at the top of any command that does.
    """
    import types

    from ..budget import Budget
    from ..llm import DEFAULT_MODEL, LLM
    from ..orchestrator import run_goal_sync
    from ..sandbox import build_sandbox
    from ..secrets import scrub
    return types.SimpleNamespace(
        Budget=Budget, LLM=LLM, DEFAULT_MODEL=DEFAULT_MODEL,
        run_goal_sync=run_goal_sync, build_sandbox=build_sandbox, scrub=scrub,
    )


def _run_outcome_blocked(world, goal_id: int) -> bool:
    """True if the goal ended in the kernel's ``blocked`` state -- paused
    awaiting a user answer, stopped by a budget/time cap, or refused by an
    input guard. ``start`` reads this before closing the world DB so it can
    exit nonzero for a halted/paused run (it used to always exit 0); a genuine
    ``done`` completion stays 0."""
    try:
        g = world.get_goal(goal_id)
    except Exception:  # pragma: no cover -- a status read must not mask the result
        return False
    return bool(g and g.status == "blocked")


def _maybe_start_progress_poller(world_path, goal_id, stop_poll):
    """Start the background goal-events poller, or return None when it should
    stay quiet: output isn't a TTY (don't litter piped logs), MAVERICK_NO_PROGRESS
    is set (runtime override), or [features] streaming is off (persistent opt-out)."""
    import threading

    from ..config import get_features
    try:
        streaming_on = get_features()["streaming"]
    except Exception:
        streaming_on = True
    if (not click.get_text_stream("stderr").isatty()
            or os.environ.get("MAVERICK_NO_PROGRESS")
            or not streaming_on):
        return None
    poller = threading.Thread(
        target=_stream_progress, args=(world_path, goal_id, stop_poll), daemon=True,
    )
    poller.start()
    return poller


_CLI_SECURITY_WARNING_LOGGERS = ("maverick.sandbox", "maverick.orchestrator")


class _CliDefaultWarningFilter(logging.Filter):
    """Keep routine WARNING noise quiet while surfacing safety warnings."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            return True
        return any(
            record.name == name or record.name.startswith(f"{name}.")
            for name in _CLI_SECURITY_WARNING_LOGGERS
        )


def _configure_cli_logging() -> None:
    """Keep routine library log lines off the consumer's terminal by default.

    The CLI installs no log handler, so Python's last-resort handler dumps any
    library WARNING/ERROR (e.g. "ignoring unreadable config.toml
    (TOMLDecodeError...)") straight to stderr mid-run. Install a root handler
    so routine warnings are suppressed by default while security-posture
    warnings still surface:

      - ``MAVERICK_DEBUG`` set  -> verbose: DEBUG-level logs to stderr.
      - ``MAVERICK_LOG_LEVEL``  -> honored as-is (operators who want logs).
      - otherwise               -> ERROR+ reaches the terminal, plus WARNING
        diagnostics from the sandbox / Shield safety path.

    Delegates to ``logging_config.configure_logging`` (idempotent) so the
    JSON-format / context-filter wiring stays in one place.
    """
    from ..logging_config import configure_logging
    default_warning_filter = False
    if os.environ.get("MAVERICK_DEBUG"):
        level = "DEBUG"
    elif os.environ.get("MAVERICK_LOG_LEVEL"):
        # Explicit operator logging preference wins as-is.
        level = os.environ["MAVERICK_LOG_LEVEL"]
    else:
        # Root must admit WARNING records so the filter below can pass through
        # sandbox / Shield safety warnings while dropping routine library noise.
        level = "WARNING"
        default_warning_filter = True
    try:
        configure_logging(level=level)
        if default_warning_filter:
            safety_filter = _CliDefaultWarningFilter()
            for handler in logging.getLogger().handlers:
                handler.addFilter(safety_filter)
    except Exception:  # pragma: no cover -- logging setup must never break the CLI
        pass


def _configure_cli_text_streams() -> None:
    """Keep redirected/frozen Windows consoles from crashing on Unicode.

    PyInstaller can freeze ``sys.stdout``/``stderr`` with the active Windows
    code page (often cp1252) and ``errors='strict'``.  Maverick's human-facing
    CLI intentionally uses a few status glyphs, so one unrepresentable glyph
    must degrade to ``?`` rather than aborting an otherwise healthy command.
    Preserve the selected encoding and only relax the error policy.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            # Click's test streams and embedded hosts may not be reconfigurable.
            # Their existing encoding policy remains authoritative.
            continue


@click.group(epilog=(
    "Day to day, the firm runs in the dashboard (`maverick dashboard`); the\n"
    "commands here are the operational surface: launchers (dashboard / mcp /\n"
    "worker), setup and health (doctor, migrate, config-lint), the audit and\n"
    "privacy record (audit, erase, erase-verify, export-user), the emergency\n"
    "stop (halt / unhalt), and the nightly dream beat."
))
@click.option("--db", default=None,
              help="World model database path (default: the active tenant's world.db).")
@click.option("--model", default=None, help="LLM model id (default: from config).")
@click.pass_context
def main(ctx: click.Context, db: str | None, model: str | None) -> None:
    """Maverick: multi-agent swarm for long-horizon work."""
    _configure_cli_text_streams()
    _configure_cli_logging()
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand == "mcp":
        from .._mcp_parent_guard import ParentGuardError, arm_from_environment

        try:
            arm_from_environment()
        except ParentGuardError as exc:
            raise click.ClickException(
                f"refusing unsafe tagged MCP launch: {exc}"
            ) from exc
    # Default the world DB to the ACTIVE TENANT's world.db (selected via
    # MAVERICK_TENANT) so one business's run history / goals / facts never pool
    # into another's -- the same isolation the channel server already gets via
    # world_for_tenant(). With no tenant this resolves to the legacy
    # ~/.maverick/world.db, so single-tenant installs are unchanged. An explicit
    # --db always wins.
    if db is None:
        if ctx.invoked_subcommand in {
            "config-lint",
            "doctor",
            "gen-stubs",
            "init",
            "preflight",
            "version",
        }:
            # These setup/diagnostic commands never need the world DB. A
            # corrupt config can make the strict client floor unknowable, so
            # resolving the ordinary Workspace path here would abort before
            # the command can report or repair the problem. `init` belongs in
            # this set because it is the recovery path for a broken config.
            from ..paths import diagnostic_data_dir

            db = str(diagnostic_data_dir("world.db"))
        else:
            from ..workspace import Workspace

            db = str(Workspace.current().db_path)
    ctx.obj["db"] = Path(db)
    if model:
        from ..llm import ModelNotAllowedError, require_model_allowed

        try:
            model = require_model_allowed(model)
        except (ModelNotAllowedError, ValueError) as exc:
            raise click.BadParameter(str(exc), param_hint="'--model'") from exc
    ctx.obj["model"] = model  # resolved lazily on first use
    # `--model` is a run-wide override. The agents resolve their model via
    # model_for_role(), not the LLM facade's default, so threading it through
    # the env is what actually makes the flag apply to every agent (it was
    # silently ignored before -- the LLM default got overridden per call).
    if model:
        os.environ["MAVERICK_MODEL_OVERRIDE"] = model




def _install_config_from_file(src: str) -> None:
    """Headless provisioning: validate SRC and install it as config.toml (0600)."""
    from pathlib import Path as _P

    from ..config import config_path
    src_path = _P(src).expanduser()
    if not src_path.is_file():
        raise click.ClickException(f"no such config file: {src}")
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # 3.10
            import tomli as tomllib  # type: ignore
        with open(src_path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        raise click.ClickException(f"invalid TOML in {src}: {e}") from e
    # Surface unknown-section / type problems, but don't block (operators may use
    # newer keys than this build knows).
    try:
        from ..config_lint import lint_config
        for finding in lint_config(cfg):
            click.echo(click.style(f"  ! {finding.section}: {finding.message}",
                                   fg="yellow"), err=True)
    except Exception:
        pass
    dst = config_path()
    from ..file_lock import (
        atomic_write_bytes,
        ensure_private_directory,
        ensure_private_file,
    )

    # A provisioned config may contain inline provider keys. Tighten the
    # directory with a protected Windows DACL and publish the bytes from a
    # create-time protected temp; POSIX mode bits passed to os.open do not
    # provide the equivalent Windows custody boundary.
    ensure_private_directory(dst.parent)
    data = src_path.read_bytes()
    try:
        installed = dst.read_bytes() if dst.is_file() else b""
        unchanged = (
            dst.is_file()
            and installed.replace(b"\r\n", b"\n")
            == data.replace(b"\r\n", b"\n")
        )
    except OSError:
        unchanged = False
    if unchanged:
        # A retried deployment should be a true no-op: do not rotate the inode,
        # mtime, or watcher state when the same logical TOML is installed,
        # including when a cross-platform checkout changed line endings.
        # Still repair/verify custody in case an older release left wide perms.
        ensure_private_file(dst, 0o600)
        click.echo(click.style(f"config unchanged -> {dst} (0600)", fg="green"))
        return
    atomic_write_bytes(dst, data, mode=0o600)
    click.echo(click.style(f"installed config -> {dst} (0600)", fg="green"))




@main.command()
def doctor() -> None:
    """Diagnose your Maverick installation."""
    from ..health import diagnose
    if diagnose():
        # At least one ✗ check: exit nonzero so `maverick doctor && ...` and CI
        # health gates can detect a broken install (it always exited 0 before).
        sys.exit(1)


@main.command()
def version() -> None:
    """Show installed package versions + runtime info."""
    import importlib.metadata

    click.echo(click.style("Maverick installed packages", bold=True))
    # PyPI distribution name for the core is `maverick-agent` (the
    # `maverick` name was squatted). Fall back to `maverick` if the
    # squatter ever releases the original name.
    pkg_names = [
        ("maverick-agent",     ("maverick-agent", "maverick")),
        ("maverick-shield",    ("maverick-shield",)),
        ("maverick-dashboard", ("maverick-dashboard",)),
        ("maverick-mcp-server", ("maverick-mcp-server",)),
        ("maverick-knowledge", ("maverick-knowledge",)),
        ("maverick-installer", ("maverick-installer",)),
    ]
    for display, candidates in pkg_names:
        version = None
        for c in candidates:
            try:
                version = importlib.metadata.version(c)
                break
            except importlib.metadata.PackageNotFoundError:
                continue
        if version:
            click.echo(f"  {display:22s} {version}")
        else:
            click.echo(f"  {display:22s} " + click.style("not installed", fg="yellow"))
    click.echo("")
    click.echo(click.style("Runtime", bold=True))
    try:
        from ..world_model import SCHEMA_VERSION
        click.echo(f"  schema:                v{SCHEMA_VERSION}")
    except Exception:
        pass
    try:
        from maverick_shield import Shield
        # warn_if_missing=False: this command prints the backend itself, so the
        # raw "SDK not installed" log line would just bleed into the table.
        s = Shield.from_config(warn_if_missing=False)
        click.echo(f"  shield backend:        {s.backend}")
    except ImportError:
        click.echo("  shield backend:        (maverick-shield not installed)")
    try:
        from ..providers import KNOWN_PROVIDERS
        click.echo(f"  providers:             {', '.join(KNOWN_PROVIDERS)}")
    except Exception:
        pass
    try:
        from ..persona import load_persona
        p = load_persona()
        if p["name"] or p["style"]:
            ident = p["name"] or "(unnamed)"
            style = p["style"] or "(default)"
            click.echo(f"  persona:               {ident} ({style})")
        else:
            click.echo("  persona:               (none)")
    except Exception:
        pass
    click.echo(f"  python:                {sys.version.split()[0]}")
    click.echo(f"  platform:              {sys.platform}")










def _show_capability_plan(profile):
    """Analyse a draft pack for capability gaps and print them. Read-only;
    returns the plan (or None if analysis was unavailable) for later apply."""
    from ..provision import analyze_profile
    try:
        from ..tools import base_tool_names
        plan = analyze_profile(profile, known_tools=base_tool_names())
    except Exception as e:  # analysis must never block onboarding
        click.echo(f"(capability analysis skipped: {e})", err=True)
        return None
    if plan.is_empty():
        return plan
    click.echo(click.style("\nCapability gaps the factory can close:", bold=True))
    for g in plan.gaps:
        click.echo(f"  - {g.describe()}")
    from .. import self_learning
    if not self_learning.enabled():
        click.echo("  (enable [self_learning] to auto-provision these on approval)")
    return plan


def _apply_capability_plan(profile, plan, llm) -> None:
    """Equip an approved pack: install catalog skills + synthesize declared
    tools through the governed paths. No-op unless self-learning is enabled.
    Records the gaps as factory-learning signals (no-op unless that's on)."""
    if plan is None or plan.is_empty():
        return
    from ..provision import apply_plan
    result = apply_plan(plan, approved=True, llm=llm)
    if result.acquired or result.generated or result.failed:
        click.echo(click.style(f"Provisioning: {result.summary()}", fg="cyan"))
    try:
        from .. import factory_learning
        factory_learning.record_provisioning(profile, plan, result)
    except Exception:  # pragma: no cover -- learning must never break onboarding
        pass






def _learning_cli_preflight(job: str) -> None:
    """Translate the strict learning stop into a stable automation failure."""
    from ..learning_guard import Halted, check_learning_halt

    try:
        check_learning_halt(job, "cli-start")
    except Halted as exc:
        raise click.ClickException(
            f"{job} refused: global learning HALT is active") from exc


def _run_learning_cli(job: str, operation):
    """Run a learning operation without exposing HALT-file contents."""
    from ..learning_guard import Halted

    try:
        return operation()
    except Halted as exc:
        raise click.ClickException(
            f"{job} refused: global learning HALT is active") from exc




















def _governance_denied_counts(principals: set[str], *, limit: int = 500) -> dict[str, int]:
    """Count recent policy-denial audit events per principal (fail-soft).

    The oversight signal is a tool the control plane refused for an agent. On
    this kernel that lands as ``capability_denied``; ``governance_denied`` is
    matched too where a build records it, so the count is forward-compatible.
    """
    counts: dict[str, int] = {}
    try:
        from ..audit import EventKind, default_audit_log
        kinds = {EventKind.CAPABILITY_DENIED,
                 getattr(EventKind, "GOVERNANCE_DENIED", "governance_denied")}
        for ev in default_audit_log().tail(limit):
            if ev.get("kind") not in kinds:
                continue
            p = ev.get("principal")
            if p in principals:
                counts[p] = counts.get(p, 0) + 1
    except Exception:  # pragma: no cover -- the oversight view never blocks on audit
        pass
    return counts




def _holdout_ledger_path(path: Path | None) -> Path:
    if path is not None:
        return path
    from ..config import get_self_harness

    configured = str(get_self_harness().get("holdout_ledger") or "").strip()
    if not configured:
        raise click.ClickException(
            "no holdout ledger path supplied; pass --path or set "
            "[self_harness] holdout_ledger")
    return Path(configured)




















def _echo_harness_cycle(report, retired: int) -> None:
    """Render one cycle's report + retirement count for the operator."""
    click.echo(f"model: {report.model_id or '(unresolved)'}")
    click.echo(f"  mined={report.mined} proposed={report.proposed} "
               f"validated={report.validated} promoted={report.promoted} retired={retired}")
    # Explain WHY a pass promoted nothing, in priority order, so the operator
    # isn't left guessing whether the loop is broken or just gated.
    if report.frozen:
        click.echo("  ⚠ learning is FROZEN (verifier drift) — promotions are paused "
                   "until calibration recovers")
    elif not report.gate_enabled:
        click.echo("  ⚠ promotion gate is OFF — set [self_improvement] enable = true "
                   "to allow promotions")
    elif report.promoted == 0 and report.proposed > 0:
        click.echo("  (dry pass — inject a live A/B scorer and enable "
                   "[self_improvement] to promote)")
    for s in report.skipped[:10]:
        click.echo(f"  - skipped: {s}")
    if report.applied_lines:
        click.echo("  promoted lines:")
        for ln in report.applied_lines:
            click.echo(f"    + {ln}")
    if getattr(report, "relapsed", None):
        click.echo("  relapsed (back on canary probation — recent outcomes turned bad):")
        for ln in report.relapsed:
            click.echo(f"    ~ {ln}")








def _migrate_learning_files(sh, learning_store, src, addenda, meta, tried) -> None:
    merged = 0
    with learning_store.rmw_lock():
        db_add = learning_store.load_addenda_db()
        for k, block in addenda.items():
            if k not in db_add:
                db_add[k] = block
            elif db_add[k] != block:
                cur = db_add[k]
                have = {sh._norm_line(x) for x in sh._bullets(cur)}
                for ln in sh._bullets(block):
                    if sh._norm_line(ln) not in have:
                        cur = sh._compose_addendum(k, cur, ln)
                db_add[k] = cur
                merged += 1
        learning_store.write_addenda_db(db_add)
        db_meta = learning_store.load_line_meta_db()
        for lid, rec in meta.items():
            db_meta.setdefault(lid, rec)
        learning_store.write_line_meta_db(db_meta)
        db_tried = learning_store.load_transfer_tried_db()
        for lid, ts in tried.items():
            db_tried.setdefault(lid, ts)
        learning_store.write_transfer_tried_db(db_tried)
    routed = sh.load_addenda()
    missing = [k for k in addenda if k not in routed]
    if missing:
        raise click.ClickException(
            f"verification failed -- keys missing after import: {missing[:3]}"
            " (file stores left untouched)")
    for p in (src, sh._meta_path(src), sh._transfer_tried_path(src)):
        if p.exists():
            p.rename(p.with_name(p.name + ".migrated"))
    click.echo(f"imported {len(addenda)} addenda key(s), {len(meta)} provenance "
               f"record(s), {len(tried)} tried pair(s) into the world store"
               + (f" ({merged} conflicting key(s) merged)" if merged else "")
               + "; file stores renamed to *.migrated.")




def _corpus_key_and_path(model: str | None) -> tuple[str, str]:
    from ..self_harness import settings
    corpus_path = settings().get("eval_corpus")
    if not corpus_path:
        raise click.ClickException("no [self_harness] eval_corpus configured.")
    if model:
        return model, corpus_path
    from ..llm import model_for_role
    return model_for_role("orchestrator"), corpus_path






































# Extends the existing `governance` group (oversight policy) with the
# governed-action audit trail: what a run did, and what a skill/source touched.








def _parse_metrics(pairs: tuple[str, ...]) -> dict:
    out: dict = {}
    for p in pairs:
        if "=" not in p:
            raise click.ClickException(f"--metric must be name=value, got {p!r}")
        name, _, val = p.partition("=")
        try:
            out[name.strip()] = float(val)
        except ValueError as e:
            raise click.ClickException(f"metric {name!r} value not numeric: {val!r}") from e
    return out










@main.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8765, type=int)
@click.option("--token", default=None,
              help="Bearer token for non-/healthz requests.")
def dashboard(host: str, port: int, token) -> None:
    """Start the local web dashboard + REST API."""
    if token:
        os.environ["MAVERICK_DASHBOARD_TOKEN"] = token
        click.echo(click.style(
            "Bearer auth enabled. Authenticate with the Authorization: Bearer header.",
            fg="yellow",
        ))
    # Mirror the module entrypoint's safety contract (app.main): refuse to bind a
    # non-loopback host without a token, so this CLI path can't expose an
    # unauthenticated admin surface even though the middleware would also 401.
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get(
        "MAVERICK_DASHBOARD_TOKEN"
    ):
        click.echo(
            "Refusing to bind dashboard to a non-loopback host without "
            "MAVERICK_DASHBOARD_TOKEN set (pass --token or set the env var).",
            err=True,
        )
        sys.exit(2)
    try:
        from maverick_dashboard.app import app as fastapi_app
    except ImportError:
        click.echo(
            "Install the dashboard from the same reviewed Maverick checkout; "
            "public-index lookup is disabled.",
            err=True,
        )
        sys.exit(2)
    import uvicorn
    click.echo(f"Maverick dashboard: http://{host}:{port}")
    click.echo(f"REST API docs:      http://{host}:{port}/docs")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@main.command()
@click.option("--http", "use_http", is_flag=True,
              help="Serve over Streamable HTTP instead of stdio.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind host (with --http).")
@click.option("--port", default=8771, type=int, show_default=True,
              help="Port (with --http).")
def mcp(use_http: bool, host: str, port: int) -> None:
    """Start the MCP server on stdio (or --http).

    The platform's surface for outside callers. Any MCP-speaking client --
    in practice, the IDE-side ones: Claude Code, Cursor, Continue, Zed --
    can drive the swarm from outside Python via this command.
    """
    try:
        from maverick_mcp.server import MCPServer
    except ImportError:
        click.echo(
            "Install the MCP server from the same reviewed Maverick checkout; "
            "public-index lookup is disabled.",
            err=True,
        )
        sys.exit(2)
    from ..deployment import require_enterprise_or_die
    require_enterprise_or_die()
    if use_http:
        try:
            from maverick_mcp.http_transport import serve
        except ImportError:
            click.echo(
                "Install the MCP HTTP extra from the same reviewed Maverick "
                "checkout; public-index lookup is disabled.",
                err=True,
            )
            sys.exit(2)
        serve(host=host, port=port)
    else:
        # Run the stdio server directly. Going through server.main() would
        # re-parse sys.argv and reject the `mcp` subcommand token (the bug
        # that made `maverick mcp` -- the command every quickstart uses --
        # exit before serving).
        MCPServer().run()














































def _propagate_coding_flags(coding_mode: bool, best_of_n: int) -> None:
    """Export the coding-mode / best-of-N env flags that ``coding_mode.from_env()``
    reads everywhere. ``--best-of-n`` only takes effect under ``--coding-mode``,
    so warn (rather than silently single-run) if it's set without it."""
    if coding_mode:
        os.environ["MAVERICK_CODING_MODE"] = "1"
    if best_of_n > 1:
        os.environ["MAVERICK_BEST_OF_N"] = str(best_of_n)
        if not coding_mode:
            click.echo(
                "WARNING: --best-of-n only takes effect with --coding-mode; "
                "without it the swarm does a single run.",
                err=True,
            )






def _sanitize_progress_content(text: str, limit: int = 200) -> str:
    """Sanitize untrusted event content before printing to a TTY.

    - Scrub secret-looking values.
    - Remove terminal control bytes / ANSI escape sequences.
    - Collapse CR/LF to spaces for one-line progress output.
    """
    from ..secrets import scrub  # lazy: only used by the streaming helper
    cleaned = scrub(text or "")
    # Strip common ANSI/OSC escape sequences.
    cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)", "", cleaned)
    # Replace newlines / carriage returns, then drop remaining control chars.
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned[:limit]


def _stream_progress(db_path, goal_id: int, stop) -> None:
    """Poll goal_events and print one line per new entry to stderr.

    Uses a fresh WorldModel so we don't share the connection with the
    main thread (SQLite WAL handles concurrent reads + one writer).
    """
    try:
        wm = open_world(db_path)
    except Exception:
        return
    seen = 0
    labels = {
        "plan": "thinking", "finding": "answer", "observation": "result",
        "error": "error", "verify": "checking", "artifact": "produced",
    }
    while not stop.is_set():
        try:
            evs = wm.goal_events(goal_id, since_id=seen, limit=200)
            for e in evs:
                label = labels.get(e.kind, e.kind)
                # Strip the hex suffix from agent names for readability.
                agent = e.agent.split("-")[0] if e.agent else "agent"
                content = _sanitize_progress_content(e.content, limit=200)
                click.echo(
                    click.style(f"  [{agent}] ", fg="bright_black")
                    + click.style(f"{label}: ", fg="cyan")
                    + content,
                    err=True,
                )
                seen = e.id
        except Exception:
            pass
        if stop.wait(timeout=1.5):
            break
    wm.close()






















@main.command("config-lint")
def config_lint_cmd() -> None:
    """Validate ~/.maverick/config.toml: unknown sections/keys + obvious type
    mistakes, with closest-match suggestions. Exits 1 if any error-level finding."""
    from ..config import config_path, load_config
    from ..config_lint import format_findings, lint_config
    # load_config() is deliberately fail-soft: a corrupt config.toml yields {}
    # with only a warning, so linting it would find nothing and print
    # "config OK" -- the one tool meant to catch a broken config blessing a
    # file in which every setting is being dropped (round-4 finding; mirrors
    # health._check_config). Parse the raw file FIRST so a syntax error is a
    # hard lint failure, not invisible.
    p = config_path()
    if not p.exists():
        # No config at all is a legitimate state (Maverick runs on built-in
        # defaults), but the file-less path used to fall through to
        # load_config() == {} and print "config OK" -- as if a real config had
        # been validated. Say plainly there's nothing to lint instead of
        # blessing a non-existent file (user-testing finding).
        click.echo(
            f"no config file at {p}; Maverick is using built-in defaults. "
            "Create one with `maverick init` (nothing to lint yet)."
        )
        return
    try:
        import tomllib
    except ModuleNotFoundError:  # 3.10
        import tomli as tomllib
    try:
        with open(p, "rb") as f:
            tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        click.echo(
            f"error: {p} is not valid TOML -- every setting in it is being "
            f"IGNORED ({type(e).__name__}: {e})", err=True,
        )
        click.echo("fix the syntax above, or back it up and re-run `maverick init`.")
        sys.exit(1)
    try:
        cfg = load_config() or {}
    except Exception as e:
        click.echo(f"could not load config: {e}", err=True)
        sys.exit(1)
    findings = lint_config(cfg)
    click.echo(format_findings(findings))
    if any(getattr(f, "severity", "") == "error" for f in findings):
        sys.exit(1)




@main.command("migrate")
@click.option("--apply", "do_apply", is_flag=True,
              help="Apply mechanical rewrites (after a timestamped backup). "
                   "Default is a dry run.")
@click.option("--config", "config_path", default=None,
              help="Path to config.toml (default: the active deployment's).")
def migrate_cmd(do_apply: bool, config_path: str | None) -> None:
    """Walk an existing config forward across versions.

    Reports migration advisories (real upgrade paths), lints unknown config
    sections (silent-no-op typos), and -- with --apply -- performs mechanical
    key renames behind a timestamped backup. Dry-run by default.
    """
    from pathlib import Path as _Path

    from ..migrate import migrate, render
    report = migrate(_Path(config_path) if config_path else None, apply=do_apply)
    click.echo(render(report))




@main.command()
@click.option("--idle-sleep", default=2.0, show_default=True,
              help="Seconds to wait when the queue is empty.")
@click.option("--once", is_flag=True,
              help="Drain ready jobs and exit (for cron / systemd timers).")
def worker(idle_sleep: float, once: bool) -> None:
    """Run the background job worker.

    Drains the job queue (``~/.maverick/jobs.db``) and runs jobs armed with
    ``maverick schedule add``. Runs until interrupted (Ctrl-C / SIGTERM).

    With ``--once``, run all currently-ready jobs and exit instead of staying
    resident -- run it from system cron or a systemd timer for scheduling
    without a persistent daemon.
    """
    from ..worker import Worker
    w = Worker(idle_sleep=idle_sleep)
    if once:
        n = w.drain()
        click.echo(f"drained {n} job(s)")
        return
    click.echo(f"worker: draining {w.queue.db_path} (Ctrl-C to stop)")
    w.run_forever()






































































def _conversation_user_matches(conv_user_id: str, requested: str, channel: str) -> bool:
    """Match a conversation's user_id for erase/export-user.

    Most channels store externally supplied user ids and must match exactly:
    identifiers such as Twilio WhatsApp ``whatsapp:+15551234567`` or Matrix
    room ids naturally contain colons, so treating any ``<prefix>:`` as the
    requested user can disclose or erase unrelated conversations.

    The only family match Maverick currently needs is the local CLI chat
    namespace: each REPL session is stored as ``local:<uuid>``, while the
    documented GDPR subject is ``--channel cli --user local``.
    """
    if conv_user_id == requested:
        return True
    return channel == "cli" and requested == "local" and conv_user_id.startswith("local:")


def _erase_subject_knowledge(channel: str, user: str) -> dict[str, int]:
    """Scrub a subject's document chunks from the knowledge plane (Art. 17).

    Extracted from ``erase`` so the command stays under the complexity cap.
    Fail-soft: knowledge is optional and a KB error must not abort the erase
    already performed on the world model — it only warns and returns ``{}``.
    """
    try:
        from .. import knowledge_admin
        return knowledge_admin.erase_subject(channel, user)
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(
            f"warning: erased the database but could not scrub the knowledge base "
            f"({type(exc).__name__}: {exc}); run `maverick knowledge residual "
            f"--channel {channel} --user {user}` to check.",
            err=True,
        )
        return {}


def _prepare_erasure_proof(world, conversation_ids):
    """Build and best-effort persist the exact pre-delete closure."""
    plan_hook = getattr(world, "plan_conversation_erasure", None)
    if not callable(plan_hook):
        error = "world backend has no pre-delete erasure planning/receipt API"
        click.echo(
            f"warning: {error}; deletion will continue without a complete "
            "post-hoc goal-graph certificate.",
            err=True,
        )
        return None, None, None, False, error

    try:
        plan = plan_hook(conversation_ids)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        click.echo(
            "warning: pre-delete erasure planning failed "
            f"({error}); deletion will continue without a complete "
            "post-hoc goal-graph certificate.",
            err=True,
        )
        return None, None, None, False, error

    try:
        from ..erasure_receipts import build_manifest, persist_receipt
        from ..paths import current_tenant_id

        manifest = build_manifest(
            tenant_id=current_tenant_id() or "shared",
            conversation_ids=plan["conversation_ids"],
            goal_ids=plan["goal_ids"],
            episode_ids=plan["episode_ids"],
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        click.echo(
            "warning: pre-delete erasure manifest could not be created "
            f"({error}); deletion will continue with an in-memory race check, "
            "but verification will remain indeterminate.",
            err=True,
        )
        return plan, None, None, False, error
    try:
        signed = persist_receipt(world, manifest)
    except Exception as exc:
        # Art. 17 deletion is not held hostage by assurance infrastructure.
        # The unsigned in-memory closure still race-checks deletion, but can
        # never support a clean certificate.
        error = f"{type(exc).__name__}: {exc}"
        click.echo(
            "warning: durable signed erasure proof is unavailable "
            f"({error}); deletion will continue, but verification will "
            "remain indeterminate.",
            err=True,
        )
        return plan, manifest, None, False, error
    return plan, manifest, signed, True, ""


def _dispatch_backend_erasure(world, conversation_ids, erasure_plan):
    """Use a backend's atomic erase hook, including closure race checks."""
    backend_erase = getattr(world, "erase_conversations", None)
    if not callable(backend_erase):
        raise click.ClickException(
            "the configured world backend does not implement the atomic "
            "erase_conversations contract; refusing a partial legacy SQL "
            "deletion. Upgrade the backend or export and erase through a "
            "supported SQLite/Postgres deployment."
        )
    if erasure_plan is None:
        return backend_erase(conversation_ids)

    import inspect

    try:
        parameters = inspect.signature(backend_erase).parameters
    except (TypeError, ValueError):
        return backend_erase(conversation_ids)
    if {
        "expected_conversation_ids",
        "expected_goal_ids",
        "expected_episode_ids",
    }.issubset(parameters):
        return backend_erase(
            conversation_ids,
            expected_conversation_ids=erasure_plan["conversation_ids"],
            expected_goal_ids=erasure_plan["goal_ids"],
            expected_episode_ids=erasure_plan["episode_ids"],
        )
    if {
        "expected_conversation_ids",
        "expected_goal_ids",
    }.issubset(parameters):
        return backend_erase(
            conversation_ids,
            expected_conversation_ids=erasure_plan["conversation_ids"],
            expected_goal_ids=erasure_plan["goal_ids"],
        )
    return backend_erase(conversation_ids)


def _erasure_failure_label(error: BaseException) -> str:
    """Describe an erase failure without echoing subject-bearing paths/data."""
    errno = getattr(error, "errno", None)
    return (
        f"{type(error).__name__} (errno {errno})"
        if isinstance(errno, int)
        else type(error).__name__
    )


def _unlink_erasure_attachments(
    attachment_paths,
) -> tuple[int, list[str]]:
    """Unlink attachment files and report every unverified outcome."""
    removed = 0
    errors: list[str] = []
    for path in attachment_paths:
        try:
            target = Path(path)
            existed = target.exists()
            target.unlink(missing_ok=True)
            if target.exists():
                raise OSError("attachment path remains after unlink")
            if existed:
                removed += 1
        except OSError as error:
            errors.append(_erasure_failure_label(error))
    return removed, errors


def _erase_conversation_user_notes(
    channel,
    conversations,
) -> tuple[int, str | None]:
    """Scrub derived subject notes with an explicit verification outcome."""
    try:
        from .. import user_notes

        return (
            user_notes.erase_notes(
                channel,
                {conv.user_id for conv in conversations if conv.user_id},
                strict=True,
            ),
            None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        error = _erasure_failure_label(exc)
        click.echo(
            "warning: erased the database but could not scrub user notes "
            f"({error}); they may retain prior preferences.",
            err=True,
        )
        return 0, error


def _persist_erasure_auxiliary_closure(
    world,
    signed_receipt,
    failures: list[str],
) -> tuple[bool, str | None]:
    """Create the post-delete proof only when every auxiliary step succeeded."""
    if failures:
        return False, "; ".join(failures)
    if signed_receipt is None:
        return False, "durable pre-delete receipt is unavailable"
    try:
        from ..erasure_receipts import persist_erasure_closure

        persist_erasure_closure(
            world,
            signed_receipt,
            expected_tenant=signed_receipt["tenant_id"],
        )
    except Exception as exc:
        error = _erasure_failure_label(exc)
        click.echo(
            "warning: auxiliary erasure closure could not be durably "
            f"stored ({error}); the clean certificate is withheld.",
            err=True,
        )
        return False, error
    return True, None


@main.command()
@click.option("--channel", required=True, help="Channel name (e.g. telegram, sms).")
@click.option("--user", required=True, help="The channel user_id to erase.")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_context
def erase(ctx, channel: str, user: str, yes: bool) -> None:
    """Erase everything Maverick knows about a (channel, user_id) pair.

    GDPR Art. 17 right-to-erasure: removes conversations, turns,
    attachments on disk, and the conversation row itself. (First line kept
    abbreviation-free so Click's short help isn't truncated at "Art.".)"""
    world = open_world(ctx.obj["db"])
    convs = [
        c for c in world.list_conversations(channel)
        if _conversation_user_matches(c.user_id, user, channel)
    ]
    if not convs:
        click.echo(f"no conversation found for {channel}:{user}")
        return
    if not yes:
        click.echo(f"This will erase {len(convs)} conversation(s) for {channel}:{user}.")
        click.confirm("Proceed?", abort=True)

    # Council security finding: previous version left goals, messages,
    # episodes, questions, goal_events, attachments-rows, and
    # processed_messages intact -- a documented Art. 17 violation. Full
    # cascade now wipes every row referencing goals tied to this user's
    # conversations, in one transaction so a partial failure rolls back.
    # Attachment FILE unlinks happen AFTER the DB transaction commits so
    # we don't leave dangling rows pointing at deleted paths if the DB
    # write fails.

    # Step 1: gather every goal_id referenced by any turn in any of
    # these conversations. We use ALL turns (not just recent), so a
    # conversation with >10k turns doesn't leave orphan attachments.
    conv_ids = [c.id for c in convs]
    (
        erasure_plan,
        erasure_manifest,
        signed_erasure_receipt,
        durable_erasure_proof,
        receipt_error,
    ) = _prepare_erasure_proof(world, conv_ids)

    goal_ids, attachment_paths, removed_turns = _dispatch_backend_erasure(
        world,
        conv_ids,
        erasure_plan,
    )

    # Step 4: now that DB rows are gone, unlink files. File bytes remain
    # personal data even when their database metadata is gone, so any unlink
    # failure blocks the clean certificate while leaving deletion completed.
    removed_attachments, attachment_errors = _unlink_erasure_attachments(
        attachment_paths
    )
    for error in attachment_errors:
        click.echo(
            "warning: erased the database but could not remove an attachment "
            f"file ({error}); the clean certificate is withheld.",
            err=True,
        )

    # Step 4b: remove derived per-user preference notes from the dreaming
    # store. These live outside world.db, so the SQL cascade above cannot
    # erase them; use the concrete matched conversation user_ids so CLI
    # family erasure (local -> local:<uuid>) removes every scoped note.
    removed_user_notes, user_notes_error = _erase_conversation_user_notes(
        channel,
        convs,
    )

    # Step 4c: scrub explicitly user-scoped global facts. Facts are global
    # key/value pairs with no per-user attribution, so erase only touches
    # facts deliberately keyed as user:<channel>:<user_id>:<name>. Arbitrary
    # substring matching is unsafe for short/common user ids because it can
    # delete unrelated operator knowledge or other users' data.
    fact_subject = _fact_subject_token(channel, user)
    scrubbed_fact_keys = world.delete_facts_matching(fact_subject)

    # Step 4d: the optional LLM cache (MAVERICK_LLM_CACHE=1) is content-
    # addressed on the full prompt -- system + messages include the user's
    # goal text and the model's replies, so the cache retains exactly the
    # PII we just erased. It can't be purged by subject (the key is a hash of
    # content, not tied to a user), so clear every authority in this tenant's
    # disposable cache. Ordinary cache.clear() is intentionally principal-
    # scoped and would leave other historical authorities behind. Only when
    # the dynamically resolved DB already exists, so a cache-disabled install
    # does not create an empty cache file. Best-effort after the command's
    # irreversible --yes confirmation above.
    llm_cache_error: str | None = None
    try:
        from ..cache.llm import LLMCache, default_db_path
        _llm_cache_db = default_db_path()
        if _llm_cache_db.exists():
            LLMCache().purge_all_authorities_for_erasure(confirmed=True)
    except Exception as exc:  # pragma: no cover - defensive
        llm_cache_error = _erasure_failure_label(exc)
        click.echo(
            f"warning: erased the database but could not clear the LLM cache "
            f"({llm_cache_error}); it may retain prior prompts.",
            err=True,
        )

    # Step 5: scrub the subject from PRIOR audit-log lines. Audit payloads
    # (goal_start / tool_call / channel events) carry channel:user_id, so
    # without this the identity we just erased stayed readable in
    # ~/.maverick/audit/*.ndjson -- an Art.17 gap (scrub_user was dead
    # code, never called). Done BEFORE recording the erase event below so
    # that event (which hashes the subject) isn't itself scrubbed.
    # If [audit] sign is enabled scrub_user verifies the chain before mutating
    # it and re-anchors only the files it changed (leaving PII in place would
    # violate Art.17, but blindly re-signing old tampering would destroy audit
    # evidence).
    audit_scrubbed = 0
    audit_scrub_error: str | None = None
    try:
        from ..audit import scrub_user
        audit_scrubbed, _ = scrub_user(channel, user, strict=True)
    except Exception as exc:
        audit_scrub_error = _erasure_failure_label(exc)
        click.echo(
            f"warning: erased the database but could not scrub the audit log "
            f"({audit_scrub_error}); use the receipt id with `erase-verify` "
            "after repairing audit storage.",
            err=True,
        )

    # Step 5b: erase the subject's document chunks from the knowledge plane.
    # A subject's uploaded documents are ingested with a `subject` provenance
    # stamp; without this, their embedded chunks survived erasure in the vector
    # store (an Art.17 gap for any deployment with knowledge RAG on). Fail-soft:
    # knowledge is optional and a KB error must not abort the erase we've done.
    knowledge_removed = _erase_subject_knowledge(channel, user)

    # Scrubbing may have re-anchored signed audit files, so drop any cached
    # signer before appending the erase marker. The compatibility hook is safe:
    # it refuses to rewrite already-broken chains unless the erase helper
    # verified them before mutation.
    from .. import audit

    audit_reanchor_error: str | None = None
    try:
        audit.reanchor_after_erase()
    except Exception as e:  # pragma: no cover - defensive
        audit_reanchor_error = _erasure_failure_label(e)
        click.echo(
            f"warning: audit re-anchor failed ({audit_reanchor_error})",
            err=True,
        )

    # GDPR Art. 30: record that an erasure happened without deriving a stable
    # identifier from the subject. Low-entropy user IDs (phone numbers, short
    # handles, numeric IDs) are enumerable, so even a truncated hash can
    # re-identify the erased person if audit logs are read.
    import secrets

    knowledge_chunks = sum(knowledge_removed.values())
    erasure_id = (
        erasure_manifest["receipt_id"]
        if isinstance(erasure_manifest, dict)
        else secrets.token_hex(16)
    )
    closure_failures = [
        *attachment_errors,
        *(
            [f"user_notes: {user_notes_error}"]
            if user_notes_error is not None
            else []
        ),
        *(
            [f"llm_cache: {llm_cache_error}"]
            if llm_cache_error is not None
            else []
        ),
        *(
            [f"audit_scrub: {audit_scrub_error}"]
            if audit_scrub_error is not None
            else []
        ),
        *(
            [f"audit_reanchor: {audit_reanchor_error}"]
            if audit_reanchor_error is not None
            else []
        ),
    ]
    audit_record_error: str | None = None
    try:
        audit_recorded = audit.record(
            "erase",
            channel=channel,
            erasure_id=erasure_id,
            durable_proof=durable_erasure_proof,
            auxiliary_checks_passed=not closure_failures,
            conversations=len(convs),
            turns=removed_turns,
            goals=len(goal_ids),
            attachments=removed_attachments,
            audit_lines_scrubbed=audit_scrubbed,
            facts_scrubbed=len(scrubbed_fact_keys),
            user_notes_scrubbed=removed_user_notes,
            knowledge_chunks_scrubbed=knowledge_chunks,
        )
        if not audit_recorded:
            audit_record_error = (
                "audit sink did not acknowledge the erase marker"
            )
    except audit.AuditRefused:
        # Deletion has already committed and cannot honestly be rolled back.
        # A policy-level refusal must still reach the caller, though: returning
        # success would turn "strict audit declined this action marker" into a
        # silently unaudited administrative action.
        raise
    except Exception as exc:  # pragma: no cover - defensive
        audit_record_error = _erasure_failure_label(exc)
    if audit_record_error is not None:
        closure_failures.append(f"audit_record: {audit_record_error}")
        click.echo(
            "warning: the erase marker was not durably recorded "
            f"({audit_record_error}); the clean certificate is withheld.",
            err=True,
        )

    (
        durable_erasure_closure,
        closure_error,
    ) = _persist_erasure_auxiliary_closure(
        world,
        signed_erasure_receipt,
        closure_failures,
    )

    # Automatic same-process proof uses the exact pre-delete closure. When the
    # signed row could not be persisted, the unsigned manifest still checks all
    # database stores but deliberately leaves durable_proof=false and
    # indeterminate=true.
    from ..erasure_verify import verify_erasure

    try:
        verification = verify_erasure(
            user,
            channel=channel,
            receipt=signed_erasure_receipt or erasure_manifest,
            world=world,
        )
    except Exception as exc:  # pragma: no cover - last-resort proof isolation
        verify_error = f"{type(exc).__name__}: {exc}"
        receipt_error = receipt_error or verify_error
        verification = {
            "clean": False,
            "residual": {},
            "errors": {"verification": verify_error},
        }

    click.echo(
        f"erased {len(convs)} conversation(s), {removed_turns} turn(s), "
        f"{len(goal_ids)} goal(s) and all linked rows, "
        f"{removed_attachments} attachment file(s), "
        f"{audit_scrubbed} audit event(s) scrubbed, "
        f"{len(scrubbed_fact_keys)} fact(s) scrubbed, "
        f"{removed_user_notes} user note(s) scrubbed, "
        f"{knowledge_chunks} knowledge chunk(s) scrubbed"
    )
    if erasure_manifest is not None:
        click.echo(
            f"  erasure receipt: {erasure_manifest['receipt_id']} "
            f"(durable proof: {'yes' if durable_erasure_proof else 'no'}; "
            "auxiliary closure: "
            f"{'yes' if durable_erasure_closure else 'no'})"
        )
    if verification["clean"]:
        click.echo("  verification: CLEAN (signed receipt closure is zero)")
    elif verification["residual"]:
        click.echo(
            "warning: automatic verification found residual data: "
            + ", ".join(
                f"{store}={count}"
                for store, count in sorted(verification["residual"].items())
            ),
            err=True,
        )
    else:
        detail = (
            closure_error
            or receipt_error
            or "; ".join(verification["errors"].values())
        )
        click.echo(
            "warning: deletion completed without a clean certificate"
            + (f" ({detail})" if detail else ""),
            err=True,
        )
    if knowledge_removed:
        click.echo(
            "  knowledge removed (per collection): "
            + ", ".join(f"{c}={n}" for c, n in sorted(knowledge_removed.items()))
        )
    if scrubbed_fact_keys:
        click.echo(
            "  facts removed (global key/value, scoped with user:<channel>:<user_id>: "
            f"prefix): {', '.join(scrubbed_fact_keys)}"
        )


@main.command("erase-verify")
@click.option("--channel", required=True, help="Channel name (e.g. telegram, sms).")
@click.option("--user", required=True, help="The channel user_id to verify.")
@click.option("--tenant", default=None, help="Tenant data plane (default: active).")
@click.option(
    "--receipt-id",
    default=None,
    help=(
        "Receipt id required for the signed scope + auxiliary completion proof."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def erase_verify(
    ctx,
    channel: str,
    user: str,
    tenant: str | None,
    receipt_id: str | None,
    as_json: bool,
) -> None:
    """Verify a (channel, user_id) was fully erased: zero residual records.

    Right-to-erasure proof (GDPR Art. 17): reuses the DSAR export, whose
    subject-matching agrees with the erase path, so any residual count is an
    incomplete erasure. A clean result requires the immutable signed scope
    receipt and its bound signed auxiliary-store completion record. Read-only;
    run it after `maverick erase`.
    """
    import json as _json

    from ..erasure_verify import verify_erasure

    world = open_world(ctx.obj["db"])
    report = verify_erasure(
        user,
        channel=channel,
        tenant=tenant,
        receipt_id=receipt_id,
        world=world,
    )
    if as_json:
        click.echo(_json.dumps(report, default=str))
        if not report["clean"]:
            raise SystemExit(1)
        return
    if report["clean"]:
        click.echo(click.style(
            f"CLEAN: no residual data for {channel}:{user}", fg="green"))
        return
    if not report["residual"]:
        click.echo(click.style(
            f"INDETERMINATE for {channel}:{user} - no clean certificate:",
            fg="yellow",
        ))
        for store, error in sorted(report["errors"].items()):
            click.echo(f"  {store}: {error}")
        if not receipt_id:
            click.echo(
                "  pass --receipt-id from the erase command to verify the "
                "signed scope and auxiliary completion closure"
            )
        raise SystemExit(1)
    click.echo(click.style(
        f"RESIDUAL DATA for {channel}:{user} — erasure incomplete:", fg="red"))
    for store, n in sorted(report["residual"].items()):
        click.echo(f"  {store}: {n}")
    raise SystemExit(1)
















@main.command("export-user")
@click.option("--channel", required=True, help="Channel name.")
@click.option("--user", required=True, help="The channel user_id to export.")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write JSON to file (default stdout).")
@click.pass_context
def export_user(ctx, channel: str, user: str, output) -> None:
    """Export everything Maverick knows about a (channel, user_id) as JSON.

    GDPR Art. 15 right-of-access. Registered as ``export-user`` so it does
    not collide with ``export`` (the goal-trajectory bundle below); a
    duplicate Click command name silently shadowed this one, making the
    data-subject export unreachable from the CLI."""
    import json
    world = open_world(ctx.obj["db"])
    convs = [
        c for c in world.list_conversations(channel)
        if _conversation_user_matches(c.user_id, user, channel)
    ]
    data = {
        "channel": channel,
        "user_id": user,
        "conversations": [],
        # Explicitly user-scoped global facts. Facts have no per-user
        # attribution, so export only includes keys deliberately namespaced as
        # user:<channel>:<user_id>:<name> rather than arbitrary substring
        # matches.
        "facts": world.facts_matching(_fact_subject_token(channel, user)),
        # Art.15 completeness: when [memory] temporal is on, superseded fact
        # values are retained in fact_history and are themselves the subject's
        # personal data, so include them (empty when temporal is off).
        "fact_history": {
            k: [
                {"value": v.value, "valid_from": v.valid_from,
                 "valid_to": v.valid_to, "source": v.source}
                for v in versions
            ]
            for k, versions in world.fact_history_matching(
                _fact_subject_token(channel, user)).items()
        },
    }
    for c in convs:
        turns = world.recent_turns(c.id, limit=10_000)
        conv_data = {
            "id": c.id,
            "created_at": c.created_at,
            "last_seen": c.last_seen,
            "turns": [
                {"role": t.role, "content": t.content, "ts": t.ts,
                 "goal_id": t.goal_id}
                for t in turns
            ],
            "attachments": [],
        }
        for t in turns:
            if t.goal_id is None:
                continue
            for a in world.list_attachments(t.goal_id):
                conv_data["attachments"].append({
                    "filename": a.filename, "mime": a.mime,
                    "size_bytes": a.size_bytes, "sha256": a.sha256,
                    "goal_id": a.goal_id,
                })
        data["conversations"].append(conv_data)

    payload = json.dumps(data, indent=2, default=str)
    if output:
        # A GDPR export carries the subject's full conversation content.
        # Create it 0o600 (not the umask's world-readable 0644) so a
        # co-tenant can't read it, and fail cleanly instead of dumping a
        # traceback on a bad/unwritable path.
        try:
            fd = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError as e:
            raise click.ClickException(f"could not write {output}: {e}") from e
        click.echo(f"exported to {output}")
    else:
        click.echo(payload)




def _watch_goal_allowed(goal_text: str) -> tuple[bool, str | None]:
    """Best-effort Shield scan for watch-mode marker goals."""
    try:
        from maverick_shield import Shield  # type: ignore
    except ImportError:
        return True, None

    try:
        verdict = Shield.from_config().scan_input(goal_text)
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "Shield raised %s during watch --run scan; failing open",
            type(exc).__name__,
        )
        return True, None

    if verdict.allowed:
        return True, None
    return False, f"blocked by Shield ({verdict.severity}): {'; '.join(verdict.reasons)}"



# ----- Audit log ---------------------------------------------------------

@main.group()
def audit() -> None:
    """Inspect the audit log (~/.maverick/audit/YYYY-MM-DD.ndjson)."""


def _require_day_opt(day: str | None) -> None:
    """Reject a ``--day`` that isn't a literal YYYY-MM-DD before it becomes a path.

    ``day`` is resolved to ``<audit_dir>/<day>.ndjson``; a value like
    ``../../etc/passwd`` would otherwise escape the audit dir. The writer/export
    layer refuses it too (a backstop for non-CLI callers); this just turns it
    into a friendly CLI error + exit 2, matching ``--since``/``--until``.
    """
    from ..audit.events import is_valid_day
    if day is not None and not is_valid_day(day):
        click.echo("error: --day must be YYYY-MM-DD", err=True)
        sys.exit(2)


@audit.command("tail")
@click.option("-n", "--num", default=50, type=int, help="Lines to tail.")
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
def audit_tail(num: int, day: str | None) -> None:
    """Print the last N audit events."""
    import json as _json

    _require_day_opt(day)
    from ..audit import default_audit_log
    for ev in default_audit_log().tail(num, day=day):
        click.echo(_json.dumps(ev, default=str))


@audit.command("grep")
@click.argument("pattern")
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
def audit_grep(pattern: str, day: str | None) -> None:
    """Regex grep over today's audit log."""
    import json as _json

    _require_day_opt(day)
    from ..audit import default_audit_log
    try:
        events = default_audit_log().grep(pattern, day=day)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    for ev in events:
        click.echo(_json.dumps(ev, default=str))


@audit.command("verify")
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
@click.option("--all", "all_days", is_flag=True,
              help="Verify every YYYY-MM-DD.ndjson day-file in the audit dir.")
@click.option("--tenant", default=None,
              help="Tenant whose audit dir to verify (default: active/none).")
@click.option("--file", "file_", default=None, type=click.Path(),
              help="A single audit file to verify (overrides --day/--all).")
@click.option(
    "--pubkey", default=None,
    help="Trusted Ed25519 pubkey (hex). Required for real third-party "
         "tamper-evidence; without it a locally-held key is trusted.",
)
def audit_verify(
    day: str | None, all_days: bool, tenant: str | None,
    file_: str | None, pubkey: str | None,
) -> None:
    """Verify the tamper-evident audit log and exit non-zero on any break.

    Walks the Ed25519 hash-chain of the audit day-file(s) and the cross-file
    tip-ledger, printing a concise OK / per-file break report. Exits 1 if any
    break is found and 0 if clean, so CI / cron / SOC 2 evidence checks can gate
    on it. By default it verifies today's day-file; ``--all`` sweeps every
    day-file in the audit dir. Only meaningful when audit signing is enabled
    ([audit] sign = true).

    If ``cryptography`` is unavailable the chain can't be verified at all; that
    is reported as a verification break and exits 1 so automation cannot pass
    unverifiable evidence as clean.
    """
    import datetime as _dt
    from pathlib import Path as _Path

    _require_day_opt(day)
    from ..audit import verify_anchors, verify_chain
    from ..paths import data_dir

    # Resolve the audit dir tenant-aware (matching the writer/signer), unless an
    # explicit --file pins one file in some other location.
    audit_dir = data_dir("audit", tenant=tenant) if tenant else data_dir("audit")

    if file_:
        paths = [_Path(file_)]
        anchor_dir = paths[0].parent
    elif all_days:
        # The anchor ledger is verified separately as the tip-ledger; don't
        # also walk it as if it were a day-file.
        paths = [p for p in sorted(audit_dir.glob("*.ndjson"))
                 if p.name != "anchors.ndjson"]
        anchor_dir = audit_dir
        if not paths:
            click.echo(f"no audit day-files in {audit_dir}")
    else:
        d = day or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        anchor_dir = audit_dir
        day_file = audit_dir / f"{d}.ndjson"
        from ..audit.signing import _have_crypto
        audit_dir_empty = not audit_dir.exists() or not any(audit_dir.iterdir())
        if not day_file.exists() and _have_crypto() and audit_dir_empty:
            # A completely absent/empty audit directory means no audit events
            # have ever been recorded for this tenant, so the requested day is
            # cleanly empty. If the directory contains any audit artifacts
            # (keys, anchors, other day-files, etc.), keep verifying the
            # requested path so verify_chain() can report a missing_file break
            # instead of allowing deletion of an unanchored day-file to pass.
            click.echo(f"no audit entries for {d} (nothing recorded that day).")
            paths = []
        else:
            paths = [day_file]

    if not pubkey:
        click.echo(
            "warning: no --pubkey given; trusting a locally-held key. For "
            "third-party tamper-evidence, pass the externally-held pubkey.",
            err=True,
        )

    any_break = False
    for path in paths:
        breaks = verify_chain(path, pubkey_hex=pubkey)
        if breaks and all(b.reason == "unsigned" for b in breaks):
            # Default deployment: signing was never on. One actionable line
            # instead of per-row tamper vocabulary -- but still exit 1, so
            # automation cannot pass unverifiable evidence as clean.
            any_break = True
            click.echo(
                f"UNVERIFIABLE: {path} — {len(breaks)} unsigned row(s); audit "
                "signing is off. Set [audit] sign = true in "
                "~/.maverick/config.toml (or MAVERICK_AUDIT_SIGN=1) so future "
                "rows are hash-chained and tamper-evident.",
                err=True,
            )
        elif breaks:
            any_break = True
            click.echo(f"FAIL: {len(breaks)} issue(s) in {path}", err=True)
            for b in breaks:
                click.echo(f"  line {b.line_no}: {b.reason} — {b.detail}", err=True)
        else:
            click.echo(f"OK: chain intact ({path})")

    # Cross-file check: a whole deleted/truncated day-file is invisible to the
    # per-file chain above; the signed tip-ledger catches it.
    anchor_breaks = verify_anchors(anchor_dir, pubkey_hex=pubkey)
    if anchor_breaks:
        any_break = True
        click.echo(
            f"FAIL: {len(anchor_breaks)} cross-file tip-ledger issue(s) in {anchor_dir}",
            err=True,
        )
        for b in anchor_breaks:
            click.echo(f"  anchor: {b.reason} — {b.detail}", err=True)
    else:
        click.echo(f"OK: tip-ledger intact ({anchor_dir})")

    # State the deletions the tip-ledger stopped reporting as breaks. Without
    # this line "OK" would read as "nothing was removed" when the truth is
    # "these days were removed by policy and here is the signed record".
    from ..audit.signing import retention_purged_days
    retired = retention_purged_days(anchor_dir)
    if retired:
        click.echo(
            f"note: {len(retired)} day-file(s) retired by [retention] policy "
            "and recorded in the signed tip-ledger:")
        for entry in retired:
            click.echo(
                f"  {entry.get('day')}: {entry.get('row_count')} row(s), "
                f"tip {str(entry.get('tip_hash'))[:12]}...")

    if any_break:
        raise SystemExit(1)


@audit.group("checkpoint")
def audit_checkpoint() -> None:
    """Publish and verify independently held live audit-tip commitments."""


@audit_checkpoint.command("publish")
@click.option(
    "--checkpoint-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Independent/WORM directory that will hold signed checkpoints.",
)
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
@click.option(
    "--pubkey",
    multiple=True,
    help=(
        "Pinned Ed25519 public key in hex. Repeat during an authorized key "
        "rotation."
    ),
)
@click.option(
    "--supersede-digest",
    default=None,
    help="Exact SHA-256 of the latest checkpoint being superseded.",
)
@click.option(
    "--lifecycle-reason",
    type=click.Choice(["gdpr_reanchor"], case_sensitive=True),
    default=None,
    help="Bounded reason for an authorized changed-prefix supersession.",
)
def audit_checkpoint_publish(
    checkpoint_dir: Path,
    day: str | None,
    pubkey: tuple[str, ...],
    supersede_digest: str | None,
    lifecycle_reason: str | None,
) -> None:
    """Publish the current signed day tip to an independent checkpoint store."""
    from ..audit.checkpoints import AuditCheckpointError, publish_checkpoint
    from ..paths import data_dir

    _require_day_opt(day)
    try:
        path = publish_checkpoint(
            data_dir("audit"),
            checkpoint_dir,
            day=day,
            trusted_pubkeys=pubkey or None,
            supersedes_checkpoint_sha256=supersede_digest,
            lifecycle_reason=lifecycle_reason,
        )
    except (AuditCheckpointError, ImportError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    import hashlib

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sequence = int(path.stem.rsplit("-", 1)[-1])
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            f"checkpoint was published but its receipt could not be read: {exc}"
        ) from exc
    click.echo(f"published {path}")
    click.echo(f"retain externally: sequence={sequence} sha256={digest}")


@audit_checkpoint.command("retire")
@click.option(
    "--checkpoint-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Independent/WORM directory containing signed checkpoints.",
)
@click.option(
    "--supersede-digest",
    required=True,
    help="Exact SHA-256 of the latest checkpoint being retired.",
)
@click.option(
    "--lifecycle-reason",
    required=True,
    type=click.Choice(["retention_purge"], case_sensitive=True),
    help="Bounded reason for retirement.",
)
@click.option(
    "--pubkey",
    multiple=True,
    help=(
        "Pinned Ed25519 public key in hex. Repeat during an authorized key "
        "rotation."
    ),
)
def audit_checkpoint_retire(
    checkpoint_dir: Path,
    supersede_digest: str,
    lifecycle_reason: str,
    pubkey: tuple[str, ...],
) -> None:
    """Retire a checkpoint after an exact signed retention purge."""
    from ..audit.checkpoints import AuditCheckpointError, retire_checkpoint
    from ..paths import data_dir

    try:
        path = retire_checkpoint(
            data_dir("audit"),
            checkpoint_dir,
            supersedes_checkpoint_sha256=supersede_digest,
            lifecycle_reason=lifecycle_reason,
            trusted_pubkeys=pubkey or None,
        )
    except (AuditCheckpointError, ImportError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"retired by signed lifecycle record {path}")


@audit_checkpoint.command("verify")
@click.option(
    "--checkpoint-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Independent/WORM directory containing signed checkpoints.",
)
@click.option(
    "--minimum-sequence",
    type=click.IntRange(min=1),
    default=None,
    help="Latest sequence retained by an independent verifier/SIEM.",
)
@click.option(
    "--minimum-digest",
    default=None,
    help="SHA-256 retained externally for --minimum-sequence.",
)
@click.option(
    "--pubkey",
    multiple=True,
    help="Pinned Ed25519 public key in hex; repeat to trust a rotation set.",
)
def audit_checkpoint_verify(
    checkpoint_dir: Path,
    minimum_sequence: int | None,
    minimum_digest: str | None,
    pubkey: tuple[str, ...],
) -> None:
    """Verify published checkpoint history and every committed audit prefix."""
    from ..audit.checkpoints import verify_checkpoints
    from ..paths import data_dir

    try:
        breaks = verify_checkpoints(
            data_dir("audit"),
            checkpoint_dir,
            trusted_pubkeys=pubkey or None,
            minimum_sequence=minimum_sequence,
            minimum_digest=minimum_digest,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if breaks:
        for item in breaks:
            click.echo(
                f"FAIL checkpoint {item.sequence}: {item.reason} — {item.detail}",
                err=True,
            )
        raise SystemExit(1)
    if pubkey and minimum_sequence is not None and minimum_digest is not None:
        click.echo(
            f"OK: independent audit checkpoints intact and externally pinned "
            f"({checkpoint_dir})"
        )
    else:
        missing = []
        if not pubkey:
            missing.append("--pubkey")
        if minimum_sequence is None:
            missing.append("--minimum-sequence")
        if minimum_digest is None:
            missing.append("--minimum-digest")
        click.echo(
            "OK: independent audit checkpoints intact internally; independent "
            f"assurance incomplete without {', '.join(missing)} "
            f"({checkpoint_dir})"
        )


@audit.command("seal")
@click.option("--dry-run", is_flag=True,
              help="Show which segments would be sealed; write nothing.")
def audit_seal(dry_run: bool) -> None:
    """Encrypt closed audit day-files at rest (confidentiality for the log).

    Seals every day-file dated before today in place with AES-256-GCM. The current
    day-file (live append) and the anchor ledger stay plaintext, and the readers +
    'audit verify' transparently decrypt sealed segments. Requires at-rest
    encryption to be enabled ([encryption] at_rest / MAVERICK_ENCRYPT_AT_REST).
    """
    from ..audit.sealing import seal_closed_segments
    from ..crypto_at_rest import EncryptionUnavailable
    try:
        report = seal_closed_segments(dry_run=dry_run)
    except EncryptionUnavailable as e:
        raise click.ClickException(str(e)) from e
    for name, status in sorted(report.items()):
        click.echo(f"  {name}: {status}")
    done = sum(1 for s in report.values() if s in ("sealed", "would seal"))
    click.echo(f"{'Would seal' if dry_run else 'Sealed'} {done} segment(s).")


@audit.command("rotate-key")
def audit_rotate_key() -> None:
    """Rotate the audit signing key (Ed25519).

    Mints a new keypair and makes it the active signer. Safe and additive: prior
    public keys are retained so 'audit verify' still validates rows signed under
    the old key (each row carries its key_id) -- no audit data is rewritten.
    Takes effect for the next day-file / process restart; restart a running
    'maverick serve' to begin signing with the new key.
    """
    from ..audit.signing import _have_crypto, rotate_audit_keypair
    if not _have_crypto():
        raise click.ClickException(
            "audit signing needs the 'cryptography' package (pip install "
            "'pyjwt[crypto]' or cryptography)."
        )
    key_id = rotate_audit_keypair()
    click.echo(f"rotated audit signing key; new active key_id = {key_id}")
    click.echo("old public keys retained for verification. Restart "
               "'maverick serve' to sign new entries with it.")


@audit.command("export")
@click.option("--format", "fmt", type=click.Choice(["json", "cef"]), default="json",
              help="Output format for SIEM ingestion (default: json).")
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
@click.option("--all", "all_days", is_flag=True,
              help="Export every YYYY-MM-DD.ndjson day-file in the audit dir.")
@click.option("--since", default=None,
              help="Start of an inclusive YYYY-MM-DD window (e.g. an incident).")
@click.option("--until", default=None,
              help="End of the inclusive YYYY-MM-DD window.")
@click.option("--tenant", default=None,
              help="Tenant whose audit dir to export (default: active/none).")
@click.option("-o", "--output", "output", default=None, type=click.Path(),
              help="Write to FILE (mode 0600; may contain PII). Default: stdout.")
def audit_export(
    fmt: str, day: str | None, all_days: bool, since: str | None,
    until: str | None, tenant: str | None, output: str | None,
) -> None:
    """Export the audit log as JSONL or ArcSight CEF for a SIEM.

    Read-only re-emission of the tamper-evident NDJSON log; it never mutates
    the log or the signing chain. By default it exports today's day-file;
    ``--since``/``--until`` export an inclusive date window (for incident
    backfill) and ``--all`` sweeps every day-file. An empty/missing log exits 0
    (a note goes to stderr) so cron/automation never fails on a quiet day.
    """
    import datetime as _dt
    import os as _os
    from pathlib import Path as _Path

    _require_day_opt(day)
    for _label, _val in (("--since", since), ("--until", until)):
        if _val is not None:
            try:
                _parsed = _dt.datetime.strptime(_val, "%Y-%m-%d")
            except ValueError:
                click.echo(f"ERROR: {_label} must be YYYY-MM-DD", err=True)
                sys.exit(2)
            if _parsed.strftime("%Y-%m-%d") != _val:
                click.echo(f"ERROR: {_label} must be YYYY-MM-DD", err=True)
                sys.exit(2)

    # Plan feature gating: SIEM audit export is a paid-tier entitlement. Deny
    # either an explicit --tenant or the active deployment tenant when it names
    # a provisioned tenant whose plan omits "audit_export"; unscoped/self-host
    # export is unaffected because feature_allowed fails open at the edges.
    from ..billing import feature_allowed
    if not feature_allowed("audit_export", tenant=tenant):
        from ..paths import current_tenant_id
        denied_tenant = tenant or current_tenant_id() or "active tenant"
        click.echo(
            f"ERROR: tenant '{denied_tenant}' plan does not include SIEM audit "
            "export (audit_export entitlement). Upgrade the tenant's plan.",
            err=True,
        )
        sys.exit(2)

    from ..audit.export import audit_event_paths, iter_audit_events, to_cef, to_jsonl

    render = to_cef if fmt == "cef" else to_jsonl
    lines = (render(ev) for ev in iter_audit_events(
        day=day, all_days=all_days, since=since, until=until, tenant=tenant,
    ))

    if output:
        output_path = _Path(output)
        output_resolved = output_path.resolve(strict=False)
        source_paths = audit_event_paths(
            day=day, all_days=all_days, since=since, until=until, tenant=tenant,
        )
        for source_path in source_paths:
            if (output_resolved == source_path.resolve(strict=False)
                    or (output_path.exists() and source_path.exists()
                        and _os.path.samefile(output_path, source_path))):
                raise click.ClickException(
                    "refusing to write audit export over a source audit log file"
                )

        # Private from creation on POSIX *and* Windows: os.open(..., 0o600)
        # only adjusts POSIX mode bits and can inherit a broad Windows DACL.
        # Stream through a protected unpublished temp, then atomically publish
        # the complete export so PII is never exposed in a permissive or torn
        # destination file.
        from ..file_lock import atomic_write_text_chunks

        n = atomic_write_text_chunks(
            output_path,
            (line + "\n" for line in lines),
            mode=0o600,
        )
        if n == 0:
            click.echo("no audit events to export", err=True)
        else:
            click.echo(f"exported {n} event(s) to {output}", err=True)
        return

    n = 0
    for line in lines:
        click.echo(line)
        n += 1
    if n == 0:
        click.echo("no audit events to export", err=True)


@audit.command("forward")
@click.option("--format", "fmt", type=click.Choice(["json", "cef"]), default="json",
              help="Wire format for the SIEM (default: json).")
@click.option("--to", "dest", default=None,
              help="Destination URI: tcp://host:port, udp://host:port, or "
                   "http(s)://host/path. Default: MAVERICK_SIEM_DEST / "
                   "[audit] siem_dest.")
@click.option("--day", default=None, help="YYYY-MM-DD (default: today).")
@click.option("--all", "all_days", is_flag=True,
              help="Forward every YYYY-MM-DD.ndjson day-file in the audit dir.")
@click.option("--since", default=None,
              help="Start of an inclusive YYYY-MM-DD window (e.g. an incident).")
@click.option("--until", default=None,
              help="End of the inclusive YYYY-MM-DD window.")
@click.option("--tenant", default=None,
              help="Tenant whose audit dir to forward (default: active/none).")
@click.option("--dry-run", is_flag=True,
              help="Validate the destination and count events; send nothing.")
def audit_forward(
    fmt: str, dest: str | None, day: str | None, all_days: bool,
    since: str | None, until: str | None, tenant: str | None, dry_run: bool,
) -> None:
    """Push the audit log to a SIEM collector over the network.

    The push counterpart of ``audit export``: same read-only re-emission of the
    tamper-evident NDJSON log, but shipped to ``--to`` (a tcp/udp syslog or
    http(s) collector) instead of a file. A transport failure exits non-zero --
    a SIEM gap is a compliance event, not something to swallow. An empty log
    exits 0 with a note (cron never fails on a quiet day).
    """
    import datetime as _dt

    _require_day_opt(day)
    for _label, _val in (("--since", since), ("--until", until)):
        if _val is not None:
            try:
                _parsed = _dt.datetime.strptime(_val, "%Y-%m-%d")
            except ValueError:
                click.echo(f"ERROR: {_label} must be YYYY-MM-DD", err=True)
                sys.exit(2)
            if _parsed.strftime("%Y-%m-%d") != _val:
                click.echo(f"ERROR: {_label} must be YYYY-MM-DD", err=True)
                sys.exit(2)

    if not dest or not dest.strip():
        import os as _os
        dest = _os.environ.get("MAVERICK_SIEM_DEST")
        if not dest:
            try:
                from ..config import load_config
                dest = (load_config() or {}).get("audit", {}).get("siem_dest")
            except Exception:
                dest = None
    if not dest or not str(dest).strip():
        click.echo(
            "ERROR: no SIEM destination (--to, MAVERICK_SIEM_DEST, or "
            "[audit] siem_dest)", err=True,
        )
        sys.exit(2)

    # Same paid-tier entitlement gate as export, including active deployment
    # tenants resolved by feature_allowed when --tenant is omitted.
    from ..billing import feature_allowed
    if not feature_allowed("audit_export", tenant=tenant):
        from ..paths import current_tenant_id
        denied_tenant = tenant or current_tenant_id() or "active tenant"
        click.echo(
            f"ERROR: tenant '{denied_tenant}' plan does not include SIEM audit "
            "export (audit_export entitlement). Upgrade the tenant's plan.",
            err=True,
        )
        sys.exit(2)

    from ..audit import forwarder
    from ..audit.export import iter_audit_events, to_cef, to_jsonl

    # Validate the destination up front so a typo fails before we read the log.
    try:
        forwarder.parse_dest(str(dest))
    except ValueError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(2)

    render = to_cef if fmt == "cef" else to_jsonl
    lines = (render(ev) for ev in iter_audit_events(
        day=day, all_days=all_days, since=since, until=until, tenant=tenant,
    ))

    if dry_run:
        n = sum(1 for _ in lines)
        click.echo(f"dry-run: {n} event(s) would ship to {dest}", err=True)
        return

    try:
        sent = forwarder.forward(lines, str(dest))
    except Exception as e:
        click.echo(f"ERROR: SIEM forward failed: {e}", err=True)
        sys.exit(1)
    if sent == 0:
        click.echo("no audit events to forward", err=True)
    else:
        click.echo(f"forwarded {sent} event(s) to {dest}", err=True)


@audit.group("worm")
def audit_worm() -> None:
    """Write-once (WORM) export of closed audit day-files (see docs/security-hardening.md)."""


@audit_worm.command("push")
@click.option("--dry-run", is_flag=True,
              help="Show which day-files would be shipped; write nothing.")
def audit_worm_push(dry_run: bool) -> None:
    """Ship closed audit day-files to the configured write-once target.

    Each day-file dated before today is shipped to an S3 Object-Lock bucket (or a
    local read-only mirror) with a retention lock, so the historical trail can't
    be altered or deleted even by a privileged insider. Idempotent: unchanged
    files are skipped; a file changed by `audit seal` / erase is re-shipped as a
    new locked version. Configure via `[audit.worm]`.
    """
    from ..audit.worm import WormUnavailable, push_closed_dayfiles
    try:
        report = push_closed_dayfiles(dry_run=dry_run)
    except WormUnavailable as e:
        raise click.ClickException(str(e)) from e
    for name, status in sorted(report.items()):
        click.echo(f"  {name}: {status}")
    failed = [
        name for name, status in report.items()
        if status.startswith(("error", "refused"))
    ]
    if failed:
        raise click.ClickException(
            f"{len(failed)} day-file(s) were not safely shipped: {sorted(failed)}"
        )
    shipped = sum(1 for s in report.values()
                  if s.startswith(("pushed", "re-pushed", "would")))
    click.echo(f"{'Would ship' if dry_run else 'Shipped'} {shipped} day-file(s).")


@audit_worm.command("verify")
def audit_worm_verify() -> None:
    """Check closed day-files against the WORM manifest.

    Reports, per closed day-file, whether its current bytes were shipped (`ok`),
    differ from the last shipped version (`changed since push` -- re-run push), or
    were never shipped (`NOT pushed`). Exits non-zero if anything is unshipped or
    diverged, so it can gate a compliance cron.
    """
    from ..audit.worm import verify
    report = verify()
    for name, status in sorted(report.items()):
        click.echo(f"  {name}: {status}")
    bad = [n for n, s in report.items() if s != "ok"]
    if bad:
        click.echo(f"{len(bad)} day-file(s) not durably shipped: {sorted(bad)}",
                   err=True)
        sys.exit(1)
    click.echo(f"all {len(report)} closed day-file(s) verified in WORM store.")


@main.command()
@click.option("--reason", default="manual halt", help="Why you're halting.")
def halt(reason: str) -> None:
    """Halt all in-flight goals by writing the HALT file."""
    from ..killswitch import _halt_file_path
    p = _halt_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason + "\n")
    click.echo(f"halt set: {p}")


@main.command("unhalt")
def unhalt() -> None:
    """Remove the HALT file to allow goals to run again."""
    from ..killswitch import _halt_file_path
    p = _halt_file_path()
    if p.exists():
        p.unlink()
        click.echo(f"cleared: {p}")
    else:
        click.echo(f"no halt file at {p}")


# ----- Cost / export / logs --------------------------------------------







# Register command groups that have been split into submodules. Imported last,
# so every shared helper and `main` is defined before the submodule decorators
# run (@main.group/@main.command register onto `main` on import).
from . import (  # noqa: E402,F401
    _knowledge_groups,
    _learning_groups,
    _practice_groups,
)

if __name__ == "__main__":
    main()


