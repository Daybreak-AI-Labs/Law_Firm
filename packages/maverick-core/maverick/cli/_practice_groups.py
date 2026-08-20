"""Practice-operations CLI: the governed per-matter dream beat.

This module retains only explicit, matter-authorized dreaming and rehearsal.
Fleet-wide automatic harness cycles and unrelated tax-preparation commands are
not part of the two-attorney product.

Registered via import at the end of the package __init__ so the @main.group
decorators fire on package import.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

import click

from . import main, open_world

_T = TypeVar("_T")


def _ensure_dreaming_allowed() -> None:
    """Translate the strict learning stop into a stable CLI refusal."""
    from ..learning_guard import Halted, check_learning_halt
    try:
        check_learning_halt("dreaming", "cli_start")
    except Halted as exc:
        raise click.ClickException(
            "dreaming refused: global learning HALT is active") from exc


def _run_dreaming_guarded(operation: Callable[[], _T]) -> _T:
    """Run one dream operation while keeping ``Halted`` out of tracebacks."""
    from ..learning_guard import Halted
    try:
        return operation()
    except Halted as exc:
        raise click.ClickException(
            "dreaming refused: global learning HALT is active") from exc


async def _run_matter_rehearsal_goal(
    *,
    world,
    llm,
    sandbox,
    prompt: str,
    matter_id: int,
    owner: str,
    domain: str,
    budget_dollars: float,
) -> str:
    """Create and run one offline test goal under fresh durable authority."""
    from ..budget import Budget
    from ..matter_context import (
        MatterContextError,
        matter_context_scope,
        resolve_goal_matter_context,
        resolve_matter_context,
        verify_context_snapshot,
    )

    try:
        prepared = resolve_matter_context(
            world,
            matter_id=matter_id,
            principal=owner,
            domain=domain,
            source="dream-rehearsal-prepare",
        )
        goal_id = world.create_matter_goal(
            f"[rehearsal] {prompt[:200]}",
            "Offline rehearsal of a previously failing matter pattern.",
            principal=prepared.principal,
            domain=prepared.domain,
            project_id=prepared.matter_id,
        )
        if goal_id is None:
            return "BLOCKED: rehearsal matter membership is no longer active"
        # Re-resolve after the atomic INSERT so a revocation or egress-policy
        # change between preparation and execution is observed before run_goal.
        context = resolve_goal_matter_context(
            world,
            goal_id,
            principal=prepared.principal,
            source="dream-rehearsal-execute",
        )
        verify_context_snapshot(
            context,
            matter_id=prepared.matter_id,
            principal=prepared.principal,
            domain=prepared.domain,
        )
    except (MatterContextError, ValueError) as exc:
        return f"BLOCKED: rehearsal authority refused ({exc})"

    from ..orchestrator import run_goal

    with matter_context_scope(context):
        return await run_goal(
            llm=llm,
            world=world,
            budget=Budget(max_dollars=budget_dollars),
            goal_id=goal_id,
            sandbox=sandbox,
            domain=context.domain,
            user_id=context.principal,
        )


@main.command()
@click.option("--max-goals", default=50, show_default=True,
              help="How many recent finished goals to replay.")
@click.option("--rehearse", is_flag=True,
              help="After consolidating, RUN the queued rehearsal cases as "
                   "real (budgeted) practice goals. Spends tokens; gated by "
                   "the calibration interlock.")
@click.option("--rehearse-budget", default=1.0, show_default=True,
              help="Max $ per rehearsal case.")
@click.option("--dry-run", is_flag=True,
              help="Run the full cycle against TEMP COPIES of every learned "
                   "store and report what WOULD change, writing nothing.")
@click.option("--list-snapshots", "list_snaps", is_flag=True,
              help="List learning-state snapshots available for --rollback.")
@click.option("--rollback", default=None, metavar="SNAPSHOT",
              help="Restore every learned store from a snapshot "
                   "('latest' or a name from --list-snapshots), then exit.")
@click.pass_context
def dream(  # noqa: C901
    ctx,
    max_goals: int,
    rehearse: bool,
    rehearse_budget: float,
    dry_run: bool,
    list_snaps: bool,
    rollback: str | None,
) -> None:
    """Run one offline dreaming cycle (experience consolidation).

    Replays recent successes and failure reflexions, groups them by
    department (domain packs), distills recurring wins into learned skills,
    consolidates recurring failures into dream insights (recalled on future
    similar goals), retires learned skills with a decayed track record, and
    prunes stale near-duplicate reflexions. The consolidation pass is
    deterministic and LLM-free by default -- costs no tokens. Opt in to
    [dreaming] llm_consolidation (or MAVERICK_LLM_CONSOLIDATION=1) to have the
    cheap summarizer model rewrite each lesson into a transferable one, metered
    by [dreaming] llm_consolidation_budget and fail-open to the deterministic
    text. Requires [dreaming] enable = true or MAVERICK_DREAMING=1; run from
    cron/systemd nightly.

    With --rehearse (and [dreaming] rehearse = true to queue cases), the
    biggest recurring failure patterns are re-run as budgeted practice goals
    (titled "[rehearsal] ...") so the next real attempt starts from a system
    that has already practiced. Refused while verifier calibration is frozen.
    """
    from .. import dreaming
    if list_snaps:
        snaps = dreaming.list_snapshots()
        click.echo("\n".join(snaps) if snaps else "(no snapshots yet)")
        return
    if rollback:
        try:
            restored = dreaming.rollback_learning_state(rollback)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        if not restored:
            raise click.ClickException("no snapshots to roll back to.")
        click.echo("Restored learned state from snapshot: "
                   + ", ".join(restored))
        return
    if not dreaming.enabled():
        raise click.ClickException(
            "dreaming is disabled. Set MAVERICK_DREAMING=1 or add\n"
            "  [dreaming]\n  enable = true\nto ~/.maverick/config.toml."
        )
    # Keep snapshot creation and provider initialization behind the same strict
    # stop as the cycle. Rollback/listing returned above and remain available.
    _ensure_dreaming_allowed()
    world = open_world(ctx.obj["db"])
    if dry_run:
        report = _run_dreaming_guarded(
            lambda: dreaming.dream_cycle_dry(world, max_goals=max_goals),
        )
        click.echo("(dry run -- nothing written) " + report.summary())
        return
    # Learning rollback, half 1: snapshot every learned store before this
    # cycle mutates anything, so `--rollback latest` can undo it wholesale.
    cfg = dreaming.settings()
    if cfg.get("snapshots", True):
        snap = dreaming.snapshot_learning_state(
            keep_last=int(cfg.get("snapshot_keep_last", 5)),
        )
        if snap is not None:
            click.echo(f"[snapshot: {snap.name}]")
    # LLM-in-the-loop consolidation (two-key opt-in: the dreaming algorithm
    # knob plus the independent learning provider-egress authority):
    # wire the SAME configured LLM the platform runs on (cheap summarizer role)
    # into insight consolidation, metered by its own small budget and scanned by
    # the shield. Fail-open: with the knob off this stays the deterministic path.
    dream_llm = dream_budget = dream_shield = None
    if dreaming._llm_consolidation_authorized(cfg):
        from ..budget import Budget
        from ..llm import LLM, model_for_role
        dream_llm = LLM(model=model_for_role("summarizer"))
        dream_budget = Budget(
            max_dollars=float(cfg.get("llm_consolidation_budget", 1.0)),
        )
        try:
            from maverick_shield import Shield
            dream_shield = Shield.from_config()
        except Exception:  # pragma: no cover -- kernel runs without the shield
            dream_shield = None
    report = _run_dreaming_guarded(
        lambda: dreaming.dream_cycle(
            world, max_goals=max_goals,
            llm=dream_llm, budget=dream_budget, shield=dream_shield,
        ),
    )
    click.echo(report.summary())
    if not rehearse:
        return
    cases = dreaming.load_rehearsals()
    if not cases:
        click.echo("Rehearsal: no queued cases (enable [dreaming] rehearse "
                   "so dream cycles queue recurring failures).")
        return
    from ..llm import LLM, model_for_role
    from ..sandbox import build_sandbox

    llm = LLM(model=ctx.obj["model"] or model_for_role("orchestrator"))
    sandbox = build_sandbox()

    async def _practice(
        prompt: str, *, matter_id: int, owner: str, domain: str,
    ) -> str:
        return await _run_matter_rehearsal_goal(
            world=world,
            llm=llm,
            sandbox=sandbox,
            prompt=prompt,
            matter_id=matter_id,
            owner=owner,
            domain=domain,
            budget_dollars=rehearse_budget,
        )

    async def _score(prompt: str, output: str) -> float:
        # Verifier-scored rehearsal: completion alone is a weak signal, so a
        # case only counts as practiced when the calibrated verifier rates
        # the answer too. Scoring spends from its own small budget.
        from ..budget import Budget
        from ..verifier import verify_proposal
        v = await verify_proposal(
            prompt, output, llm, Budget(max_dollars=max(0.25, rehearse_budget / 4)),
        )
        return float(getattr(v, "confidence", 0.0) or 0.0)

    async def _run_all_scopes() -> tuple[int, int]:
        passed = total = 0
        scopes = sorted({
            (int(case["matter_id"]), str(case["owner"]))
            for case in cases
        })
        for matter_id, owner in scopes:
            scope_passed, scope_total = await dreaming.rehearse(
                _practice,
                scorer=_score,
                matter_id=matter_id,
                owner=owner,
            )
            passed += scope_passed
            total += scope_total
        return passed, total

    try:
        passed, total = _run_dreaming_guarded(
            lambda: asyncio.run(_run_all_scopes()),
        )
    except dreaming.RehearsalFrozen as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Rehearsal: {passed}/{total} previously-failing pattern(s) "
               "now complete (verifier-scored).")

