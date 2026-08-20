"""Retained offline/media subprocess helpers stay confined and scrub secrets."""
from __future__ import annotations

import importlib

import pytest
from maverick.tools import safe_media_args, scrub_child_env


def test_scrub_child_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("CLIO_TOKEN", "clio-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrub_child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLIO_TOKEN" not in env
    assert "PATH" in env


@pytest.mark.parametrize(
    "bad",
    [
        "--lua-filter=/tmp/evil.lua",
        "--template=/tmp/t.html",
        "--pdf-engine=weasyprint",
        "concat:/etc/passwd|/etc/shadow",
        "--resource-path=/etc",
    ],
)
def test_safe_media_args_drops_self_contained_dangerous_flags(bad, monkeypatch):
    monkeypatch.delenv("MAVERICK_ALLOW_RAW_MEDIA_ARGS", raising=False)
    out = safe_media_args(["-quality", "90", bad])
    assert bad not in out
    assert out == ["-quality", "90"]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("-i", "/etc/passwd"),
        ("--input", "/etc/shadow"),
        ("-filter_complex", "movie=/etc/passwd"),
    ],
)
def test_safe_media_args_drops_flag_and_value(flag, value, monkeypatch):
    monkeypatch.delenv("MAVERICK_ALLOW_RAW_MEDIA_ARGS", raising=False)
    assert safe_media_args(["-strip", flag, value, "-quality", "80"]) == [
        "-strip",
        "-quality",
        "80",
    ]


def test_safe_media_args_passthrough_requires_explicit_operator_opt_in(monkeypatch):
    monkeypatch.setenv("MAVERICK_ALLOW_RAW_MEDIA_ARGS", "1")
    raw = ["-i", "/etc/passwd", "--lua-filter=x.lua"]
    assert safe_media_args(raw) == raw


def test_retained_shell_out_modules_use_the_shared_chokepoint():
    import pathlib

    import maverick.tools as tools_package

    tool_dir = pathlib.Path(tools_package.__file__).parent
    expectations = {
        "apply_patch": "scrub_child_env",
        "ffmpeg_tool": "sandbox_run",
    }
    for module, seam in expectations.items():
        assert seam in (tool_dir / f"{module}.py").read_text(encoding="utf-8")


def test_ffmpeg_rejects_option_shaped_input_without_sandbox():
    module = importlib.import_module("maverick.tools.ffmpeg_tool")
    with pytest.raises(ValueError, match="may not begin with '-'"):
        module._safe_path(None, "-i")
    assert module._safe_path(None, "in.mp4") == "in.mp4"
