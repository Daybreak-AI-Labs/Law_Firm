"""Governing enterprise-connector WRITES in the live tool path (opt-in).

A write is previewed + approval-gated against a standing operator approver (the
agent can't self-approve); reads pass through. Off by default leaves the
registry untouched.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from maverick.governed_tools import (
    _is_governed_write,
    apply_governed_connectors,
    governance_enabled,
    wrap_connector_tool,
)
from maverick.tools import Tool, ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["get", "post", "put", "patch", "delete"]},
        "path": {"type": "string"},
        "confirm": {"type": "boolean"},
    },
    "required": ["op", "path"],
}


def _fake_tool(calls):
    def _fn(args):
        calls.append(dict(args))
        return f"DID {args.get('op')} {args.get('path')} confirm={args.get('confirm')}"
    return Tool(name="acme", description="Acme connector", input_schema=_SCHEMA, fn=_fn)



def _graphql_tool(calls):
    schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["query"]},
            "query": {"type": "string", "description": "GraphQL query or mutation."},
            "variables": {"type": "object"},
            "confirm": {"type": "boolean"},
        },
        "required": ["op", "query"],
    }

    def _fn(args):
        calls.append(dict(args))
        return f"GQL {args.get('op')} confirm={args.get('confirm')}"

    return Tool(name="monday", description="monday GraphQL", input_schema=schema, fn=_fn)


def _salesforce_tool(calls):
    schema = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": ["soql", "record_create", "record_update", "record_delete"],
            },
            "sobject": {"type": "string"},
            "id": {"type": "string"},
            "fields": {"type": "object"},
            "confirm": {"type": "boolean"},
        },
        "required": ["op"],
    }

    def _fn(args):
        calls.append(dict(args))
        return f"SF {args.get('op')} {args.get('sobject')} confirm={args.get('confirm')}"

    return Tool(name="salesforce", description="Salesforce", input_schema=schema, fn=_fn)


def _teams_style_tool(calls):
    """A bespoke external writer with no legacy ``confirm`` field."""
    schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["send", "history"]},
            "text": {"type": "string"},
        },
        "required": ["op"],
    }

    def _fn(args):
        calls.append(dict(args))
        return f"TEAMS {args.get('op')}"

    return Tool(name="teams", description="Teams", input_schema=schema, fn=_fn)


def _op_tool(name, operations, calls, *, confirm=True):
    properties = {
        "op": {"type": "string", "enum": list(operations)},
        "path": {"type": "string"},
    }
    if confirm:
        properties["confirm"] = {"type": "boolean"}

    def _fn(args):
        calls.append(dict(args))
        return f"{name}:{args.get('op')}"

    return Tool(
        name=name,
        description=f"{name} connector",
        input_schema={
            "type": "object", "properties": properties, "required": ["op"],
        },
        fn=_fn,
    )


class TestWrap:
    def test_read_passes_through(self):
        calls = []
        wrapped = wrap_connector_tool(_fake_tool(calls))
        out = wrapped.fn({"op": "get", "path": "/x"})
        assert "DID get /x" in out
        assert calls == [{"op": "get", "path": "/x"}]

    def test_write_without_approver_is_refused(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["acme"], "approver": ""})
        calls = []
        wrapped = wrap_connector_tool(_fake_tool(calls))
        out = wrapped.fn({"op": "post", "path": "/accounts", "body": {"n": 1}})
        assert "REFUSED (governed)" in out
        assert "approver" in out
        assert calls == []  # the underlying write never ran

    def test_write_with_approver_commits(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_APPROVER", "ops@corp")
        calls = []
        wrapped = wrap_connector_tool(_fake_tool(calls))
        out = wrapped.fn({"op": "post", "path": "/accounts"})
        # The original write ran, with confirm forced true (approval cleared it).
        assert calls and calls[0]["confirm"] is True
        assert "DID post /accounts" in out

    def test_description_marks_governed(self):
        wrapped = wrap_connector_tool(_fake_tool([]))
        assert "governed" in wrapped.description

    def test_graphql_query_passes_through(self):
        calls = []
        wrapped = wrap_connector_tool(_graphql_tool(calls))
        out = wrapped.fn({"op": "query", "query": "query { boards { id } }"})
        assert "GQL query" in out
        assert calls == [{"op": "query", "query": "query { boards { id } }"}]

    def test_graphql_mutation_without_approver_is_refused(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["monday"], "approver": ""})
        calls = []
        wrapped = wrap_connector_tool(_graphql_tool(calls))
        out = wrapped.fn({"op": "query", "query": "mutation { create_item { id } }", "confirm": True})
        assert "REFUSED (governed)" in out
        assert calls == []

    def test_graphql_bom_prefixed_mutation_is_governed(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg
        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["monday"], "approver": ""},
        )
        calls = []
        wrapped = wrap_connector_tool(_graphql_tool(calls))
        out = wrapped.fn({
            "op": "query", "query": "\ufeffmutation { create_item { id } }",
        })
        assert "REFUSED (governed)" in out
        assert calls == []

    def test_graphql_mutation_after_escaped_block_string_is_governed(
        self, monkeypatch,
    ):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg
        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["monday"], "approver": ""},
        )
        calls = []
        document = (
            'query Q { echo(text: """ignored \\""" {""") }\n'
            "mutation M { create_item { id } }"
        )
        out = wrap_connector_tool(_graphql_tool(calls)).fn({
            "op": "query", "query": document,
        })
        assert "REFUSED (governed)" in out
        assert calls == []

    def test_graphql_mutation_with_approver_commits(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_APPROVER", "ops@corp")
        calls = []
        wrapped = wrap_connector_tool(_graphql_tool(calls))
        out = wrapped.fn({"op": "query", "query": "mutation { create_item { id } }"})
        assert "GQL query" in out
        assert calls and calls[0]["confirm"] is True


class TestApply:
    def test_off_by_default_is_noop(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_GOVERNED_CONNECTORS", raising=False)
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": False, "connectors": ["acme"], "approver": ""})
        reg = ToolRegistry()
        reg.register(_fake_tool([]))
        assert apply_governed_connectors(reg) == []
        assert "governed" not in reg.get("acme").description

    def test_enabled_wraps_configured(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "1")
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["acme"], "approver": "ops"})
        reg = ToolRegistry()
        reg.register(_fake_tool([]))
        assert apply_governed_connectors(reg) == ["acme"]
        assert "governed" in reg.get("acme").description

    def test_enabled_wraps_bespoke_salesforce_schema(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "1")
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["salesforce"], "approver": ""})
        calls = []
        reg = ToolRegistry()
        reg.register(_salesforce_tool(calls))
        assert apply_governed_connectors(reg) == ["salesforce"]
        assert "governed" in reg.get("salesforce").description
        out = reg.get("salesforce").fn({
            "op": "record_create",
            "sobject": "Account",
            "fields": {"Name": "poc"},
            "confirm": True,
        })
        assert "REFUSED (governed)" in out
        assert calls == []

    def test_skips_non_op_tool(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "1")
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["plain"], "approver": "ops"})
        reg = ToolRegistry()
        reg.register(Tool(name="plain", description="no op schema",
                          input_schema={"type": "object", "properties": {}}, fn=lambda a: "x"))
        assert apply_governed_connectors(reg) == []  # left ungoverned, not crashed

    def test_unknown_connector_skipped(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "1")
        import maverick.config as cfg
        monkeypatch.setattr(cfg, "get_governed_connectors",
                            lambda: {"enable": True, "connectors": ["absent"], "approver": "ops"})
        assert apply_governed_connectors(ToolRegistry()) == []


class TestThroughRegistryRun:
    def test_governed_write_runs_via_registry(self, monkeypatch):
        # End-to-end through ToolRegistry.run (async), proving the wrapper's sync
        # fn executes correctly in the agent's tool-execution path.
        monkeypatch.setenv("MAVERICK_GOVERNED_APPROVER", "ops")
        calls = []
        reg = ToolRegistry()
        reg.register(wrap_connector_tool(_fake_tool(calls)))
        out = asyncio.run(reg.run("acme", {"op": "put", "path": "/x"}))
        assert "DID put /x" in out and calls[0]["confirm"] is True

    def test_governance_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "1")
        assert governance_enabled() is True
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "0")
        assert governance_enabled() is False


class TestEnterpriseBoundary:
    def test_enterprise_is_mandatory_even_with_disable_override(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_GOVERNED_CONNECTORS", "0")
        monkeypatch.setattr("maverick.governed_tools._enterprise_mode", lambda: True)
        assert governance_enabled() is True

    def test_enterprise_discovers_every_compatible_connector(self, monkeypatch):
        import maverick.config as cfg

        monkeypatch.setattr("maverick.governed_tools._enterprise_mode", lambda: True)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": False, "connectors": [], "approver": ""},
        )
        reg = ToolRegistry()
        reg.register(_fake_tool([]))
        reg.register(Tool(
            name="plain", description="read-only", parallel_safe=True,
            input_schema={"type": "object", "properties": {}}, fn=lambda _a: "ok",
        ))
        assert apply_governed_connectors(reg) == ["acme"]
        assert "transaction-receipted" in reg.get("acme").description
        assert "governed" not in reg.get("plain").description

    def test_enterprise_governs_bespoke_writer_without_confirm_field(
        self, monkeypatch,
    ):
        import maverick.config as cfg
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr("maverick.governed_tools._enterprise_mode", lambda: True)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": False, "connectors": [], "approver": ""},
        )
        monkeypatch.setattr(
            consent,
            "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )
        receipts = []
        monkeypatch.setattr(
            actions,
            "record_tool_lineage",
            lambda *a, **k: receipts.append(k) or True,
        )
        calls = []
        reg = ToolRegistry(principal="user:alice")
        reg.register(_teams_style_tool(calls))

        assert apply_governed_connectors(reg, goal_id=31) == ["teams"]
        out = reg.get("teams").fn({"op": "send", "text": "production notice"})

        assert out == "TEAMS send"
        assert calls == [{
            "op": "send", "text": "production notice", "confirm": True,
        }]
        assert [receipt["phase"] for receipt in receipts] == ["PREPARE", "COMMIT"]
        assert all(receipt["actor"] == "user:alice" for receipt in receipts)

    def test_bespoke_read_operation_passes_through_without_approval(self, monkeypatch):
        import maverick.config as cfg
        import maverick.safety.consent as consent

        monkeypatch.setattr("maverick.governed_tools._enterprise_mode", lambda: True)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": False, "connectors": [], "approver": ""},
        )
        monkeypatch.setattr(
            consent,
            "require_consent",
            lambda *a, **k: pytest.fail("read operation requested approval"),
        )
        calls = []
        reg = ToolRegistry()
        reg.register(_teams_style_tool(calls))
        assert apply_governed_connectors(reg) == ["teams"]

        assert reg.get("teams").fn({"op": "history"}) == "TEAMS history"
        assert calls == [{"op": "history"}]

    @pytest.mark.parametrize(
        ("name", "operation"),
        [
            ("asana", "task_create"),
            ("s3", "put"),
            ("ses", "send"),
            ("redis", "set"),
            ("clickup", "task_create"),
            ("replicate", "run"),
        ],
    )
    def test_custom_connector_writes_cannot_fall_through(
        self, monkeypatch, name, operation,
    ):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg

        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": [name], "approver": ""},
        )
        calls = []
        wrapped = wrap_connector_tool(
            _op_tool(name, [operation], calls, confirm=name != "replicate"),
        )

        out = wrapped.fn({"op": operation, "path": "/production"})

        assert out.startswith("REFUSED (governed)")
        assert calls == []

    @pytest.mark.parametrize(
        ("name", "operation"),
        [
            ("asana", "task_create"), ("asana", "task_complete"),
            ("calendly", "cancel"), ("clickup", "task_create"),
            ("cloudflare", "dns_create"), ("cloudflare", "dns_update"),
            ("cloudflare", "dns_delete"), ("cloudflare", "purge"),
            ("confluence", "page_create"), ("confluence", "page_update"),
            ("databricks", "job_run"), ("datadog", "submit_event"),
            ("datadog", "submit_metric"), ("dropbox", "upload"),
            ("dropbox", "share"), ("elasticsearch", "index"),
            ("github_actions", "dispatch"), ("github_actions", "cancel"),
            ("gmail", "send"), ("home_assistant", "call_service"),
            ("hubspot", "contact_create"), ("hubspot", "contact_update"),
            ("lambda", "invoke"), ("mongodb", "insert"),
            ("msgraph", "send_mail"), ("pagerduty", "acknowledge"),
            ("pagerduty", "resolve"), ("pagerduty", "trigger"),
            ("redis", "set"), ("redis", "lpush"), ("redis", "publish"),
            ("replicate", "run"), ("replicate", "cancel"),
            ("sentry", "resolve"), ("ses", "send"),
            ("shopify", "refund_create"), ("sns", "publish"),
            ("sns", "sms"), ("sns", "subscribe"), ("sns", "unsubscribe"),
            ("spotify", "play"), ("spotify", "pause"),
            ("stripe", "refund_create"), ("trello", "card_create"),
            ("trello", "card_move"), ("trello", "comment"),
            ("twilio", "sms_send"), ("twilio", "call_create"),
            ("vercel", "cancel"), ("zoom", "meeting_create"),
            ("zoom", "meeting_delete"), ("s3", "put"),
        ],
    )
    def test_reviewed_mutating_inventory_is_governed(self, name, operation):
        tool = _op_tool(name, [operation], [], confirm=name != "replicate")
        assert _is_governed_write(tool, {"op": operation}) is True

    def test_confirm_schema_unknown_operation_fails_closed(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg

        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["future"], "approver": ""},
        )
        calls = []
        wrapped = wrap_connector_tool(
            _op_tool("future", ["future_mutation"], calls),
        )

        assert wrapped.fn({"op": "future_mutation"}).startswith("REFUSED (governed)")
        assert calls == []

    def test_custom_connector_read_still_passes_without_approval(self, monkeypatch):
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent,
            "require_consent",
            lambda *a, **k: pytest.fail("safe read requested approval"),
        )
        calls = []
        wrapped = wrap_connector_tool(
            _op_tool("asana", ["task_get", "task_create"], calls),
            enterprise=True,
        )

        assert wrapped.fn({"op": "task_get"}) == "asana:task_get"
        assert calls == [{"op": "task_get"}]

    def test_all_sql_is_governed_because_lexical_reads_are_not_pure(
        self, monkeypatch,
    ):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg

        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["database"], "approver": ""},
        )
        calls = []
        tool = _op_tool("database", ["query"], calls)
        wrapped = wrap_connector_tool(tool)

        for statement in (
            "SELECT 1",
            "SELECT 'owned' INTO OUTFILE '/tmp/pwn'",
            "DELETE FROM customers",
            "SELECT 1; DELETE FROM customers",
            "PRAGMA user_version = 7",
            "/*M! DELETE FROM customers */ SELECT 1",
        ):
            assert wrapped.fn({
                "op": "query", "sql": statement,
            }).startswith("REFUSED (governed)"), statement
        assert calls == []

    def test_lambda_dryrun_is_non_effecting_but_live_invoke_is_governed(
        self, monkeypatch,
    ):
        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        import maverick.config as cfg

        monkeypatch.setattr(
            cfg, "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["lambda"], "approver": ""},
        )
        calls = []
        wrapped = wrap_connector_tool(_op_tool("lambda", ["invoke"], calls))

        assert wrapped.fn({
            "op": "invoke", "invocation_type": "DryRun",
        }) == "lambda:invoke"
        assert wrapped.fn({
            "op": "invoke", "invocation_type": "Event",
        }).startswith("REFUSED (governed)")
        assert calls == [{
            "op": "invoke", "invocation_type": "DryRun",
        }]

    def test_enterprise_rejects_invalid_explicit_policy(self, monkeypatch):
        import maverick.config as cfg

        monkeypatch.setattr("maverick.governed_tools._enterprise_mode", lambda: True)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["missing"], "approver": ""},
        )
        with pytest.raises(RuntimeError, match="missing or incompatible"):
            apply_governed_connectors(ToolRegistry())

    def test_unreadable_enterprise_policy_cannot_remove_wrappers(self, monkeypatch):
        import maverick.config as cfg
        import maverick.enterprise as enterprise

        monkeypatch.setattr(
            cfg,
            "config_source_errors",
            lambda **_kwargs: {"config.toml": "unreadable"},
        )
        monkeypatch.setattr(enterprise, "enterprise_enabled", lambda: False)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": False, "connectors": [], "approver": ""},
        )
        registry = ToolRegistry()
        registry.register(_fake_tool([]))

        assert governance_enabled() is True
        assert apply_governed_connectors(registry) == ["acme"]
        assert "[governed:" in registry.get("acme").description

    def test_enterprise_policy_exception_cannot_remove_wrappers(self, monkeypatch):
        import maverick.config as cfg
        import maverick.enterprise as enterprise

        monkeypatch.setattr(cfg, "config_source_errors", lambda **_kwargs: {})
        monkeypatch.setattr(
            enterprise,
            "enterprise_enabled",
            lambda: (_ for _ in ()).throw(OSError("policy unavailable")),
        )

        assert governance_enabled() is True

    def test_fresh_human_consent_and_prepare_commit_receipts(self, monkeypatch):
        import maverick.config as cfg
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.delenv("MAVERICK_GOVERNED_APPROVER", raising=False)
        monkeypatch.setattr(
            cfg,
            "get_governed_connectors",
            lambda: {"enable": True, "connectors": ["acme"], "approver": ""},
        )
        consent_calls = []

        def _consent(*args, **kwargs):
            consent_calls.append((args, kwargs))
            return SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            )

        receipts = []

        def _record(*args, **kwargs):
            receipts.append((args, kwargs))
            return True

        monkeypatch.setattr(consent, "require_consent", _consent)
        monkeypatch.setattr(actions, "record_tool_lineage", _record)
        calls = []
        wrapped = wrap_connector_tool(
            _fake_tool(calls), goal_id=17, actor="user:alice", enterprise=True,
        )
        out = wrapped.fn({"op": "post", "path": "/accounts", "body": {"n": 1}})

        assert "DID post /accounts" in out
        assert calls[0]["confirm"] is True
        assert consent_calls[0][1]["allow_auto_approve"] is False
        assert consent_calls[0][1]["consult_ledger"] is False
        assert consent_calls[0][1]["scope"].startswith("/accounts#sha256=")
        assert "request_sha256=" in consent_calls[0][1]["detail"]
        assert '"body": {"n": 1}' in consent_calls[0][1]["detail"]
        assert [entry[1]["phase"] for entry in receipts] == ["PREPARE", "COMMIT"]
        assert receipts[0][1]["strict"] is True
        assert receipts[0][1]["transaction_id"] == receipts[1][1]["transaction_id"]
        assert receipts[0][1]["actor"] == "user:alice"
        assert receipts[0][1]["approver"] == "user:reviewer"
        assert receipts[0][0][0] == 17

    def test_enterprise_refuses_an_unattributed_dashboard_approval(
        self, monkeypatch,
    ):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent,
            "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="",
            ),
        )
        receipts = []
        monkeypatch.setattr(
            actions, "record_tool_lineage",
            lambda *a, **k: receipts.append(k) or True,
        )
        calls = []

        out = wrap_connector_tool(_fake_tool(calls), enterprise=True).fn({
            "op": "post", "path": "/accounts", "body": {"n": 1},
        })

        assert out.startswith("REFUSED (governed)")
        assert "authenticated approver identity" in out
        assert receipts == []
        assert calls == []

    def test_approval_binds_a_frozen_request_and_actual_approver(
        self, monkeypatch,
    ):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setenv("MAVERICK_GOVERNED_APPROVER", "configured-label")
        request = {
            "op": "post", "path": "/accounts", "body": {"role": "viewer"},
        }

        def _consent(*_args, **_kwargs):
            # Simulate a caller retaining and mutating the submitted object
            # while the approval round trip is in progress.
            request["body"]["role"] = "admin"
            return SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            )

        receipts = []
        monkeypatch.setattr(consent, "require_consent", _consent)
        monkeypatch.setattr(
            actions, "record_tool_lineage",
            lambda *a, **k: receipts.append((a, k)) or True,
        )
        calls = []

        out = wrap_connector_tool(_fake_tool(calls), enterprise=True).fn(request)

        assert out.startswith("DID post")
        assert calls[0]["body"] == {"role": "viewer"}
        assert receipts[0][1]["approver"] == "user:reviewer"
        assert receipts[0][0][2]["body"] == {"role": "viewer"}

    def test_prepare_failure_refuses_before_external_effect(self, monkeypatch):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent, "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )
        monkeypatch.setattr(actions, "record_tool_lineage", lambda *a, **k: False)
        calls = []
        out = wrap_connector_tool(_fake_tool(calls), enterprise=True).fn({
            "op": "delete", "path": "/accounts/1",
        })
        assert out.startswith("REFUSED (governed)")
        assert "PREPARE" in out
        assert calls == []

    def test_missing_commit_receipt_is_indeterminate_not_retryable(self, monkeypatch):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent, "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )

        def _record(*args, **kwargs):
            return kwargs["phase"] == "PREPARE"

        monkeypatch.setattr(actions, "record_tool_lineage", _record)
        calls = []
        out = wrap_connector_tool(_fake_tool(calls), enterprise=True).fn({
            "op": "put", "path": "/accounts/1",
        })
        assert calls and calls[-1]["confirm"] is True
        assert out.startswith("INDETERMINATE (governed)")
        assert "do not retry automatically" in out

    def test_strict_commit_receipt_exception_is_indeterminate(self, monkeypatch):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent, "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )

        def _record(*args, **kwargs):
            if kwargs["phase"] == "COMMIT":
                raise OSError("ledger disk unavailable")
            return True

        monkeypatch.setattr(actions, "record_tool_lineage", _record)
        calls = []
        out = wrap_connector_tool(_fake_tool(calls), enterprise=True).fn({
            "op": "post", "path": "/accounts",
        })
        assert calls and calls[0]["confirm"] is True
        assert out.startswith("INDETERMINATE (governed)")
        assert "do not retry automatically" in out

    @pytest.mark.parametrize("failure_kind", ["raise", "error-result"])
    def test_connector_failure_after_prepare_is_indeterminate(
        self, monkeypatch, failure_kind,
    ):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent, "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )
        receipts = []

        def _record(*args, **kwargs):
            receipts.append(kwargs["phase"])
            return True

        monkeypatch.setattr(actions, "record_tool_lineage", _record)

        def _fail(_args):
            if failure_kind == "raise":
                raise TimeoutError("response lost")
            return "ERROR: upstream connection reset"

        tool = Tool(
            name="acme", description="Acme", input_schema=_SCHEMA, fn=_fail,
        )
        out = wrap_connector_tool(tool, enterprise=True).fn({
            "op": "delete", "path": "/accounts/1",
        })
        assert out.startswith("INDETERMINATE (governed)")
        assert "reconcile" in out
        assert receipts == ["PREPARE", "INDETERMINATE"]

    @pytest.mark.parametrize("failure_kind", ["raise", "error-result"])
    def test_connector_failure_never_returns_secret_detail_to_model(
        self, monkeypatch, failure_kind,
    ):
        import maverick.governed_actions as actions
        import maverick.safety.consent as consent

        monkeypatch.setattr(
            consent, "require_consent",
            lambda *a, **k: SimpleNamespace(
                granted=True, source="dashboard", actor="user:reviewer",
            ),
        )
        monkeypatch.setattr(actions, "record_tool_lineage", lambda *a, **k: True)
        secret = "postgres://admin:super-secret@db.internal/customer"  # pragma: allowlist secret

        def _fail(_args):
            if failure_kind == "raise":
                raise RuntimeError(secret)
            return f"ERROR: upstream rejected DSN {secret}"

        out = wrap_connector_tool(Tool(
            name="acme", description="Acme", input_schema=_SCHEMA, fn=_fail,
        ), enterprise=True).fn({"op": "delete", "path": "/accounts/1"})

        assert out.startswith("INDETERMINATE (governed)")
        assert "super-secret" not in out
        assert "postgres://" not in out
