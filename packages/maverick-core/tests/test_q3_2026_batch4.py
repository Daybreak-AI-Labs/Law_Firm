"""Q3 2026 batch 4: Kubernetes sandbox, HuggingFace tool,
context compactor, OTEL span wiring."""
from __future__ import annotations

# ---------- Context compactor ----------

def test_compactor_passes_through_under_budget():
    from maverick.context_compactor import compact
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    out = compact(msgs, target_tokens=1000)
    assert out.messages == msgs
    assert out.dropped == []
    assert out.kept_marker is None


def test_compactor_drops_least_relevant_when_over_budget():
    from maverick.context_compactor import compact
    msgs = []
    # 10 old turns about completely unrelated topics (low relevance).
    for i in range(10):
        msgs.append({"role": "user",
                     "content": f"old topic {i}: " + "padding " * 50})
        msgs.append({"role": "assistant",
                     "content": f"reply about topic {i}: " + "filler " * 50})
    # Current user message about a different topic.
    msgs.append({"role": "user", "content": "current question about feature X"})
    msgs.append({"role": "assistant", "content": "reply about feature X"})

    out = compact(msgs, target_tokens=200, preserve_tail=2)
    assert out.tokens_after <= out.tokens_before
    assert len(out.dropped) > 0
    assert out.kept_marker is not None
    assert "compacted" in out.kept_marker
    # The last two turns are always preserved verbatim.
    assert out.messages[-2:] == msgs[-2:]


def test_compactor_keeps_most_relevant_older_turn():
    from maverick.context_compactor import compact
    msgs = [
        # Highly relevant older turn (shares vocab with the query)
        {"role": "user", "content": "tell me about feature X and its history"},
        {"role": "assistant", "content": "feature X dates back to 2024 ..."},
        # Filler older turns — heavy enough to push over the budget.
        *[
            {"role": "user",
             "content": f"weather report number {i} " + ("padding " * 60)}
            for i in range(8)
        ],
        # The tail / current focus
        {"role": "user", "content": "now explain feature X again briefly"},
    ]
    out = compact(msgs, target_tokens=80, preserve_tail=1)
    # Drop happened.
    assert out.dropped
    # The relevant first turn survives (in either order).
    survived = [m for m in out.messages if "history" in str(m.get("content", ""))]
    assert survived, "the most-relevant old turn should be kept"


def test_compactor_empty_history():
    from maverick.context_compactor import compact
    r = compact([], target_tokens=100)
    assert r.messages == []
    assert r.dropped == []
    assert r.tokens_before == r.tokens_after == 0


def test_compactor_estimate_tokens_increases_with_text():
    from maverick.context_compactor import estimate_tokens
    short = estimate_tokens([{"role": "user", "content": "hi"}])
    long = estimate_tokens([{"role": "user", "content": "hi " * 500}])
    assert long > short


# ---------- OTEL span wiring ----------

def test_tool_dispatch_opens_span(monkeypatch):
    """ToolRegistry.run wraps the call in a trace_span context."""
    import asyncio

    from maverick.tools import Tool, ToolRegistry

    seen = {"name": None, "attrs": None}

    class _FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_trace_span(name, attributes=None):
        seen["name"] = name
        seen["attrs"] = attributes
        return _FakeCtx()

    import maverick.observability as obs
    monkeypatch.setattr(obs, "trace_span", _fake_trace_span)

    reg = ToolRegistry()
    reg.register(Tool(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        fn=lambda args: "ok",
    ))
    out = asyncio.run(reg.run("echo", {}))
    assert out == "ok"
    assert seen["name"] == "tool.run"
    # The span carries the back-compat tool.name plus the GenAI-semconv tool
    # attributes (gen_ai.operation.name/tool.name/tool.type) merged in.
    assert seen["attrs"]["tool.name"] == "echo"
    assert seen["attrs"]["gen_ai.operation.name"] == "execute_tool"
    assert seen["attrs"]["gen_ai.tool.name"] == "echo"


def test_llm_complete_opens_span(monkeypatch):
    from maverick.llm import LLM

    seen = {"calls": []}

    class _FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_trace_span(name, attributes=None):
        seen["calls"].append((name, attributes))
        return _FakeCtx()

    import maverick.observability as obs
    monkeypatch.setattr(obs, "trace_span", _fake_trace_span)

    class _FakeResp:
        text = "ok"
        thinking = None
        tool_calls = []
        stop_reason = "end_turn"
        cache_creation_tokens = 0
        cache_read_tokens = 0
        raw = None
        thinking_blocks = []
        thinking_signature = None

    class _FakeClient:
        def complete(self, **kwargs):
            return _FakeResp()

    llm = LLM(model="anthropic:claude-haiku-4-5-20251001", api_key="dummy")
    monkeypatch.setattr(llm, "_get_client", lambda provider: _FakeClient())

    llm.complete(system="s", messages=[{"role": "user", "content": "hi"}])
    assert seen["calls"]
    name, attrs = seen["calls"][0]
    # The LLM span now follows the OTel GenAI semantic conventions: span name
    # "<operation> <model>" plus gen_ai.* attributes. The legacy llm.provider
    # attribute is kept alongside for back-compat. (Assert on the convention
    # shape, not the exact model-id spelling, so the test doesn't couple to
    # how the id is normalized.)
    assert name.startswith("chat claude-haiku-4-5")
    assert attrs and attrs.get("gen_ai.system") == "anthropic"
    assert attrs.get("gen_ai.request.model") == name.split(" ", 1)[1]
    assert attrs.get("llm.provider") == "anthropic"
