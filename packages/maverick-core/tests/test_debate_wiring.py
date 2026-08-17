"""`maverick debate QUESTION` — a CLI entry point for the debate primitive.

run_debate (two LLM debaters argue, a judge picks a winner) existed and was
tested but had no way to invoke it. The command builds a proponent + skeptic
from the configured LLM, runs the debate, and prints the judged verdict.
"""
from __future__ import annotations


class TestTranscriptWindow:
    def test_short_transcript_rendered_whole(self):
        from maverick.debate import DebateTurn, _build_messages_for_turn
        transcript = [DebateTurn(speaker=f"s{i}", text=f"arg {i}") for i in range(3)]
        msg = _build_messages_for_turn("Q?", transcript, "s0", "pro")[0]["content"]
        assert "arg 0" in msg and "elided" not in msg

    def test_long_transcript_windowed(self):
        # Every speaker used to receive the FULL growing transcript each round
        # (O((P*R)^2) input tokens); only the last window turns render now.
        from maverick.debate import (
            _TRANSCRIPT_WINDOW_TURNS,
            DebateTurn,
            _build_messages_for_turn,
        )
        transcript = [DebateTurn(speaker=f"s{i}", text=f"arg {i}") for i in range(20)]
        msg = _build_messages_for_turn("Q?", transcript, "s0", "pro")[0]["content"]
        assert "arg 19" in msg
        assert "arg 0" not in msg
        assert f"({20 - _TRANSCRIPT_WINDOW_TURNS} earlier turn(s) elided)" in msg
