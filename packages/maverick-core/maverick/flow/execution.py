"""Run a stored flow with live executors + durable, resumable run state.

This binds the pure interpreter (:mod:`.runner`) to the real system: an ``agent``
node becomes a governed goal, an ``action`` node a tool call, and every work
node's outcome is recorded per-node (:mod:`.node_outcomes`) for the self-rewrite
pass. The run's state is persisted after each pause/finish (:mod:`.store`) so a
flow paused on an approval survives a restart and resumes days later.

The executors are injected (``agent_runner`` / ``action_runner``), so the whole
driver is testable without an LLM or a live connector; :func:`default_agent_runner`
and :func:`default_action_runner` supply the production bindings.
"""
from __future__ import annotations

import contextvars
import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any

from ..tool_results import tool_result_failed
from . import node_outcomes, store
from .ir import Flow, FlowNode
from .runner import (
    PAUSED_STATUSES,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INDETERMINATE,
    STATUS_PAUSED_APPROVAL,
    STATUS_RESUMING,
    STATUS_RUNNING,
    run_flow,
)

TRIGGER_NODE = "__trigger__"  # reserved node id under which a trigger's run-outcome is grounded

_RESUMABLE = PAUSED_STATUSES


class FlowNotResumable(ValueError):
    """A resume was requested for a run that is not paused."""


def _approval_actor_matches(actor: str, assignee: str) -> bool:
    expected = str(assignee or "").strip().casefold()
    if not expected:
        return True
    principal = str(actor or "").strip().casefold()
    aliases = {principal}
    if principal.startswith("user:") and principal[5:]:
        subject = principal[5:]
        aliases.update({subject, f"@{subject}"})
    return expected in aliases

# agent_runner(brief, data) -> (result, outcome: float | None)
# action_runner(tool, params, data) -> (result, outcome: float | None)
AgentRunner = Callable[[str, dict], "tuple[Any, float | None]"]
ActionRunner = Callable[[str, dict, dict], "tuple[Any, float | None]"]


DEFAULT_AGENT_BUDGET = 5.0   # matches default_agent_runner's per-node cap


def _budget_gated(agent_runner: AgentRunner, cap: float, per_call: float, spent: list) -> AgentRunner:
    """Wrap an agent runner with a per-RUN aggregate spend ceiling. Each agent
    node is reserved at its worst-case per-node budget (``per_call``) against a
    shared, lock-guarded total; once the next reservation would exceed ``cap`` the
    node is skipped instead of spawning another goal. This bounds fan-out spend --
    a wide ``parallel`` or a long ``foreach`` can't multiply cost without a
    ceiling. Conservative: it counts each node at its cap, so it never
    under-counts real spend. Thread-safe for concurrent branches.

    A skipped node grounds a ``None`` outcome, NOT ``0.0``: it never ran, so it is
    not a failure -- grounding 0.0 would make a healthy node look flaky (skewing
    self-rewrite proposals), burn its retries against the still-exhausted gate,
    and fire its on_error branch. ``None`` means "no signal," which is correct."""
    import threading
    lock = threading.Lock()

    def run(brief: str, data: dict, **kw) -> tuple:
        with lock:
            if spent[0] + per_call > cap:
                return (f"skipped: flow budget exhausted (${cap:g} cap)", None)
            spent[0] += per_call
        return agent_runner(brief, data, **kw)
    return run


def _spend_tracked(agent_runner: AgentRunner, per_call: float, spent: list) -> AgentRunner:
    """Wrap a runner to accumulate reserved spend (``per_call`` per invocation)
    into ``spent`` so the run's total cost can be persisted -- used when there is
    NO ``max_dollars`` gate (the gate already tracks spend itself)."""
    import threading
    lock = threading.Lock()

    def run(brief: str, data: dict, **kw) -> tuple:
        with lock:
            spent[0] += per_call
        return agent_runner(brief, data, **kw)
    return run


# The node an agent runner is currently serving, set by _call_agent around the
# call and read by default_agent_runner to attribute the goal's tool usage back
# to the node (for harden-tool inference). A ContextVar, not a kwarg, so the
# runner contract stays (brief, data[, wall]) -- no new signature to break, and
# no double-invocation of the stateful budget wrappers on a fallback path. Set +
# read happen synchronously on one thread (even under the foreach pool), so the
# value is always the right node.
_active_node: contextvars.ContextVar[tuple[str, str, str, str]] = contextvars.ContextVar(
    "_flow_active_node", default=("", "", "", ""))


def _runner_accepts_wall(runner: AgentRunner) -> bool:
    """Inspect the runner contract without executing a stateful call twice."""
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        # The production contract accepts ``wall``. For opaque callables, make
        # one contract-compliant invocation and surface any error; never retry a
        # possibly side-effecting call based on its exception type.
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "wall"
            and parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        )
        for parameter in parameters
    )


def _call_agent(runner: AgentRunner, brief: str, data: dict, wall: float,
                flow_id: str = "", node_id: str = "", revision: str = "",
                cohort: str = "") -> tuple:
    """Invoke an agent runner, bounding its wall-clock to a node ``timeout`` when
    set so a timed-out node's underlying goal self-terminates near the point the
    flow gives up waiting -- instead of running on to its default cap, still
    spending budget after the flow moved on. Injected 2-arg test runners that
    don't accept ``wall`` are called plainly. Publishes the current node id on a
    ContextVar so the production runner can attribute its goal's tools to it."""
    token = _active_node.set((flow_id, node_id, revision, cohort))
    try:
        if wall and wall > 0 and _runner_accepts_wall(runner):
            return runner(brief, data, wall=wall)
        return runner(brief, data)
    finally:
        _active_node.reset(token)


def _serialize_runs(fn):
    """Hold one durable run lock across state check, execution, and save.

    Queue delivery is intentionally at-least-once.  Without this boundary two
    workers can both load the same paused approval and both execute the approved
    side effect before either publishes ``completed``.  The second worker now
    observes the first worker's terminal state and receives FlowNotResumable.
    """
    @wraps(fn)
    def guarded(flow: Flow, **kwargs):
        resume_id = kwargs.get("resume_run_id")
        run_id = resume_id or kwargs.get("run_id")
        if not run_id:
            return fn(flow, **kwargs)
        with store.run_lock(str(run_id)):
            if not resume_id:
                current = store.load_run(str(run_id))
                if current is not None and current.status == STATUS_RUNNING:
                    # A previous worker crossed the durable execution boundary
                    # and vanished.  Its last tool may have committed, so an
                    # at-least-once delivery must quarantine rather than replay.
                    current.status = STATUS_INDETERMINATE
                    current.error = (
                        current.error
                        or "fresh run interrupted after execution began; external effects may have committed"
                    )
                    store.save_run(current)
                    return current
                if current is not None and current.status not in {"queued", STATUS_CLAIMED}:
                    # At-least-once queue redelivery after the first attempt
                    # published a terminal/pause state is a no-op.
                    return current
            try:
                return fn(flow, **kwargs)
            except BaseException as exc:
                # If execution escaped after the durable resume claim, the
                # external-effect boundary is unknowable.  Preserve that honest
                # state and never let queue redelivery replay it automatically.
                current = store.load_run(str(run_id))
                if current is not None and current.status in {STATUS_RESUMING, STATUS_RUNNING}:
                    current.status = STATUS_INDETERMINATE
                    phase = "resume" if resume_id else "execution"
                    current.error = f"{phase} interrupted: {type(exc).__name__}: {exc}"
                    store.save_run(current)
                raise
    return guarded


@_serialize_runs
def execute(flow: Flow, *, agent_runner: AgentRunner, action_runner: ActionRunner,  # noqa: C901
            owner: str = "", data: dict | None = None,
            approve_fn: Callable[[FlowNode, dict], str] | None = None,
            run_id: str | None = None,
            resume_run_id: str | None = None, decision: str | None = None,
            decided_by: str = "",
            expired: bool = False,
            from_failure: bool = False,
            inputs: dict | None = None, record_outcomes: bool = True,
            agent_budget_dollars: float = DEFAULT_AGENT_BUDGET, origin: str = "manual",
            now: Callable[[], float] | None = None) -> store.FlowRun:
    """Run ``flow`` (or resume a paused run) with live executors, persisting the
    resulting :class:`~.store.FlowRun`. ``run_id`` pre-assigns a fresh run's id
    (so a caller can hand it back for polling); ``decision`` is the human verdict
    when resuming a run paused on an approval (``approved`` / ``rejected``).
    ``record_outcomes=False`` (a dry run) still traces per-node status for the
    overlay but does NOT ground outcomes -- a mock run's fake successes must not
    pollute the self-rewrite signal."""
    resume = None
    prior = None
    definition_digest = ""
    release_digest = ""
    release_id = ""
    definition_version = 0
    definition_revision = ""
    subflow_digests: dict[str, str | None] = {}
    execution_channel = ""
    execution_user_id = ""
    allowed_suites: list[str] | None = None
    has_pinned_plan = False
    approval_resume = False
    approval_actor = ""
    if resume_run_id:
        prior = store.load_run(resume_run_id)
        if prior is None:
            raise ValueError(f"no such flow run {resume_run_id!r}")
        # Only a paused run may be resumed -- or, with ``from_failure``, a FAILED
        # run whose failing node was recorded (it re-enters AT that node with the
        # data as of the failure, instead of re-running the whole flow). Without
        # this guard, resuming a completed run re-executes the whole flow
        # (duplicate side effects) and resuming a rejected run runs the very node
        # a human rejected -- defeating the approval gate.
        retryable_failure = (from_failure and prior.status == STATUS_FAILED
                             and bool(prior.cursor))
        approval_resume = prior.status == STATUS_PAUSED_APPROVAL
        if prior.status not in _RESUMABLE and not retryable_failure:
            raise FlowNotResumable(
                f"run {resume_run_id!r} is not paused (status {prior.status!r})"
                + ("; it has no failure cursor to retry from"
                   if from_failure and prior.status == STATUS_FAILED else ""))
        # Resume the immutable graph captured at run start.  This makes an
        # approval meaningful: an edit made while the run is paused cannot swap
        # the reviewed downstream action (or a referenced child flow) for a new
        # one.  Runs created before snapshots existed retain legacy behaviour.
        if prior.definition_digest:
            flow = store.load_flow_snapshot(prior.definition_digest)
            definition_digest = prior.definition_digest
            release_digest = prior.release_digest
            release_id = prior.release_id
            definition_version = prior.definition_version
            definition_revision = prior.definition_revision or flow.revision
            subflow_digests = dict(prior.subflow_digests)
            has_pinned_plan = True
        execution_channel = prior.execution_channel
        execution_user_id = prior.execution_user_id
        allowed_suites = (
            list(prior.allowed_suites) if prior.allowed_suites is not None else None
        )
        run_id = prior.run_id
        owner = owner or prior.owner
        # Free-form human input merges into the run data on resume, so an approval
        # can also COLLECT data (a corrected value, a note) -- not just yes/no.
        # The merge is append-only for existing run data: a resume caller must not
        # be able to change values that earlier nodes produced and that a human
        # may have reviewed at the pause. When the paused node declares a `form`,
        # only its declared field names are accepted as the server-side allowlist;
        # form-less nodes keep free-form *new* inputs for compatibility.
        resume_data = dict(prior.data)
        if inputs:
            paused = flow.node(prior.cursor) if prior.cursor else None
            declared = [str(f.get("name")) for f in getattr(paused, "form", None) or []
                        if isinstance(f, dict) and f.get("name")]
            allowed = set(declared) if declared else set(inputs)
            inputs = {k: v for k, v in inputs.items()
                      if k in allowed and k not in resume_data}
            resume_data.update(inputs)
        resume = {"node_id": prior.cursor, "data": resume_data}
        if retryable_failure:
            resume["rerun"] = True
        elif prior.status == STATUS_PAUSED_APPROVAL:
            # `expired` rides OUT OF BAND (the sweep's signal), never through
            # the human-verdict string -- so no caller can forge an expiry.
            if expired:
                resume["expired"] = True
            else:
                verdict = str(decision or "").strip()
                paused = flow.node(prior.cursor) if prior.cursor else None
                allowed_verdicts = {"approved", "rejected"}
                if paused is not None:
                    allowed_verdicts.update(str(v) for v in paused.choices)
                if not verdict or verdict not in allowed_verdicts:
                    raise ValueError("an explicit valid approval decision is required")
                approval_actor = str(decided_by or "").strip()
                if not approval_actor:
                    raise ValueError("an attributable approver identity is required")
                assignee = str(
                    (prior.human or {}).get("assignee")
                    or (paused.assignee if paused is not None else "")
                    or ""
                )
                if not _approval_actor_matches(approval_actor, assignee):
                    raise ValueError("the approver is not the declared assignee")
                resume["decision"] = verdict
    rid = run_id or store.new_run_id()
    # A fresh run may have a queued placeholder (saved at enqueue with the
    # idempotency key + original inputs) -- preserve those instead of resetting.
    placeholder = store.load_run(rid) if (run_id and not resume_run_id) else None
    if placeholder is not None and placeholder.definition_digest:
        flow = store.load_flow_snapshot(placeholder.definition_digest)
        definition_digest = placeholder.definition_digest
        release_digest = placeholder.release_digest
        release_id = placeholder.release_id
        definition_version = placeholder.definition_version
        definition_revision = placeholder.definition_revision or flow.revision
        subflow_digests = dict(placeholder.subflow_digests)
        has_pinned_plan = True
    if placeholder is not None:
        execution_channel = placeholder.execution_channel
        execution_user_id = placeholder.execution_user_id
        allowed_suites = (
            list(placeholder.allowed_suites)
            if placeholder.allowed_suites is not None else None
        )
    if has_pinned_plan:
        # Durable runs created before owner-bound snapshots were introduced may
        # already contain a crafted cross-user child manifest.  Revalidate the
        # immutable objects before any node executor is reached.
        expected_flow_id = (
            prior.flow_id
            if prior is not None
            else placeholder.flow_id
            if placeholder is not None
            else flow.id
        )
        if flow.id != expected_flow_id:
            raise store.FlowSnapshotError(
                "flow snapshot identity does not match the durable run"
            )
        store.validate_snapshot_bundle_owners(flow, subflow_digests)
    plan_errors = flow.validate()
    if plan_errors:
        raise ValueError(
            "invalid flow execution plan: " + "; ".join(plan_errors))
    if not has_pinned_plan:
        definition_digest, definition_version, subflow_digests = (
            store.snapshot_flow_bundle(flow))
        release_digest = store.release_digest_for(
            definition_digest, subflow_digests,
        )
        release_id = release_digest
        definition_revision = flow.revision
    evidence_cohort = store.definition_cohort(
        definition_revision or flow.revision,
        definition_version or flow.version,
    )
    # Per-node run trace for the designer overlay. Seeded from the prior run on
    # resume so the pre-pause nodes' status survives across the pause.
    node_states: dict = dict(prior.nodes) if (resume_run_id and prior) else {}
    # Wall-clock per work node (accumulated across retries), measured at the
    # executor seam so the pure runner's on_node contract stays unchanged.
    durations: dict[str, float] = {}
    # A concurrent foreach runs the SAME body node ids on several threads through
    # this shared sink; guard the trace mutations so the duration read-modify-
    # write can't lose updates.
    import threading as _threading
    trace_lock = _threading.Lock()

    def _timed(node_id: str, fn, *args):
        import time as _time
        t0 = _time.perf_counter()
        try:
            return fn(*args)
        finally:
            dt = _time.perf_counter() - t0
            with trace_lock:
                durations[node_id] = durations.get(node_id, 0.0) + dt

    def _on_node(node: FlowNode, status: str, outcome: float | None) -> None:
        with trace_lock:
            entry: dict = {"status": status, "outcome": outcome}
            if node.id in durations:
                entry["seconds"] = round(durations[node.id], 3)
            node_states[node.id] = entry
        if outcome is not None and record_outcomes:
            node_outcomes.record(
                flow.id,
                node.id,
                node.kind,
                outcome,
                revision=definition_revision or flow.revision,
                cohort=evidence_cohort,
            )

    # Origin (what fired this run) comes from the queued placeholder when present,
    # else the caller's argument -- carried across a resume from the prior run.
    run_origin = (prior.origin if (resume_run_id and prior)
                  else (placeholder.origin if placeholder else origin))
    # Track reserved agent spend for the run. Seed resumed runs from the durable
    # cost already reserved before the pause so flow.max_dollars remains an
    # aggregate per-run cap across approval/delay resume boundaries. The
    # max_dollars gate updates this list too; without a gate, wrap a plain spend
    # tracker so cost is still recorded.
    spent = [float(prior.cost_dollars) if (resume_run_id and prior) else 0.0]
    if record_outcomes:
        if flow.max_dollars and flow.max_dollars > 0:
            # Aggregate per-run spend ceiling: a wide parallel / long foreach can't
            # fan out unbounded cost (per-node budget alone doesn't bound the COUNT).
            agent_runner = _budget_gated(agent_runner, flow.max_dollars, agent_budget_dollars, spent)
        else:
            agent_runner = _spend_tracked(agent_runner, agent_budget_dollars, spent)

    # Write a "running" state before executing so a long run is distinguishable
    # from a stuck "queued" one on the dashboard (a fresh, non-resume run only).
    if not resume_run_id:
        store.save_run(store.FlowRun(
            run_id=rid, flow_id=flow.id, status=STATUS_RUNNING, owner=owner,
            data=dict(data or {}), origin=run_origin,
            created=(placeholder.created if placeholder else 0.0),
            input_data=(placeholder.input_data if placeholder else dict(data or {})),
            idem_key=(placeholder.idem_key if placeholder else ""),
            dry_run=(placeholder.dry_run if placeholder else not record_outcomes),
            definition_digest=definition_digest,
            release_digest=release_digest,
            release_id=release_id,
            definition_version=definition_version,
            definition_revision=definition_revision,
            subflow_digests=subflow_digests,
            execution_channel=execution_channel,
            execution_user_id=execution_user_id,
            allowed_suites=allowed_suites,
        ))

    def _resolve_pinned_flow(flow_ref: str) -> Flow | None:
        if flow_ref in subflow_digests:
            digest = subflow_digests[flow_ref]
            return store.load_flow_snapshot(digest) if digest else None
        # Backward compatibility for a run persisted before subflow snapshots
        # existed.  New runs never reach this path for a resolvable reference.
        return store.load_flow(flow_ref) if (resume_run_id and not has_pinned_plan) else None

    if resume_run_id and prior is not None:
        # First-writer-wins claim BEFORE any downstream executor is invoked.  A
        # crash after this point leaves an indeterminate run, never the old
        # paused row that another delivery could replay.
        prior.status = STATUS_RESUMING
        if approval_resume and not expired:
            prior.decided_by = approval_actor
        store.save_run(prior)

    res = run_flow(
        flow,
        agent_fn=lambda node, brief, d: _timed(node.id, lambda: _call_agent(
            agent_runner,
            brief,
            d,
            node.timeout,
            flow_id=flow.id,
            node_id=node.id,
            revision=definition_revision or flow.revision,
            cohort=evidence_cohort,
        )),
        action_fn=lambda node, params, d: _timed(node.id, lambda: action_runner(node.tool, params, d)),
        approve_fn=approve_fn, data=data, resume=resume, on_node=_on_node, now=now,
        # A subflow node resolves to another SAVED flow; the runner's flow_stack
        # guards against a cyclic reference (this loader stays a plain lookup).
        resolve_flow=_resolve_pinned_flow,
    )
    # Ground the TRIGGER's run-outcome (which triggers produce valuable runs vs.
    # noise) when a triggered run reaches a terminal state -- the top-of-funnel
    # signal, kept under a reserved node id so it's separable from work nodes.
    if record_outcomes and run_origin != "manual" and res.status in (STATUS_COMPLETED, STATUS_FAILED):
        node_outcomes.record(
            flow.id,
            TRIGGER_NODE,
            "trigger",
            1.0 if res.status == STATUS_COMPLETED else 0.0,
            revision=definition_revision or flow.revision,
            cohort=evidence_cohort,
        )

    # Snapshot the paused human task's declared UI (choices/form/assignee) onto
    # the run, like `prompt` -- the viewer must render what the node said AT
    # PAUSE time, not whatever the flow definition says by the time a human looks.
    human: dict = {}
    if res.status == STATUS_PAUSED_APPROVAL and res.cursor:
        paused_node = flow.node(res.cursor)
        if paused_node is not None:
            human = {"choices": list(paused_node.choices),
                     "assignee": paused_node.assignee,
                     "form": [dict(f) for f in paused_node.form]}
    run = store.FlowRun(
        run_id=rid, flow_id=flow.id, status=res.status, data=res.data,
        cursor=res.cursor, resume_at=res.resume_at, prompt=res.prompt,
        error=res.error, owner=owner, origin=run_origin, cost_dollars=round(spent[0], 4),
        human=human,
        decided_by=(
            approval_actor
            if resume_run_id and prior is not None and approval_resume and not expired
            else (prior.decided_by if prior is not None else "")
        ),
        created=(prior.created if resume_run_id and prior
                 else (placeholder.created if placeholder else 0.0)),
        nodes=node_states,
        # Keep the original trigger payload (from the queued placeholder or this
        # fresh data) / carry it across a resume, so a failed run can be retried
        # from scratch with the same inputs; carry the idempotency key too.
        input_data=(prior.input_data if (resume_run_id and prior)
                    else (placeholder.input_data if placeholder else dict(data or {}))),
        idem_key=(prior.idem_key if (resume_run_id and prior)
                  else (placeholder.idem_key if placeholder else "")),
        dry_run=(prior.dry_run if (resume_run_id and prior)
                 else (placeholder.dry_run if placeholder else not record_outcomes)),
        definition_digest=definition_digest,
        release_digest=release_digest,
        release_id=release_id,
        definition_version=definition_version,
        definition_revision=definition_revision,
        subflow_digests=subflow_digests,
        execution_channel=execution_channel,
        execution_user_id=execution_user_id,
        allowed_suites=allowed_suites,
    )
    store.save_run(run)
    # Learn the shape of this flow's output keys from a REAL run's data (never a
    # dry run -- mock executors produce placeholder shapes), so the designer's
    # data pills can offer nested keys. Field names + types only; best-effort.
    if record_outcomes and res.status != STATUS_INDETERMINATE:
        from .schema_infer import infer_output_shapes
        # record_flow_schema owns the best-effort swallow.
        store.record_flow_schema(
            flow.id,
            infer_output_shapes(flow, res.data),
            revision=definition_revision or flow.revision,
        )
    return run


def default_agent_runner(
    world,
    *,
    owner: str = "",
    budget_dollars: float = 5.0,
    budget_wall_seconds: float = 3600.0,
    channel: str | None = None,
    user_id: str | None = None,
    allowed_suites: frozenset[str] | None = None,
    concurrency_principal: str | None = None,
) -> AgentRunner:
    """Production ``agent_runner``: an agent node becomes a governed goal.

    Creates a goal from the (rendered) brief, runs it through the durable runner,
    and grounds a terminal outcome (done=1.0 / not=0.0) as the node's signal.
    Best-effort: a runner failure yields a failure result + a 0.0 outcome rather
    than raising into the flow."""
    def run(brief: str, data: dict, wall: float | None = None) -> tuple[Any, float | None]:
        gid = world.create_goal(str(brief)[:200], str(brief)[:8000], owner=owner)
        # A per-node timeout bounds this goal's wall so a timed-out node's goal
        # self-terminates instead of running to the default cap after the flow
        # gave up on it (kernel rule 3 still holds -- this only ever lowers it).
        ws = min(budget_wall_seconds, wall) if (wall and wall > 0) else budget_wall_seconds
        try:
            from ..runner import run_goal_in_thread
            result = run_goal_in_thread(
                gid,
                max_dollars=budget_dollars,
                max_wall_seconds=ws,
                channel=channel,
                user_id=user_id,
                allowed_suites=allowed_suites,
                concurrency_principal=(concurrency_principal or owner or user_id),
            )
        except Exception as e:  # pragma: no cover -- runner wiring varies by env
            return (f"agent step failed: {type(e).__name__}: {e}", 0.0)
        g = world.get_goal(gid)
        outcome = 1.0 if (g and g.status == "done") else 0.0
        # Attribute this goal's tool usage back to the node (from the ContextVar
        # _call_agent published), so the self-rewrite pass can infer which tool a
        # reliably-one-tool agent node should harden into. Only on SUCCESS: a node
        # with retries invokes this runner once PER ATTEMPT, so recording every
        # attempt would let one retried run masquerade as many "runs" and inflate
        # the inference support -- and a failed attempt's tools are noise anyway.
        # A reliable node (the only harden candidate) succeeds ~once per run, so
        # this yields ~one tool record per logical run, aligned with node_outcomes.
        fid, nid, revision, cohort = _active_node.get()
        if fid and nid and outcome == 1.0:
            _capture_node_tools(
                fid, nid, gid, revision=revision, cohort=cohort,
            )
        return (result if result is not None else (g.result if g else ""), outcome)
    return run


def _capture_node_tools(
    flow_id: str, node_id: str, goal_id: int, *, revision: str = "",
    cohort: str = "",
) -> None:
    """Record the distinct tools an agent node's goal used (for harden inference).
    No-op when trajectory capture is off (nothing to read); never raises."""
    try:
        from .. import trajectory_store
        if not trajectory_store.enabled():
            return
        tools = trajectory_store.tools_for_goal(goal_id)
        from . import node_tools
        node_tools.record(
            flow_id, node_id, tools, revision=revision, cohort=cohort,
        )
    except Exception:  # pragma: no cover -- capture must never break a run
        pass


def default_action_runner(
    world,
    sandbox=None,
    *,
    channel: str | None = None,
    user_id: str | None = None,
) -> ActionRunner:
    """Production ``action_runner``: resolve the named tool from the base registry
    and call it with the rendered params. Outcome is success (1.0) unless the tool
    returns a failed, refused, or indeterminate result (0.0). Never raises into
    the flow. High-risk nodes turn that 0.0 into the runner's indeterminate
    quarantine rather than an automatic retry."""
    def run(tool: str, params: dict, data: dict) -> tuple[Any, float | None]:
        try:
            # One authorization gate for every non-agent dispatch. This used to
            # be action_tool_policy_error alone -- no shield scan, no governance
            # policy, and no audit row -- on a path that is live (bound at
            # automation_queue) and reaches every registered connector including
            # stripe, gmail, s3, salesforce, sap and workday.
            #
            # The missing audit row was the worst of it: the attestation's
            # policy_envelope claims "no RECORDED action violated the envelope",
            # so an unrecorded dispatch satisfied it trivially and made the
            # claim unfalsifiable rather than merely incomplete.
            from ..tool_authz import authorize

            if denial := authorize(tool, params, origin="flow",
                                   principal=user_id):
                return (denial, 0.0)
            from ..sandbox import LocalBackend
            from ..tools import base_registry
            reg = base_registry(
                world,
                sandbox or LocalBackend(),
                goal_id=None,
                channel=channel,
                user_id=user_id,
            )
            t = next((x for x in reg.all() if x.name == tool), None)
            if t is None:
                return (f"ERROR: no such tool {tool!r}", 0.0)
            # Go through the registry's authoritative dispatch path: it owns the
            # ToolFn(args_dict) contract, sync/async/generator normalization,
            # tracing, cache invalidation and retry-safety policy.
            from ..workflow import _drive
            result = _drive(reg.run(tool, params))
        except Exception as e:
            from ..budget import BudgetExceeded
            from ..killswitch import Halted
            if isinstance(e, (BudgetExceeded, Halted)):
                raise
            return (f"ERROR: {type(e).__name__}: {e}", 0.0)
        ok = not tool_result_failed(result)
        return (result, 1.0 if ok else 0.0)
    return run


def sandbox_runners() -> tuple[AgentRunner, ActionRunner]:
    """Mock executors for a DRY RUN: they exercise the graph (routing, data
    threading, {{ }} rendering) without creating a real goal or calling a real
    connector, so a user can safely test a flow with sample input before wiring
    it to a live trigger. Each returns a labeled placeholder + a success outcome."""
    def agent(brief: str, data: dict) -> tuple[Any, float | None]:
        return (f"[dry-run agent] {str(brief)[:120]}", 1.0)

    def action(tool: str, params: dict, data: dict) -> tuple[Any, float | None]:
        return ({"dry_run": True, "tool": tool, "params": params}, 1.0)
    return agent, action


__all__ = [
    "execute", "default_agent_runner", "default_action_runner", "sandbox_runners",
    "STATUS_RUNNING", "STATUS_RESUMING", "STATUS_INDETERMINATE",
]
