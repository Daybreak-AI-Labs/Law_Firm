"""The production-posture ratchet.

19,000 tests pass with ``MAVERICK_SECURE_DEFAULT=0`` — a configuration no
customer runs. Under the real posture (audit signing on, at-rest encryption
on, consent fail-closed, tool-risk ceiling applied) **91 of 15,869 fail**.

That number was projected as two to three weeks of work. It is not: none of
the 91 is a product bug. They are tests asserting a legacy default —
``test_off_by_default`` for at-rest encryption is *correct* to fail once
encryption defaults on — or fixtures that never granted consent because
consent used to auto-approve.

So the posture job records rather than blocks, and the record only shrinks.
A blocking job that is red on every PR gets switched off, and then nothing
measures the posture at all.
"""

from __future__ import annotations

import json

import pytest
from maverick import posture_gate as gate

_REPORT = """
FAILED packages/maverick-core/tests/test_a.py::test_one - AssertionError: boom
FAILED packages/maverick-core/tests/test_b.py::test_two[a b] - TypeError
ERROR packages/maverick-core/tests/test_c.py::test_three
91 failed, 15778 passed, 197 skipped in 819.93s
"""


def test_it_parses_node_ids() -> None:
    assert gate.parse_report(_REPORT) == {
        "packages/maverick-core/tests/test_a.py::test_one",
        "packages/maverick-core/tests/test_b.py::test_two[a b]",
        "packages/maverick-core/tests/test_c.py::test_three",
    }


def test_a_parametrized_id_with_spaces_survives() -> None:
    """The bug that undercounted the first baseline by 16 of 91.

    A ``\\S+`` capture stops at the space inside ``[a b]``, so those entries
    were dropped. An undercounted baseline is worse than none: it looks like a
    ratchet while letting the missing entries through as "new" forever.
    """
    ids = gate.parse_report("FAILED tests/t.py::test_x[first second] - E")
    assert ids == {"tests/t.py::test_x[first second]"}


def test_the_committed_baseline_is_substantial() -> None:
    """Anti-vacuity: an empty baseline would make every check below pass."""
    baseline = gate.load_baseline()
    assert len(baseline) > 50, len(baseline)
    assert all("::" in node for node in baseline)


def test_the_baseline_file_shape() -> None:
    data = json.loads(gate.BASELINE.read_text(encoding="utf-8"))
    assert data["count"] == len(data["failures"])
    assert data["failures"] == sorted(data["failures"])


# -- the ratchet -----------------------------------------------------------

def test_a_new_failure_is_drift() -> None:
    new, fixed = gate.drift({"a::b", "c::d"}, {"a::b"})
    assert new == ["c::d"] and fixed == []


def test_a_fixed_failure_must_be_removed_from_the_baseline() -> None:
    """Otherwise the register becomes an exemption list instead of debt."""
    new, fixed = gate.drift({"a::b"}, {"a::b", "c::d"})
    assert new == [] and fixed == ["c::d"]


def test_no_drift_is_clean() -> None:
    assert gate.drift({"a::b"}, {"a::b"}) == ([], [])


# -- vacuity: the gate must not pass on a report that never ran ------------

def test_a_truncated_report_is_an_error_not_a_pass(tmp_path, capsys) -> None:
    """The failure mode this repo has been closing everywhere.

    A killed or empty posture run produces a report with no summary. Reading
    that as "zero failures" would turn the whole job into a green check that
    inspected nothing.
    """
    report = tmp_path / "posture.txt"
    report.write_text("", encoding="utf-8")
    assert gate.main(["--ci", "--report", str(report)]) == 2
    assert "no pytest summary" in capsys.readouterr().err


def test_a_missing_report_is_an_error(tmp_path, capsys) -> None:
    assert gate.main(["--ci", "--report", str(tmp_path / "nope.txt")]) == 2
    assert "no report at" in capsys.readouterr().err


def test_a_completed_run_with_zero_failures_is_accepted(tmp_path) -> None:
    """Positive control: refusing every report would also pass the tests above."""
    report = tmp_path / "posture.txt"
    report.write_text("15869 passed in 800s\n", encoding="utf-8")
    # Every recorded failure now passes, so this is drift -- but it parsed.
    assert gate.main(["--ci", "--report", str(report)]) == 1


@pytest.mark.parametrize("summary", ["1 failed, 2 passed", "3 error", "9 passed"])
def test_summary_detection(tmp_path, summary) -> None:
    report = tmp_path / "posture.txt"
    report.write_text(summary + "\n", encoding="utf-8")
    assert gate.main(["--report", str(report)]) in (0, 1)
