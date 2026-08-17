"""Learning-proof and domains CLI commands.

Split out of cli/__init__.py. Registered by importing this module at the end
of the package __init__ so the @main.command decorators fire on package import.
"""
from __future__ import annotations

import click

from . import main


@main.command("domains-lint")
@click.option("--ci", is_flag=True,
              help="Exit non-zero when any pack has an ERROR-level finding.")
@click.option("--warnings", "show_warnings", is_flag=True,
              help="Also print warning-level findings (quality gaps).")
def domains_lint(ci: bool, show_warnings: bool) -> None:
    """Lint every domain pack (built-in + operator) for envelope and
    quality gaps.

    Errors weaken the safety envelope (empty tool allowlist = ALL tools,
    missing/unknown max_risk); warnings are pack-quality gaps (thin persona,
    no knowledge sources, no deny list). Operator packs in the workspace
    domains dir are linted alongside the built-ins.
    """
    from ..domain import available_domains, lint_profile
    domains = available_domains()
    n_err = n_warn = 0
    for name in sorted(domains):
        errors, warnings = lint_profile(domains[name])
        n_err += len(errors)
        n_warn += len(warnings)
        for e in errors:
            click.echo(f"ERROR  {name}: {e}", err=True)
        if show_warnings:
            for w in warnings:
                click.echo(f"warn   {name}: {w}")
    click.echo(f"{len(domains)} pack(s): {n_err} error(s), {n_warn} warning(s)"
               + ("" if show_warnings else " (use --warnings to list them)"))
    if ci and n_err:
        raise click.ClickException(f"{n_err} pack error(s)")
