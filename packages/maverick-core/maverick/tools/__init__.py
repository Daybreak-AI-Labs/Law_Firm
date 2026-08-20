"""Bounded tool registry for the two-person law-firm runtime.

Each tool is a name + JSON schema + executor function. The executor may be a
sync function returning str, or an async coroutine returning str.

The public constructor keeps its historical arguments for compatibility, but
the firm base registry does not auto-load MCP, plugin, gRPC, generated, mobile,
computer-use, or ambient-credential tool surfaces.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .. import killswitch
from ..budget import BudgetExceeded


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def as_bool(value: Any) -> bool:
    """Strict confirm gate for destructive or costly tool ops.

    Only a real boolean ``True`` authorises a live action. ``bool("false")``
    is ``True`` in Python, so a stringy confirm (from a non-conforming MCP
    client or a loose LLM) must fail closed to a dry run rather than fire a
    refund / delete / send. Shared so every gated tool decides the same way.
    """
    return value is True


def scrub_child_env() -> dict[str, Any]:
    """Env for a tool subprocess with secrets stripped.

    Tools that shell out (git/ffmpeg/pandoc/tesseract/adb/...) have no need
    for provider keys or connector tokens; inheriting the full ``os.environ``
    let a prompt-injected agent (or a hostile input file / repo config) read
    them out of the child. Reuse the sandbox's deny-by-pattern scrubber so a
    newly added credential is covered by default.
    """
    from ..sandbox.local import scrub_env
    return scrub_env()


# Media-tool flags that turn "convert a file" into arbitrary code exec or
# arbitrary file/URL read when injected via a freeform args[] array.
_DANGEROUS_MEDIA_FLAGS = (
    "-i", "--input", "-f", "--from", "--lua-filter", "--filter",
    "-lavfi", "-filter_complex", "-vf", "-af", "concat:",
    "--include-in-header", "--include-before-body", "--include-after-body",
    "--template", "--metadata-file", "--resource-path", "--extract-media",
    "--pdf-engine", "--syntax-definition", "--abbreviations", "--data-dir",
)


def safe_media_args(raw: Any) -> list[str]:
    """Filter a model-supplied freeform args[] list for media tools.

    By default DROP dangerous flags (input/filter/template injection that
    bypasses the tool's path confinement -- e.g. ``pandoc --lua-filter=x.lua``
    = arbitrary code, ``ffmpeg -i /etc/passwd`` = arbitrary file read). An
    operator who genuinely needs raw passthrough can opt in with
    ``MAVERICK_ALLOW_RAW_MEDIA_ARGS=1`` (then the list passes verbatim).
    """
    items = [str(a) for a in (raw or [])]
    if _env_true("MAVERICK_ALLOW_RAW_MEDIA_ARGS"):
        return items
    safe: list[str] = []
    skip_next = False
    for a in items:
        if skip_next:
            # This token is the value of a dropped flag (e.g. the path after
            # a bare `-i`); drop it too.
            skip_next = False
            continue
        low = a.lower()
        dangerous = False
        takes_value = False
        for f in _DANGEROUS_MEDIA_FLAGS:
            if f.endswith(":"):
                if low.startswith(f):          # e.g. concat:...
                    dangerous = True
                    break
            elif low == f:                     # bare flag -> value is the next token
                dangerous = True
                takes_value = True
                break
            elif low.startswith(f + "="):      # flag=value -> self-contained
                dangerous = True
                break
        if dangerous:
            skip_next = takes_value
            continue
        safe.append(a)
    return safe


def sandbox_run(
    sandbox: Any,
    argv: list[str],
    *,
    timeout: float = 120.0,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run ``argv`` through the sandbox chokepoint; return (code, stdout, stderr).

    Media tools (ffmpeg/imagemagick/pandoc/tesseract/pa11y...) historically
    shelled out on the host with ``subprocess.run`` even when a sandbox was
    wired in, bypassing the chokepoint that confines model-driven commands and
    lets tests swap the backend (CLAUDE.md rule #4). This routes the command
    through ``sandbox.exec()`` instead.

    Through a sandbox, the argv is shell-quoted with ``shlex.join`` and handed
    to ``exec()`` (which runs ``sh -c``); ``stdin`` is fed via a base64 pipe so
    arbitrary text reaches the process without shell-quoting hazards. When no
    sandbox is wired in, falls back to a scrubbed-env ``subprocess.run`` of the
    raw argv list (no shell interpolation -- the chokepoint guard forbids that
    outside the sandbox backends), passing ``stdin`` directly.
    """
    if sandbox is not None and hasattr(sandbox, "exec"):
        import shlex
        cmd = shlex.join(argv)
        if stdin is not None:
            import base64
            b64 = base64.b64encode(stdin.encode("utf-8")).decode("ascii")
            cmd = f"printf %s {shlex.quote(b64)} | base64 -d | {cmd}"
        try:
            res = sandbox.exec(cmd, timeout=timeout)
        except TypeError:
            # Backend without a per-call timeout kwarg.
            res = sandbox.exec(cmd)
        return res.exit_code, res.stdout, res.stderr

    import subprocess
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout, env=scrub_child_env(), input=stdin,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {timeout}s"


def _forward_chunk(listener, name: str, chunk: Any) -> str:
    """Streaming tool_result: forward one chunk to the registry listener
    (best-effort) and return it as text for accumulation."""
    piece = chunk if isinstance(chunk, str) else str(chunk)
    if listener is not None:
        try:
            listener(name, piece)
        except Exception:  # listener must never break the call
            pass
    return piece


async def _execute_tool_fn(fn, args: dict[str, Any], stream_chunk) -> str:
    """Run a tool fn under every supported contract.

    str-returning (sync or async) is the classic contract. **Streaming
    tool_result**: a fn may be an async generator, or return a sync
    generator/iterator of chunks — chunks flow through ``stream_chunk`` as
    they are produced (the dashboard/TUI live-view seam) and the joined text
    is the tool_result the model sees, so the model protocol is unchanged.
    Sync fns (and sync generators) drain on a worker thread so a slow tool
    can't stall the event loop.
    """
    if inspect.iscoroutinefunction(fn):
        out = await fn(args)
    elif inspect.isasyncgenfunction(fn):
        parts: list[str] = []
        async for chunk in fn(args):
            parts.append(stream_chunk(chunk))
        return "".join(parts)
    else:
        out = await asyncio.to_thread(fn, args)
    if inspect.isawaitable(out):
        out = await out
    if inspect.isgenerator(out) or (hasattr(out, "__next__") and not isinstance(out, str)):
        parts = []

        def _drain() -> None:
            for chunk in out:
                parts.append(stream_chunk(chunk))

        await asyncio.to_thread(_drain)
        return "".join(parts)
    # The fn contract is ``-> str``; a tool that returns None (a latent bug in
    # tool code) used to propagate None into _cap_tool_output and crash the agent
    # turn with "NoneType has no len()". Coerce to a stable string instead.
    if out is None:
        return ""
    return out if isinstance(out, str) else str(out)


ToolFn = Callable[[dict[str, Any]], str | Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    # When True, the agent loop may run this tool CONCURRENTLY with the
    # other tool calls in the same model turn (asyncio.gather). Only set
    # it on side-effect-free, idempotent reads (read_file, list_dir,
    # repo_map, dep_graph). Anything that writes the workspace, shells
    # out, spawns children, sends a message, or holds a remote rate limit
    # must stay False so it executes serially. The loop only parallelises
    # a turn when EVERY call in it is parallel_safe, so the default of
    # False is always safe — it just forgoes the speedup. Not part of
    # ``to_anthropic()``: it must never alter the tool catalog the model
    # sees (that would bust the prompt cache).
    parallel_safe: bool = False

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self, *, principal: str | None = None):
        self._tools: dict[str, Tool] = {}
        # Authenticated execution identity used by credential-bearing tools.
        # Stored on the registry and rebound with a ContextVar per invocation so
        # sync tools running in worker threads cannot borrow another run's saved
        # SaaS connection.
        self._principal = str(principal or "").strip() or None
        self._firm_secure = False
        self._firm_allowed: frozenset[str] | None = None
        self._firm_matter_snapshot: tuple[int, str, str] | None = None
        self._configure_firm_boundary()
        self._acl_allowed: set[str] = set()
        self._acl_denied: set[str] = set()
        self._acl_max_risk: str | None = None
        # Streaming tool_result: an optional listener that receives
        # (tool_name, chunk) for tools whose fn yields chunks (generators).
        # The model still gets the joined result; this is the live-UX seam.
        self._chunk_listener = None
        # Memoized to_anthropic() payload. The exposed tool set is stable
        # across turns (it only changes on register / set_acl), but the agent
        # loop otherwise re-serialises every schema each turn. Consumers copy
        # before mutating, so sharing the cached list is safe.
        self._anthropic_cache: list[dict[str, Any]] | None = None

    def _configure_firm_boundary(self) -> None:
        """Resolve the immutable tool envelope for this firm run.

        Secure-default registries are created while an exact ``MatterContext``
        is bound.  Only the selected legal profile's declared tools plus the
        small, non-client-data kernel can ever be registered.  A registry made
        without that context is intentionally kernel-only and cannot be
        widened later by binding ambient state.
        """
        from ..security_defaults import secure_by_default

        self._firm_secure = secure_by_default()
        if not self._firm_secure:
            return

        allowed = set(FIRM_KERNEL_TOOL_NAMES)
        try:
            from ..domain import enabled_domains, suite_for
            from ..matter_context import (
                GOAL_EXECUTION_PURPOSE,
                current_matter_context,
            )

            context = current_matter_context()
            principal_matches = (
                context is not None
                and (self._principal is None or self._principal == context.principal)
            )
            if (
                context is not None
                and principal_matches
                and context.purpose == GOAL_EXECUTION_PURPOSE
                and suite_for(context.domain) == "legal"
            ):
                profile = enabled_domains().get(context.domain)
                if profile is not None:
                    declared = set(profile.allow_tools) - set(profile.deny_tools)
                    allowed.update(declared & LEGAL_PROFILE_TOOL_NAMES)
                    self._firm_matter_snapshot = (
                        context.matter_id,
                        context.principal,
                        context.domain,
                    )
        except Exception:
            # Policy discovery is an authorization boundary.  Kernel-only is
            # the safe result of any missing/corrupt context or profile data.
            pass
        self._firm_allowed = frozenset(allowed)

    def _firm_allows(self, name: str, *, require_bound_context: bool) -> bool:
        if not self._firm_secure:
            return True
        if self._firm_allowed is None or name not in self._firm_allowed:
            return False
        if name in FIRM_CONTEXT_FREE_KERNEL_TOOL_NAMES:
            return True
        if self._firm_matter_snapshot is None:
            return False
        if not require_bound_context:
            return True
        try:
            from ..matter_context import current_matter_context

            context = current_matter_context()
            return context is not None and (
                context.matter_id,
                context.principal,
                context.domain,
            ) == self._firm_matter_snapshot
        except Exception:
            return False

    def set_acl(
        self,
        *,
        allowed: set[str] | None = None,
        denied: set[str] | None = None,
        max_risk: str | None = None,
    ) -> None:
        self._acl_allowed = set(allowed or set())
        self._acl_denied = set(denied or set())
        self._acl_max_risk = max_risk
        self._anthropic_cache = None

    def _acl_allows(self, name: str) -> bool:
        if self._acl_allowed and name not in self._acl_allowed:
            return False
        if self._acl_denied and name in self._acl_denied:
            return False
        if self._acl_max_risk:
            from ..safety.tool_risk import risk_rank, tool_risk

            if risk_rank(tool_risk(name)) > risk_rank(self._acl_max_risk):
                return False
        return True

    def set_chunk_listener(self, listener) -> None:
        """Register ``listener(tool_name, chunk)`` for streaming tool output.

        Tools that return an iterator/generator of str chunks stream through
        it as they produce output (dashboard/TUI live view); the joined text
        remains the tool_result the model sees. Pass None to clear. Listener
        errors are swallowed — observability must never break a tool call.
        """
        self._chunk_listener = listener

    def register(self, tool: Tool) -> None:
        if not self._firm_allows(tool.name, require_bound_context=True):
            return
        if not self._acl_allows(tool.name):
            return
        if self._firm_secure and tool.name in self._tools:
            # Later registration may never shadow a retained first-party firm
            # tool under the production posture.
            return
        self._tools[tool.name] = tool
        self._anthropic_cache = None

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_anthropic(self) -> list[dict[str, Any]]:
        if self._anthropic_cache is None:
            self._anthropic_cache = [t.to_anthropic() for t in self._tools.values()]
        return self._anthropic_cache

    def _run_denial(self, name: str) -> str | None:
        if name not in self._tools:
            return f"ERROR: unknown tool {name!r}"
        if not self._firm_allows(name, require_bound_context=True):
            return "ERROR: tool is outside the bound matter/profile authority"
        if self._firm_secure and name not in FIRM_CONTEXT_FREE_KERNEL_TOOL_NAMES:
            try:
                from ..matter_context import refresh_matter_context

                context = refresh_matter_context()
                live = (context.matter_id, context.principal, context.domain)
                if live != self._firm_matter_snapshot:
                    raise RuntimeError("matter authority changed")
            except Exception:
                return "ERROR: live matter authority is unavailable or revoked"
        return None

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if denial := self._run_denial(name):
            return denial
        try:
            from ..observability import gen_ai_tool_attributes, trace_span
        except ImportError:  # pragma: no cover
            import contextlib

            def trace_span(*a, **kw):  # type: ignore
                return contextlib.nullcontext()

            def gen_ai_tool_attributes(tool_name, **kw):  # type: ignore
                return {}
        with trace_span(
            "tool.run",
            attributes={"tool.name": name, **gen_ai_tool_attributes(name)},
        ):
            import time as _perf_time
            _t0 = _perf_time.perf_counter()
            try:
                # Opt-in tool-output cache (default OFF): serve a memoized
                # result for side-effect-free (parallel_safe) tools so a
                # repeated read doesn't re-do the work. Never caches writes
                # or error results. See tool_cache.py.
                _tool = self._tools[name]
                # Cache identity is an authorization boundary. Resolve it
                # before lookup (the connector ContextVar is bound only while
                # invoking the function) and include both tenant and principal
                # so two users or clients cannot share a credential-bearing
                # read result merely because the tool arguments match.
                from ..paths import current_tenant_id

                if self._firm_matter_snapshot is None:
                    _matter_namespace = "matter=<none>\0domain=<none>"
                else:
                    _matter_namespace = (
                        f"matter={self._firm_matter_snapshot[0]}\0"
                        f"domain={self._firm_matter_snapshot[2]}"
                    )
                _cache_namespace = (
                    f"tenant={current_tenant_id() or '<shared>'}\0"
                    f"principal={self._principal or '<anonymous>'}\0"
                    f"{_matter_namespace}"
                )
                try:
                    from ..cache.tool import get_cached, store_cached
                except ImportError:  # pragma: no cover
                    get_cached = store_cached = None  # type: ignore[assignment]
                if get_cached is not None:
                    _hit, _cached = get_cached(
                        _tool, args, namespace=_cache_namespace,
                    )
                    if _hit:
                        try:
                            from ..observability import record_metric as _rm
                            _rm("tool_calls",
                                labels={"tool": name, "status": "cache"})
                        except Exception:  # pragma: no cover
                            pass
                        return _cached
                try:
                    from ..chaos import maybe_fail
                    maybe_fail("tool_dispatch",
                               message=f"chaos: tool_dispatch on {name!r}")
                except ImportError:
                    pass
                async def _invoke() -> str:
                    from ..connections import bind_principal

                    with bind_principal(self._principal):
                        return await _execute_tool_fn(
                            self._tools[name].fn, args,
                            lambda chunk: _forward_chunk(self._chunk_listener, name, chunk),
                        )

                # One shared reliability policy: transient upstream failures
                # on retry-safe (non-high-risk) tools are retried with backoff.
                from ..tool_reliability import run_with_retry
                result = await run_with_retry(name, _invoke)
                from ..tool_results import ToolResultState, classify_tool_result
                result_state = classify_tool_result(result)
                if store_cached is not None and result_state is ToolResultState.SUCCEEDED:
                    try:
                        store_cached(
                            _tool, args, result,
                            namespace=_cache_namespace,
                        )
                        # A side-effectful tool may have changed what any
                        # cached read returned; drop stale reads.
                        from ..cache.tool import note_side_effect
                        note_side_effect(_tool)
                    except Exception:  # pragma: no cover
                        pass
                try:
                    from ..observability import record_metric as _rm
                    _rm("tool_calls", labels={"tool": name, "status": result_state.value})
                except Exception:  # pragma: no cover
                    pass
                return result
            except (BudgetExceeded, killswitch.Halted):
                # Control-flow stop signals are NOT tool errors: a spawned
                # child (spawn_subagent/spawn_swarm/spawn_specialist) shares
                # the parent Budget and killswitch, and re-raises these to
                # halt the whole run immediately. They must propagate to the
                # agent loop's `except (BudgetExceeded, killswitch.Halted)`
                # handler (agent.py) instead of being folded into an "ERROR:"
                # string by the blanket handler below — otherwise the budget
                # cap / killswitch is not enforced and the run keeps going.
                raise
            except Exception as e:
                # Tool errors (incl. an injected tool_dispatch chaos failure)
                # are surfaced as a tool-result string so the agent can react
                # — this mirrors how real tool exceptions behave. The chaos
                # gap the council flagged is on the LLM path, fixed by wiring
                # maybe_fail("llm_call") into complete_async (not here).
                try:
                    from ..observability import record_metric as _rm
                    _rm("tool_calls", labels={"tool": name, "status": "error"})
                except Exception:  # pragma: no cover
                    pass
                return f"ERROR: {type(e).__name__}: {e}"
            finally:
                # Always-on per-tool latency profile (complements OTel spans).
                # Records on both the success and error paths; never raises.
                _elapsed_ms = (_perf_time.perf_counter() - _t0) * 1000.0
                try:
                    from ..tool_latency import record as _rec_latency
                    _rec_latency(name, _elapsed_ms)
                except Exception:  # pragma: no cover -- profiling never breaks a tool
                    pass
                # Opt-in per-tool latency budget (default OFF): record a breach
                # if the call ran longer than [tools] latency_budget_ms. Fail-open.
                try:
                    from ..latency_budget import note_elapsed as _note_budget
                    _note_budget(name, _elapsed_ms)
                except Exception:  # pragma: no cover -- budget never breaks a tool
                    pass


# Exact names referenced by the retained legal profiles.  Some are abstract
# repository adapters supplied by a deployment; keeping their names reserves a
# governed seam without auto-discovering arbitrary plugins or remote servers.
LEGAL_PROFILE_TOOL_NAMES = frozenset({
    "carta_read",
    "clause_library_read",
    "clio_read",
    "contract_clause_read",
    "contract_read",
    "contract_repository_read",
    "contractbook_read",
    "dependency_manifest_read",
    "docusign_read",
    "incident_record_read",
    "invoice_read",
    "ironclad_read",
    "knowledge_search",
    "list_attachments",
    "read_attachment",
    "read_file",
    "spreadsheet",
    "sql_query",
    "web_search",
})

# Non-client-data orchestration seams that the retained agent runtime imports
# directly.  These do not grant filesystem, connector, or cross-matter recall.
FIRM_KERNEL_TOOL_NAMES = frozenset({
    "ask_user",
    "budget_status",
    "citation_verifier",
    "delegate_to_agent",
    "kv_memory",
    "list_specialists",
    "recv_from_agent",
    "send_to_agent",
    "spawn_specialist",
    "spawn_subagent",
    "spawn_swarm",
})

FIRM_CONTEXT_FREE_KERNEL_TOOL_NAMES = frozenset({
    "budget_status",
    "citation_verifier",
})

FIRM_RUNTIME_MAX_TOOL_NAMES = frozenset(
    LEGAL_PROFILE_TOOL_NAMES
    | FIRM_KERNEL_TOOL_NAMES
)
CORE_TOOL_NAMES = FIRM_RUNTIME_MAX_TOOL_NAMES

def base_registry(
    world,
    sandbox,
    goal_id: int | None = None,
    enable_web_search: bool = False,
    channel: str | None = None,
    user_id: str | None = None,
    budget: Any = None,
) -> ToolRegistry:
    """Build the bounded two-person-firm tool registry.

    There is no MCP, plugin, gRPC, generated-tool, computer-use, mobile, or
    deferred expansion hook. Under secure defaults, ToolRegistry additionally
    intersects this fixed catalog with the exact bound legal profile.
    """
    from .ask_user import ask_user
    from .attachments import list_attachments_tool, read_attachment_tool
    from .budget_status import budget_status
    from .citation_verifier import citation_verifier
    from .fs import read_file
    from .kv_memory import kv_memory
    from .spreadsheet import spreadsheet
    from .sql_query import sql_query

    principal = str(user_id or "").strip()
    if principal and not principal.startswith("user:"):
        principal = f"user:{principal}"
    if not principal:
        from ..connections import current_principal

        principal = str(current_principal() or "").strip()
    if not principal:
        try:
            from ..matter_context import current_matter_context

            context = current_matter_context()
            principal = context.principal if context is not None else ""
        except Exception:
            principal = ""

    reg = ToolRegistry(principal=principal or None)
    reg.register(read_file(sandbox))
    reg.register(ask_user(world, goal_id=goal_id))
    reg.register(list_attachments_tool(world, goal_id))
    reg.register(read_attachment_tool(world, goal_id))
    reg.register(spreadsheet(sandbox))
    reg.register(sql_query(sandbox))
    reg.register(citation_verifier())
    reg.register(kv_memory(world, goal_id))
    reg.register(budget_status(budget=budget))

    if enable_web_search:
        from .web_search import web_search

        reg.register(web_search())
    # Five explicit, GET-only legal-system adapters replace the inherited
    # enterprise connector generator. Profile and MatterContext filtering is
    # still applied by ToolRegistry.register.
    from .enterprise_connectors import enterprise_connectors

    for connector in enterprise_connectors():
        reg.register(connector)

    _apply_rate_limits(reg)
    _apply_tool_acl(reg, channel=channel, user_id=user_id)

    return reg


def base_tool_names() -> set[str] | None:
    """Return the complete name ceiling for the firm runtime.

    This is declarative and side-effect free: introspection never opens a
    database, imports a plugin, scans generated code, or starts an MCP client.
    """
    return set(FIRM_RUNTIME_MAX_TOOL_NAMES)


def _apply_tool_acl(reg: ToolRegistry, *, channel: str | None, user_id: str | None) -> None:
    # Apply allow/deny lists from ~/.maverick/config.toml [security].
    # Operational discovery errors remain fail-soft, but an unreadable
    # operator policy must stop registry construction. Swallowing that
    # security error would leave every tool registered after a deny-list
    # became corrupt.
    from ..runtime_overrides import RuntimeOverridesSecurityError

    try:
        from ..safety.tool_acl import apply_to_registry
        apply_to_registry(reg, channel=channel, user_id=user_id)
    except RuntimeOverridesSecurityError:
        raise
    except Exception as e:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning("tool_acl: %s", e)


def _apply_rate_limits(reg: ToolRegistry, *, fail_silently: bool = False) -> None:
    try:
        from ..safety.rate_limiter import apply_to_registry as _rl_apply
        _rl_apply(reg)
    except Exception as e:  # pragma: no cover
        if fail_silently:
            return
        import logging as _logging
        _logging.getLogger(__name__).warning("rate_limiter: %s", e)


default_registry = base_registry
