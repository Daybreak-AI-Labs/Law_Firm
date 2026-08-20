"""Q3 2026 tests for ffmpeg and the workflow engine."""
from __future__ import annotations

from unittest.mock import MagicMock

# ---------- ffmpeg ----------

def test_ffmpeg_requires_op():
    from maverick.tools.ffmpeg_tool import ffmpeg_tool
    assert "op is required" in ffmpeg_tool().fn({})


def test_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: None)
    from maverick.tools.ffmpeg_tool import ffmpeg_tool
    out = ffmpeg_tool().fn({"op": "convert",
                              "input_path": "/tmp/a.mp4",
                              "output_path": "/tmp/b.mp4"})
    assert "ffmpeg" in out and "PATH" in out


def test_ffmpeg_convert_calls_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/" + b)
    captured = {"cmd": None}

    def _run(cmd, *a, **k):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _run)
    from maverick.tools.ffmpeg_tool import ffmpeg_tool
    out = ffmpeg_tool().fn({
        "op": "convert", "input_path": "/tmp/a.mp4",
        "output_path": "/tmp/b.mp4",
    })
    assert "wrote /tmp/b.mp4" in out
    # Sandbox-less path runs a shell string through subprocess.run(shell=True).
    assert "ffmpeg" in captured["cmd"]


def test_ffmpeg_info_parses_ffprobe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/" + b)
    import json
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(
            returncode=0,
            stdout=json.dumps({
                "format": {"format_name": "mov,mp4", "duration": "10.0", "bit_rate": "1000"},
                "streams": [{"codec_type": "video", "codec_name": "h264",
                              "width": 1920, "height": 1080}],
            }),
            stderr="",
        ),
    )
    from maverick.tools.ffmpeg_tool import ffmpeg_tool
    out = ffmpeg_tool().fn({"op": "info", "input_path": "/tmp/a.mp4"})
    assert "mov,mp4" in out and "1920x1080" in out


# ---------- Workflow engine ----------

def test_workflow_runs_simple_dag():
    from unittest.mock import MagicMock

    from maverick.workflow import Step, Workflow

    reg = MagicMock()
    outputs = iter(["A-out", "B-out", "C-out"])

    async def _run(name, args):
        return next(outputs)

    reg.run = _run
    wf = Workflow(steps=[
        Step("a", "toolA", {"x": 1}),
        Step("b", "toolB", {"x": "${a.out}"}),
        Step("c", "toolC", {"x": "${b.out}", "y": "${a.out}"}),
    ])
    res = wf.run(reg)
    assert [s.name for s in res.steps] == ["a", "b", "c"]
    assert res.steps[1].output == "B-out"
    # placeholders were resolved
    # (we can't see them directly via reg here; just confirm no failures)
    assert not res.failed


def test_workflow_stops_on_error():
    from maverick.workflow import Step, Workflow

    class _Reg:
        async def run(self, name, args):
            if name == "boom":
                return "ERROR: nope"
            return "ok"

    wf = Workflow(steps=[
        Step("a", "ok-tool"),
        Step("b", "boom"),
        Step("c", "ok-tool", depends_on=["b"]),
    ])
    res = wf.run(_Reg())
    assert res.failed
    # Should run a + b only, not c.
    assert [s.name for s in res.steps] == ["a", "b"]


def test_workflow_rejects_cycle():
    from maverick.workflow import Step, Workflow, WorkflowCycle
    try:
        Workflow(steps=[
            Step("a", "x", depends_on=["b"]),
            Step("b", "x", depends_on=["a"]),
        ])
    except WorkflowCycle:
        return
    raise AssertionError("expected WorkflowCycle")


def test_workflow_rejects_self_dep():
    from maverick.workflow import Step, Workflow, WorkflowCycle
    try:
        Workflow(steps=[Step("a", "x", depends_on=["a"])])
    except WorkflowCycle:
        return
    raise AssertionError("expected WorkflowCycle")


def test_workflow_rejects_unknown_dep():
    from maverick.workflow import Step, Workflow
    try:
        Workflow(steps=[Step("a", "x", {"y": "${nope.out}"})])
    except ValueError as e:
        assert "unknown dependency" in str(e)
        return
    raise AssertionError("expected ValueError")


def test_workflow_resolves_string_placeholders():
    from maverick.workflow import Step, Workflow

    seen: list[dict] = []

    class _Reg:
        async def run(self, name, args):
            seen.append(args)
            return "first-output"

    wf = Workflow(steps=[
        Step("first", "echo", {"msg": "hi"}),
        Step("second", "echo",
             {"prefix": "got: ${first.out}!", "n": 1}),
    ])
    wf.run(_Reg())
    assert seen[1]["prefix"] == "got: first-output!"
    assert seen[1]["n"] == 1
