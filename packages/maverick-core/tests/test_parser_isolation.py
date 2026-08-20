"""Memory-safe parsing: whitelist, caps, real child round-trip, pdf wiring."""
from __future__ import annotations

import subprocess

import pytest
from maverick import parser_isolation as pi


def test_whitelist_enforced():
    with pytest.raises(ValueError, match="unknown parser"):
        pi.parse_isolated("arbitrary.module:evil", b"x")


def test_size_cap_enforced_before_child(monkeypatch):
    called = []
    monkeypatch.setattr(pi, "_run_bounded_child",
                        lambda *a, **k: called.append(1))
    big = b"x" * (pi.MAX_INPUT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        pi.parse_isolated("pdf_text", big)
    assert called == []  # the child never saw the oversized input


def test_child_uses_isolated_mode_and_neutral_cwd(monkeypatch):
    captured = {}

    class _OK:
        returncode = 0
        stdout = b'{"ok": true, "result": null}'
        stderr = b""

    def _run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return _OK()

    monkeypatch.setattr(pi, "_run_bounded_child", _run)
    pi.parse_isolated("pdf_text", b"%PDF-fake")
    assert captured["args"][:2] == [pi.sys.executable, "-I"]
    assert captured["cwd"] == pi.os.path.abspath(pi.os.sep)


def test_child_imports_real_package_not_attacker_cwd(monkeypatch, tmp_path):
    attacker_module = tmp_path / "maverick" / "tools"
    attacker_module.mkdir(parents=True)
    marker = tmp_path / "PWNED"
    (tmp_path / "maverick" / "__init__.py").write_text("")
    (attacker_module / "__init__.py").write_text("")
    (attacker_module / "pdf_reader.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "def extract_text_from_bytes(data, **kwargs):\n"
        "    return 'attacker controlled'\n"
    )

    monkeypatch.chdir(tmp_path)
    try:
        result = pi.parse_isolated("pdf_text", b"%PDF-1.4 fake")
    except RuntimeError:
        result = None

    assert result != "attacker controlled"
    assert not marker.exists()


def test_real_child_roundtrip(monkeypatch):
    # a pure-python whitelisted entry: json.loads(bytes) -> the parsed value
    entry = pi.ParserEntry(name="jsonval", module="json", func="loads",
                           feeds="test", memory_safe=True)
    monkeypatch.setitem(pi.PARSERS, "jsonval", entry)
    result = pi.parse_isolated("jsonval", b'{"a": [1, 2]}')
    assert result == {"a": [1, 2]}


def test_real_knowledge_docx_child_imports_and_fails_closed():
    pytest.importorskip("maverick_knowledge.parse")
    with pytest.raises(RuntimeError, match="corrupt zip container"):
        pi.parse_isolated(
            "knowledge_docx_text",
            b"not-a-docx",
            max_uncompressed_bytes=1024,
        )


def test_child_parser_error_surfaces(monkeypatch):
    # json.dumps(bytes) raises inside the child -> ok:False -> RuntimeError
    entry = pi.ParserEntry(name="boom", module="json", func="dumps",
                           feeds="test", memory_safe=True)
    monkeypatch.setitem(pi.PARSERS, "boom", entry)
    with pytest.raises(RuntimeError, match="TypeError"):
        pi.parse_isolated("boom", b"\x00\x01")


def test_child_death_is_runtime_error(monkeypatch):
    class _Dead:
        returncode = -11
        stdout = b""
        stderr = b"Segmentation fault"

    monkeypatch.setattr(pi, "_run_bounded_child", lambda *a, **k: _Dead())
    with pytest.raises(RuntimeError, match="died"):
        pi.parse_isolated("pdf_text", b"%PDF-fake")


def test_malformed_child_protocol_fails_closed(monkeypatch):
    class _Malformed:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    monkeypatch.setattr(pi, "_run_bounded_child", lambda *a, **k: _Malformed())

    with pytest.raises(RuntimeError, match="malformed response"):
        pi.parse_isolated("pdf_text", b"%PDF-fake")


def test_timeout_is_runtime_error(monkeypatch):
    with pytest.raises(RuntimeError, match="timed out"):
        pi._run_bounded_child(
            [pi.sys.executable, "-I", "-c", "import time; time.sleep(5)"],
            data=b"",
            timeout=0.05,
            env=pi.os.environ.copy(),
            cwd=pi.os.path.abspath(pi.os.sep),
            name="hang",
        )


def test_child_spawn_failure_is_runtime_error(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("spawn denied")),
    )

    with pytest.raises(RuntimeError, match="could not start"):
        pi.parse_isolated("pdf_text", b"%PDF-fake")


def test_child_env_is_scrubbed(monkeypatch):
    captured = {}

    class _OK:
        returncode = 0
        stdout = b'{"ok": true, "result": null}'
        stderr = b""

    def _run(*a, **k):
        captured.update(k)
        return _OK()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")  # pragma: allowlist secret
    monkeypatch.setattr(pi, "_run_bounded_child", _run)
    pi.parse_isolated("pdf_text", b"%PDF-fake")
    assert "ANTHROPIC_API_KEY" not in captured["env"]


@pytest.mark.parametrize(
    ("stream", "cap_name"),
    [("stdout", "MAX_CHILD_STDOUT_BYTES"),
     ("stderr", "MAX_CHILD_STDERR_BYTES")],
)
def test_hostile_child_output_is_killed_at_ipc_cap(monkeypatch, stream, cap_name):
    monkeypatch.setattr(pi, cap_name, 64)
    code = (
        "import sys; "
        f"sys.{stream}.buffer.write(b'x' * 4096); "
        f"sys.{stream}.flush()"
    )

    with pytest.raises(RuntimeError, match=rf"{stream} cap"):
        pi._run_bounded_child(
            [pi.sys.executable, "-I", "-c", code],
            data=b"",
            timeout=5,
            env=pi.os.environ.copy(),
            cwd=pi.os.path.abspath(pi.os.sep),
            name="spew",
        )


def test_should_isolate_on_by_default(monkeypatch):
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    assert pi.should_isolate() is True


def test_broken_config_cannot_disable_isolation(monkeypatch):
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: (_ for _ in ()).throw(OSError("unreadable config")),
    )

    assert pi.should_isolate() is True


def test_only_clearly_named_trusted_escape_hatch_disables_isolation(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.setenv("MAVERICK_ISOLATE_PARSERS", "0")
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    assert pi.should_isolate() is True

    monkeypatch.setenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", "1")
    assert pi.should_isolate() is False

    # Either force-on setting wins if the trusted bypass is accidentally present.
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"security": {"isolate_parsers": True}},
    )
    assert pi.should_isolate() is True

    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.setenv("MAVERICK_ISOLATE_PARSERS", "1")
    assert pi.should_isolate() is True


def test_inventory_renders_policy(monkeypatch):
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    out = pi.inventory()
    assert "pdf_text" in out and "knowledge_docx_text" in out
    assert "firm-safe default" in out and "ISOLATE" in out


def test_pdf_reader_routes_through_isolation(monkeypatch, tmp_path):
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    import maverick.parser_isolation as real_pi

    calls = {}

    def fake_isolated(name, data, **kw):
        calls["name"] = name
        return "ISOLATED TEXT"

    monkeypatch.setattr(real_pi, "parse_isolated", fake_isolated)
    monkeypatch.chdir(tmp_path)  # pdf_reader confines paths to cwd
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4 fake")
    from maverick.tools.pdf_reader import _run_read_pdf
    out = _run_read_pdf({"source": "x.pdf"})
    assert out == "ISOLATED TEXT"
    assert calls["name"] == "pdf_text"


def test_pdf_reader_isolated_failure_refuses_inprocess_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("MAVERICK_ISOLATE_PARSERS", raising=False)
    monkeypatch.delenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", raising=False)
    monkeypatch.setattr("maverick.config.load_config", dict)
    import maverick.parser_isolation as real_pi
    monkeypatch.setattr(real_pi, "parse_isolated",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("child died (exit -11)")))
    monkeypatch.chdir(tmp_path)  # pdf_reader confines paths to cwd
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4 hostile")
    from maverick.tools.pdf_reader import _run_read_pdf
    out = _run_read_pdf({"source": "x.pdf"})
    assert out.startswith("ERROR: isolated PDF parse failed")


def test_pdf_reader_bounds_local_input_before_child(monkeypatch, tmp_path):
    import maverick.parser_isolation as real_pi

    monkeypatch.setattr(real_pi, "MAX_INPUT_BYTES", 8)
    monkeypatch.setattr(
        real_pi,
        "parse_isolated",
        lambda *a, **k: pytest.fail("oversized bytes reached the child"),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "large.pdf").write_bytes(b"x" * 9)
    from maverick.tools.pdf_reader import _run_read_pdf

    out = _run_read_pdf({"source": "large.pdf"})

    assert out.startswith("ERROR: could not read PDF")


def test_pdf_reader_trusted_escape_hatch_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_TRUSTED_IN_PROCESS_PARSERS", "1")
    import maverick.parser_isolation as real_pi
    import maverick.tools.pdf_reader as pdf_reader

    monkeypatch.setattr(
        real_pi,
        "parse_isolated",
        lambda *a, **k: pytest.fail("trusted bypass should not spawn a child"),
    )
    monkeypatch.setattr(
        pdf_reader,
        "extract_text_from_bytes",
        lambda *a, **k: "TRUSTED IN-PROCESS TEXT",
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4 trusted fixture")

    out = pdf_reader._run_read_pdf({"source": "x.pdf"})

    assert out == "TRUSTED IN-PROCESS TEXT"
