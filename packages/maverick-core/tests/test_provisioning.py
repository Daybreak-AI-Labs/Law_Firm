"""Headless provisioning (`init --from-file`)."""
from __future__ import annotations

import pytest
from maverick.file_lock import private_path_is_restricted

# ---- headless provisioning -------------------------------------------------










def test_init_from_file_installs_exact_bytes(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main

    expected = b'[client]\nid = "acme-corp"\nenforce = true\n'
    src = tmp_path / "client.toml"
    src.write_bytes(expected)
    dst = tmp_path / "installed" / "config.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    result = CliRunner().invoke(main, ["init", "--from-file", str(src)])

    assert result.exit_code == 0, result.output
    assert dst.read_bytes() == expected
    assert private_path_is_restricted(dst, 0o600)
    assert private_path_is_restricted(dst.parent, 0o700)


def test_init_from_file_installs_opened_bytes_not_later_path_contents(
    monkeypatch, tmp_path
):
    """A path replacement after validation cannot change the installed snapshot."""
    import maverick.config_lint as config_lint
    from click.testing import CliRunner
    from maverick.cli import main

    trusted = b'[client]\nid = "trusted"\nenforce = true\n'
    attacker = b'[client]\nid = "attacker"\nenforce = false\n'
    src = tmp_path / "client.toml"
    replacement = tmp_path / "replacement.toml"
    src.write_bytes(trusted)
    replacement.write_bytes(attacker)
    dst = tmp_path / "installed" / "config.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    def replace_validated_path(parsed):
        assert parsed["client"]["id"] == "trusted"
        replacement.replace(src)
        return []

    monkeypatch.setattr(config_lint, "lint_config", replace_validated_path)

    result = CliRunner().invoke(main, ["init", "--from-file", str(src)])

    assert result.exit_code == 0, result.output
    assert src.read_bytes() == attacker
    assert dst.read_bytes() == trusted


def test_init_from_file_rejects_alias_source(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main

    target = tmp_path / "target.toml"
    target.write_text('[client]\nid = "target"\n', encoding="utf-8")
    alias = tmp_path / "alias.toml"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    result = CliRunner().invoke(main, ["init", "--from-file", str(alias)])

    assert result.exit_code != 0
    assert "regular, non-aliased file" in result.output
    assert not dst.exists()


def test_init_from_file_rejects_non_regular_source(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main

    src = tmp_path / "config-directory"
    src.mkdir()
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    result = CliRunner().invoke(main, ["init", "--from-file", str(src)])

    assert result.exit_code != 0
    assert "regular, non-aliased file" in result.output
    assert not dst.exists()


def test_init_from_file_rejects_oversized_source(monkeypatch, tmp_path):
    import maverick.cli as cli
    from click.testing import CliRunner

    src = tmp_path / "large.toml"
    src.write_bytes(b'[client]\nid = "acme"\n' + b"# padding\n" * 20)
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))
    monkeypatch.setattr(cli, "_MAX_CONFIG_SOURCE_BYTES", 64)

    result = CliRunner().invoke(cli.main, ["init", "--from-file", str(src)])

    assert result.exit_code != 0
    assert "exceeds the 64-byte limit" in result.output
    assert not dst.exists()


def test_init_from_file_rejects_source_change_during_read(monkeypatch, tmp_path):
    import os

    import maverick.cli as cli
    from click.testing import CliRunner

    src = tmp_path / "changing.toml"
    payload = b'[client]\nid = "trusted"\n' + (b"# padding\n" * 20_000)
    src.write_bytes(payload)
    original = src.stat()
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    real_read = cli.os.read
    changed = False

    def racing_read(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            with src.open("r+b") as stream:
                stream.seek(len(payload) - 2)
                stream.write(b"X")
                stream.flush()
                os.fsync(stream.fileno())
            # Restoring mtime must not make an in-place rewrite acceptable;
            # ctime/share denial and same-handle verification remain fences.
            os.utime(
                src,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
        return chunk

    monkeypatch.setattr(cli.os, "read", racing_read)

    result = CliRunner().invoke(cli.main, ["init", "--from-file", str(src)])

    assert changed is True
    assert result.exit_code != 0
    assert "config source changed while it was read" in result.output
    assert not dst.exists()


def test_windows_config_source_handle_denies_concurrent_write(tmp_path):
    import os

    import maverick.cli as cli

    if os.name != "nt":
        pytest.skip("Windows sharing semantics only")
    payload = b'[client]\nid = "trusted"\n'
    src = tmp_path / "read-shared-only.toml"
    src.write_bytes(payload)

    fd = cli._open_config_source_fd(src)
    try:
        with pytest.raises(OSError):
            src.open("r+b")
        assert os.read(fd, len(payload) + 1) == payload
    finally:
        os.close(fd)

    assert src.read_bytes() == payload
    with src.open("r+b"):
        pass


def test_windows_config_source_conversion_failure_closes_handle(
    monkeypatch, tmp_path
):
    import os

    import maverick.cli as cli

    if os.name != "nt":
        pytest.skip("Windows handle conversion only")
    import msvcrt

    src = tmp_path / "conversion-failure.toml"
    src.write_bytes(b'[client]\nid = "trusted"\n')

    def fail_conversion(handle, flags):
        raise OSError("synthetic conversion failure")

    monkeypatch.setattr(msvcrt, "open_osfhandle", fail_conversion)

    with pytest.raises(OSError, match="synthetic conversion failure"):
        cli._open_config_source_fd(src)

    # A leaked read-shared-only handle would keep this write open denied.
    with src.open("r+b"):
        pass


def test_init_from_file_rejects_same_handle_snapshot_mismatch(monkeypatch, tmp_path):
    import maverick.cli as cli
    from click.testing import CliRunner

    src = tmp_path / "unstable.toml"
    src.write_bytes(b'[client]\nid = "trusted"\n')
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    real_lseek = cli.os.lseek
    real_read = cli.os.read
    verifying = False
    changed = False

    def tracking_lseek(fd, offset, whence):
        nonlocal verifying
        position = real_lseek(fd, offset, whence)
        if offset == 0 and whence == cli.os.SEEK_SET:
            verifying = True
        return position

    def mismatching_read(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if verifying and chunk and not changed:
            # Same length, with real fstat/lstat metadata unchanged.
            changed = True
            return bytes((chunk[0] ^ 1,)) + chunk[1:]
        return chunk

    monkeypatch.setattr(cli.os, "lseek", tracking_lseek)
    monkeypatch.setattr(cli.os, "read", mismatching_read)

    result = CliRunner().invoke(cli.main, ["init", "--from-file", str(src)])

    assert changed is True
    assert result.exit_code != 0
    assert "config source changed while it was read" in result.output
    assert not dst.exists()


def test_init_from_file_rejects_invalid_toml(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main

    src = tmp_path / "bad.toml"
    src.write_bytes(b"this is = = not toml")
    dst = tmp_path / "out.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(dst))

    result = CliRunner().invoke(main, ["init", "--from-file", str(src)])

    assert result.exit_code != 0
    assert "invalid TOML" in result.output
    assert not dst.exists()


def test_init_from_file_missing(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "out.toml"))
    r = CliRunner().invoke(main, ["init", "--from-file", str(tmp_path / "nope.toml")])
    assert r.exit_code != 0


