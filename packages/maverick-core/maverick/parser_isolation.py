"""Process-isolated parsing of registered untrusted byte formats.

The parsers that touch **attacker-controllable bytes** — PDFs from
attachments, images from channels, HTML from fetched pages — are largely
C-extension-backed (pdfplumber/pdfminer, pypdf, Pillow, lxml). A memory-safety
bug there is an in-process RCE/corruption foothold fed directly by untrusted
input. Rewriting those parsers isn't realistic; **isolating** them is:

* :data:`PARSERS` — the inventory: each untrusted-input parser path, what
  feeds it, and whether its implementation is memory-safe (pure Python) or
  C-extension (isolate-worthy). The policy, in code.
* :func:`parse_isolated` — run a whitelisted parser entry point in a child
  Python process with the **secret-scrubbed env** (same posture as plugin
  isolation): a separate address space, so a heap bug or segfault on hostile
  bytes kills the child — never the kernel — and an exploited parser child
  holds no provider keys. Input goes over stdin (bytes), result over stdout
  (JSON), input and both output pipes are byte-capped, and a hard timeout
  kills a child that does not finish.
* Firm-safe default: registered untrusted parsers always use the child.
  ``MAVERICK_ISOLATE_PARSERS=1`` remains a force-on compatibility knob. The
  only force-off is the deliberately alarming
  ``MAVERICK_TRUSTED_IN_PROCESS_PARSERS=1`` escape hatch for trusted fixtures
  and controlled diagnostics; it must never be set for uploaded documents.

Only entries in :data:`PARSERS` may run in the child (a whitelist keyed by
name — never an arbitrary dotted path from the model).

This is a process and credential boundary, not an OS sandbox: the child still
runs as the service account. Production confinement must separately restrict
that account's filesystem, network, CPU, and memory access.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_INPUT_BYTES = 64 * 1024 * 1024   # refuse absurd inputs before any parse
MAX_CHILD_STDOUT_BYTES = 16 * 1024 * 1024
MAX_CHILD_STDERR_BYTES = 256 * 1024
_PIPE_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class ParserEntry:
    name: str          # whitelist key
    module: str        # module imported INSIDE the child
    func: str          # function(data: bytes, **kwargs) -> JSON-able result
    feeds: str         # what untrusted source reaches it
    memory_safe: bool  # pure-Python (True) vs C-extension-backed (False)


# The inventory/policy: untrusted-input parsers and their safety class.
PARSERS: dict[str, ParserEntry] = {
    "pdf_text": ParserEntry(
        name="pdf_text",
        module="maverick.tools.pdf_reader",
        func="extract_text_from_bytes",
        feeds="attachments / channel uploads",
        memory_safe=False,  # pdfplumber(pdfminer)/pypdf C-accelerated paths
    ),
    "image_meta": ParserEntry(
        name="image_meta",
        module="maverick.parser_isolation",
        func="_probe_image_meta",
        feeds="channel image uploads",
        memory_safe=False,  # Pillow decoders are C
    ),
    "knowledge_pdf_text": ParserEntry(
        name="knowledge_pdf_text",
        module="maverick_knowledge.parse",
        func="_extract_pdf_bytes",
        feeds="knowledge uploads / attachment ingestion",
        memory_safe=False,  # pypdf content filters include C-backed codecs
    ),
    "knowledge_docx_text": ParserEntry(
        name="knowledge_docx_text",
        module="maverick_knowledge.parse",
        func="_extract_docx_bytes",
        feeds="knowledge uploads / attachment ingestion",
        memory_safe=False,  # python-docx uses lxml's C extension
    ),
}


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def trusted_inprocess_enabled() -> bool:
    """Explicit trusted/test-only bypass for callers that cannot spawn a child."""
    return _env_enabled("MAVERICK_TRUSTED_IN_PROCESS_PARSERS")


def should_isolate() -> bool:
    """Return the firm-safe policy: isolate unless the trusted bypass is explicit."""
    if _env_enabled("MAVERICK_ISOLATE_PARSERS"):
        return True
    try:
        from .config import load_config
        if bool(((load_config() or {}).get("security") or {})
                .get("isolate_parsers", False)):
            return True
    except Exception:  # a broken config must not weaken the parsing boundary
        pass
    return not trusted_inprocess_enabled()


_CHILD_TEMPLATE = """\
import json, sys
sys.path.insert(0, {import_root!r})
data = sys.stdin.buffer.read()
from importlib import import_module
fn = getattr(import_module({module!r}), {func!r})
kwargs = json.loads({kwargs_json!r})
try:
    result = fn(data, **kwargs)
    sys.stdout.write(json.dumps({{"ok": True, "result": result}}))
except Exception as e:
    sys.stdout.write(json.dumps({{"ok": False,
                                  "error": f"{{type(e).__name__}}: {{e}}"}}))
"""


def _kill_child(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _read_capped_pipe(
    proc: subprocess.Popen,
    stream,
    sink: bytearray,
    limit: int,
    label: str,
    exceeded: list[str],
    io_errors: list[BaseException],
) -> None:
    try:
        while True:
            remaining = limit + 1 - len(sink)
            if remaining <= 0:
                exceeded.append(label)
                _kill_child(proc)
                return
            chunk = stream.read(min(_PIPE_CHUNK_BYTES, remaining))
            if not chunk:
                return
            sink.extend(chunk)
            if len(sink) > limit:
                exceeded.append(label)
                _kill_child(proc)
                return
    except (OSError, ValueError) as exc:
        io_errors.append(exc)
        _kill_child(proc)


def _write_child_input(proc: subprocess.Popen, data: bytes) -> None:
    try:
        assert proc.stdin is not None
        proc.stdin.write(data)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        # A parser may fail before consuming all input. Its exit/output is the
        # authoritative result, so a broken input pipe is expected.
        pass


def _wait_for_child(proc: subprocess.Popen, deadline: float) -> bool:
    """Wait until the shared deadline; return True after a timeout/kill."""
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        return False
    except subprocess.TimeoutExpired:
        _kill_child(proc)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        return True


def _close_child_streams(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass


def _run_bounded_child(
    args: list[str],
    *,
    data: bytes,
    timeout: float,
    env: dict,
    cwd: str,
    name: str,
) -> subprocess.CompletedProcess:
    """Run a parser child while keeping both output pipes strictly bounded.

    ``subprocess.run(capture_output=True)`` accumulates unbounded bytes before
    returning. A compromised parser could therefore exhaust the parent's
    memory merely by writing to stdout/stderr. Dedicated readers retain at
    most each configured cap plus one sentinel byte, kill on overflow, and
    drain concurrently so neither pipe can deadlock the child.
    """
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    except OSError as exc:
        raise RuntimeError(f"parser child {name!r} could not start: {exc}") from exc

    stdout = bytearray()
    stderr = bytearray()
    exceeded: list[str] = []
    io_errors: list[BaseException] = []

    assert proc.stdout is not None and proc.stderr is not None
    threads = [
        threading.Thread(
            target=_read_capped_pipe,
            args=(
                proc,
                proc.stdout,
                stdout,
                MAX_CHILD_STDOUT_BYTES,
                "stdout",
                exceeded,
                io_errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_read_capped_pipe,
            args=(
                proc,
                proc.stderr,
                stderr,
                MAX_CHILD_STDERR_BYTES,
                "stderr",
                exceeded,
                io_errors,
            ),
            daemon=True,
        ),
        threading.Thread(target=_write_child_input, args=(proc, data), daemon=True),
    ]
    deadline = time.monotonic() + max(0.0, timeout)
    for thread in threads:
        thread.start()

    timed_out = _wait_for_child(proc, deadline)

    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    threads_alive = any(thread.is_alive() for thread in threads)
    _close_child_streams(proc)

    if timed_out:
        raise RuntimeError(f"parser {name!r} timed out after {timeout}s")
    if exceeded:
        label = exceeded[0]
        limit = (
            MAX_CHILD_STDOUT_BYTES if label == "stdout" else MAX_CHILD_STDERR_BYTES
        )
        raise RuntimeError(
            f"parser {name!r} exceeded the {limit}-byte {label} cap"
        )
    if threads_alive:
        _kill_child(proc)
        raise RuntimeError(f"parser {name!r} IPC did not close before timeout")
    if io_errors:
        raise RuntimeError(f"parser {name!r} IPC failed: {io_errors[0]}")

    return subprocess.CompletedProcess(
        args=args,
        returncode=proc.returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def parse_isolated(name: str, data: bytes, *, timeout: float = DEFAULT_TIMEOUT,
                   **kwargs):
    """Run the whitelisted parser ``name`` on ``data`` in a scrubbed child.

    Returns the parser's JSON-able result. Raises ``ValueError`` for an
    unknown parser or oversized input, ``RuntimeError`` for a child that
    crashed/timed out/errored. Untrusted callers must fail closed; in-process
    parsing is reserved for the explicitly named trusted/test escape hatch.
    """
    entry = PARSERS.get(name)
    if entry is None:
        raise ValueError(f"unknown parser {name!r}; whitelisted: {sorted(PARSERS)}")
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(
            f"input of {len(data)} bytes exceeds the {MAX_INPUT_BYTES}-byte cap")
    # kwargs are baked as a JSON literal — nothing user-controlled becomes code.
    # The child runs in isolated mode with a neutral cwd, then imports Maverick
    # from the same trusted package root as this parent module.  That keeps
    # python -c from resolving whitelisted dotted names through an attacker
    # controlled workspace package.
    import_root = str(Path(__file__).resolve().parents[1])
    code = _CHILD_TEMPLATE.format(
        import_root=import_root, module=entry.module, func=entry.func,
        kwargs_json=json.dumps(kwargs, default=str),
    )
    from .tools import scrub_child_env
    proc = _run_bounded_child(
        [sys.executable, "-I", "-c", code],
        data=data,
        timeout=timeout,
        env=scrub_child_env(),
        cwd=os.path.abspath(os.sep),
        name=name,
    )
    if proc.returncode != 0:
        # a segfault/abort on hostile bytes lands HERE, not in the kernel
        raise RuntimeError(
            f"parser child {name!r} died (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except ValueError as e:
        raise RuntimeError(f"parser {name!r} returned non-JSON output") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"parser {name!r} returned a malformed response")
    if payload.get("ok") is not True:
        raise RuntimeError(f"parser {name!r} failed: {payload.get('error')}")
    return payload.get("result")


def _probe_image_meta(data: bytes) -> dict:
    """Child-side image probe: format/size via Pillow without full decode."""
    import io
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover -- optional extra
        raise RuntimeError("Pillow not installed ([parsers] extra)") from e
    with Image.open(io.BytesIO(data)) as im:
        return {"format": im.format, "width": im.width, "height": im.height,
                "mode": im.mode}


def inventory() -> str:
    """Render the parser policy table (the auditable inventory)."""
    lines = ["untrusted-input parsers:"]
    for e in sorted(PARSERS.values(), key=lambda e: e.name):
        safety = "memory-safe (pure python)" if e.memory_safe else \
            "C-extension — ISOLATE"
        lines.append(f"  {e.name:<12} {e.module}.{e.func}")
        lines.append(f"      feeds: {e.feeds}; {safety}")
    state = "ON (firm-safe default)" if should_isolate() else \
        "OFF (explicit trusted/test in-process escape hatch)"
    lines.append(f"isolation: {state}")
    return "\n".join(lines)


__all__ = ["PARSERS", "ParserEntry", "parse_isolated", "should_isolate",
           "trusted_inprocess_enabled", "inventory", "MAX_INPUT_BYTES",
           "MAX_CHILD_STDOUT_BYTES", "MAX_CHILD_STDERR_BYTES"]
