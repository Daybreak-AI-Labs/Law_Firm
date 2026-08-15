"""A corpus of SPEC-AMBIGUOUS Python tasks for the governed self-improvement
uplift experiment. Each task's specification is deliberately terse: a reasonable
first attempt often picks the WRONG convention (Python's default rounding,
floor-division, non-overlapping count, str.title lowercasing, ...), and only the
tests pin the exact required behaviour.

Two disjoint test sets:
  * ``public_tests``  -- reveal the convention on one input; the agent MAY run
    these to self-repair (exactly how a real ticket's visible tests work);
  * ``hidden_tests``  -- check the SAME convention on DIFFERENT inputs; used only
    for grading (held-out).

A one-shot agent never sees the public tests, so it guesses the convention and
fails the hidden ones ~a third of the time. A self-repairing agent runs the
public tests, learns the convention from the failure, and generalises to hidden.
That gap -- honest, on real code -- is the headroom. The starter stub is a no-op
that fails everything, so the agent implements from the spec.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BugTask:
    instance_id: str
    problem: str
    buggy_code: str
    entry_point: str
    public_tests: str
    hidden_tests: str


def _stub(name: str, args: str) -> str:
    return f"def {name}({args}):\n    pass\n"


TASKS: list[BugTask] = [
    BugTask(
        "round_half_up", "Round x to the nearest integer.", _stub("f", "x"), "f",
        "assert f(2.5) == 3\n",
        "assert f(3.5) == 4\nassert f(-2.5) == -3\nassert f(0.5) == 1\n"),
    BugTask(
        "trunc_div", "Integer-divide a by b (truncate the result toward zero).",
        _stub("f", "a, b"), "f",
        "assert f(-7, 2) == -3\n",
        "assert f(7, -2) == -3\nassert f(-9, 4) == -2\nassert f(9, 4) == 2\n"),
    BugTask(
        "lower_median", "Return the median of the list; for even length take the LOWER middle.",
        _stub("f", "xs"), "f",
        "assert f([1,2,3,4]) == 2\n",
        "assert f([4,1,3,2,6,5]) == 3\nassert f([7,1]) == 1\n"),
    BugTask(
        "cstyle_mod", "Return a mod b where the result takes the sign of the dividend a.",
        _stub("f", "a, b"), "f",
        "assert f(-1, 3) == -1\n",
        "assert f(-7, 3) == -1\nassert f(7, -3) == 1\nassert f(8, 3) == 2\n"),
    BugTask(
        "count_overlap", "Count how many times sub occurs in s, counting OVERLAPPING matches.",
        _stub("f", "s, sub"), "f",
        "assert f('aaa', 'aa') == 2\n",
        "assert f('abababa', 'aba') == 3\nassert f('aaaa', 'aa') == 3\n"),
    BugTask(
        "title_keep_caps", "Capitalise the first character of each word; leave the rest UNCHANGED.",
        _stub("f", "s"), "f",
        "assert f('hi thERE') == 'Hi ThERE'\n",
        "assert f('a bC dEf') == 'A BC DEf'\nassert f('the iOS app') == 'The IOS App'\n"),
    BugTask(
        "inclusive_range", "Return the list of integers from a to b, inclusive of both ends.",
        _stub("f", "a, b"), "f",
        "assert f(1, 3) == [1,2,3]\n",
        "assert f(5, 5) == [5]\nassert f(2, 6) == [2,3,4,5,6]\n"),
    BugTask(
        "one_based_index", "Return the 1-based index of the first occurrence of t in xs, or 0 if absent.",
        _stub("f", "xs, t"), "f",
        "assert f([9,8,7], 8) == 2\n",
        "assert f([1,2,3], 3) == 3\nassert f([1,2], 5) == 0\n"),
    BugTask(
        "dedup_keep_last", "Remove duplicates keeping the LAST occurrence of each value; preserve order.",
        _stub("f", "xs"), "f",
        "assert f([1,2,1,3]) == [2,1,3]\n",
        "assert f([1,2,3,2,1]) == [3,2,1]\nassert f([4,4,4]) == [4]\n"),
    BugTask(
        "ci_sort_stable", "Sort the strings case-insensitively; keep equal keys in their original order.",
        _stub("f", "xs"), "f",
        "assert f(['b','A','a']) == ['A','a','b']\n",
        "assert f(['B','b','A','a']) == ['A','a','B','b']\nassert f(['Cat','cat','ant']) == ['ant','Cat','cat']\n"),
    BugTask(
        "truncate_ellipsis", "If len(s) > n, truncate so the result (including a '...' suffix) is exactly n chars.",
        _stub("f", "s, n"), "f",
        "assert f('hello world', 8) == 'hello...'\n",
        "assert f('abcdefgh', 5) == 'ab...'\nassert f('hi', 5) == 'hi'\n"),
    BugTask(
        "clusive_slice", "Return xs[i..j] INCLUSIVE of both indices i and j.",
        _stub("f", "xs, i, j"), "f",
        "assert f([0,1,2,3,4], 1, 3) == [1,2,3]\n",
        "assert f([0,1,2,3,4], 0, 0) == [0]\nassert f([5,6,7,8], 1, 3) == [6,7,8]\n"),
    BugTask(
        "sign", "Return the sign of x as -1, 0, or 1.", _stub("f", "x"), "f",
        "assert f(-5) == -1\n",
        "assert f(0) == 0\nassert f(9) == 1\nassert f(-0.1) == -1\n"),
    BugTask(
        "second_smallest", "Return the second SMALLEST distinct value in the list.",
        _stub("f", "xs"), "f",
        "assert f([5,1,1,2]) == 2\n",
        "assert f([3,3,3,1]) == 3\nassert f([9,7,8]) == 8\n"),
    BugTask(
        "flatten_one", "Flatten the list by ONE level only (do not recurse into deeper lists).",
        _stub("f", "xss"), "f",
        "assert f([[1,2],[3,[4]]]) == [1,2,3,[4]]\n",
        "assert f([[1],[2,3]]) == [1,2,3]\nassert f([]) == []\n"),
    BugTask(
        "round_to_even", "Round x to the nearest integer; on a .5 tie round to the nearest EVEN integer.",
        _stub("f", "x"), "f",
        "assert f(2.5) == 2\n",
        "assert f(3.5) == 4\nassert f(0.5) == 0\nassert f(1.5) == 2\n"),
    BugTask(
        "split_keep_empty", "Split s on commas, KEEPING empty fields.",
        _stub("f", "s"), "f",
        "assert f('a,,b') == ['a','','b']\n",
        "assert f(',x,') == ['','x','']\nassert f('') == ['']\n"),
    BugTask(
        "nth_from_end", "Return the n-th element from the END of the list (n=1 is the last).",
        _stub("f", "xs, n"), "f",
        "assert f([1,2,3,4], 1) == 4\n",
        "assert f([1,2,3,4], 2) == 3\nassert f([9], 1) == 9\n"),
    BugTask(
        "capitalize_sentences", "Capitalise the first letter of each sentence (sentences end with '. ').",
        _stub("f", "s"), "f",
        "assert f('hi there. how are you') == 'Hi there. How are you'\n",
        "assert f('one. two. three') == 'One. Two. Three'\n"),
    BugTask(
        "clamp_exclusive_hi", "Clamp x to [lo, hi) -- lo inclusive, hi EXCLUSIVE (max allowed is hi-1 for ints).",
        _stub("f", "x, lo, hi"), "f",
        "assert f(10, 0, 10) == 9\n",
        "assert f(-3, 0, 10) == 0\nassert f(5, 0, 10) == 5\n"),
]
