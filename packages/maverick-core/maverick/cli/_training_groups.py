"""``maverick reward-model`` -- train + apply the CPU reward model.

Exposes :mod:`maverick.training.reward_model` through the main CLI (it otherwise
only had a ``python -m`` entry), so an operator can distil verifier rewards into
a fast scalar reward and rank trajectories without a GPU. Registered by import
at the end of the package __init__, like the other ``_*_groups`` modules.
"""
from __future__ import annotations

import json as _json
from dataclasses import asdict

import click

from . import main


def _strict_json_object(raw: str) -> dict:
    """Decode one finite JSON object without silently collapsing duplicate keys."""
    def reject_constant(token: str) -> object:
        raise ValueError(f"non-standard JSON constant {token!r}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON field {key!r}")
            decoded[key] = value
        return decoded

    value = _json.loads(
        raw,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("outputs must be a CASE_ID-to-output JSON object")
    return value


@main.group("reward-model")
def reward_model_group() -> None:
    """Train + apply the CPU reward model (RLAIF flywheel, no GPU).

    The model learns from verifier-reward preference pairs over structural
    trajectory features and scores new attempts -- a cheap pre-filter before a
    deep verifier pass.
    """


@reward_model_group.command("train")
@click.argument("data", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "-o", "out", required=True, type=click.Path(dir_okay=False),
              help="Output path for the learned reward-model JSON.")
@click.option("--min-margin", type=float, default=0.5, show_default=True,
              help="Minimum verifier-reward gap for a preference pair.")
@click.option("--max-pairs", type=int, default=64, show_default=True,
              help="Max preference pairs per task family.")
@click.option("--epochs", type=int, default=200, show_default=True)
@click.option("--lr", type=float, default=0.1, show_default=True)
def reward_model_train_cmd(data, out, min_margin, max_pairs, epochs, lr) -> None:
    """Train a reward model from a Klear-format trajectory JSONL (from
    ``maverick`` training.ingest)."""
    from ..training.reward_model import load_klear, train_reward_model
    rows = load_klear(data)
    if not rows:
        raise click.ClickException(f"no trajectories loaded from {data}")
    model, report = train_reward_model(
        rows, min_margin=min_margin, max_pairs_per_group=max_pairs,
        epochs=epochs, lr=lr)
    if report["pairs"] == 0:
        raise click.ClickException(
            "no preference pairs (need >=2 attempts per task family with a "
            f"reward gap >= {min_margin}); nothing to train on.")
    model.save(out)
    click.echo(
        f"trained on {report['pairs']} pairs -- {report['accuracy']:.0%} pairwise "
        f"accuracy, loss {report['loss']:.4f} -> {out}")


@reward_model_group.command("score")
@click.argument("model_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("data", type=click.Path(exists=True, dir_okay=False))
@click.option("--top", type=int, default=0,
              help="Show only the top-N highest-scoring trajectories (0 = all).")
def reward_model_score_cmd(model_path, data, top) -> None:
    """Score each trajectory in a Klear JSONL with a trained reward MODEL_PATH,
    printing ``id`` and score sorted high-to-low."""
    from ..training.reward_model import PreferenceRewardModel, load_klear
    try:
        model = PreferenceRewardModel.load(model_path)
    except (ValueError, OSError) as e:
        raise click.ClickException(f"cannot load reward model: {e}") from e
    rows = load_klear(data)
    scored = sorted(
        ({"id": r.get("id"), "score": round(model.score(r), 6)} for r in rows),
        key=lambda x: x["score"], reverse=True)
    if top > 0:
        scored = scored[:top]
    click.echo(_json.dumps(scored, indent=2))


@main.group("model-improvement")
def model_improvement_group() -> None:
    """Inspect deterministic tasksets and specialist-model candidates."""


@model_improvement_group.command("status")
def model_improvement_status_cmd() -> None:
    """Show control posture, taskset readiness, and catalog identity."""
    from ..config import get_model_improvement
    from ..training.environments import (
        list_environment_ids,
        load_environment,
        promotion_readiness,
    )
    from ..training.specialist_models import load_catalog

    settings = get_model_improvement()
    catalog = load_catalog()
    environments = []
    for environment_id in list_environment_ids():
        pack = load_environment(environment_id)
        environments.append(promotion_readiness(
            pack,
            minimum_train_families=settings["minimum_train_families"],
            minimum_holdout_families=settings["minimum_holdout_families"],
        ))
    click.echo(_json.dumps({
        "config": settings,
        "catalog": {
            "as_of": catalog.as_of,
            "sha256": catalog.digest,
            "candidates": len(catalog.models),
            "claim": "planning candidates; no model is production-qualified",
        },
        "environments": environments,
    }, indent=2, sort_keys=True))


@model_improvement_group.command("models")
@click.option("--role", required=True, type=str)
@click.option("--memory-gib", required=True, type=float)
@click.option("--context-tokens", default=16_384, show_default=True, type=int)
@click.option("--concurrency", default=1, show_default=True, type=int)
def model_improvement_models_cmd(
    role,
    memory_gib,
    context_tokens,
    concurrency,
) -> None:
    """List planning candidates for one role and memory envelope."""
    from ..training.specialist_models import candidate_matrix, load_catalog

    catalog = load_catalog()
    rows = []
    for candidate, estimate in candidate_matrix(
        role,
        available_memory_gib=memory_gib,
        context_tokens=context_tokens,
        concurrency=concurrency,
        catalog=catalog,
    ):
        rows.append({
            "catalog_id": candidate.catalog_id,
            "model_id": candidate.model_id,
            "upstream_revision": candidate.upstream_revision,
            "license": candidate.license,
            "status": candidate.status,
            "planning_priority": candidate.planning_priority,
            "estimate": asdict(estimate),
        })
    click.echo(_json.dumps({
        "catalog_sha256": catalog.digest,
        "role": role,
        "planning_only": True,
        "candidates": rows,
    }, indent=2, sort_keys=True))


@model_improvement_group.command("score")
@click.argument("environment_id")
@click.argument("outputs", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--split",
    type=click.Choice(["train", "validation", "holdout"]),
    default="holdout",
    show_default=True,
)
@click.option(
    "--require-perfect",
    is_flag=True,
    help="Exit non-zero unless every selected case passes.",
)
def model_improvement_score_cmd(
    environment_id,
    outputs,
    split,
    require_perfect,
) -> None:
    """Score a CASE_ID-to-output JSON object without an LLM judge."""
    from pathlib import Path

    from ..training.environments import evaluate_outputs, load_environment

    try:
        raw = _strict_json_object(Path(outputs).read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError, ValueError) as exc:
        raise click.ClickException(f"cannot read outputs: {exc}") from exc
    evaluation = evaluate_outputs(
        load_environment(environment_id),
        raw,
        split=split,
    )
    payload = {
        "schema": evaluation.schema,
        "environment_id": evaluation.environment_id,
        "environment_digest": evaluation.environment_digest,
        "split": evaluation.split,
        "score": evaluation.score,
        "pass_rate": evaluation.pass_rate,
        "evaluation_sha256": evaluation.digest,
        "missing_case_ids": list(evaluation.missing_case_ids),
        "extra_case_ids": list(evaluation.extra_case_ids),
        "results": [asdict(result) for result in evaluation.results],
    }
    click.echo(_json.dumps(payload, indent=2, sort_keys=True))
    if require_perfect and evaluation.pass_rate != 1.0:
        raise click.ClickException("selected split did not pass perfectly")
