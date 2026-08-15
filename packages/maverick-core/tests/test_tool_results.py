"""Shared semantic contract for string-based tool results."""
from maverick.tool_results import (
    ToolResultState,
    classify_tool_result,
    tool_result_failed,
)


def test_classifies_reserved_result_states():
    assert classify_tool_result("created") is ToolResultState.SUCCEEDED
    assert classify_tool_result("ERROR: timeout") is ToolResultState.FAILED
    assert classify_tool_result("REFUSED (governed): approval missing") is ToolResultState.REFUSED
    assert classify_tool_result("⚠ BLOCKED by hook") is ToolResultState.REFUSED
    assert classify_tool_result("BLOCKED by Shield") is ToolResultState.REFUSED
    assert classify_tool_result("DRY RUN: would POST /accounts") is ToolResultState.PREVIEW
    assert classify_tool_result("INDETERMINATE: may have committed") is ToolResultState.INDETERMINATE


def test_classification_looks_through_security_frame():
    framed = '<tool_output trust="untrusted">\nINDETERMINATE: receipt lost\n</tool_output>'
    assert classify_tool_result(framed) is ToolResultState.INDETERMINATE
    assert tool_result_failed(framed) is True


def test_natural_language_is_not_misclassified():
    assert tool_result_failed("The customer refused the offer") is False


def test_workflow_stops_on_preview_refusal_or_indeterminate_result():
    from maverick.workflow import Step, Workflow

    class Registry:
        def __init__(self, result):
            self.result = result
            self.calls = []

        async def run(self, name, args):
            self.calls.append(name)
            return self.result

    for reserved in (
        "DRY RUN: would write without confirm",
        "REFUSED (governed): approval missing",
        "INDETERMINATE (governed): write may have committed",
    ):
        registry = Registry(reserved)
        result = Workflow([
            Step("write", "connector"),
            Step("notify", "notify", depends_on=["write"]),
        ]).run(registry)
        assert result.failed is True
        assert registry.calls == ["connector"]
        assert result.steps[0].error == reserved
