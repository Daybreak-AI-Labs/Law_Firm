"""The law-firm CLI must not grow unaudited remote upload paths."""
from __future__ import annotations

from pathlib import Path


def test_remote_run_share_commands_are_not_registered():
    from maverick.cli import main

    forbidden = {"gist", "run-share", "run_share", "share-run", "share_run"}
    assert forbidden.isdisjoint(main.commands)


def test_cli_contains_no_gist_upload_endpoint():
    import maverick.cli

    cli_dir = Path(maverick.cli.__file__).resolve().parent
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(cli_dir.glob("*.py"))
    ).lower()

    assert "api.github.com/gists" not in source
    assert "gist.github.com" not in source


def test_audit_network_forwarding_is_not_registered():
    from click.testing import CliRunner
    from maverick.cli import audit, main

    assert "forward" not in audit.commands
    result = CliRunner().invoke(
        main,
        ["audit", "forward", "--to", "https://collector.invalid/audit"],
    )
    assert result.exit_code == 2
    assert "No such command 'forward'" in result.output
