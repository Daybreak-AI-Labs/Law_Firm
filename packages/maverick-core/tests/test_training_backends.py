"""Governed external-training backend compatibility tests."""
from __future__ import annotations

import json
from dataclasses import replace
from importlib.machinery import PathFinder
from pathlib import Path

import pytest
from maverick.training import backends
from maverick.training import environments as env
from maverick.training import verifiers_adapter as va

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

_NOW = 2_000_000_000.0


@pytest.fixture(autouse=True)
def _trusted_clock(monkeypatch):
    monkeypatch.setattr(env.time, "time", lambda: _NOW)


def _promotion_ready_pack(environment_id):
    seed = env.load_environment(environment_id)
    cases = []
    for split in ("train", "holdout"):
        sources = seed.split(split)
        for index in range(20):
            source = sources[index % len(sources)]
            raw = source.public_dict()
            raw.update({
                "case_id": f"{source.case_id}-proof-{index:02d}",
                "family_id": f"{source.family_id}-proof-{split}-{index:02d}",
                "prompt": f"{source.prompt}\nIndependent proof variant {index:02d}.",
                "answer_visibility": (
                    "sealed" if split == "holdout" else "published"
                ),
            })
            cases.append(env.EnvironmentCase.from_mapping(raw))
    return env.EnvironmentPack(
        environment_id=seed.environment_id,
        version=seed.version,
        description=f"{seed.description} promotion-ready test pack",
        cases=tuple(cases),
    )


def _workspace(tmp_path, **changes):
    values = {
        "root": tmp_path,
        "python_executable": tmp_path / ".venv" / "bin" / "python",
        "revision": backends.PRIME_RL_COMMIT,
        "verifiers_revision": backends.PRIME_RL_VERIFIERS_SUBMODULE_COMMIT,
        "python_version": "3.12.4",
        "operator_provisioned": True,
        "nvidia_available": True,
    }
    values.update(changes)
    return backends.PrimeRLWorkspace(**values)


def _bundle_path(tmp_path, tenant_id, bundle):
    return (
        tmp_path
        / "maverick-bundles"
        / backends.training_tenant_namespace(tenant_id)
        / bundle.package_id
    )


def _materialize(tmp_path, tenant_id, bundle):
    path = _bundle_path(tmp_path, tenant_id, bundle)
    if path.exists():
        assert bundle.verify_materialized(path)
    else:
        bundle.materialize(path)
    return path


def _spec(
    tmp_path,
    *,
    tenant_id="alpha",
    attempt=1,
    previous=None,
    checkpoint="",
    resume_step=-1,
    inherited=None,
    learning_rate=0.000003,
):
    pack = _promotion_ready_pack("privacy_assessment_v1")
    bundle = va.build_verifiers_export_bundle(
        pack,
        tenant_id=tenant_id,
    )
    bundle_path = _materialize(tmp_path, tenant_id, bundle)
    spec = backends.generate_prime_rl_run_spec(
        pack,
        bundle,
        _workspace(tmp_path),
        run_id="privacy-specialist-001",
        tenant_id=tenant_id,
        target="tenant_local",
        hosted_boundary_permission=False,
        base_model_path=tmp_path / "models" / "qwen",
        base_model_revision="a" * 40,
        base_model_sha256="b" * 64,
        environment_bundle_path=bundle_path,
        attempt=attempt,
        parent_spec_digest=previous.digest if previous is not None else "",
        resume_from_checkpoint_sha256=checkpoint,
        resume_from_checkpoint_path=(
            Path(previous.output_dir) if previous is not None else None
        ),
        resume_step=resume_step,
        inherited_environment=inherited,
        learning_rate=learning_rate,
    )
    return pack, bundle, spec


def _write_checkpoint(spec, *, content=b"checkpoint state"):
    output_dir = Path(spec.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "model-state.bin").write_bytes(content)
    manifest = backends.build_training_checkpoint_manifest(spec)
    (output_dir / backends.TRAINING_CHECKPOINT_MANIFEST_BASENAME).write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return str(manifest["checkpoint_sha256"])


def _backend(tenant_id="alpha"):
    return backends.NonExecutingTrainingBackend(
        tenant_id=tenant_id,
        name=backends.PRIME_RL_BACKEND,
        version=backends.PRIME_RL_VERSION,
        revision=backends.PRIME_RL_COMMIT,
    )


def _tenant_pack_and_registry():
    def tenant_case(case_id, split):
        raw = {
            "schema": env.CASE_SCHEMA,
            "case_id": case_id,
            "environment_id": "tenant_review_v1",
            "split": split,
            "family_id": f"family-{case_id}",
            "prompt": f"Return a JSON decision for reviewed example {case_id}.",
            "evidence_text": f"reviewed example {case_id}",
            "expected": {"decision": "review"},
            "required_citations": [],
            "provenance": "tenant_trace",
            "source_uri": f"https://customer.example/evidence/{case_id}",
            "license": "customer-controlled",
            "data_classification": "confidential",
            "answer_visibility": "sealed" if split == "holdout" else "published",
            "redaction_evidence": {"status": "not_applicable"},
        }
        admitted = env.admitted_content_sha256(raw)
        return env.EnvironmentCase.from_mapping({
            **raw,
            "consent": {
                "schema": env.CONSENT_SCHEMA,
                "record_id": f"consent-{case_id}",
                "tenant_id": "alpha",
                "case_id": case_id,
                "environment_id": "tenant_review_v1",
                "admitted_content_sha256": admitted,
                "allowed_purpose": "tenant_training",
                "valid_until": 4_000_000_000,
                "retention_days": 365,
                "approved_by": "privacy-officer",
                "revoked": False,
            },
            "redaction_evidence": {
                "schema": env.REDACTION_EVIDENCE_SCHEMA,
                "evidence_id": f"redaction-{case_id}",
                "case_id": case_id,
                "environment_id": "tenant_review_v1",
                "status": "reviewed_no_detector_matches",
                "detector": "maverick.provable_redaction",
                "detector_version": "1.0.0",
                "detector_code_sha256": "a" * 64,
                "input_sha256": "b" * 64,
                "output_sha256": admitted,
                "pass_count": 1,
                "residual_labels": [],
                "human_reviewed": True,
                "revoked": False,
            },
        })

    pack = env.EnvironmentPack(
        environment_id="tenant_review_v1",
        version="1.0.0",
        description="tenant review examples",
        cases=tuple(
            tenant_case(f"tenant-{split}-{index:02d}", split)
            for split in ("train", "holdout")
            for index in range(20)
        ),
    )
    registry = env.TrustedEvidenceRegistry.from_mapping({
        "schema": env.TRUSTED_EVIDENCE_REGISTRY_SCHEMA,
        "registry_id": "privacy-authority-001",
        "revision": "snapshot-2026-07-23",
        "consent_records": {
            case.consent.record_id: case.consent.digest
            for case in pack.cases
            if case.consent is not None
        },
        "redaction_records": {
            case.redaction_evidence.evidence_id: case.redaction_evidence.digest
            for case in pack.cases
        },
    })
    return pack, registry


def test_prime_run_spec_is_inert_offline_secret_free_and_exactly_pinned(tmp_path):
    shadow = tmp_path / "prime_rl"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(
        "raise RuntimeError('cwd shadow executed')\n",
        encoding="utf-8",
    )
    inherited = {
        "AWS_SECRET_ACCESS_KEY": "not-for-training",
        "CUDA_VISIBLE_DEVICES": "2,3",
        "HF_TOKEN": "hf-secret",
        "NCCL_DEBUG": "WARN",
        "OPENAI_API_KEY": "sk-secret",
        "PATH": "/untrusted/bin",
        "PYTHONPATH": "/attacker/ambient-package",
        "WANDB_MODE": "online",
    }
    _pack, bundle, first = _spec(tmp_path, inherited=inherited)
    _pack, _bundle, second = _spec(tmp_path, inherited=inherited)

    assert first.digest == second.digest
    assert first.verify() is True
    assert first.backend == "prime-rl"
    assert first.backend_version == "0.7.0"
    assert first.backend_revision == "d334ea52940b47f426293a7d146239e3fbf91caa"
    assert first.backend_tag == "v0.7.0"
    assert backends.PRIME_RL_TAG_OBJECT == (
        "8cab1e5f6e9ad916d292eb5b49a8800f765ba089"
    )
    assert dict(first.backend_dependency_revisions) == {
        "verifiers": "6c64ce6a3a01e8edde7c3c0e8e5315fb236e9faa",
    }
    assert bundle.verifiers_version == "0.2.0"
    assert first.environment_bundle_digest == bundle.digest
    assert first.boundary_admission_digest == bundle.admission_digest
    assert first.environment_split == "train"
    assert first.argv == (
        str((tmp_path / ".venv" / "bin" / "python").resolve()),
        "-P",
        "-m",
        "prime_rl.entrypoints.rl",
        "@",
        first.config_path,
    )
    assert first.shell is False
    assert Path(first.cwd, "prime_rl", "__init__.py").is_file()
    assert Path(first.config_path).exists() is False
    assert Path(first.output_dir).exists() is False
    assert backends.training_tenant_namespace("alpha") in first.output_dir

    process_environment = dict(first.environment)
    assert process_environment["WANDB_MODE"] == "disabled"
    assert process_environment["WANDB_DISABLED"] == "true"
    assert process_environment["HF_HUB_OFFLINE"] == "1"
    assert process_environment["PYTHONNOUSERSITE"] == "1"
    assert process_environment["PYTHONSAFEPATH"] == "1"
    assert process_environment["TRANSFORMERS_OFFLINE"] == "1"
    assert process_environment["UV_OFFLINE"] == "1"
    assert process_environment["CUDA_VISIBLE_DEVICES"] == "2,3"
    assert process_environment["NCCL_DEBUG"] == "WARN"
    assert process_environment["PYTHONPATH"] == str(
        (
            Path(first.environment_bundle_path)
            / "src"
        ).resolve(),
    )
    assert process_environment["PYTHONPATH"] != "/attacker/ambient-package"
    assert (
        PathFinder.find_spec(
            bundle.module_name,
            [process_environment["PYTHONPATH"]],
        )
        is not None
    )
    for forbidden in (
        "AWS_SECRET_ACCESS_KEY",
        "HF_TOKEN",
        "OPENAI_API_KEY",
        "PATH",
    ):
        assert forbidden not in process_environment

    serialized = first.config_text + json.dumps(first.public_dict(), sort_keys=True)
    assert "not-for-training" not in serialized
    assert "hf-secret" not in serialized
    assert "sk-secret" not in serialized
    assert "curl " not in serialized
    assert "pip install" not in serialized
    assert first.public_dict()["boundary"]["target"] == "tenant_local"
    assert first.public_dict()["boundary"]["admission_digest"] == bundle.admission_digest

    config = tomllib.loads(first.config_text)
    assert config["model"]["name"] == str((tmp_path / "models" / "qwen").resolve())
    assert config["env_vars"]["WANDB_MODE"] == "disabled"
    assert config["env_vars"]["PYTHONPATH"] == process_environment["PYTHONPATH"]
    assert config["orchestrator"]["collect_inference_metrics"] is False
    assert config["orchestrator"]["train"]["env"] == [
        {"taskset": {"id": bundle.package_id}},
    ]
    assert config["deployment"] == {
        "type": "single_node",
        "gpus_per_node": 2,
        "num_train_gpus": 1,
        "num_infer_gpus": 1,
    }

    legacy_config = first.config_text.replace(
        f'taskset = {{ id = "{bundle.package_id}" }}',
        f'id = "{bundle.package_id}"',
    )
    assert legacy_config != first.config_text
    assert replace(first, config_text=legacy_config).verify() is False

    attacker_environment = tuple(
        (
            name,
            "/attacker/ambient-package",
        )
        if name == "PYTHONPATH"
        else (name, value)
        for name, value in first.environment
    )
    attacker_config = first.config_text.replace(
        process_environment["PYTHONPATH"].replace("\\", "\\\\"),
        "/attacker/ambient-package",
    )
    assert replace(
        first,
        environment=attacker_environment,
        config_text=attacker_config,
    ).verify() is False
    unsafe_argv = tuple(item for item in first.argv if item != "-P")
    assert replace(first, argv=unsafe_argv).verify() is False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"operator_provisioned": False}, "operator-provisioned"),
        ({"nvidia_available": False}, "NVIDIA"),
        ({"python_version": "3.11.9"}, "Python 3.12"),
        ({"revision": "c" * 40}, backends.PRIME_RL_COMMIT),
        ({"verifiers_revision": "d" * 40}, "Verifiers submodule"),
    ],
)
def test_prime_workspace_pin_and_operator_requirements_fail_closed(
    tmp_path,
    changes,
    message,
):
    pack = _promotion_ready_pack("privacy_assessment_v1")
    bundle = va.build_verifiers_export_bundle(pack, tenant_id="alpha")
    path = _materialize(tmp_path, "alpha", bundle)

    with pytest.raises(backends.TrainingBackendError, match=message):
        backends.generate_prime_rl_run_spec(
            pack,
            bundle,
            _workspace(tmp_path, **changes),
            run_id="privacy-specialist-001",
            tenant_id="alpha",
            target="tenant_local",
            hosted_boundary_permission=False,
            base_model_path=tmp_path / "models" / "qwen",
            base_model_revision="a" * 40,
            base_model_sha256="b" * 64,
            environment_bundle_path=path,
        )


def test_prime_refuses_eval_split_and_standalone_verifiers_profile(tmp_path):
    pack = env.load_environment("privacy_assessment_v1")
    evaluation = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
        split="holdout",
        purpose=va.BundlePurpose.EVALUATION,
    )
    evaluation_path = _materialize(tmp_path, "alpha", evaluation)
    common = {
        "run_id": "privacy-specialist-001",
        "tenant_id": "alpha",
        "target": "tenant_local",
        "hosted_boundary_permission": False,
        "base_model_path": tmp_path / "models" / "qwen",
        "base_model_revision": "a" * 40,
        "base_model_sha256": "b" * 64,
    }
    with pytest.raises(backends.TrainingBackendError, match="split='train'"):
        backends.generate_prime_rl_run_spec(
            pack,
            evaluation,
            _workspace(tmp_path),
            environment_bundle_path=evaluation_path,
            **common,
        )

    training_pack = _promotion_ready_pack("privacy_assessment_v1")
    standalone = va.build_verifiers_export_bundle(
        training_pack,
        tenant_id="alpha",
        compatibility=va.VerifiersCompatibility.STANDALONE_V021,
    )
    standalone_path = _materialize(tmp_path, "alpha", standalone)
    with pytest.raises(backends.TrainingBackendError, match="not compatible"):
        backends.generate_prime_rl_run_spec(
            training_pack,
            standalone,
            _workspace(tmp_path),
            environment_bundle_path=standalone_path,
            **common,
        )


def test_prime_run_spec_rechecks_hosted_permission_bundle_and_materialization(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        backends,
        "_configured_model_improvement",
        lambda: {"enable": True, "allow_hosted": True},
    )
    pack = _promotion_ready_pack("dsar_routing_v1")
    hosted = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
        target="hosted",
        hosted_boundary_permission=True,
    )
    hosted_path = _materialize(tmp_path, "alpha", hosted)
    arguments = {
        "run_id": "dsar-specialist-001",
        "tenant_id": "alpha",
        "target": "hosted",
        "hosted_boundary_permission": True,
        "base_model_path": tmp_path / "models" / "qwen",
        "base_model_revision": "a" * 40,
        "base_model_sha256": "b" * 64,
        "environment_bundle_path": hosted_path,
    }
    spec = backends.generate_prime_rl_run_spec(
        pack,
        hosted,
        _workspace(tmp_path),
        **arguments,
    )
    assert spec.boundary_target == "hosted"

    with pytest.raises(backends.TrainingBackendError, match="permission"):
        backends.generate_prime_rl_run_spec(
            pack,
            hosted,
            _workspace(tmp_path),
            **{**arguments, "hosted_boundary_permission": False},
        )

    with pytest.raises(backends.TrainingBackendError, match="integrity"):
        backends.generate_prime_rl_run_spec(
            pack,
            replace(hosted, digest="0" * 64),
            _workspace(tmp_path),
            **arguments,
        )

    task_path = hosted_path / f"src/{hosted.module_name}/tasks.jsonl"
    task_path.write_bytes(task_path.read_bytes() + b"\n")
    with pytest.raises(backends.TrainingBackendError, match="materialized"):
        backends.generate_prime_rl_run_spec(
            pack,
            hosted,
            _workspace(tmp_path),
            **arguments,
        )


def test_environment_scrubber_drops_credentials_and_forces_offline_modes():
    scrubbed = dict(backends.scrub_training_environment({
        "AZURE_OPENAI_API_KEY": "secret",
        "CUDA_VISIBLE_DEVICES": "0",
        "HF_HUB_OFFLINE": "0",
        "NCCL_SOCKET_IFNAME": "eth0",
        "WANDB_MODE": "online",
    }))

    assert scrubbed == {
        "CUDA_VISIBLE_DEVICES": "0",
        "DO_NOT_TRACK": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "NCCL_SOCKET_IFNAME": "eth0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "UV_OFFLINE": "1",
        "WANDB_DISABLED": "true",
        "WANDB_MODE": "disabled",
    }
    with pytest.raises(backends.TrainingBackendError, match="not bounded"):
        backends.scrub_training_environment({"TMPDIR": "bad\nvalue"})


def test_backend_cancel_and_resume_revalidate_bundle_and_parent(
    tmp_path,
    monkeypatch,
):
    pack, bundle, initial = _spec(tmp_path)
    monkeypatch.setattr(env.time, "time", lambda: _NOW - 1)
    with pytest.raises(backends.TrainingBackendError, match="stale"):
        _backend().prepare(
            initial,
            pack=pack,
            bundle=bundle,
        )
    monkeypatch.setattr(env.time, "time", lambda: _NOW + 1)
    with pytest.raises(backends.TrainingBackendError, match="stale"):
        _backend().prepare(
            replace(initial, boundary_decision_digest="d" * 64),
            pack=pack,
            bundle=bundle,
        )
    backend = _backend()
    assert isinstance(backend, backends.TrainingBackend)

    prepared = backend.prepare(initial, pack=pack, bundle=bundle)
    assert prepared.state == backends.TrainingRunState.PREPARED
    assert backend.status(initial.run_id) == prepared
    with pytest.raises(backends.TrainingBackendError, match="already prepared"):
        backend.prepare(initial, pack=pack, bundle=bundle)

    checkpoint = _write_checkpoint(initial)
    cancelled = backend.cancel(initial.run_id)
    assert cancelled.state == backends.TrainingRunState.CANCELLED
    assert cancelled.checkpoint_sha256 == checkpoint
    assert backend.cancel(initial.run_id) == cancelled

    monkeypatch.setattr(env.time, "time", lambda: _NOW)
    _pack, resumed_bundle, resumed_spec = _spec(
        tmp_path,
        attempt=2,
        previous=initial,
        checkpoint=checkpoint,
        resume_step=17,
    )
    monkeypatch.setattr(env.time, "time", lambda: _NOW + 2)
    resumed = backend.resume(
        resumed_spec,
        pack=pack,
        bundle=resumed_bundle,
    )
    assert resumed.state == backends.TrainingRunState.PREPARED
    assert resumed.attempt == 2
    assert resumed_spec.parent_spec_digest == initial.digest
    assert (
        resumed_spec.immutable_semantics_digest
        == initial.immutable_semantics_digest
    )
    resume_config = tomllib.loads(resumed_spec.config_text)["ckpt"]
    assert resume_config["resume_step"] == 17
    assert resume_config["output_dir"] == initial.output_dir

    with pytest.raises(backends.TrainingBackendError, match="only after"):
        backend.resume(
            resumed_spec,
            pack=pack,
            bundle=resumed_bundle,
        )


@pytest.mark.parametrize(
    "mutation",
    ["model", "config", "argv", "environment", "parent", "checkpoint_path"],
)
def test_resume_rejects_every_immutable_semantic_change(tmp_path, mutation):
    pack, bundle, initial = _spec(
        tmp_path,
        inherited={"CUDA_VISIBLE_DEVICES": "0"},
    )
    backend = _backend()
    backend.prepare(initial, pack=pack, bundle=bundle)
    checkpoint = _write_checkpoint(initial)
    backend.cancel(initial.run_id)
    _pack, resumed_bundle, resumed = _spec(
        tmp_path,
        attempt=2,
        previous=initial,
        checkpoint=checkpoint,
        inherited={"CUDA_VISIBLE_DEVICES": "0"},
    )

    if mutation == "model":
        candidate = replace(resumed, base_model_sha256="d" * 64)
    elif mutation == "config":
        candidate = replace(
            resumed,
            config_text=resumed.config_text.replace(
                "lr = 3e-06",
                "lr = 4e-06",
            ),
        )
    elif mutation == "argv":
        candidate = replace(
            resumed,
            argv=(
                str((tmp_path / ".venv" / "alternate-python").resolve()),
                *resumed.argv[1:],
            ),
        )
    elif mutation == "environment":
        changed_env = tuple(
            ("CUDA_VISIBLE_DEVICES", "1") if name == "CUDA_VISIBLE_DEVICES" else (name, value)
            for name, value in resumed.environment
        )
        candidate = replace(
            resumed,
            environment=changed_env,
            config_text=resumed.config_text.replace(
                'CUDA_VISIBLE_DEVICES = "0"',
                'CUDA_VISIBLE_DEVICES = "1"',
            ),
        )
    elif mutation == "checkpoint_path":
        alternate = str(
            (
                tmp_path
                / "maverick-runs"
                / backends.training_tenant_namespace("alpha")
                / "privacy-specialist-001"
                / "unrelated-output"
            ).resolve(),
        )
        candidate = replace(
            resumed,
            resume_from_checkpoint_path=alternate,
            config_text=resumed.config_text.replace(
                resumed.resume_from_checkpoint_path.replace("\\", "\\\\"),
                alternate.replace("\\", "\\\\"),
            ),
        )
    else:
        candidate = replace(resumed, parent_spec_digest="e" * 64)

    if mutation == "parent":
        message = "parent_spec_digest"
    elif mutation == "checkpoint_path":
        message = "integrity|checkpoint path"
    else:
        message = "immutable"
    with pytest.raises(backends.TrainingBackendError, match=message):
        backend.resume(
            candidate,
            pack=pack,
            bundle=resumed_bundle,
        )


def test_resume_validates_checkpoint_before_generic_integrity(tmp_path):
    pack, bundle, initial = _spec(tmp_path)
    backend = _backend()
    backend.prepare(initial, pack=pack, bundle=bundle)
    checkpoint = _write_checkpoint(initial)
    backend.cancel(initial.run_id)
    _pack, resumed_bundle, resumed = _spec(
        tmp_path,
        attempt=2,
        previous=initial,
        checkpoint=checkpoint,
    )
    with pytest.raises(backends.TrainingBackendError, match="checkpoint"):
        backend.resume(
            replace(resumed, resume_from_checkpoint_sha256=""),
            pack=pack,
            bundle=resumed_bundle,
        )


def test_resume_requires_existing_observed_checkpoint(tmp_path):
    pack, bundle, initial = _spec(tmp_path)
    backend = _backend()
    backend.prepare(initial, pack=pack, bundle=bundle)
    cancelled = backend.cancel(initial.run_id)

    assert cancelled.checkpoint_sha256 == ""
    with pytest.raises(
        backends.TrainingBackendError,
        match="no observed manifest",
    ):
        _spec(
            tmp_path,
            attempt=2,
            previous=initial,
            checkpoint="c" * 64,
        )


def test_resume_rechecks_checkpoint_bytes_and_parent_provenance(tmp_path):
    pack, bundle, initial = _spec(tmp_path)
    backend = _backend()
    backend.prepare(initial, pack=pack, bundle=bundle)
    checkpoint = _write_checkpoint(initial)
    backend.cancel(initial.run_id)
    _pack, resumed_bundle, resumed = _spec(
        tmp_path,
        attempt=2,
        previous=initial,
        checkpoint=checkpoint,
    )

    checkpoint_file = Path(initial.output_dir) / "model-state.bin"
    checkpoint_file.write_bytes(b"tampered checkpoint state")
    with pytest.raises(
        backends.TrainingBackendError,
        match="bytes differ",
    ):
        backend.resume(resumed, pack=pack, bundle=resumed_bundle)

    checkpoint_file.write_bytes(b"checkpoint state")
    wrong_parent = replace(initial, base_model_sha256="d" * 64)
    with pytest.raises(
        backends.TrainingBackendError,
        match="another run attempt",
    ):
        _spec(
            tmp_path,
            attempt=2,
            previous=wrong_parent,
            checkpoint=checkpoint,
        )


def test_tenant_namespaces_paths_packages_and_lifecycle_do_not_collide(tmp_path):
    alpha_pack, alpha_bundle, alpha_spec = _spec(tmp_path, tenant_id="alpha")
    beta_pack, beta_bundle, beta_spec = _spec(tmp_path, tenant_id="beta")

    assert alpha_bundle.package_id != beta_bundle.package_id
    assert alpha_spec.environment_bundle_path != beta_spec.environment_bundle_path
    assert alpha_spec.output_dir != beta_spec.output_dir
    assert alpha_spec.run_id == beta_spec.run_id

    alpha_backend = _backend("alpha")
    with pytest.raises(backends.TrainingBackendError, match="another tenant"):
        alpha_backend.prepare(
            beta_spec,
            pack=beta_pack,
            bundle=beta_bundle,
        )
    alpha_backend.prepare(
        alpha_spec,
        pack=alpha_pack,
        bundle=alpha_bundle,
    )
    beta_backend = _backend("beta")
    beta_backend.prepare(
        beta_spec,
        pack=beta_pack,
        bundle=beta_bundle,
    )


def test_tenant_training_revalidates_external_authority_registry(
    tmp_path,
    monkeypatch,
):
    pack, registry = _tenant_pack_and_registry()
    monkeypatch.setenv("MAVERICK_TENANT", "alpha")

    def unavailable_registry():
        raise env.EnvironmentError("trusted registry is unavailable")

    monkeypatch.setattr(
        env,
        "_server_trusted_evidence_registry",
        unavailable_registry,
    )
    with pytest.raises(backends.TrainingBackendError, match="trusted registry"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
        )

    monkeypatch.setattr(
        env,
        "_server_trusted_evidence_registry",
        lambda: registry,
    )
    bundle = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    path = _materialize(tmp_path, "alpha", bundle)
    spec = backends.generate_prime_rl_run_spec(
        pack,
        bundle,
        _workspace(tmp_path),
        run_id="tenant-specialist-001",
        tenant_id="alpha",
        target="tenant_local",
        hosted_boundary_permission=False,
        base_model_path=tmp_path / "models" / "qwen",
        base_model_revision="a" * 40,
        base_model_sha256="b" * 64,
        environment_bundle_path=path,
    )
    backend = _backend()
    monkeypatch.setattr(
        env,
        "_server_trusted_evidence_registry",
        unavailable_registry,
    )
    with pytest.raises(backends.TrainingBackendError, match="trusted registry"):
        backend.prepare(
            spec,
            pack=pack,
            bundle=bundle,
        )
    monkeypatch.setattr(
        env,
        "_server_trusted_evidence_registry",
        lambda: registry,
    )
    result = backend.prepare(
        spec,
        pack=pack,
        bundle=bundle,
    )
    assert result.state == backends.TrainingRunState.PREPARED
