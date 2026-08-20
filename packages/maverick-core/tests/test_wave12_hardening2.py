"""Retained cache, budget, and Anthropic SDK robustness regressions."""
from __future__ import annotations

import pickle


class TestCacheTtlNormalization:
    def test_trailing_space_normalized(self):
        from maverick.budget import _cache_write_mult_from_ttl
        assert _cache_write_mult_from_ttl("1h ") == 2.0
        assert _cache_write_mult_from_ttl(" 1h") == 2.0

    def test_uppercase_normalized(self):
        from maverick.budget import _cache_write_mult_from_ttl
        assert _cache_write_mult_from_ttl("1H") == 2.0

    def test_duration_parsing_handles_unknown_strings(self):
        from maverick.budget import _cache_write_mult_from_ttl
        # 30 minutes > 5m → 1h rate
        assert _cache_write_mult_from_ttl("30m") == 2.0
        # 2h > 1h → 1h rate (the upper tier)
        assert _cache_write_mult_from_ttl("2h") == 2.0
        # 7200s = 2h → 1h rate
        assert _cache_write_mult_from_ttl("7200s") == 2.0
        # 5m or below → 5m rate
        assert _cache_write_mult_from_ttl("5m") == 1.25
        assert _cache_write_mult_from_ttl("1m") == 1.25

    def test_unknown_garbage_defaults_to_5m(self):
        from maverick.budget import _cache_write_mult_from_ttl
        assert _cache_write_mult_from_ttl("forever") == 1.25
        assert _cache_write_mult_from_ttl("") == 1.25
        assert _cache_write_mult_from_ttl(None) == 1.25


class TestBudgetPickling:
    def test_round_trip_preserves_counters(self):
        from maverick.budget import Budget
        from maverick.llm import MODEL_SONNET

        b = Budget(max_dollars=100.0)
        b.record_tokens(10_000, 1000, model=MODEL_SONNET)
        original_dollars = b.dollars

        # multiprocessing.Pool ships via pickle.
        data = pickle.dumps(b)
        b2 = pickle.loads(data)
        assert b2.input_tokens == 10_000
        assert b2.output_tokens == 1000
        assert abs(b2.dollars - original_dollars) < 0.001
        # The new Budget has its own monotonic clock + lock.
        assert b2.elapsed() >= 0
        b2.record_tokens(1000, 100, model=MODEL_SONNET)
        # Both lock and counter survived.
        assert b2.input_tokens == 11_000


    def test_round_trip_preserves_elapsed_for_wall_cap(self):
        from maverick.budget import Budget, BudgetExceeded

        b = Budget(max_wall_seconds=0.01)
        # Exhaust wall-time budget before serialization.
        while b.elapsed() <= 0.02:
            pass
        try:
            b.check()
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("pre-pickle budget should already be exceeded")

        b2 = pickle.loads(pickle.dumps(b))
        # Must remain exceeded after unpickle (no timer reset bypass).
        try:
            b2.check()
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("post-unpickle wall cap bypassed")


class TestAnthropicProviderRobust:
    def test_resp_usage_none_does_not_crash(self):
        from maverick.budget import Budget
        from maverick.providers.anthropic_provider import AnthropicClient

        client = AnthropicClient.__new__(AnthropicClient)

        class _Resp:
            content = []
            usage = None
            stop_reason = "end_turn"

        budget = Budget(max_dollars=10.0)
        # Must not raise even when usage is None entirely.
        resp = client._parse_response(_Resp(), budget, model="claude-sonnet-4-6")
        assert resp.text == ""
        assert budget.dollars == 0.0

    def test_string_usage_values_dont_crash(self):
        from maverick.budget import Budget
        from maverick.providers.anthropic_provider import AnthropicClient

        client = AnthropicClient.__new__(AnthropicClient)

        class _Usage:
            input_tokens = "100"
            output_tokens = "20"
            cache_creation_input_tokens = None
            cache_read_input_tokens = None

        class _Resp:
            content = []
            usage = _Usage()
            stop_reason = "end_turn"

        budget = Budget(max_dollars=10.0)
        client._parse_response(_Resp(), budget, model="claude-sonnet-4-6")
        # Should have coerced "100"→100; ~$0.001 spent.
        assert budget.input_tokens == 100

    def test_string_usage_garbage_does_not_crash(self):
        from maverick.budget import Budget
        from maverick.providers.anthropic_provider import AnthropicClient

        client = AnthropicClient.__new__(AnthropicClient)

        class _Usage:
            input_tokens = "not_a_number"
            output_tokens = 50

        class _Resp:
            content = []
            usage = _Usage()
            stop_reason = "end_turn"

        budget = Budget(max_dollars=10.0)
        # Must not raise — defensive _safe_int returns 0.
        client._parse_response(_Resp(), budget, model="claude-sonnet-4-6")
        assert budget.input_tokens == 0
        assert budget.output_tokens == 50

    def test_duplicate_tool_names_no_crash(self):
        from maverick.providers.anthropic_provider import _cached_tools
        tools = [
            {"name": "shell", "input_schema": {}},
            {"name": "shell", "input_schema": {}},  # duplicate
            {"name": None, "input_schema": {}},     # malformed
        ]
        # Must not raise on None name.
        out = _cached_tools(tools)
        assert len(out) == 3
        assert "cache_control" in out[-1]


