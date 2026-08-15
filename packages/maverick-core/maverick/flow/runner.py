"""Flow interpreter -- runs a :class:`~.ir.Flow` deterministically.

The control-flow nodes (branch / foreach / parallel / approval / delay) are
evaluated here; the *work* nodes call injected executors (``agent_fn`` for an
agentic goal, ``action_fn`` for a deterministic tool call), so the interpreter is
pure and unit-testable without an LLM or a live connector. A ``data`` dict is
threaded through the graph -- each node may read prior values (``{{key}}`` in a
brief/param, ``key`` in a condition) and write its result under ``output``.

Durability / resume: the top-level run PAUSES at an ``approval`` node (when the
``approve_fn`` policy returns ``pending``) or a ``delay`` node, returning enough
state to continue later (:func:`run_flow` with ``resume=``). foreach bodies and
parallel branches run to completion in one pass (v1: no pause inside a loop
body/branch -- an approval there must resolve via ``approve_fn``); the outer graph
is where human-in-the-loop lives.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..budget import BudgetExceeded
from ..killswitch import Halted
from .ir import (
    MAX_FLOW_NODE_RETRY_SLEEP,
    NODE_ACTION,
    NODE_AGENT,
    NODE_APPROVAL,
    NODE_BRANCH,
    NODE_DELAY,
    NODE_FOREACH,
    NODE_PARALLEL,
    NODE_SCOPE,
    NODE_SETVAR,
    NODE_SUBFLOW,
    NODE_SWITCH,
    NODE_WAIT,
    NODE_WHILE,
    Flow,
    FlowNode,
    max_node_retries,
)

# Run statuses ----------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_RESUMING = "resuming"
STATUS_COMPLETED = "completed"
STATUS_PAUSED_APPROVAL = "paused_approval"
STATUS_PAUSED_DELAY = "paused_delay"
STATUS_PAUSED_EVENT = "paused_event"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_INDETERMINATE = "indeterminate"

# The single source of truth for "which statuses mean a run is parked and
# resumable" and "which statuses mean a run is still in flight". Every gate that
# used to hand-list these literals (the resume/retry endpoints, the sweep, the
# JS overlay) should consume these instead -- a new pause status is then added
# in one place, not six.
PAUSED_STATUSES = frozenset({
    STATUS_PAUSED_APPROVAL, STATUS_PAUSED_DELAY, STATUS_PAUSED_EVENT,
})
ACTIVE_STATUSES = frozenset({
    STATUS_QUEUED, STATUS_CLAIMED, STATUS_RUNNING, STATUS_RESUMING,
}) | PAUSED_STATUSES
# A quarantined run may have committed an external side effect, but no reliable
# acknowledgement reached the engine.  It is deliberately neither active nor
# retryable: an operator must reconcile the external system before choosing a
# compensating or replacement action.
QUARANTINED_STATUSES = frozenset({STATUS_INDETERMINATE})

_WHILE_DEFAULT_CAP = 100   # while-loop iterations when the node sets no limit
_PARALLEL_MAX_WORKERS = 8   # cap fan-out threads for the whole flow run

_COND = re.compile(r"^\s*(\S+)\s*(==|!=|>=|<=|>|<|contains)\s*(.+?)\s*$")
_FN_CALL = re.compile(r"^([a-zA-Z_]\w*)\s*\((.*)\)$")


def render(text: str, data: dict, *, allow_secrets: bool = False) -> str:
    """Substitute ``{{expr}}`` from ``data``. ``expr`` is a dotted key
    (``{{a.b}}``) or a transform call over keys/literals (``{{upper(name)}}``,
    ``{{default(x, "n/a")}}``, ``{{concat(first, " ", last)}}``). An unfilled bare
    key stays literal (unchanged); a function always resolves (missing args are
    empty).

    Secret resolution is opt-in and reserved for transient action parameters.
    Other render sites feed persisted flow data, prompts, or scoped inputs, so
    ``{{secret(...)}}`` must remain unavailable there.
    """
    src = str(text)
    out: list[str] = []
    pos = 0
    while True:
        start = src.find("{{", pos)
        if start < 0:
            out.append(src[pos:])
            break
        end = src.find("}}", start + 2)
        if end < 0:
            out.append(src[pos:])
            break
        out.append(src[pos:start])
        raw = src[start:end + 2]
        expr = src[start + 2:end].strip()
        v = _eval_expr(expr, data, allow_secrets=allow_secrets)
        out.append(raw if v is None else _as_text(v))
        pos = end + 2
    return "".join(out)


def _as_text(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))          # render add(2,3) as "5", not "5.0"
    return str(v)


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _fn_slice(a: list) -> Any:
    """slice(value, start[, end]) -- substring or sublist. Missing end = to the
    end. Works on a string or a list; out-of-range indices clamp like Python."""
    if not a:
        return ""
    seq = a[0]
    if not isinstance(seq, (str, list, tuple)):
        seq = _as_text(seq)
    start = int(_num(a[1])) if len(a) > 1 else 0
    end = int(_num(a[2])) if len(a) > 2 else len(seq)
    return seq[start:end]


# Python's backtracking ``re`` engine has no match timeout. Keep flow regexes
# intentionally small and conservative: permit literal/character-class searches
# and simple capture groups used for extraction, but reject constructs known to
# create super-linear backtracking (alternation, nested groups, backreferences,
# lookarounds, and quantified groups whose body is itself quantified).
_REGEX_MAX_PATTERN = 200
_REGEX_MAX_SUBJECT = 2000
_GROUP_QUANTIFIERS = frozenset("*+?")


def _regex_is_safe(pattern: str) -> bool:
    """Return whether ``pattern`` stays within the flow-safe regex subset."""
    in_class = False
    escaped = False
    groups: list[dict[str, bool]] = []
    last_group_had_quantifier = False

    for i, ch in enumerate(pattern):
        if escaped:
            if ch.isdigit() or ch in {"g", "k"}:
                return False
            escaped = False
            last_group_had_quantifier = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_class:
            if ch == "]":
                in_class = False
            continue
        if ch == "[":
            in_class = True
            last_group_had_quantifier = False
            continue
        if ch == "|":
            return False
        if ch == "(":
            if groups:
                return False
            if pattern[i + 1:i + 2] == "?":
                return False
            groups.append({"has_quantifier": False})
            last_group_had_quantifier = False
            continue
        if ch == ")":
            if not groups:
                return False
            last_group_had_quantifier = groups.pop()["has_quantifier"]
            continue
        if ch in _GROUP_QUANTIFIERS or ch == "{":
            if groups:
                groups[-1]["has_quantifier"] = True
            elif last_group_had_quantifier:
                return False
            last_group_had_quantifier = False
            continue
        last_group_had_quantifier = False

    return not escaped and not in_class and not groups


def _fn_regex(a: list) -> str:
    """regex(value, pattern) -- the first match; group 1 if the pattern has a
    capture group, else the whole match. Empty on no match / bad or unsafe
    pattern. Guarded against ReDoS: the pattern is length-capped and rejected
    if it has a nested unbounded quantifier, and the subject is length-capped
    (Python's ``re`` offers no match timeout, so prevention is the only lever)."""
    if len(a) < 2:
        return ""
    pattern = str(a[1])
    if len(pattern) > _REGEX_MAX_PATTERN or not _regex_is_safe(pattern):
        return ""
    subject = _as_text(a[0])[:_REGEX_MAX_SUBJECT]
    try:
        m = re.search(pattern, subject)
    except re.error:
        return ""
    if not m:
        return ""
    return m.group(1) if m.groups() else m.group(0)


def _fn_datefmt(a: list) -> str:
    """datefmt(value, fmt) -- format an epoch-seconds number or an ISO-8601
    string with a strftime pattern (UTC). Bad input yields ''. Default fmt is
    ISO date. E.g. datefmt(now(), "%Y-%m-%d")."""
    if not a:
        return ""
    import datetime as _dt
    fmt = str(a[1]) if len(a) > 1 else "%Y-%m-%d"
    val = a[0]
    try:
        if isinstance(val, (int, float)) or (isinstance(val, str) and val.strip().replace(".", "", 1).isdigit()):
            dt = _dt.datetime.fromtimestamp(_num(val), tz=_dt.timezone.utc)
        else:
            dt = _dt.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except (ValueError, OverflowError, OSError):
        return ""


def _now_utc():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _add_days(value: Any, n: Any) -> str:
    """``value`` (an ISO date/datetime, or '' for today) plus ``n`` days, as an ISO
    date (YYYY-MM-DD) -- so "due in 3 days" is ``{{add_days('', 3)}}`` and it
    orders correctly against other ISO dates. A bad date -> ''."""
    import datetime as _dt
    s = str(value or "").strip()
    try:
        base = _dt.date.fromisoformat(s[:10]) if s else _now_utc().date()
        return (base + _dt.timedelta(days=int(_num(n)))).isoformat()
    except (TypeError, ValueError):
        return ""


def _resolve_secret(name: Any) -> str:
    """Resolve a named secret from the governed provider (file vault / env) at
    render time -- so a connector credential can be `{{secret('STRIPE_KEY')}}` in
    an action param WITHOUT the raw value living in the flow definition or the
    persisted run data (it only ever materializes in the transient rendered call).
    Missing -> '' (never raises, never the literal key)."""
    try:
        from ..secret_provider import get_secret
        return get_secret(str(name)) or ""
    except Exception:  # pragma: no cover -- a secret lookup must never break a render
        return ""


# Safe transform functions usable inside {{ }} -- no eval, pure string/collection/
# arithmetic helpers. Each takes the evaluated arg list.
_FUNCS: dict[str, Callable[[list], Any]] = {
    "upper": lambda a: str(a[0]).upper() if a else "",
    "lower": lambda a: str(a[0]).lower() if a else "",
    "trim": lambda a: str(a[0]).strip() if a else "",
    "title": lambda a: str(a[0]).title() if a else "",
    "length": lambda a: (len(a[0]) if a and hasattr(a[0], "__len__") else 0),
    "concat": lambda a: "".join(_as_text(x) for x in a),
    "replace": lambda a: (str(a[0]).replace(str(a[1]), str(a[2])) if len(a) >= 3 else (str(a[0]) if a else "")),
    "default": lambda a: (a[0] if (a and a[0] not in (None, "", [], {})) else (a[1] if len(a) > 1 else "")),
    "json": lambda a: (__import__("json").dumps(a[0]) if a else ""),
    # date/time (UTC): enable "due in N days" / "older than a week" workflows,
    # ISO-formatted so they order correctly with the lexical comparison above.
    "now": lambda a: _now_utc().isoformat(timespec="seconds"),
    "today": lambda a: _now_utc().date().isoformat(),
    "add_days": lambda a: _add_days(a[0], a[1]) if len(a) >= 2 else "",
    "first": lambda a: (a[0][0] if a and isinstance(a[0], (list, tuple)) and a[0] else ""),
    "last": lambda a: (a[0][-1] if a and isinstance(a[0], (list, tuple)) and a[0] else ""),
    # string / collection transforms
    "split": lambda a: (str(a[0]).split(str(a[1])) if len(a) >= 2 else str(a[0]).split() if a else []),
    "join": lambda a: (str(a[1]).join(_as_text(x) for x in a[0]) if len(a) >= 2 and isinstance(a[0], (list, tuple)) else (_as_text(a[0]) if a else "")),
    "slice": _fn_slice,
    "index": lambda a: (a[0][int(_num(a[1]))] if a and isinstance(a[0], (list, tuple)) and -len(a[0]) <= int(_num(a[1])) < len(a[0]) else ""),
    "regex": _fn_regex,
    "keys": lambda a: (list(a[0].keys()) if a and isinstance(a[0], dict) else []),
    # arithmetic
    "add": lambda a: sum(_num(x) for x in a),
    "sub": lambda a: (_num(a[0]) - _num(a[1])) if len(a) >= 2 else _num(a[0] if a else 0),
    "mul": lambda a: (_num(a[0]) * _num(a[1])) if len(a) >= 2 else _num(a[0] if a else 0),
    "div": lambda a: (_num(a[0]) / _num(a[1])) if (len(a) >= 2 and _num(a[1])) else 0,
    "round": lambda a: round(_num(a[0]), int(_num(a[1])) if len(a) > 1 else 0) if a else 0,
    "abs": lambda a: abs(_num(a[0])) if a else 0,
    "int": lambda a: int(_num(a[0])) if a else 0,
    "datefmt": _fn_datefmt,
}


def _split_args(s: str) -> list[str]:
    """Comma-split respecting quotes AND nested parens, so a literal comma or a
    nested call's arguments (``default(x, "y")``) survive as one argument."""
    out, cur, q, depth = [], "", None, 0
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth = max(0, depth - 1)
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if s.strip():
        out.append(cur)
    return [a.strip() for a in out]


def _arg_value(tok: str, data: dict, *, allow_secrets: bool = False) -> Any:
    """A function argument: a quoted/number/bool literal, a NESTED transform call
    (evaluated recursively), else a data key (missing -> '')."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(tok)
        except ValueError:
            pass
    m = _FN_CALL.match(tok)
    funcs = _funcs(allow_secrets=allow_secrets)
    if m and m.group(1) in funcs:          # nested call: {{upper(default(x,"y"))}}
        return _eval_expr(tok, data, allow_secrets=allow_secrets)
    return _dig(data, tok, "")


def _funcs(*, allow_secrets: bool = False) -> dict[str, Callable[[list], Any]]:
    if not allow_secrets:
        return _FUNCS
    return {**_FUNCS, "secret": lambda a: _resolve_secret(a[0]) if a else ""}


def _eval_expr(expr: str, data: dict, *, allow_secrets: bool = False) -> Any:
    """Evaluate a ``{{ }}`` expression: a transform call (args may themselves be
    transform calls), else a dotted-key dig (``None`` for a missing bare key so
    render keeps it literal)."""
    m = _FN_CALL.match(expr.strip())
    funcs = _funcs(allow_secrets=allow_secrets)
    if m and m.group(1) in funcs:
        args = [_arg_value(a, data, allow_secrets=allow_secrets) for a in _split_args(m.group(2))]
        try:
            return funcs[m.group(1)](args)
        except Exception:  # a bad transform never breaks rendering
            return ""
    return _dig(data, expr.strip())


def _dig(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _coerce(token: str) -> Any:
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        return token[1:-1]
    low = token.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def _eval_predicate(expr: str, data: dict) -> bool:
    """One ``<key> <op> <value>`` comparison (or a bare truthiness test)."""
    m = _COND.match(str(expr or ""))
    if not m:
        return bool(_dig(data, str(expr).strip()))
    key, op, rhs = m.group(1), m.group(2), _coerce(m.group(3))
    lhs = _dig(data, key)
    try:
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        if op == "contains":
            return str(rhs) in str(lhs) if lhs is not None else False
        # Ordering: numeric when BOTH sides are numbers, else lexical -- so an ISO
        # date/string comparison (`created >= '2026-01-01'`) is honoured instead of
        # silently false. A None lhs (missing key) is never ordered-true.
        if lhs is None:
            return False
        lo, ro = _order_operands(lhs, rhs)
        return {">": lo > ro, "<": lo < ro, ">=": lo >= ro, "<=": lo <= ro}[op]
    except (TypeError, ValueError):
        return False


def _order_operands(lhs: Any, rhs: Any) -> tuple[Any, Any]:
    """Coerce a pair for an ordering comparison: floats when both parse as numbers
    (so `10 > 9` isn't the string-wise `'10' > '9'` == False), else strings (so
    dates and other text order lexically instead of raising / being dropped)."""
    try:
        return float(lhs), float(rhs)
    except (TypeError, ValueError):
        return str(lhs), str(rhs)


def _split_bool_ops(expr: str, op: str) -> list[str]:
    """Split on a boolean operator only when it appears outside quotes."""
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i = 0
    op_len = len(op)
    while i < len(expr):
        ch = expr[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if (
            expr[i : i + op_len].lower() == op
            and (i == 0 or expr[i - 1].isspace())
            and (i + op_len == len(expr) or expr[i + op_len].isspace())
        ):
            out.append("".join(cur).strip())
            cur = []
            i += op_len
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur).strip())
    return out


def eval_condition(expr: str, data: dict) -> bool:
    """A safe boolean predicate over flow data (no eval).

    A predicate is ``<key> <op> <value>`` (``<value>`` a number / 'quoted' /
    bareword / true|false), or a bare key (truthiness). Predicates compose with
    ``and`` / ``or`` (``or`` of ``and`` groups, standard precedence), e.g.
    ``amount > 100 and region == 'EU'``. Unparsable predicates are falsey."""
    s = str(expr or "").strip()
    for or_part in _split_bool_ops(s, "or"):
        parts = [p for p in _split_bool_ops(or_part, "and") if p.strip()]
        if parts and all(_eval_predicate(p, data) for p in parts):
            return True
    return False


@dataclass
class FlowRunResult:
    status: str
    data: dict = field(default_factory=dict)
    cursor: str | None = None       # node paused/rejected at (for resume)
    resume_at: float | None = None  # epoch for a delay pause
    prompt: str = ""                # approval prompt for the UI
    error: str = ""


# Executor signatures (injected):
#   agent_fn(node, rendered_brief, data) -> (result: str, outcome: float | None)
#   action_fn(node, rendered_params, data) -> (result, outcome: float | None)
#   approve_fn(node, data) -> "approved" | "rejected" | "pending"
#   on_node(node, status: str, outcome: float | None) -> None   (telemetry/grounding)
AgentFn = Callable[[FlowNode, str, dict], tuple[Any, float | None]]
ActionFn = Callable[[FlowNode, dict, dict], tuple[Any, float | None]]


def _safe_error_text(value: Any, *, limit: int = 500) -> str:
    """Redact untrusted executor text before persistence or catch routing."""
    try:
        from .redact import redact

        safe = str(redact(str(value)))
    except Exception:  # pragma: no cover - error reporting must fail closed
        safe = "execution failed (details redacted)"
    return safe[:limit]


class _Pause(Exception):
    def __init__(self, kind: str, node: FlowNode, data: dict, resume_at: float | None = None):
        self.kind, self.node, self.data, self.resume_at = kind, node, data, resume_at


class _Reject(Exception):
    def __init__(self, node: FlowNode, data: dict):
        self.node, self.data = node, data


class _NodeError(Exception):
    """An executor raised inside a specific node -- carries the node id so a
    failed run records WHERE it failed (the retry-from-failure entry point)."""

    def __init__(self, node_id: str, cause: Exception):
        super().__init__(f"node {node_id!r}: {_safe_error_text(cause)}")
        self.node_id = node_id
        self.cause = cause


class _Indeterminate(Exception):
    """A work boundary may have committed an effect without proving its state."""

    def __init__(self, node_id: str, detail: str, *, boundary: str = "high-risk action"):
        super().__init__(
            f"{boundary} {node_id!r} is indeterminate: {_safe_error_text(detail)}"
        )
        self.node_id = node_id


@dataclass
class _Ctx:
    agent_fn: AgentFn
    action_fn: ActionFn
    approve_fn: Callable[[FlowNode, dict], str] | None
    on_node: Callable[[FlowNode, str, float | None], None]
    now: Callable[[], float]
    allow_pause: bool
    resolve_flow: Callable[[str], Flow | None] | None = None
    flow_stack: set = field(default_factory=set)   # subflow ids on the call stack
    deadline: float | None = None                  # whole-flow wall-clock deadline
    parallel_slots: Any = None                     # shared worker budget for fan-out

    def nested(self) -> _Ctx:
        # A copy that cannot pause (foreach body / subflow run straight). Shares
        # flow_stack + deadline so cycle detection and the whole-flow deadline
        # span the linear nesting.
        return _Ctx(self.agent_fn, self.action_fn, self.approve_fn, self.on_node,
                    self.now, allow_pause=False, resolve_flow=self.resolve_flow,
                    flow_stack=self.flow_stack, deadline=self.deadline,
                    parallel_slots=self.parallel_slots)

    def branched(self) -> _Ctx:
        # For a CONCURRENT parallel branch: its own flow_stack copy (independent
        # cycle detection -- a sibling branch's subflow isn't a cycle here), but a
        # shared clock/deadline and the shared on_node sink.
        return _Ctx(self.agent_fn, self.action_fn, self.approve_fn, self.on_node,
                    self.now, allow_pause=False, resolve_flow=self.resolve_flow,
                    flow_stack=set(self.flow_stack), deadline=self.deadline,
                    parallel_slots=self.parallel_slots)


def _render_params(node: FlowNode, data: dict) -> dict:
    return {k: render(v, data, allow_secrets=True) if isinstance(v, str) else v
            for k, v in (node.params or {}).items()}


def _invoke_work(node: FlowNode, data: dict, ctx: _Ctx):
    def _call():
        if node.kind == NODE_AGENT:
            return ctx.agent_fn(node, render(node.brief, data), data)
        return ctx.action_fn(node, _render_params(node, data), data)
    if not (node.timeout and node.timeout > 0):
        return _call()
    # Soft per-node timeout: Python cannot kill a running thread safely.  Once a
    # call crosses the deadline we therefore MUST wait for its authoritative
    # result.  Discarding a later success and labelling it failed would invite a
    # retry of an external mutation that actually committed.
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_call)
        try:
            return fut.result(timeout=node.timeout)
        except _cf.TimeoutError:
            return fut.result()


def _run_work(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    # Retry a failed work node up to `retries` more times -- a transient tool/agent
    # failure (outcome 0.0) OR a hard exception from the executor gets another
    # attempt before we give up. With `retry_backoff` set, wait an exponentially
    # growing interval between attempts (capped) instead of re-hammering instantly;
    # backoff 0 keeps the original tight loop (and adds no sleep to existing flows).
    backoff = max(0.0, node.retry_backoff)
    result: Any = None
    outcome: float | None = None
    last_exc: Exception | None = None
    retries = min(max(0, node.retries), max_node_retries())
    high_risk_action = False
    ambiguous_agent = node.kind == NODE_AGENT
    if node.kind == NODE_AGENT:
        # An agent attempt may already have invoked a mutating tool before its
        # goal failed.  Without a replay ledger for the whole agent trajectory,
        # automatically creating a second goal is never side-effect safe.
        retries = 0
    elif node.kind == NODE_ACTION:
        # High-risk tools may have committed before a response was lost.  The
        # registry deliberately executes them once; a flow-level retry must not
        # bypass that exactly-once safety boundary.
        from ..tool_reliability import is_retry_safe
        high_risk_action = not is_retry_safe(node.tool)
        if high_risk_action:
            retries = 0
    for attempt in range(retries + 1):
        if ctx.deadline is not None and ctx.now() > ctx.deadline:
            raise RuntimeError("flow exceeded its wall-clock deadline during node retries")
        if attempt and backoff:
            _sleep_retry_backoff(ctx, min(MAX_FLOW_NODE_RETRY_SLEEP, backoff * (2 ** (attempt - 1))))
        try:
            result, outcome = _invoke_work(node, data, ctx)
            last_exc = None
        except (BudgetExceeded, Halted):
            raise
        except Exception as e:   # a raising executor is a retryable failure, not instant death
            last_exc, outcome = e, 0.0
            continue
        if outcome != 0.0:
            break
    # A hard exception that survived every retry stays terminal (records WHERE it
    # failed via _NodeError up the chain) -- retries buy attempts, not silence.
    # A reserved refusal/preview proves the effect was intentionally NOT
    # attempted (approval denied, Shield blocked, or confirm gate returned its
    # dry-run preview). Those are terminal failures, not reconciliation cases.
    # Ordinary ERROR/exception results remain ambiguous for a high-risk tool
    # because the remote system may have committed before acknowledgement loss.
    definitive_no_effect = False
    if high_risk_action and last_exc is None and outcome == 0.0:
        from ..tool_results import ToolResultState, classify_tool_result

        definitive_no_effect = classify_tool_result(result) in {
            ToolResultState.PREVIEW,
            ToolResultState.REFUSED,
        }
    if (high_risk_action or ambiguous_agent) and not definitive_no_effect and (
        last_exc is not None or outcome == 0.0
    ):
        detail = _safe_error_text(
            f"{type(last_exc).__name__}: {last_exc}"
            if last_exc is not None else result
        )
        # Ambiguity is not a grounded failure signal.  Preserve the trace status
        # but do not feed a guessed 0.0 into self-learning.
        ctx.on_node(node, STATUS_INDETERMINATE, None)
        raise _Indeterminate(
            node.id,
            detail[:500],
            boundary="agent trajectory" if ambiguous_agent else "high-risk action",
        )
    if last_exc is not None:
        raise last_exc
    ctx.on_node(node, "done", outcome)
    # A node that still failed routes to its on_error handler if it has one, so a
    # flow can catch and recover instead of poisoning `data` and marching on.
    # Never publish a failed connector result under its ordinary output key: it
    # may contain raw auth headers/error bodies and is not valid business data.
    if outcome == 0.0 and node.on_error:
        return node.on_error
    if outcome == 0.0:
        raise RuntimeError(f"work node failed: {_safe_error_text(result)}")
    if node.output:
        data[node.output] = result
    return node.next


def _sleep_retry_backoff(ctx: _Ctx, seconds: float) -> None:
    if seconds <= 0:
        return
    if ctx.deadline is None:
        time.sleep(seconds)
        return
    remaining = ctx.deadline - ctx.now()
    if remaining <= 0:
        raise RuntimeError("flow exceeded its wall-clock deadline during retry backoff")
    time.sleep(min(seconds, remaining))
    if seconds > remaining:
        raise RuntimeError("flow exceeded its wall-clock deadline during retry backoff")


def _run_subflow(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    sub = ctx.resolve_flow(node.flow_ref) if ctx.resolve_flow else None
    if sub is None or sub.id in ctx.flow_stack:   # missing OR would recurse -> failure
        ctx.on_node(node, "error", 0.0)
        if node.on_error is not None:
            return node.on_error
        raise RuntimeError(f"subflow {node.flow_ref!r} is missing or cyclic")
    # Runs on the SAME data dict (threaded), so the sub-flow reads parent data and
    # its outputs flow back. Nested: it cannot pause (v1 -- HITL lives up top). The
    # flow id is on the stack for its duration so a cyclic reference is caught.
    ctx.flow_stack.add(sub.id)
    # Two modes. Default (no subflow_inputs): the child runs on the SHARED data dict
    # -- it reads parent data and its outputs flow back (v1 behaviour). Scoped (with
    # subflow_inputs): the child runs on an ISOLATED dict seeded with ONLY the mapped
    # inputs, so a reusable subflow can't read or clobber unrelated parent keys; its
    # results return solely via `output`. Nested: it cannot pause (HITL lives up top).
    if node.subflow_inputs:
        child = {str(k): (render(v, data) if isinstance(v, str) else v)
                 for k, v in node.subflow_inputs.items()}
        seeded = set(child)
        try:
            _run_chain(sub, child, ctx.nested(), sub.start)
        finally:
            ctx.flow_stack.discard(sub.id)
        if node.output:
            data[node.output] = {k: child[k] for k in child if k not in seeded}
    else:
        before = set(data)
        try:
            _run_chain(sub, data, ctx.nested(), sub.start)
        finally:
            ctx.flow_stack.discard(sub.id)
        # Capture the subflow's return -- the keys it ADDED -- under node.output, so a
        # reusable subflow's result can be referenced by name instead of only leaking
        # into the shared data dict by side effect.
        if node.output:
            data[node.output] = {k: data[k] for k in data if k not in before}
    # Ground success as 1.0 (failure already grounds 0.0 above), so a subflow
    # node has a symmetric success-rate signal the self-rewrite loop can measure.
    ctx.on_node(node, "done", 1.0)
    return node.next


def _run_branch(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    took = eval_condition(node.condition, data)
    ctx.on_node(node, "true" if took else "false", None)
    nxt = node.if_true if took else node.if_false
    return nxt if nxt is not None else node.next


_FOREACH_DEFAULT_CAP = 1000   # runaway guard when a foreach sets no explicit limit


def _run_switch(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    """n-way route: the value under the ``condition`` data key is compared (as
    text) to each case's ``value``; the first match routes to its ``to``. No
    match falls through to ``next`` (the default arm)."""
    subject = _as_text(_dig(data, node.condition))
    for case in node.cases:
        if _as_text(case.get("value")) == subject:
            ctx.on_node(node, f"case:{_as_text(case.get('value'))[:40]}", None)
            to = case.get("to")
            return to if to is not None else node.next
    ctx.on_node(node, "default", None)
    return node.next


def _run_while(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    """Run the body sub-flow while the condition holds, on the SAME data dict
    (so the body can change what the condition sees). Iterations are capped by
    ``limit`` (default 100) as the runaway guard; the body may set ``_break``."""
    cap = node.limit if node.limit and node.limit > 0 else _WHILE_DEFAULT_CAP
    passes = 0
    while passes < cap and eval_condition(node.condition, data):
        _run_chain(node.body, data, ctx.nested(), node.body.start)
        passes += 1
        if data.get("_break"):
            data.pop("_break", None)
            break
    if node.output:
        data[node.output] = passes
    ctx.on_node(node, "done", None)
    return node.next


def _run_wait(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    """Pause the run until an external event resumes it (a webhook delivery,
    an API resume). Nested contexts can't pause, so there it is a no-op step
    (the outer graph is where waiting lives, same as approvals)."""
    if ctx.allow_pause:
        ctx.on_node(node, "waiting", None)
        raise _Pause("event", node, data)
    # Nested (foreach/while/scope body, parallel branch): can't pause. Fail
    # CLOSED like a nested approval rather than silently continuing -- otherwise
    # downstream nodes run before the awaited event's data has arrived.
    ctx.on_node(node, "skipped", None)
    raise _Reject(node, data)


def _run_scope(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    """try/catch: run the body on the shared data; if a body node raises, route
    to ``on_error`` (the catch arm) instead of failing the whole run. Without an
    ``on_error`` the failure propagates -- a try with no catch still fails."""
    try:
        _run_chain(node.body, data, ctx.nested(), node.body.start)
    except _NodeError as e:
        if node.on_error is None:
            raise
        data["_error"] = _safe_error_text(e)  # safe for downstream catch branches
        ctx.on_node(node, "caught", 0.0)
        return node.on_error
    ctx.on_node(node, "done", None)
    return node.next


def _run_foreach(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    items = list(_dig(data, node.items) or [])
    if node.limit and node.limit > 0:
        items = items[:node.limit]          # cap iterations (a runaway-list guard)
    elif len(items) > _FOREACH_DEFAULT_CAP:
        # No explicit limit: a foreach of action nodes has no per-node budget gate
        # (that wraps agents only), so bound the fan-out to a safe default and
        # SURFACE the truncation rather than silently firing 10k+ connector calls.
        items = items[:_FOREACH_DEFAULT_CAP]
        data["_foreach_truncated"] = True
    results = []
    if node.concurrent and len(items) > 1:
        # Concurrent iteration: each item gets an isolated data copy and its own
        # branch context (independent cycle detection), on the same bounded pool
        # the parallel node uses. Results keep ITEM ORDER; ``_break`` doesn't
        # apply (iterations overlap, there is no "early" to stop at).
        import concurrent.futures as _cf
        subs = []
        for item in items:
            sub = dict(data)
            sub[node.var] = item
            subs.append(sub)
        threaded = []
        inline = []
        for sub in subs:
            if ctx.parallel_slots is not None and ctx.parallel_slots.acquire(blocking=False):
                threaded.append(sub)
            else:
                inline.append(sub)
        first_error = None
        if threaded:
            with _cf.ThreadPoolExecutor(max_workers=len(threaded)) as ex:
                futs = [ex.submit(_run_chain_with_parallel_slot, node.body, sub,
                                  ctx.branched(), node.body.start)
                        for sub in threaded]
                for sub in inline:
                    try:
                        _run_chain(node.body, sub, ctx.branched(), node.body.start)
                    except Exception as e:
                        first_error = first_error or e
                for f in futs:
                    try:
                        f.result()
                    except Exception as e:
                        first_error = first_error or e
        else:
            for sub in inline:
                _run_chain(node.body, sub, ctx.branched(), node.body.start)
        if first_error is not None:
            raise first_error
        results = subs
    else:
        for item in items:
            sub = dict(data)
            sub[node.var] = item
            _run_chain(node.body, sub, ctx.nested(), node.body.start)
            results.append(sub)      # per-iteration data snapshot (body outputs included)
            if node.output:
                # Publish partial results after EACH iteration, so a whole-flow
                # deadline or a failure mid-loop keeps the batch's completed work
                # (run_flow returns the data as-of the abort) instead of losing it.
                data[node.output] = list(results)
            if sub.get("_break"):    # the body asked to stop early (sequential only)
                break
    if node.output:
        data[node.output] = results
    ctx.on_node(node, "done", None)
    return node.next


def _run_parallel(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    base = dict(data)                     # frozen pre-parallel snapshot
    branch_outputs = [dict(base) for _ in node.branches]   # each isolated (sees only base)
    if len(node.branches) > 1:
        # Run the branches CONCURRENTLY. Each has an isolated data copy and its
        # own flow_stack (ctx.branched()); the shared world DB serialises writes
        # (WAL + busy_timeout). A branch that raises propagates after the pool
        # joins the rest, so there are no leaked threads.
        import concurrent.futures as _cf
        futs = []
        inline = []
        for br, sub in zip(node.branches, branch_outputs, strict=True):
            if ctx.parallel_slots is not None and ctx.parallel_slots.acquire(blocking=False):
                futs.append((br, sub))
            else:
                inline.append((br, sub))
        first_error = None
        if futs:
            with _cf.ThreadPoolExecutor(max_workers=len(futs)) as ex:
                futs = [ex.submit(_run_chain_with_parallel_slot, br, sub, ctx.branched(), br.start)
                        for br, sub in futs]
                for br, sub in inline:
                    try:
                        _run_chain(br, sub, ctx.branched(), br.start)
                    except Exception as e:
                        first_error = first_error or e
                for f in futs:
                    try:
                        f.result()        # wait for all; re-raise the first error
                    except Exception as e:
                        first_error = first_error or e
        else:
            for br, sub in inline:
                _run_chain(br, sub, ctx.branched(), br.start)
        if first_error is not None:
            raise first_error
    else:
        for br, sub in zip(node.branches, branch_outputs, strict=True):
            _run_chain(br, sub, ctx.nested(), br.start)
    for sub in branch_outputs:            # merge new keys deterministically, in order
        for k, v in sub.items():          # (a later branch wins a key conflict)
            if k not in base:
                data[k] = v
    if node.output:
        data[node.output] = branch_outputs
    ctx.on_node(node, "done", None)
    return node.next


def _approval_outcome(decision: str) -> float | None:
    # A decided approval is the highest-value grounded HUMAN signal: which gates
    # humans keep rejecting (rework them) vs. rubber-stamp (automate them away).
    # A still-pending pause is not yet a decision, so it grounds nothing.
    if decision == "approved":
        return 1.0
    if decision == "rejected":
        return 0.0
    return None


def _run_approval(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    decision = ctx.approve_fn(node, data) if ctx.approve_fn else "pending"
    # Ground the decided verdict (B7): approved=1.0 / rejected=0.0 / pending=None --
    # the highest-value human signal for the self-rewrite loop.
    ctx.on_node(node, decision, _approval_outcome(decision))
    # "approved" -- or any verdict the node explicitly offers (a choice approval:
    # "ship"/"hold"/"escalate") -- continues; the verdict is recorded to `output`
    # so downstream nodes (a branch/switch) can route on it.
    if decision == "approved" or (node.choices and decision in node.choices):
        if node.output:
            data[node.output] = decision
        return node.next
    if decision == "rejected":
        raise _Reject(node, data)
    if ctx.allow_pause:                    # pending + top level -> wait for a human
        # An approval's EXPIRY is `expires_after` (its own field, so it can't be
        # confused with the execution `timeout`). Older flows that encoded it in
        # `timeout` are migrated at LOAD time (FlowNode.from_dict), so every
        # consumer -- this runner, the designer, the sweep -- sees one field.
        expiry = node.expires_after
        resume_at = ctx.now() + expiry if expiry and expiry > 0 else None
        raise _Pause("approval", node, data, resume_at=resume_at)
    raise _Reject(node, data)              # nested can't pause -> treat as not approved


def _run_delay(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    if ctx.allow_pause and node.seconds > 0:
        raise _Pause("delay", node, data, resume_at=ctx.now() + node.seconds)
    return node.next


def _run_setvar(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    """Set flow-data keys from expressions -- the 'compose'/'set variable' step.
    A string value is rendered (so ``{{add(count,1)}}`` computes a loop counter and
    ``{{upper(name)}}`` composes a field); a non-string is stored as-is. This is
    what makes computed state + a while-loop (setvar counter -> branch -> back)
    expressible in the graph."""
    for key, expr in (node.assignments or {}).items():
        data[str(key)] = render(expr, data) if isinstance(expr, str) else expr
    ctx.on_node(node, "done", None)
    return node.next


_HANDLERS = {
    NODE_AGENT: _run_work, NODE_ACTION: _run_work, NODE_BRANCH: _run_branch,
    NODE_SWITCH: _run_switch, NODE_FOREACH: _run_foreach, NODE_WHILE: _run_while,
    NODE_PARALLEL: _run_parallel, NODE_APPROVAL: _run_approval,
    NODE_DELAY: _run_delay, NODE_WAIT: _run_wait, NODE_SCOPE: _run_scope,
    NODE_SUBFLOW: _run_subflow, NODE_SETVAR: _run_setvar,
}


def _run_node(node: FlowNode, data: dict, ctx: _Ctx) -> str | None:
    handler = _HANDLERS.get(node.kind)
    if handler is None:
        raise ValueError(f"unknown node kind {node.kind!r}")
    return handler(node, data, ctx)


def _run_chain(flow: Flow, data: dict, ctx: _Ctx, start: str | None) -> None:
    cursor = start
    seen = 0
    limit = max(1000, len(flow.nodes) * 100)   # cycle backstop
    while cursor is not None:
        if ctx.deadline is not None and ctx.now() > ctx.deadline:
            raise RuntimeError(f"flow {flow.id!r} exceeded its wall-clock deadline")
        node = flow.node(cursor)
        if node is None:
            return
        try:
            cursor = _run_node(node, data, ctx)
        except (_Pause, _Reject, _NodeError, _Indeterminate):
            raise
        except Exception as e:
            raise _NodeError(node.id, e) from e
        seen += 1
        if seen > limit:
            raise RuntimeError(f"flow {flow.id!r} exceeded {limit} steps (cycle?)")


def _run_chain_with_parallel_slot(flow: Flow, data: dict, ctx: _Ctx, start: str | None) -> None:
    try:
        _run_chain(flow, data, ctx, start)
    finally:
        if ctx.parallel_slots is not None:
            ctx.parallel_slots.release()


def run_flow(flow: Flow, *, agent_fn: AgentFn, action_fn: ActionFn,
             data: dict | None = None,
             approve_fn: Callable[[FlowNode, dict], str] | None = None,
             on_node: Callable[[FlowNode, str, float | None], None] | None = None,
             resume: dict | None = None,
             now: Callable[[], float] | None = None,
             resolve_flow: Callable[[str], Flow | None] | None = None) -> FlowRunResult:
    """Run ``flow`` to completion or the next pause.

    ``resume`` continues a paused run: ``{"node_id", "data", "decision"?}`` --
    ``decision`` is the human's verdict on the approval node that paused it
    (``approved`` continues, ``rejected`` ends); a delay resume omits it.
    ``resolve_flow(flow_ref) -> Flow | None`` resolves a ``subflow`` node's target
    (with the caller's cycle/depth guard); ``None`` makes a subflow a no-op error.
    """
    _now = now or time.time
    ctx = _Ctx(agent_fn, action_fn, approve_fn, on_node or (lambda *_: None),
               _now, allow_pause=True, resolve_flow=resolve_flow,
               flow_stack={flow.id},   # so the top flow can't recurse into itself
               deadline=(_now() + flow.max_seconds if flow.max_seconds and flow.max_seconds > 0 else None),
               parallel_slots=threading.BoundedSemaphore(_PARALLEL_MAX_WORKERS))
    if resume:
        data = dict(resume.get("data") or data or {})
        node = flow.node(resume.get("node_id"))
        decision = resume.get("decision")
        # Ground the HUMAN verdict on resume too (B7) -- the approval node's handler
        # isn't re-run (we resume AFTER it), so without this the decision signal
        # is lost on the resume path exactly as it is captured on the auto path.
        if node is not None and decision in ("approved", "rejected"):
            (on_node or (lambda *_: None))(node, decision, _approval_outcome(decision))
        # The paused/failed node must still exist in this (current) flow version.
        # If an edit or rollback removed or replaced it between pause and resume,
        # fail CLOSED -- silently falling back to flow.start would re-execute the
        # whole flow and re-drive every pre-pause side effect.
        if node is None:
            return FlowRunResult(
                status=STATUS_FAILED, data=data, cursor=resume.get("node_id"),
                error=(f"cannot resume: node {resume.get('node_id')!r} no longer "
                       "exists in this flow version"))
        if decision == "rejected":
            return FlowRunResult(status=STATUS_REJECTED, data=data, cursor=node.id)
        # Default continuation: a pause resumes AFTER its node (the approval/
        # delay is done); a failure retry re-enters AT the node ("rerun").
        start = node.id if resume.get("rerun") else node.next
        if resume.get("expired") and node.kind == NODE_APPROVAL:
            # The approval's expiry lapsed (the sweep resumed it with the
            # OUT-OF-BAND ``expired`` flag -- never a decision string a caller
            # could forge). Route to `on_expire` if set (an escalation lane),
            # else fail closed (reject). The lane runs through the chain below.
            if node.on_expire is None:
                return FlowRunResult(status=STATUS_REJECTED, data=data, cursor=node.id)
            if node.output:
                data[node.output] = "expired"
            start = node.on_expire
        elif node.kind == NODE_APPROVAL and not resume.get("rerun"):
            # Resuming an approval gate: accept ONLY the literal "approved" or one
            # of the node's explicitly declared choices. Anything else fails
            # CLOSED (rejected), so a loosened decision string can never wave a
            # run past a human gate. The accepted verdict is recorded to the
            # node's output for downstream routing (matches the inline path).
            if decision == "approved" or (decision and decision in node.choices):
                if node.output:
                    data[node.output] = decision
            else:
                return FlowRunResult(status=STATUS_REJECTED, data=data, cursor=node.id)
    else:
        data = dict(data or {})
        start = flow.start
    try:
        _run_chain(flow, data, ctx, start)
        return FlowRunResult(status=STATUS_COMPLETED, data=data)
    except _Pause as p:
        status = {"approval": STATUS_PAUSED_APPROVAL,
                  "event": STATUS_PAUSED_EVENT}.get(p.kind, STATUS_PAUSED_DELAY)
        return FlowRunResult(status=status, data=p.data, cursor=p.node.id,
                             resume_at=p.resume_at, prompt=p.node.prompt)
    except _Reject as r:
        return FlowRunResult(status=STATUS_REJECTED, data=r.data, cursor=r.node.id)
    except _NodeError as e:  # an executor blew up inside a known node -- keep the
        # node id as the cursor so the run can be retried from right there.
        return FlowRunResult(status=STATUS_FAILED, data=data,
                              error=_safe_error_text(e, limit=2000),
                              cursor=e.node_id)
    except _Indeterminate as e:
        return FlowRunResult(status=STATUS_INDETERMINATE, data=data,
                              error=_safe_error_text(e, limit=2000),
                              cursor=e.node_id)
    except (BudgetExceeded, Halted):
        raise
    except Exception as e:  # a node executor blew up -> surface, don't crash the caller
        return FlowRunResult(
            status=STATUS_FAILED, data=data,
            error=_safe_error_text(e, limit=2000),
        )


__all__ = [
    "run_flow", "render", "eval_condition", "FlowRunResult",
    "STATUS_QUEUED", "STATUS_CLAIMED", "STATUS_RUNNING", "STATUS_RESUMING",
    "STATUS_COMPLETED", "STATUS_PAUSED_APPROVAL", "STATUS_PAUSED_DELAY",
    "STATUS_PAUSED_EVENT", "STATUS_REJECTED", "STATUS_FAILED", "STATUS_INDETERMINATE",
    "PAUSED_STATUSES", "ACTIVE_STATUSES", "QUARANTINED_STATUSES",
]
