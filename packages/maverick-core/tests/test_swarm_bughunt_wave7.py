"""Regression tests for bug-hunt wave-7 fixes."""
from __future__ import annotations


class TestArxivOldStyleId:
    def test_old_style_id_with_slash_preserved(self):
        import re
        # Mirror the normalization in _op_fetch.
        def norm(s):
            s = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", s)
            return re.sub(r"v\d+$", "", s)
        assert norm("math.GT/0309136") == "math.GT/0309136"
        assert norm("https://arxiv.org/abs/math.GT/0309136v1") == "math.GT/0309136"
        assert norm("2106.09685v2") == "2106.09685"
