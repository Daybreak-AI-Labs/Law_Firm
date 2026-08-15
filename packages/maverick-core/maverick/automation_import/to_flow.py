"""Lower an imported automation to a :class:`~maverick.flow.ir.Flow`.

The prose ``render()`` collapses a workflow into one agentic goal; this instead
preserves each step as its own typed node, chained in order:

* a step that maps cleanly to a single tool becomes a deterministic ``action``
  node (auditable and replayable) with its params carried through; high-risk
  actions are additionally protected by an explicit human approval gate;
* every other step becomes an ``agent`` node whose brief is the step's
  instruction (the "figure it out" node).

That is the "do both" migration: the graph keeps the workflow's shape while any
step can still be a full agentic goal. A one-step automation lowers to a single
``agent`` node -- i.e. exactly what ``render()`` + a template already produce, so
this is a superset, not a replacement.

Source-graph branch/loop capture, per importer:

* n8n ``IF``/``Filter`` -> ``branch`` (arms wired);
* Power Automate (WDL) ``If`` -> ``branch``, ``Foreach`` -> ``foreach``,
  ``Switch`` -> ``switch``, ``Until`` -> ``while`` (negated exit), ``Scope`` ->
  ``scope`` (bodies wired);
* Workato ``if`` -> ``branch``, ``foreach``/``repeat`` -> ``foreach``;
* Make ``BasicRouter`` routes -> ``parallel`` fan-out branches, with each
  filtered route guarded by its own ``branch`` so all matching routes can run.

Everything else lowers to an ordered chain. A UiPath import is a single process
*invocation* (the Orchestrator API returns no .xaml), so its internal control
flow can't be captured -- the report marks it ``approximated`` rather than
pretending the process's branches survived. :func:`to_flow_with_report` says
exactly what happened per step, so an import never silently pretends to be
lossless.
"""
from __future__ import annotations

import re

from ..flow.ir import (
    NODE_ACTION,
    NODE_AGENT,
    NODE_APPROVAL,
    NODE_BRANCH,
    NODE_FOREACH,
    NODE_PARALLEL,
    NODE_SCOPE,
    NODE_SWITCH,
    NODE_WHILE,
    Flow,
    FlowNode,
    ensure_high_risk_approvals,
    partition_draft_validation_errors,
)
from .ir import ImportedAutomation, ImportedStep, _safe_line, safe_params


def _has_blocking_draft_errors(
    flow: Flow, *, inherited_approval: bool = False,
) -> bool:
    errors = flow.validate(_inherited_approval=inherited_approval)
    blocking, _ = partition_draft_validation_errors(errors)
    return bool(blocking)


def _append_classification_notes(flow: Flow, report: list[dict]) -> None:
    _, unclassified = partition_draft_validation_errors(flow.validate())
    for error in unclassified:
        report.append({
            "step": "tool classification required",
            "node": "",
            "kind": NODE_ACTION,
            "fidelity": "preserved",
            "note": (
                "preview only; install and risk-classify before save or "
                f"execution: {error}"
            ),
        })


def _govern_imported(flow: Flow, report: list[dict] | None = None) -> Flow:
    """Apply the mandatory generated-graph safety boundary.

    Imported connector metadata (including a static ``confirm`` parameter) is
    not human authorization. Insert durable top-level approval gates before
    high-risk work, including a coarse gate before containers whose nested
    flows cannot safely pause on their own.
    """
    prior_ids = set(flow.nodes)
    governed = ensure_high_risk_approvals(flow)
    if report is not None:
        for node in governed.nodes.values():
            if node.id in prior_ids or node.kind != NODE_APPROVAL:
                continue
            report.append({
                "step": node.label or node.id,
                "node": node.id,
                "kind": NODE_APPROVAL,
                "fidelity": "approximated",
                "note": "human approval gate inserted before high-risk imported work",
            })
        _append_classification_notes(governed, report)
    return governed


def _node_for_step(step: ImportedStep, node_id: str, nxt: str | None) -> FlowNode:
    tool = step.tools_hint[0] if len(step.tools_hint) == 1 else ""
    label = step.name or step.operation or tool or "step"
    if tool and step.operation:
        # A clean single-tool mapping -> a deterministic action node. Params are
        # the (secret-redacted) step inputs, so they aren't dumped raw.
        params = safe_params(step.params)
        # External platforms identify Slack with the provider slug ``slack``;
        # Lightwork's installed, risk-classified direct-action contract is
        # ``slack_bot``.  Bind only the operation whose semantics are exact.
        # Unknown Slack operations deliberately remain unclassified previews
        # instead of being guessed into an executable tool call.
        slack_op = re.sub(r"[^a-z]", "", str(step.operation).lower())
        if tool == "slack" and slack_op in {
            "post", "postmessage", "createmessage", "sendmessage",
        }:
            tool = "slack_bot"
            params.pop("operation", None)
            params["op"] = "post"
        return FlowNode(id=node_id, kind=NODE_ACTION, tool=tool,
                        params=params, next=nxt,
                        output=f"{node_id}_out", label=label)
    brief_bits = [step.name or step.operation or "Carry out this step"]
    if step.description and step.description != step.name:
        brief_bits.append(step.description)
    if step.app:
        brief_bits.append(f"(using {step.app})")
    return FlowNode(id=node_id, kind=NODE_AGENT, brief=" ".join(brief_bits).strip(),
                    next=nxt, output=f"{node_id}_out", label=label)


# n8n operator names -> the flow condition-evaluator's operators.
_N8N_OPS = {"gt": ">", "larger": ">", "gte": ">=", "largerEqual": ">=",
            "lt": "<", "smaller": "<", "lte": "<=", "smallerEqual": "<=",
            "equal": "==", "equals": "==", "notEqual": "!=", "notEquals": "!=",
            "contains": "contains"}


def _n8n_condition(node: dict) -> str:
    """Best-effort condition text from an n8n IF node (v1 or v2 param shapes),
    mapped to the flow evaluator's ``<key> <op> <value>`` syntax. Structure is
    what we preserve; the operator refines the exact test in the designer."""
    params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    conds = params.get("conditions")
    if isinstance(conds, dict):
        # v2: {"conditions": {"conditions": [{"leftValue","operator","rightValue"}]}}
        inner = conds.get("conditions")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            c = inner[0]
            op = c.get("operator")
            raw_op = op.get("operation") if isinstance(op, dict) else op
            left = _safe_line(c.get("leftValue", ""), max_chars=40)
            right = _safe_line(c.get("rightValue", ""), max_chars=40)
            return f"{left} {_N8N_OPS.get(str(raw_op), '==')} {right}".strip()
        # v1: {"conditions": {"number":[{value1,operation,value2}], "string":[...]}}
        for arr in conds.values():
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                c = arr[0]
                left = _safe_line(c.get("value1", ""), max_chars=40)
                right = _safe_line(c.get("value2", ""), max_chars=40)
                return f"{left} {_N8N_OPS.get(str(c.get('operation')), '==')} {right}".strip()
    return "result == true"


def _n8n_targets(connections: dict, node_name: str, idmap: dict):
    """(true-target, false-target) flow ids for an n8n node's first two output
    groups (IF: group 0 = true, group 1 = false)."""
    groups = (connections.get(node_name, {}) or {}).get("main", []) or []

    def first(gi):
        if gi < len(groups):
            for edge in groups[gi] or []:
                tgt = edge.get("node") if isinstance(edge, dict) else None
                if tgt in idmap:
                    return idmap[tgt]
        return None
    return first(0), first(1)


def _n8n_to_flow(automation: ImportedAutomation) -> Flow | None:
    """Lower an n8n workflow's raw graph to a Flow, capturing IF/Filter nodes as
    real ``branch`` nodes (true/false wired to their downstream nodes) instead of
    flattening them. Returns ``None`` if the raw graph isn't usable (caller falls
    back to the linear lowering)."""
    from .n8n import _is_trigger_node, _short_type, _step_from_node
    raw = automation.raw if isinstance(automation.raw, dict) else {}
    nodes = [n for n in (raw.get("nodes") or []) if isinstance(n, dict) and n.get("name")]
    connections = raw.get("connections") if isinstance(raw.get("connections"), dict) else {}
    if not nodes or not connections:
        return None
    by_name = {n["name"]: n for n in nodes}
    action_names = [nm for nm, n in by_name.items() if not _is_trigger_node(n)]
    if not action_names:
        return None
    idmap = {nm: f"n{i}" for i, nm in enumerate(action_names)}
    fnodes: dict[str, FlowNode] = {}
    for nm in action_names:
        node, nid = by_name[nm], idmap[nm]
        t_tgt, f_tgt = _n8n_targets(connections, nm, idmap)
        if _short_type(node.get("type", "")) in ("if", "filter"):
            fnodes[nid] = FlowNode(id=nid, kind=NODE_BRANCH, condition=_n8n_condition(node),
                                   if_true=t_tgt, if_false=f_tgt, label=nm)
        else:
            fnodes[nid] = _node_for_step(_step_from_node(node), nid, t_tgt)
    trigger_node = next((n for n in nodes if _is_trigger_node(n)), None)
    start = None
    if trigger_node:
        start = _n8n_targets(connections, trigger_node["name"], idmap)[0]
    start = start or idmap[action_names[0]]
    flow = Flow(id=automation.template_name(), name=automation.name, start=start, nodes=fnodes)
    flow = _govern_imported(flow)
    return flow if not _has_blocking_draft_errors(flow) else None


# ---- Power Automate (WDL) graph capture --------------------------------------

# WDL comparison functions -> the flow condition-evaluator's operators.
_WDL_OPS = {"equals": "==", "notEquals": "!=", "greater": ">", "greaterOrEquals": ">=",
            "less": "<", "lessOrEquals": "<=", "contains": "contains"}


def _pa_operand(v) -> str:
    """A WDL operand (`@triggerBody()?['status']`, `@outputs('X')?['body/id']`,
    a literal) reduced to a flow-data key / literal the condition grammar takes."""
    s = str(v).strip().strip("'\"")
    fields = re.findall(r"\['([^']+)'\]", s)
    if fields:
        return _safe_line(fields[-1].rsplit("/", 1)[-1], max_chars=40)
    s = s.removeprefix("@")
    return _safe_line(s, max_chars=40)


def _pa_condition(expr) -> str:
    """Best-effort ``<key> <op> <value>`` from a WDL If expression (dict or
    ``@fn(a, b)`` string form). Structure is what we preserve; the operator text
    refines the exact test in the designer."""
    if isinstance(expr, dict):
        if "not" in expr and isinstance(expr["not"], dict):
            inner = expr["not"]
            for k, args in inner.items():
                if k in _WDL_OPS and isinstance(args, list) and len(args) >= 2:
                    op = "!=" if _WDL_OPS[k] == "==" else _WDL_OPS[k]
                    return f"{_pa_operand(args[0])} {op} {_pa_operand(args[1])}"
        for k in ("and", "or"):
            arms = expr.get(k)
            if isinstance(arms, list) and arms and isinstance(arms[0], dict):
                return _pa_condition(arms[0])   # first clause carries the shape
        for k, args in expr.items():
            if k in _WDL_OPS and isinstance(args, list) and len(args) >= 2:
                return f"{_pa_operand(args[0])} {_WDL_OPS[k]} {_pa_operand(args[1])}"
    if isinstance(expr, str):
        m = re.match(r"@?(\w+)\((.+)\)\s*$", expr.strip())
        if m and m.group(1) in _WDL_OPS and "," in m.group(2):
            left, right = m.group(2).split(",", 1)
            return f"{_pa_operand(left)} {_WDL_OPS[m.group(1)]} {_pa_operand(right)}"
    return "result == true"


_NEGATED_OPS = {"==": "!=", "!=": "==", ">": "<=", "<=": ">", "<": ">=", ">=": "<"}


def _negate_condition(cond: str) -> str:
    """Invert a simple ``<key> <op> <value>`` condition (WDL Until exits when its
    expression is TRUE; our while loops WHILE the condition is true)."""
    parts = cond.split(" ", 2)
    if len(parts) == 3 and parts[1] in _NEGATED_OPS:
        return f"{parts[0]} {_NEGATED_OPS[parts[1]]} {parts[2]}"
    return "result != true"     # un-negatable shape -> loop until result is set true


def _pa_wire_tail(nodes: dict[str, FlowNode], nxt: str | None) -> None:
    """Point an arm's loose ends at the If's successor, so both arms rejoin the
    chain after the branch (WDL's implicit join)."""
    if not nxt:
        return
    for n in nodes.values():
        if n.next is None:
            n.next = nxt


def _pa_lower(actions: dict, prefix: str,
              report: list[dict]) -> tuple[dict[str, FlowNode], str | None]:
    """Lower one WDL action dict (one nesting level) into chained flow nodes.
    Returns ``(nodes, start_id)``. ``If`` arms live in the SAME node dict (wired
    to rejoin after the branch); a ``Foreach`` body becomes a nested Flow."""
    from .power_automate import _step, _toposort_actions
    order = [n for n in _toposort_actions(actions) if isinstance(actions.get(n), dict)]
    ids = {name: f"{prefix}{i}" for i, name in enumerate(order)}
    nodes: dict[str, FlowNode] = {}
    for i, name in enumerate(order):
        spec = actions[name]
        nid = ids[name]
        nxt = ids[order[i + 1]] if i + 1 < len(order) else None
        atype = str(spec.get("type") or "")
        label = name.replace("_", " ").strip()
        if atype == "If":
            then_d = spec.get("actions") if isinstance(spec.get("actions"), dict) else {}
            else_env = spec.get("else") if isinstance(spec.get("else"), dict) else {}
            else_d = else_env.get("actions") if isinstance(else_env.get("actions"), dict) else {}
            t_nodes, t_start = _pa_lower(then_d, nid + "t", report)
            f_nodes, f_start = _pa_lower(else_d, nid + "f", report)
            _pa_wire_tail(t_nodes, nxt)
            _pa_wire_tail(f_nodes, nxt)
            nodes.update(t_nodes)
            nodes.update(f_nodes)
            nodes[nid] = FlowNode(id=nid, kind=NODE_BRANCH, condition=_pa_condition(spec.get("expression")),
                                  if_true=t_start or nxt, if_false=f_start or nxt,
                                  next=nxt, label=label)
            report.append({"step": name, "node": nid, "kind": NODE_BRANCH,
                           "fidelity": "preserved", "note": "WDL If -> branch (both arms wired)"})
        elif atype == "Foreach":
            body_d = spec.get("actions") if isinstance(spec.get("actions"), dict) else {}
            b_nodes, b_start = _pa_lower(body_d, "b", report)
            body = Flow(id="", name=f"{label} body", start=b_start or "",
                        nodes=b_nodes) if b_nodes else None
            items = _pa_operand(spec.get("foreach") or "") or "items"
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_FOREACH, items=items,
                                      body=body, next=nxt, output=f"{nid}_out", label=label)
                report.append({"step": name, "node": nid, "kind": NODE_FOREACH,
                               "fidelity": "preserved", "note": "WDL Foreach -> foreach with a nested body"})
            else:   # a loop with no readable body -> an agent step that does the loop's intent
                nodes[nid] = _node_for_step(_step(name, spec), nid, nxt)
                report.append({"step": name, "node": nid, "kind": nodes[nid].kind,
                               "fidelity": "approximated", "note": "Foreach body unreadable -> agent step"})
        elif atype == "Switch":
            cases_d = spec.get("cases") if isinstance(spec.get("cases"), dict) else {}
            default_env = spec.get("default") if isinstance(spec.get("default"), dict) else {}
            default_d = default_env.get("actions") if isinstance(default_env.get("actions"), dict) else {}
            case_entries = []
            for ci, (cname, cspec) in enumerate(cases_d.items()):
                cspec = cspec if isinstance(cspec, dict) else {}
                arm_d = cspec.get("actions") if isinstance(cspec.get("actions"), dict) else {}
                a_nodes, a_start = _pa_lower(arm_d, f"{nid}c{ci}", report)
                _pa_wire_tail(a_nodes, nxt)
                nodes.update(a_nodes)
                case_entries.append({"value": _safe_line(cspec.get("case", cname), max_chars=60),
                                     "to": a_start or nxt})
            d_nodes, d_start = _pa_lower(default_d, nid + "d", report)
            _pa_wire_tail(d_nodes, nxt)
            nodes.update(d_nodes)
            if case_entries:
                nodes[nid] = FlowNode(id=nid, kind=NODE_SWITCH,
                                      condition=_pa_operand(spec.get("expression") or ""),
                                      cases=case_entries, next=d_start or nxt, label=label)
                report.append({"step": name, "node": nid, "kind": NODE_SWITCH,
                               "fidelity": "preserved",
                               "note": "WDL Switch -> switch (cases + default wired)"})
            else:   # no readable cases -> an agent step with the switch's intent
                nodes[nid] = _node_for_step(_step(name, spec), nid, nxt)
                report.append({"step": name, "node": nid, "kind": nodes[nid].kind,
                               "fidelity": "approximated", "note": "Switch cases unreadable -> agent step"})
        elif atype == "Until":
            body_d = spec.get("actions") if isinstance(spec.get("actions"), dict) else {}
            b_nodes, b_start = _pa_lower(body_d, "b", report)
            body = Flow(id="", name=f"{label} body", start=b_start or "",
                        nodes=b_nodes) if b_nodes else None
            limit_env = spec.get("limit") if isinstance(spec.get("limit"), dict) else {}
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_WHILE,
                                      condition=_negate_condition(_pa_condition(spec.get("expression"))),
                                      body=body, limit=int(limit_env.get("count") or 0),
                                      next=nxt, output=f"{nid}_out", label=label)
                report.append({"step": name, "node": nid, "kind": NODE_WHILE,
                               "fidelity": "preserved",
                               "note": "WDL Until -> while (exit condition negated; "
                                       "checks before each pass, not after)"})
            else:
                nodes[nid] = _node_for_step(_step(name, spec), nid, nxt)
                report.append({"step": name, "node": nid, "kind": nodes[nid].kind,
                               "fidelity": "approximated", "note": "Until body unreadable -> agent step"})
        elif atype == "Scope":
            body_d = spec.get("actions") if isinstance(spec.get("actions"), dict) else {}
            b_nodes, b_start = _pa_lower(body_d, "s" + nid, report)
            if b_nodes and b_start:
                body = Flow(id="", name=f"{label} body", start=b_start, nodes=b_nodes)
                nodes[nid] = FlowNode(id=nid, kind=NODE_SCOPE, body=body, next=nxt, label=label)
                report.append({"step": name, "node": nid, "kind": NODE_SCOPE,
                               "fidelity": "preserved",
                               "note": "WDL Scope -> scope (wire on_error in the designer "
                                       "to add a catch arm)"})
            else:
                nodes[nid] = _node_for_step(_step(name, spec), nid, nxt)
                report.append({"step": name, "node": nid, "kind": nodes[nid].kind,
                               "fidelity": "approximated", "note": "Scope body unreadable -> agent step"})
        else:
            node = _node_for_step(_step(name, spec), nid, nxt)
            nodes[nid] = node
            if node.kind == NODE_ACTION:
                fid, note = "preserved", "connector call -> action node"
            else:
                fid, note = "approximated", "no single-tool mapping -> agent step"
            report.append({"step": name, "node": nid, "kind": node.kind,
                           "fidelity": fid, "note": note})
    return nodes, (ids[order[0]] if order else None)


def _pa_to_flow(automation: ImportedAutomation,
                report: list[dict]) -> Flow | None:
    """Lower a Power Automate flow's WDL action graph, preserving If/Foreach.
    Returns ``None`` when the raw definition isn't usable (caller falls back)."""
    from .power_automate import _definition
    raw = automation.raw if isinstance(automation.raw, dict) else {}
    d = _definition(raw) if raw else {}
    actions = d.get("actions") if isinstance(d.get("actions"), dict) else {}
    if not actions:
        return None
    nodes, start = _pa_lower(actions, "n", report)
    if not nodes or not start:
        return None
    flow = Flow(id=automation.template_name(), name=automation.name,
                start=start, nodes=nodes)
    flow = _govern_imported(flow, report)
    if _has_blocking_draft_errors(flow):
        report.clear()
        return None
    return flow


# ---- Workato recipe-tree graph capture ---------------------------------------

# Workato condition operations -> the flow condition-evaluator's operators.
_WK_OPS = {"equals_to": "==", "not_equals_to": "!=", "greater_than": ">",
           "less_than": "<", "greater_than_or_equals_to": ">=",
           "less_than_or_equals_to": "<=", "contains": "contains"}


def _wk_operand(v) -> str:
    """A Workato operand (``#{_('data.salesforce.opportunity.stage')}``, a
    literal) reduced to a flow-data key / literal."""
    s = str(v).strip()
    m = re.search(r"_\('([^']+)'\)", s)
    if m:
        return _safe_line(m.group(1).rsplit(".", 1)[-1], max_chars=40)
    return _safe_line(s, max_chars=40)


def _wk_condition(inp: dict) -> str:
    op = _WK_OPS.get(str(inp.get("operation") or ""), "==")
    left = _wk_operand(inp.get("a") or inp.get("left") or "")
    right = _wk_operand(inp.get("b") or inp.get("right") or "")
    if left:
        return f"{left} {op} {right}".strip()
    return "result == true"


def _wk_lower(block: list, prefix: str,
              report: list[dict]) -> tuple[dict[str, FlowNode], str | None]:
    """Lower one Workato ``block`` list (one nesting level) into chained nodes.
    ``if`` steps become branch nodes (the nested block is the then-arm, wired to
    rejoin); ``foreach``/``repeat`` steps become foreach nodes with the nested
    block as the body."""
    from .workato import _step as _wk_step
    steps = [n for n in (block or []) if isinstance(n, dict)]
    ids = {i: f"{prefix}{i}" for i in range(len(steps))}
    nodes: dict[str, FlowNode] = {}
    for i, node in enumerate(steps):
        nid = ids[i]
        nxt = ids[i + 1] if i + 1 < len(steps) else None
        keyword = str(node.get("keyword") or "").lower()
        name = str(node.get("description") or node.get("name") or keyword or "step")
        inp = node.get("input") if isinstance(node.get("input"), dict) else {}
        nested = node.get("block") if isinstance(node.get("block"), list) else []
        if keyword == "if":
            t_nodes, t_start = _wk_lower(nested, nid + "t", report)
            _pa_wire_tail(t_nodes, nxt)
            nodes.update(t_nodes)
            nodes[nid] = FlowNode(id=nid, kind=NODE_BRANCH, condition=_wk_condition(inp),
                                  if_true=t_start or nxt, if_false=nxt, next=nxt,
                                  label=_safe_line(name, max_chars=60))
            report.append({"step": name, "node": nid, "kind": NODE_BRANCH,
                           "fidelity": "preserved",
                           "note": "Workato IF -> branch (then-arm wired; no-match falls through)"})
        elif keyword in ("foreach", "repeat"):
            b_nodes, b_start = _wk_lower(nested, "b", report)
            body = Flow(id="", name=f"{name} body", start=b_start or "",
                        nodes=b_nodes) if b_nodes else None
            items = _wk_operand(inp.get("source") or inp.get("list") or "") or "items"
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_FOREACH, items=items, body=body,
                                      next=nxt, output=f"{nid}_out",
                                      label=_safe_line(name, max_chars=60))
                report.append({"step": name, "node": nid, "kind": NODE_FOREACH,
                               "fidelity": "preserved",
                               "note": "Workato repeat -> foreach with a nested body"})
            else:
                nodes[nid] = _node_for_step(_wk_step(node), nid, nxt)
                report.append({"step": name, "node": nid, "kind": nodes[nid].kind,
                               "fidelity": "approximated",
                               "note": "repeat body unreadable -> agent step"})
        else:
            fnode = _node_for_step(_wk_step(node), nid, nxt)
            nodes[nid] = fnode
            report.append({"step": name, "node": nid, "kind": fnode.kind,
                           "fidelity": ("preserved" if fnode.kind == NODE_ACTION
                                        else "approximated"),
                           "note": ("connector call -> action node" if fnode.kind == NODE_ACTION
                                    else "no single-tool mapping -> agent step")})
    return nodes, (ids[0] if steps else None)


def _wk_to_flow(automation: ImportedAutomation,
                report: list[dict]) -> Flow | None:
    """Lower a Workato recipe's code tree, preserving IF/repeat structure.
    Returns ``None`` when the tree isn't usable (caller falls back)."""
    from .base import ImporterError
    from .workato import _parse_code
    raw = automation.raw if isinstance(automation.raw, dict) else {}
    try:
        tree = _parse_code(raw) if raw else None
    except ImporterError:
        return None
    block = tree.get("block") if isinstance(tree, dict) else None
    if not isinstance(block, list) or not block:
        return None
    nodes, start = _wk_lower(block, "n", report)
    if not nodes or not start:
        return None
    flow = Flow(id=automation.template_name(), name=automation.name,
                start=start, nodes=nodes)
    flow = _govern_imported(flow, report)
    if _has_blocking_draft_errors(flow):
        report.clear()
        return None
    return flow


# ---- UiPath activity-tree capture (best-effort) ------------------------------
# A UiPath Release/Schedule is correctly a one-step "run process X" import (the
# logic is compiled XAML the API doesn't expose). But an EXPORTED workflow whose
# raw carries an activity tree (some Studio/JSON exports do) has real structure
# -- Sequence / If / While / ForEach / TryCatch -- worth capturing.

_UIPATH_CHILD_KEYS = ("activities", "children", "body", "Body", "Activities")


def _uipath_children(node: dict) -> list:
    for k in _UIPATH_CHILD_KEYS:
        v = node.get(k)
        if isinstance(v, list):
            return [c for c in v if isinstance(c, dict)]
        if isinstance(v, dict):     # a single wrapped child (Body: {...})
            return [v]
    return []


def _uipath_type(node: dict) -> str:
    raw = str(node.get("type") or node.get("$type") or node.get("activityType")
              or node.get("Activity") or "")
    return raw.rsplit(".", 1)[-1].split(",")[0].strip().lower()


def _uipath_label(node: dict) -> str:
    return _safe_line(node.get("displayName") or node.get("DisplayName")
                      or node.get("name") or _uipath_type(node) or "step", max_chars=60)


def _uipath_lower(activities: list, prefix: str,
                  report: list[dict]) -> tuple[dict[str, FlowNode], str | None]:
    """Lower an ordered UiPath activity list (one nesting level) into chained
    nodes. Sequence/Flowchart flatten; If -> branch; While/DoWhile -> while;
    ForEach -> foreach; TryCatch -> scope; everything else -> a step node."""
    # flatten any Sequence/Flowchart wrappers at this level, in order
    flat: list[dict] = []
    for a in activities:
        if _uipath_type(a) in ("sequence", "flowchart", "statemachine"):
            flat.extend(_uipath_children(a))
        else:
            flat.append(a)
    ids = {i: f"{prefix}{i}" for i in range(len(flat))}
    nodes: dict[str, FlowNode] = {}

    def _lowered_body(act: dict, body_prefix: str, name: str):
        """The activity's children as a nested body Flow, or None when the
        body is unreadable (the caller then falls back to one step node)."""
        b_nodes, b_start = _uipath_lower(_uipath_children(act), body_prefix, report)
        return (Flow(id="", name=name, start=b_start, nodes=b_nodes)
                if b_nodes and b_start else None)

    def _step_fallback(act: dict, nid: str, nxt: str | None, label: str, note: str):
        """Shared 'body unreadable -> one approximated step node' arm."""
        nodes[nid] = _node_for_step(_uipath_step(act), nid, nxt)
        report.append({"step": label, "node": nid, "kind": nodes[nid].kind,
                       "fidelity": "approximated", "note": note})

    for i, act in enumerate(flat):
        nid = ids[i]
        nxt = ids[i + 1] if i + 1 < len(flat) else None
        atype = _uipath_type(act)
        label = _uipath_label(act)
        cond = _safe_line(act.get("condition") or act.get("Condition") or "result == true", max_chars=60)
        if atype == "if":
            then_acts = _uipath_children(act.get("then") or act.get("Then") or {}) or _uipath_children(act)
            else_acts = _uipath_children(act.get("else") or act.get("Else") or {})
            t_nodes, t_start = _uipath_lower(then_acts, nid + "t", report)
            f_nodes, f_start = _uipath_lower(else_acts, nid + "f", report)
            _pa_wire_tail(t_nodes, nxt)
            _pa_wire_tail(f_nodes, nxt)
            nodes.update(t_nodes)
            nodes.update(f_nodes)
            nodes[nid] = FlowNode(id=nid, kind=NODE_BRANCH, condition=cond,
                                  if_true=t_start or nxt, if_false=f_start or nxt,
                                  next=nxt, label=label)
            report.append({"step": label, "node": nid, "kind": NODE_BRANCH,
                           "fidelity": "preserved", "note": "UiPath If -> branch (arms wired)"})
        elif atype in ("while", "dowhile"):
            body = _lowered_body(act, "b", f"{label} body")
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_WHILE, condition=cond, body=body,
                                      next=nxt, output=f"{nid}_out", label=label)
                report.append({"step": label, "node": nid, "kind": NODE_WHILE,
                               "fidelity": "preserved", "note": "UiPath While -> while with a nested body"})
            else:
                _step_fallback(act, nid, nxt, label, "While body unreadable -> agent step")
        elif atype in ("foreach", "foreachrow", "parallelforeach"):
            body = _lowered_body(act, "b", f"{label} body")
            items = _safe_line(act.get("values") or act.get("Values") or act.get("collection") or "items", max_chars=40)
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_FOREACH, items=items, body=body,
                                      concurrent=(atype == "parallelforeach"),
                                      next=nxt, output=f"{nid}_out", label=label)
                report.append({"step": label, "node": nid, "kind": NODE_FOREACH,
                               "fidelity": "preserved", "note": "UiPath ForEach -> foreach with a nested body"})
            else:
                _step_fallback(act, nid, nxt, label, "ForEach body unreadable -> agent step")
        elif atype in ("trycatch", "try"):
            try_act = act.get("try") or act.get("Try")
            # Lower the Try block AS an activity ([try_act]) -- not its children --
            # so a single container Try (a While/If/ForEach) keeps its control
            # node; _uipath_lower flattens a Sequence wrapper itself. Falls back
            # to act's own children when Try isn't a nested dict.
            body = (_lowered_body({"children": [try_act]}, "s" + nid, f"{label} body")
                    if isinstance(try_act, dict) else None) or _lowered_body(act, "s" + nid, f"{label} body")
            if body is not None:
                nodes[nid] = FlowNode(id=nid, kind=NODE_SCOPE, body=body, next=nxt, label=label)
                report.append({"step": label, "node": nid, "kind": NODE_SCOPE,
                               "fidelity": "preserved", "note": "UiPath TryCatch -> scope (wire on_error for a catch arm)"})
            else:
                _step_fallback(act, nid, nxt, label, "Try body unreadable -> agent step")
        else:
            node = _node_for_step(_uipath_step(act), nid, nxt)
            nodes[nid] = node
            report.append({"step": label, "node": nid, "kind": node.kind,
                           "fidelity": ("preserved" if node.kind == NODE_ACTION else "approximated"),
                           "note": ("connector call -> action node" if node.kind == NODE_ACTION
                                    else "UiPath activity -> agent step")})
    return nodes, (ids[0] if flat else None)


def _uipath_step(act: dict) -> ImportedStep:
    label = _uipath_label(act)
    atype = _uipath_type(act)
    # an InvokeWorkflow/InvokeProcess maps to the uipath connector; else an agent
    is_invoke = "invoke" in atype or "process" in atype
    return ImportedStep(
        name=label,
        description=f"UiPath {atype} activity.",
        app="uipath" if is_invoke else "",
        operation="start_job" if is_invoke else atype,
        params={}, tools_hint=["uipath"] if is_invoke else [])


def _uipath_to_flow(automation: ImportedAutomation, report: list[dict]) -> Flow | None:
    """Capture a UiPath workflow's activity tree if the raw carries one; else
    None (a Release/Schedule has no exposed graph -> the one-step lowering)."""
    raw = automation.raw if isinstance(automation.raw, dict) else {}
    root = raw.get("Root") or raw.get("root") or raw.get("workflow") or raw.get("Workflow") or raw
    activities = _uipath_children(root) if isinstance(root, dict) else []
    if not activities:
        return None
    nodes, start = _uipath_lower(activities, "n", report)
    if not nodes or not start:
        return None
    flow = Flow(id=automation.template_name(), name=automation.name, start=start, nodes=nodes)
    flow = _govern_imported(flow, report)
    if _has_blocking_draft_errors(flow):
        return None   # the dispatcher clears the report on a None fallback
    return flow


def _n8n_structural(automation: ImportedAutomation, report: list[dict]) -> Flow | None:
    graph = _n8n_to_flow(automation)
    if graph is not None:
        _linear_report(graph, report)
        _append_classification_notes(graph, report)
    return graph


_STRUCTURAL_LOWERERS = {
    "n8n": _n8n_structural,
    "power_automate": _pa_to_flow,
    "workato": _wk_to_flow,
    "uipath": _uipath_to_flow,
}


# ---- Make (Integromat) router capture ----------------------------------------

# Make filter operator tokens (`text:equal`, `number:greater`, ...) -> the flow
# condition-evaluator's operators. We key on the part after the colon.
_MAKE_OPS = {"equal": "==", "notequal": "!=", "greater": ">", "greaterorequal": ">=",
             "less": "<", "lessorequal": "<=", "contain": "contains", "contains": "contains"}


def _make_condition(route: dict) -> str | None:
    """A route filter reduced to the flow condition grammar, or ``None`` when the
    route has no filter (Make's unconditional fallback route). A Make filter is
    ``{name, conditions: [[{a, o, b}]]}`` -- an OR of AND-groups; we take the
    first leaf as the representative test (the operator refines it in the
    designer, exactly like the n8n/WDL paths)."""
    filt = route.get("filter") if isinstance(route.get("filter"), dict) else None
    if not filt:
        return None
    groups = filt.get("conditions")
    if isinstance(groups, list):
        for grp in groups:
            if isinstance(grp, list) and grp and isinstance(grp[0], dict):
                c = grp[0]
                left = _safe_line(c.get("a", ""), max_chars=40)
                raw_op = str(c.get("o", "")).rsplit(":", 1)[-1].lower()
                right = _safe_line(c.get("b", ""), max_chars=40)
                return f"{left} {_MAKE_OPS.get(raw_op, '==')} {right}".strip()
    return "result == true"


def _has_router(modules: list) -> bool:
    """Whether any module (or nested route) is a router -- if not, the flat
    fallback already captures the scenario faithfully, so we skip this path."""
    for m in modules or []:
        if not isinstance(m, dict):
            continue
        routes = m.get("routes")
        if routes:
            return True
    return False


def _make_seq(modules: list, nodes: dict, ctr: dict, report: list[dict],
              join: str | None) -> str | None:
    """Lower an ordered Make module list into a chain (tail -> ``join``),
    returning the start node id. A router module becomes a parallel fan-out. Built
    right-to-left so each node knows its successor without a second pass."""
    nxt = join
    from .make import _step_from_module
    for mod in reversed([m for m in modules if isinstance(m, dict)]):
        if mod.get("routes"):
            nxt = _make_router(mod.get("routes"), nodes, ctr, report, nxt)
            continue
        nid = f"n{ctr['i']}"
        ctr["i"] += 1
        step = _step_from_module(mod)
        fnode = _node_for_step(step, nid, nxt)
        nodes[nid] = fnode
        report.append({"step": step.name, "node": nid, "kind": fnode.kind,
                       "fidelity": ("preserved" if fnode.kind == NODE_ACTION else "approximated"),
                       "note": ("connector call -> action node" if fnode.kind == NODE_ACTION
                                else "no single-tool mapping -> agent step")})
        nxt = nid
    return nxt


def _make_route_flow(route: dict, idx: int, report: list[dict]) -> Flow | None:
    """Lower one Make router route as an isolated fan-out branch sub-flow."""
    ctr = {"i": 0}
    nodes: dict[str, FlowNode] = {}
    body = _make_seq(route.get("flow", []) or [], nodes, ctr, report, None)
    cond = _make_condition(route)
    if cond is not None:
        bid = f"r{idx}_guard"
        nodes[bid] = FlowNode(id=bid, kind=NODE_BRANCH, condition=cond,
                              if_true=body, if_false=None, label="router route")
        report.append({"step": "router route", "node": bid, "kind": NODE_BRANCH,
                       "fidelity": "preserved",
                       "note": "Make router route filter -> branch guard"})
        body = bid
    if not nodes or body is None:
        return None
    flow = Flow(id=f"make_route_{idx}", name=f"Make router route {idx + 1}",
                start=body, nodes=nodes)
    # This is a nested parallel branch. Its nearest durable parent inserts one
    # coarse approval before the parallel container, so validate this fragment
    # under inherited authorization without putting an unresumable approval in
    # the branch itself.
    return None if _has_blocking_draft_errors(
        flow, inherited_approval=True) else flow


def _make_router(routes: list, nodes: dict, ctr: dict, report: list[dict],
                 join: str | None) -> str | None:
    """Lower a Make router into parallel route sub-flows.

    Make routers are fan-out containers: each route evaluates independently and
    every matching route can run for the same bundle. Model that with a
    ``parallel`` node whose branches are isolated route flows; filtered routes
    start with a guard branch, while unfiltered routes run unconditionally.
    """
    branches = [_make_route_flow(r, i, report)
                for i, r in enumerate(routes or []) if isinstance(r, dict)]
    branches = [b for b in branches if b is not None]
    if not branches:
        return join
    pid = f"n{ctr['i']}"
    ctr["i"] += 1
    nodes[pid] = FlowNode(id=pid, kind=NODE_PARALLEL, branches=branches,
                          next=join, label="router")
    report.append({"step": "router", "node": pid, "kind": NODE_PARALLEL,
                   "fidelity": "preserved",
                   "note": "Make BasicRouter -> parallel fan-out; all matching routes can run"})
    return pid


def _make_to_flow(automation: ImportedAutomation, report: list[dict]) -> Flow | None:
    """Lower a Make scenario's blueprint, preserving ``BasicRouter`` routes as real
    ``branch`` nodes instead of the flat ``_flatten_flow`` inlining. Returns
    ``None`` when there's no router (flat fallback suffices) or the blueprint isn't
    usable (caller falls back to the linear lowering)."""
    from .make import _unwrap
    raw = automation.raw if isinstance(automation.raw, dict) else {}
    if not raw:
        return None
    bp = _unwrap(raw)
    flow_list = bp.get("flow") if isinstance(bp, dict) else None
    if not isinstance(flow_list, list) or len(flow_list) < 2:
        return None
    modules = flow_list[1:]                       # drop the trigger module
    if not _has_router(modules):
        return None
    ctr = {"i": 0}
    nodes: dict[str, FlowNode] = {}
    start = _make_seq(modules, nodes, ctr, report, None)
    if not nodes or start is None:
        return None
    flow = Flow(id=automation.template_name(), name=automation.name,
                start=start, nodes=nodes)
    flow = _govern_imported(flow, report)
    if _has_blocking_draft_errors(flow):
        report.clear()
        return None
    return flow


def _linear_report(flow: Flow, report: list[dict], source: str = "") -> None:
    """Fidelity entries derived from an already-lowered graph (the n8n path and
    the flat fallback), so every import surfaces a report.

    ``source`` lets an invocation-only importer be honest: a UiPath import is a
    single ``start_job`` action, and the Orchestrator API returns no .xaml, so the
    invoked process's internal If/Switch/While/ForEach are *not visible* -- that
    is an approximation of the real workflow, not a preserved one, and saying so
    is the whole point of the report."""
    for n in flow.nodes.values():
        if source == "uipath" and n.kind == NODE_ACTION:
            entry = ("approximated",
                     "UiPath process invocation; the process's internal "
                     "If/Switch/While/ForEach isn't visible from Orchestrator metadata")
        elif n.kind == NODE_BRANCH:
            entry = ("preserved", "source IF -> branch (arms wired)")
        elif n.kind == NODE_APPROVAL:
            entry = ("approximated",
                     "human approval gate inserted before high-risk imported work")
        elif n.kind == NODE_ACTION:
            entry = ("preserved", "connector call -> action node")
        else:
            entry = ("approximated", "no structural/tool mapping -> agent step")
        report.append({"step": n.label or n.id, "node": n.id, "kind": n.kind,
                       "fidelity": entry[0], "note": entry[1]})


def to_flow_with_report(automation: ImportedAutomation) -> tuple[Flow, list[dict]]:
    """Like :func:`to_flow`, plus a per-step migration fidelity report:
    ``{step, node, kind, fidelity: preserved|approximated, note}`` -- so the
    importer can say exactly what carried over structurally and what became an
    agent step (which still runs; it is an approximation, not a drop)."""
    report: list[dict] = []
    # Per-source structural lowerers: source -> fn(automation, report) -> Flow|None.
    # Registering one here is all it takes to capture a new source's graph
    # structurally -- the dispatch below needs no change. A lowerer fills
    # ``report`` and returns None when the raw has no usable structure (the
    # caller then falls back to the ordered-chain lowering).
    lowerer = _STRUCTURAL_LOWERERS.get(automation.source)
    if lowerer is not None:
        graph = lowerer(automation, report)
        if graph is not None:
            return graph, report
        report.clear()
    if automation.source == "make":
        graph = _make_to_flow(automation, report)
        if graph is not None:
            return graph, report
        report.clear()
    steps = automation.steps
    if not steps:
        # No extractable actions -> a single agent node that runs the automation's
        # intent, so an empty import still yields a runnable (template-equivalent)
        # flow rather than nothing.
        brief = automation.description or automation.name or "Run the imported automation"
        n = FlowNode(id="n0", kind=NODE_AGENT, brief=brief, label=automation.name)
        flow = Flow(id=automation.template_name(), name=automation.name,
                    start="n0", nodes={"n0": n})
        report.append({"step": automation.name or "automation", "node": "n0",
                       "kind": NODE_AGENT, "fidelity": "approximated",
                       "note": "no extractable steps -> one agent step with the automation's intent"})
        return flow, report
    nodes: dict[str, FlowNode] = {}
    for i, step in enumerate(steps):
        node_id = f"n{i}"
        nxt = f"n{i + 1}" if i + 1 < len(steps) else None
        nodes[node_id] = _node_for_step(step, node_id, nxt)
    flow = Flow(id=automation.template_name(), name=automation.name,
                start="n0", nodes=nodes)
    flow = _govern_imported(flow)
    _linear_report(flow, report, source=automation.source)
    _append_classification_notes(flow, report)
    return flow, report


def to_flow(automation: ImportedAutomation) -> Flow:
    """Build a :class:`Flow` from an imported automation. n8n IF/Filter and
    Power Automate If/Foreach are captured structurally; everything else lowers
    to an ordered chain (see :func:`to_flow_with_report` for the fidelity log)."""
    return to_flow_with_report(automation)[0]


__all__ = ["to_flow", "to_flow_with_report"]
