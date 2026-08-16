"""Analytics, outcomes, and cost-reporting CLI commands.

Split out of cli/__init__.py. Registered by importing this module at the end
of the package __init__ so the @main.command decorators fire on package import.

open_world is resolved lazily inside each command that opens the world DB, so
tests that monkeypatch maverick.cli.open_world still reach those commands.
"""
from __future__ import annotations

import click

from . import _strip_terminal_control, main


@main.command("record-outcome")
@click.argument("goal_id", type=int)
@click.argument("episode_id", type=int)
@click.argument("value", type=float)
@click.option("--kind", default="", help="What the outcome is (e.g. invoice_paid, renewed).")
def record_outcome(goal_id: int, episode_id: int, value: float, kind: str) -> None:
    """Feed a REAL downstream outcome back to a past episode (the grounded reward).

    The Consequence Engine's ingestion entrypoint -- a system-of-record connector
    (or a human) calls this once reality reports back: ``maverick record-outcome
    <goal_id> <episode_id> <value>`` with value in [0,1] (paid=1.0, reopened=0.0,
    or a graded result). The flywheel then prefers this over the verifier proxy
    when it next turns, so learning is grounded in what actually happened.
    """
    from ..consequence import record_outcome as _rec
    ok = _rec(goal_id, episode_id, value, kind=kind)
    click.echo(
        f"recorded outcome {value:g} for goal {goal_id} episode {episode_id}"
        f"{(' (' + kind + ')') if kind else ''}" if ok
        else "failed to record outcome")




@main.command("flywheel")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def flywheel(as_json: bool) -> None:
    """Turn the Cognitive Data Engine flywheel once over the Operating Record.

    One grounded pass: triage production failures by causal impact, mine
    self-correcting guardrails, consolidate beneficial habits into procedural
    memory, and propose process improvements -- learning from REAL outcomes where
    they've reported back. A no-op unless ``[data_engine]`` is enabled.
    """
    import json as _json

    from ..flywheel import maybe_run
    rep = maybe_run()
    if as_json:
        click.echo(_json.dumps({
            "n_episodes": rep.n_episodes,
            "guardrails": [g.to_dict() for g in rep.guardrails],
            "memories": [m.to_dict() for m in rep.memories],
            "hypotheses": [{"swap": f"{h.baseline_action}->{h.candidate_action}",
                            "predicted_lift": h.predicted_lift} for h in rep.hypotheses],
            "predicted_lift": rep.predicted_lift,
        }))
        return
    if not rep.acted:
        click.echo("Flywheel: nothing to learn yet "
                   "(data engine off, or no failures/habits in the corpus).")
        return
    click.echo(click.style(f"Flywheel — one turn over {rep.n_episodes} episodes", bold=True))
    if rep.guardrails:
        click.echo(f"  guardrails learned: {len(rep.guardrails)} "
                   f"(recoverable lift ~{rep.predicted_lift:.2f})")
        for g in rep.guardrails[:5]:
            click.echo(f"    avoid '{g.action}' (severity {g.severity:.2f})")
    if rep.memories:
        click.echo(f"  habits consolidated: {len(rep.memories)}")
        for m in rep.memories[:5]:
            click.echo(f"    prefer '{m.action}' (strength {m.strength:.2f})")
    if rep.hypotheses:
        click.echo(f"  improvements proposed: {len(rep.hypotheses)}")
        for h in rep.hypotheses[:5]:
            click.echo(f"    swap '{h.baseline_action}' -> '{h.candidate_action}' "
                       f"(predicted +{h.predicted_lift:.2f})")


@main.command("cost-retro")
@click.option("--top", type=int, default=10, help="How many costliest goals to show.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def cost_retro(ctx, top: int, as_json: bool) -> None:
    """Cost retrospective: where spend went, and what to do about it.

    Reads recorded per-goal spend and reports the costliest goals, how much
    went to failed work, how concentrated spend is, and actionable
    observations. Read-only.
    """
    import json as _json

    from ..cost.retrospective import retrospective
    from . import open_world  # lazy: monkeypatch-reachable
    world = open_world(ctx.obj["db"])
    rep = retrospective(world, top_n=top)
    if as_json:
        click.echo(_json.dumps(rep))
        return
    click.echo(click.style(
        f"Cost retrospective — ${rep['total_spend']:.2f} across "
        f"{rep['priced_goals']} priced goal(s)", bold=True))
    if rep["failed_spend"]:
        click.echo(f"  failed work: ${rep['failed_spend']:.2f} "
                   f"({rep['failed_share']:.0%})")
    if rep["top_goals"]:
        click.echo(click.style("\nCostliest goals", bold=True))
        for r in rep["top_goals"]:
            flag = " [FAILED]" if r["failed"] else ""
            title = _strip_terminal_control(r["title"])
            click.echo(f"  #{r['goal_id']} ${r['cost']:.2f} "
                       f"({r['episodes']} ep){flag}  {title}")
    click.echo(click.style("\nObservations", bold=True))
    for o in rep["observations"]:
        click.echo(f"  • {o}")


@main.command("charts")
@click.option("--days", type=int, default=7, help="How many days to chart.")
@click.option("--plain", is_flag=True, help="Force plain ASCII (no rich panels).")
@click.pass_context
def charts(ctx, days: int, plain: bool) -> None:
    """Inline terminal charts: spend/day, goal throughput, tool latency.

    Sparklines + bars drawn from recorded data — the usage ledger (spend),
    the world model (done/failed per day), and the tool-latency profile.
    Uses ``rich`` panels when installed; falls back to plain ASCII. Sections
    with no data say so. Read-only.
    """
    from .. import terminal_charts, tool_latency
    from . import open_world  # lazy: monkeypatch-reachable
    world = open_world(ctx.obj["db"])
    report = tool_latency.report()
    if plain:
        click.echo(terminal_charts.render_dashboard(world, None, report, days=days))
        return
    out = terminal_charts.render_dashboard_rich(world, None, report, days=days)
    if isinstance(out, str):
        click.echo(out)
    else:
        from rich.console import Console
        Console().print(out)
