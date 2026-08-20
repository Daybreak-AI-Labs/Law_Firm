"""Regression: file/path/source tools must not crash on non-string model args.

Each of these takes a path/patch/source straight from the model; a non-string
value (int/list/dict) used to raise an uncaught AttributeError/TypeError.
"""
from __future__ import annotations


class _Sandbox:
    def __init__(self, workdir):
        self.workdir = str(workdir)




def test_apply_patch_non_string_patch(tmp_path):
    from maverick.tools.apply_patch import apply_patch
    fn = apply_patch(_Sandbox(tmp_path)).fn
    for v in (5, 1.5, True, [1, 2, 3], {"a": 1}):
        out = fn({"patch": v})
        assert isinstance(out, str)
        assert out.startswith("ERROR")
