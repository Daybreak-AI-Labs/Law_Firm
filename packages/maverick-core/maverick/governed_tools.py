"""Govern enterprise-connector WRITES in the live tool path.

The enterprise REST connectors expose a service as a ``confirm=true``-gated tool
-- but the AGENT can set ``confirm`` itself, so a consequential write to a system
of record has no human in the loop. Standard deployments opt in with
``[governed_connectors] enable``; enterprise mode enforces the boundary for
every compatible connector. Writes are simulated, human-gated, and surrounded
by durable PREPARE/COMMIT receipts. The agent cannot self-approve. Reads pass
through unchanged.

The governed ``apply`` reuses the ORIGINAL tool's write path (the same env auth +
SSRF-safe request), so wrapping never changes *where* a write goes -- only that
it must clear the approval gate first. Standard mode remains inert when the
feature is disabled; enterprise mode is deliberately fail closed.

Connectors whose tool exposes an ``op`` plus ``confirm`` schema are wrapped,
including the ``make_rest_tool`` family and bespoke Salesforce/ServiceNow
connectors. The wrapper derives a stable resource label from ``path`` when
available, or from connector-specific record identifiers for bespoke tools.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid

from .tools import Tool

log = logging.getLogger(__name__)

_WRITE_OPS = (
    "post", "put", "patch", "delete",
    "record_create", "record_update", "record_delete",
    "create", "update",
)

# Bespoke external-system tools do not all use the REST factory's
# ``op`` + ``confirm`` schema. Their mutating operations still cross the same
# production trust boundary and therefore need the same durable transaction
# protocol in enterprise mode. Keep this explicit: inferring writes from words
# like ``call`` or from tool risk would either miss mutations or gate reads.
_BESPOKE_WRITE_OPS: dict[str, frozenset[str]] = {
    "asana": frozenset({"task_complete", "task_create"}),
    "calendar": frozenset({"create_event"}),
    "calendly": frozenset({"cancel"}),
    "clickup": frozenset({"task_create"}),
    "cloudflare": frozenset({"dns_create", "dns_delete", "dns_update", "purge"}),
    "confluence": frozenset({"page_create", "page_update"}),
    "databricks": frozenset({"job_run"}),
    "datadog": frozenset({"submit_event", "submit_metric"}),
    "discord_bot": frozenset({"post", "react", "reply"}),
    "dropbox": frozenset({"delete", "share", "upload"}),
    "email": frozenset({"send"}),
    "elasticsearch": frozenset({"delete", "index"}),
    "ga4": frozenset({"send_event"}),
    "github_actions": frozenset({"cancel", "dispatch"}),
    "github_issues": frozenset({"create"}),
    "gmail": frozenset({"send"}),
    "gitlab": frozenset({"issue_comment", "issue_create", "mr_comment"}),
    "gitlab_issues": frozenset({"create"}),
    "home_assistant": frozenset({"call_service"}),
    "hubspot": frozenset({"contact_create", "contact_update"}),
    "jira": frozenset({"comment", "create", "transition"}),
    "lambda": frozenset({"invoke"}),
    "linear": frozenset({"comment", "create", "update_status"}),
    "mixpanel": frozenset({"people_set", "track"}),
    "mongodb": frozenset({"delete", "insert", "update"}),
    "msgraph": frozenset({"send_mail"}),
    "notion": frozenset({"page_append", "page_create"}),
    # A described OpenAPI operation may be mutating even when its HTTP method
    # is hidden behind the operation id. Treat execution (not discovery) as the
    # consequential boundary.
    "openapi_runner": frozenset({"call"}),
    "plausible": frozenset({"event"}),
    "posthog": frozenset({"capture", "identify"}),
    "pagerduty": frozenset({"acknowledge", "resolve", "trigger"}),
    "redis": frozenset({"delete", "lpush", "publish", "set"}),
    # Replicate's paid ``run`` has no legacy confirm field. Explicit metadata
    # makes the tool governable and prevents a paid remote effect from falling
    # outside the transaction boundary.
    "replicate": frozenset({"cancel", "run"}),
    "sentry": frozenset({"resolve"}),
    "ses": frozenset({"send"}),
    "shopify": frozenset({"refund_create"}),
    "sns": frozenset({"publish", "sms", "subscribe", "unsubscribe"}),
    "spotify": frozenset({"pause", "play"}),
    "stripe": frozenset({"refund_create"}),
    "slack_bot": frozenset({"join", "post", "upload"}),
    "teams": frozenset({"send"}),
    "trello": frozenset({"card_create", "card_move", "comment"}),
    "twilio": frozenset({"call_create", "sms_send"}),
    "vercel": frozenset({"cancel"}),
    "zoom": frozenset({"meeting_create", "meeting_delete"}),
}

# Reviewed, tool-specific read metadata for confirm-bearing bespoke connectors.
# A global verb allowlist is insufficient: a future vendor could legitimately
# name a mutating operation ``get`` or reuse another connector's read verb.
# Unknown tool/operation pairs are governed until explicitly reviewed.
_BESPOKE_READ_OPS: dict[str, frozenset[str]] = {
    "airtable": frozenset({"get", "list"}),
    "asana": frozenset({"projects", "task_get", "tasks", "workspaces"}),
    "calendly": frozenset({"event_invitees", "event_types", "events", "me"}),
    "clickup": frozenset({"lists", "spaces", "task_get", "tasks", "teams"}),
    "cloudflare": frozenset({"dns_list"}),
    "confluence": frozenset({"page_get", "search"}),
    "databricks": frozenset({"jobs_list", "run_get"}),
    "datadog": frozenset({"monitor_get", "monitors"}),
    "dropbox": frozenset({"download", "list"}),
    "dynamodb": frozenset({"get", "query", "scan", "tables"}),
    "dynamics": frozenset({"query"}),
    "elasticsearch": frozenset({"count", "get", "indices", "search"}),
    "gdrive": frozenset({"export", "get", "list"}),
    "github_actions": frozenset({"jobs", "run_get", "runs", "workflows"}),
    "gmail": frozenset({"get", "labels", "list"}),
    "home_assistant": frozenset({"history", "state_get", "states"}),
    "hubspot": frozenset({"companies", "contact_get", "contacts", "deals"}),
    "lambda": frozenset({"get_function", "list_functions", "recent_logs"}),
    "mongodb": frozenset({"collections", "count", "find", "find_one"}),
    "msgraph": frozenset({"drive_list", "events", "me", "messages"}),
    "pagerduty": frozenset({"incident_get", "incidents", "on_call"}),
    "redis": frozenset({"get", "info", "keys", "lrange"}),
    "replicate": frozenset({"models", "predict_get"}),
    "s3": frozenset({"get", "list_buckets", "list_objects", "presign"}),
    "salesforce": frozenset({"record_get", "soql"}),
    "sentry": frozenset({"events", "issue_get", "issues", "releases"}),
    "servicenow": frozenset({"get", "query"}),
    "ses": frozenset({"quota", "verified_identities"}),
    "shopify": frozenset({
        "customer_get", "inventory", "order_get", "orders", "product_get",
        "products",
    }),
    "sns": frozenset({"topics"}),
    "spotify": frozenset({
        "playback_state", "playlist_get", "playlists", "search", "track_get",
    }),
    "stripe": frozenset({
        "balance", "charges", "customer_get", "customer_search", "subscriptions",
    }),
    "trello": frozenset({"boards", "card_get", "cards", "lists"}),
    "twilio": frozenset({"lookup", "sms_list"}),
    "vercel": frozenset({
        "deployment_get", "deployment_logs", "deployments", "domains", "projects",
    }),
    "zoom": frozenset({"meeting_get", "meetings", "recordings"}),
}

_SQL_CONNECTORS = frozenset({
    "bigquery", "database", "databricks", "oracle", "snowflake",
})


def _enterprise_mode() -> bool:
    """Central enterprise predicate, strict on policy-source uncertainty.

    The connector wrapper is a mandatory enterprise floor.  If the config
    source that may contain ``[enterprise] mode = true`` is unreadable, the
    safe interpretation at this effect boundary is enterprise-on; treating a
    loader error as ``False`` would silently remove approval and receipts.
    """
    try:
        from .config import config_source_errors
        from .enterprise import enterprise_enabled

        if config_source_errors():
            log.error(
                "governed_connectors: config source unreadable; enforcing "
                "enterprise connector governance",
            )
            return True
        return bool(enterprise_enabled())
    except Exception:
        log.exception(
            "governed_connectors: enterprise policy unavailable; enforcing "
            "connector governance",
        )
        return True


def _approver() -> str:
    """The standing approver of record for governed connector writes."""
    a = os.environ.get("MAVERICK_GOVERNED_APPROVER", "").strip()
    if a:
        return a
    try:
        from .config import get_governed_connectors
        return str(get_governed_connectors().get("approver", "")).strip()
    except Exception:  # pragma: no cover -- config must never break tool assembly
        return ""


def _is_governable_op_tool(tool: Tool) -> bool:
    """Whether ``tool`` speaks an op/confirm schema we can govern.

    ``make_rest_tool`` connectors include ``path``; Salesforce and ServiceNow
    use bespoke record identifiers. GraphQL connectors also expose ``op`` plus
    ``confirm``, but mutations are declared in the ``query`` document instead of
    the ``op`` value, so the wrapper detects those writes separately. In all
    cases, governance can safely force ``confirm=True`` only after approval
    because the original tool keeps full responsibility for validating and
    applying the requested write.
    """
    props = (tool.input_schema or {}).get("properties", {})
    if not isinstance(props, dict) or "op" not in props:
        return False
    return "confirm" in props or tool.name in _BESPOKE_WRITE_OPS


def _is_graphql_op_tool(tool: Tool) -> bool:
    """Whether ``tool`` uses the GraphQL op/query/confirm connector schema."""
    props = (tool.input_schema or {}).get("properties", {})
    if not isinstance(props, dict) or "query" not in props or "confirm" not in props:
        return False
    op = props.get("op")
    if not isinstance(op, dict):
        return False
    enum = op.get("enum")
    return isinstance(enum, list) and {str(item).lower() for item in enum} == {"query"}


def _is_declared_rest_op_tool(tool: Tool) -> bool:
    """Whether a schema declares the reviewed HTTP verb/path contract."""
    props = (tool.input_schema or {}).get("properties", {})
    if not isinstance(props, dict) or "path" not in props:
        return False
    raw_op = props.get("op")
    enum = raw_op.get("enum") if isinstance(raw_op, dict) else None
    operations = {str(item).lower() for item in enum or []}
    return bool(operations) and operations <= {"get", "post", "put", "patch", "delete"}


def _is_governed_write(tool: Tool, args: dict) -> bool:
    """Return whether this connector call needs the governed write boundary.

    Confirm-bearing connector operations fail closed: only an explicitly safe
    read may bypass governance. That makes new/custom operation names safe by
    default instead of depending on an ever-growing write denylist.
    """
    op = str(args.get("op", "")).strip().lower()
    if _is_graphql_op_tool(tool):
        from .tools._rest_connector import _graphql_has_mutation

        return _graphql_has_mutation(str(args.get("query") or ""))
    if tool.name in _SQL_CONNECTORS and op in {"query", "sql"}:
        # SQL cannot be proven side-effect-free from a leading keyword across
        # dialects: SELECT INTO/OUTFILE, locks, sequences, and stored/volatile
        # functions all mutate. Every credentialed SQL execution therefore
        # crosses the governed boundary; the approval preview binds the exact
        # statement before the connector receives ``confirm=True``.
        return True
    if tool.name == "lambda" and op == "invoke":
        return str(args.get("invocation_type") or "RequestResponse").lower() != "dryrun"
    if op in _BESPOKE_WRITE_OPS.get(tool.name, frozenset()):
        return True
    if op in _WRITE_OPS:
        return True
    if _is_declared_rest_op_tool(tool):
        return op != "get"
    props = (tool.input_schema or {}).get("properties", {})
    if not isinstance(props, dict) or "confirm" not in props:
        return False
    return op not in _BESPOKE_READ_OPS.get(tool.name, frozenset())


def _resource_label(tool_name: str, args: dict) -> str:
    """Return a stable, non-secret label for the governed write preview."""
    path = str(args.get("path") or "").strip()
    if path:
        return path
    if tool_name == "salesforce":
        sobject = str(args.get("sobject") or "").strip()
        rid = str(args.get("id") or "").strip()
        return "/".join(part for part in ("sobjects", sobject, rid) if part)
    if tool_name == "servicenow":
        table = str(args.get("table") or "").strip()
        sys_id = str(args.get("sys_id") or "").strip()
        return "/".join(part for part in ("table", table, sys_id) if part)
    return str(args.get("op") or "write")


def _approval_detail(
    effect: str,
    args: dict,
    *,
    transaction_id: str,
    request_digest: str,
) -> str:
    """Bounded, secret-redacted request shown to the approving human.

    Field names alone are not enough to approve a system-of-record mutation.
    The digest binds the decision and receipts to the exact frozen request;
    the redacted preview lets the operator understand what is changing.
    """
    try:
        request = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # pragma: no cover -- tool inputs are normally JSON
        request = repr(args)
    try:
        from .safety.secret_detector import redact

        request, _ = redact(request)
    except Exception:  # pragma: no cover -- retain the binding when optional DLP fails
        request = "(request preview unavailable; inspect the bound digest)"
    if len(request) > 2000:
        request = request[:2000] + "...(truncated)"
    return (
        f"{effect}; transaction_id={transaction_id}; "
        f"request_sha256={request_digest}; request={request}"
    )


def wrap_connector_tool(
    tool: Tool,
    *,
    goal_id: int | None = None,
    actor: str = "",
    enterprise: bool | None = None,
) -> Tool:
    """Return a connector with simulate, approval, and durable receipts.

    The external effect is never attempted until a PREPARE receipt is fsynced.
    A missing COMMIT receipt after the effect produces an explicit
    ``INDETERMINATE`` outcome so callers do not blindly retry a write.
    """
    from .governed_actions import (
        ActionError,
        ActionSpec,
        GovernedActions,
        record_tool_lineage,
    )

    strict = _enterprise_mode() if enterprise is None else bool(enterprise)

    def _receipt(
        *,
        action: str,
        args: dict,
        effect: str,
        approver: str,
        transaction_id: str,
        phase: str,
        result: str = "",
        strict_write: bool,
    ) -> bool:
        return record_tool_lineage(
            goal_id,
            action,
            args,
            actor=actor,
            effect=effect,
            result=result,
            approver=approver,
            transaction_id=transaction_id,
            phase=phase,
            sources=(f"tool:{tool.name}",),
            force=True,
            strict=strict_write,
        )

    def _fn(args: dict) -> str:
        op = str(args.get("op", "")).strip().lower()
        if not _is_governed_write(tool, args):
            return tool.fn(args)
        approver = _approver()
        path = _resource_label(tool.name, args)
        ga = GovernedActions()
        action = f"{tool.name}.write"
        ga.register(ActionSpec(
            name=action,
            params={"op": str, "path": str},
            risk="high",
            simulate=lambda p: (
                f"would {p['op'].upper()} {tool.name} {p['path']}".rstrip()
            ),
        ))
        params = {"op": op, "path": path}
        try:
            preview = ga.simulate(action, params)
            if preview.requires_approval and not approver and not strict:
                return (
                    f"REFUSED (governed): {preview.effect}. A governed write needs an "
                    "approver of record; the agent cannot self-approve. Set "
                    "[governed_connectors] approver or MAVERICK_GOVERNED_APPROVER."
                )

            # Freeze the exact request before approval so a caller retaining a
            # reference to the input dict cannot race a different payload into
            # the external call after the human decision.  ``confirm`` is part
            # of the binding because it is forced at the effect boundary.
            receipt_args = copy.deepcopy(args)
            receipt_args["confirm"] = True
            request_bytes = json.dumps(
                receipt_args, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")
            request_digest = hashlib.sha256(request_bytes).hexdigest()
            transaction_id = uuid.uuid4().hex

            # Standard mode retains the legacy operator-of-record gate and the
            # consent primitive's compatibility default. Enterprise requires a
            # fresh explicit human decision: neither silent auto-approval nor a
            # persistent ledger grant can authorize this external effect.
            from .safety.consent import require_consent

            decision = require_consent(
                action,
                risk="high",
                scope=(
                    f"{path}#sha256={request_digest}"
                    if strict else path
                ),
                detail=_approval_detail(
                    preview.effect,
                    receipt_args,
                    transaction_id=transaction_id,
                    request_digest=request_digest,
                ),
                provenance="governed-connector",
                allow_auto_approve=not strict,
                consult_ledger=not strict,
            )
            if not decision.granted:
                return (
                    f"REFUSED (governed): {preview.effect}. "
                    "A human approval was not granted."
                )

            decision_actor = str(getattr(decision, "actor", "") or "").strip()
            if strict and decision.source == "dashboard" and not decision_actor:
                return (
                    "REFUSED (governed): the dashboard decision did not carry "
                    "an authenticated approver identity; no write was attempted."
                )
            approval_identity = (
                (decision_actor or decision.source)
                if strict else (approver or decision_actor or decision.source)
            )
            try:
                prepared = _receipt(
                    action=f"{action}.prepare",
                    args=receipt_args,
                    effect=preview.effect,
                    approver=approval_identity,
                    transaction_id=transaction_id,
                    phase="PREPARE",
                    strict_write=strict,
                )
            except Exception:  # strict ledger failure: effect has not started
                prepared = False
            if not prepared:
                return (
                    "REFUSED (governed): the durable PREPARE receipt could not "
                    "be persisted; no external write was attempted."
                )

            try:
                result = tool.fn(receipt_args)
            except Exception as exc:  # noqa: BLE001 -- preserve failure receipt
                # Exception messages from HTTP/database libraries routinely
                # embed credentialed URLs, headers, and DSNs. Receipts and the
                # model-visible result get only a bounded type marker; the
                # transaction id is sufficient for privileged reconciliation.
                detail = type(exc).__name__[:120]
                try:
                    _receipt(
                        action=f"{action}.indeterminate",
                        args=receipt_args,
                        effect=preview.effect,
                        result=detail,
                        approver=approval_identity,
                        transaction_id=transaction_id,
                        phase="INDETERMINATE",
                        strict_write=False,
                    )
                except Exception:  # receipt is best-effort after ambiguity
                    pass
                return (
                    "INDETERMINATE (governed): the connector raised after its "
                    "external-effect boundary; reconcile before retrying. "
                    f"transaction_id={transaction_id}; failure_type={detail}"
                )

            # A string error from a connector is also ambiguous: several
            # connector implementations catch network exceptions internally,
            # after the remote system may have accepted the request. Never
            # convert that uncertainty into an ordinary retryable failure.
            from .tool_results import ToolResultState, classify_tool_result

            result_state = classify_tool_result(result)
            if result_state is not ToolResultState.SUCCEEDED:
                safe_result = result_state.value
                try:
                    _receipt(
                        action=f"{action}.indeterminate",
                        args=receipt_args,
                        effect=preview.effect,
                        result=safe_result,
                        approver=approval_identity,
                        transaction_id=transaction_id,
                        phase="INDETERMINATE",
                        strict_write=False,
                    )
                except Exception:
                    pass
                return (
                    "INDETERMINATE (governed): the connector did not prove a "
                    "committed or definitively aborted write; reconcile before "
                    f"retrying. transaction_id={transaction_id}; "
                    f"result_state={safe_result}"
                )

            try:
                committed = _receipt(
                    action=f"{action}.commit",
                    args=receipt_args,
                    effect=preview.effect,
                    result=str(result),
                    approver=approval_identity,
                    transaction_id=transaction_id,
                    phase="COMMIT",
                    strict_write=strict,
                )
            except Exception:  # effect completed but durable ACK did not
                committed = False
            if not committed:
                return (
                    "INDETERMINATE (governed): the connector write may have "
                    "succeeded, but its COMMIT receipt could not be persisted. "
                    f"transaction_id={transaction_id}; do not retry automatically."
                )
            return str(result)
        except ActionError:
            return "ERROR (governed): action policy rejected the request"
        except Exception as exc:  # noqa: BLE001 -- policy errors refuse writes
            return (
                "ERROR (governed): internal policy failure; no retry without "
                f"operator review (failure_type={type(exc).__name__[:120]})"
            )

    note = " [governed: writes simulated, human-gated, and transaction-receipted]"
    return Tool(
        name=tool.name,
        description=(tool.description + note)[:1024],
        input_schema=tool.input_schema,
        fn=_fn,
        parallel_safe=False,
    )


def governance_enabled() -> bool:
    """Whether the live tool path governs connector writes.

    Enterprise is a mandatory floor. Standard deployments remain opt-in via
    config or ``MAVERICK_GOVERNED_CONNECTORS``.
    """
    if _enterprise_mode():
        return True
    try:
        from .config import env_flag, get_governed_connectors
        ov = env_flag("MAVERICK_GOVERNED_CONNECTORS")
        if ov is not None:
            return ov
        return bool(get_governed_connectors().get("enable", False))
    except Exception:  # pragma: no cover
        return False


def apply_governed_connectors(reg, *, goal_id: int | None = None) -> list[str]:
    """Replace external write tools with governed wrappers.

    In standard mode an explicit connector list scopes the opt-in; an empty
    list means all compatible tools. Enterprise always wraps every compatible
    tool and rejects explicitly named missing/incompatible tools rather than
    silently leaving an expected security boundary open.
    """
    if not governance_enabled():
        return []
    enterprise = _enterprise_mode()
    try:
        from .config import get_governed_connectors
        names = get_governed_connectors().get("connectors", [])
    except Exception as exc:  # pragma: no cover
        if enterprise:
            raise RuntimeError("cannot load governed connector policy") from exc
        return []

    configured = [str(name).strip() for name in names if str(name).strip()]
    available = {tool.name: tool for tool in reg.all()}
    if enterprise and configured:
        invalid = [
            name for name in configured
            if name not in available or not _is_governable_op_tool(available[name])
        ]
        if invalid:
            raise RuntimeError(
                "enterprise governed connector policy names missing or "
                f"incompatible tools: {', '.join(sorted(invalid))}"
            )

    if enterprise or not configured:
        candidates = [
            name for name, tool in available.items()
            if _is_governable_op_tool(tool)
        ]
    else:
        candidates = configured

    wrapped: list[str] = []
    for name in candidates:
        try:
            tool = reg.get(name)
        except Exception:  # noqa: BLE001 -- unknown/ACL'd name: skip
            continue
        if not _is_governable_op_tool(tool):
            log.info("governed_connectors: %s is not an op/confirm tool; left ungoverned", name)
            continue
        reg.register(wrap_connector_tool(
            tool,
            goal_id=goal_id,
            actor=str(getattr(reg, "_principal", "") or ""),
            enterprise=enterprise,
        ))
        wrapped.append(name)
    return wrapped


__all__ = ["wrap_connector_tool", "apply_governed_connectors", "governance_enabled"]
