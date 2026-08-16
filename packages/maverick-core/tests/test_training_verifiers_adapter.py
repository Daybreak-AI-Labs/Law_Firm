"""Non-executing Verifiers compatibility export tests."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import sys
import types
from dataclasses import replace

import pytest
from maverick.training import backends
from maverick.training import environments as env
from maverick.training import verifiers_adapter as va
from maverick.training.backends import TrainingBackendError

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


def test_prime_profile_is_deterministic_content_addressed_and_exactly_pinned():
    pack = _promotion_ready_pack("dsar_routing_v1")

    first = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    second = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )

    assert first == second
    assert first.verify() is True
    assert first.verify_against(pack) is True
    assert first.manifest_sha256 == hashlib.sha256(
        first.file_map()["manifest.json"],
    ).hexdigest()
    manifest = first.manifest()
    files = first.file_map()
    assert manifest["bundle_digest"] == first.digest
    assert manifest["upstream"] == {
        "name": "verifiers",
        "repository": va.VERIFIERS_REPOSITORY,
        "profile": "prime-rl-v0.7.0",
        "tag": "v0.2.0",
        "version": "0.2.0",
        "commit": "6c64ce6a3a01e8edde7c3c0e8e5315fb236e9faa",
    }
    assert manifest["admission"]["digest"] == first.admission_digest
    assert (
        manifest["admission"]["decision_digest"]
        == first.boundary_decision_digest
        == first.boundary_decision().digest
    )
    assert manifest["controls"] == {
        "execution_performed": False,
        "auto_install": False,
        "network_required": False,
        "upload_enabled": False,
        "telemetry_enabled": False,
        "model_visible_fields": ["prompt"],
        "ambient_platform_dependency": False,
        "trusted_scoring_function": (
            f"{first.module_name}.scorer.score_output"
        ),
        "trusted_scoring_source_sha256": hashlib.sha256(
            files[f"src/{first.module_name}/scorer.py"],
        ).hexdigest(),
        "runtime_scorer_integrity_check": True,
        "runtime_task_integrity_check": True,
    }
    for path, commitment in manifest["files"].items():
        assert hashlib.sha256(files[path]).hexdigest() == commitment["sha256"]
        assert len(files[path]) == commitment["bytes"]

    project = files["pyproject.toml"].decode("utf-8")
    upstream_lock = files["UPSTREAM.lock.json"].decode("utf-8")
    project_data = tomllib.loads(project)
    assert project_data["project"]["requires-python"] == "~=3.12.0"
    assert project_data["project"]["dependencies"] == ["verifiers==0.2.0"]
    assert va.VERIFIERS_COMMIT in upstream_lock
    assert "/main" not in project + upstream_lock
    assert "curl " not in project + upstream_lock
    assert "pip install" not in project + upstream_lock

    changed_files = tuple(
        replace(item, content=item.content + b"\n")
        if item.path.endswith("tasks.jsonl")
        else item
        for item in first.files
    )
    assert replace(first, files=changed_files).verify() is False
    assert replace(first, admission_digest="0" * 64).verify() is False

    changed_scorer_files = tuple(
        replace(item, content=item.content + b"\n")
        if item.path.endswith("/scorer.py")
        else item
        for item in first.files
    )
    assert replace(first, files=changed_scorer_files).verify() is False

    changed_manifest = first.manifest()
    changed_manifest["controls"]["upload_enabled"] = True
    changed_manifest_blob = va._canonical_bytes(changed_manifest)
    changed_files = tuple(
        replace(item, content=changed_manifest_blob)
        if item.path == "manifest.json"
        else item
        for item in first.files
    )
    assert replace(
        first,
        files=changed_files,
        manifest_sha256=hashlib.sha256(changed_manifest_blob).hexdigest(),
    ).verify() is False

    wrong_pack = env.load_environment("privacy_assessment_v1")
    assert first.verify_against(wrong_pack) is False


def test_standalone_v021_is_explicit_and_not_claimed_as_prime_compatible():
    pack = _promotion_ready_pack("dsar_routing_v1")
    standalone = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
        compatibility=va.VerifiersCompatibility.STANDALONE_V021,
    )

    assert standalone.verify()
    assert standalone.compatibility == "standalone-v0.2.1"
    assert standalone.verifiers_version == "0.2.1"
    assert standalone.verifiers_revision == (
        "ab65b6e8d34b03d162408d4bcb854430a86809e6"
    )
    assert standalone.manifest()["upstream"]["tag"] == "v0.2.1"
    assert "verifiers==0.2.1" in (
        standalone.file_map()["pyproject.toml"].decode("utf-8")
    )
    assert (
        json.loads(standalone.file_map()["UPSTREAM.lock.json"])[
            "prime_rl_compatibility"
        ]
        is None
    )


def test_export_keeps_only_prompt_model_visible_and_runtime_verifies_tasks():
    pack = _promotion_ready_pack("privacy_assessment_v1")
    bundle = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    files = bundle.file_map()
    task_path = f"src/{bundle.module_name}/tasks.jsonl"
    rows = [
        json.loads(line)
        for line in files[task_path].decode("utf-8").splitlines()
    ]

    assert len(rows) == len(pack.split("train"))
    assert bundle.case_ids == tuple(sorted(row["case_id"] for row in rows))
    for row in rows:
        assert set(row) == {
            "case_id",
            "case_digest",
            "environment_digest",
            "prompt",
            "case_payload",
        }
        assert row["prompt"] == next(
            case.prompt for case in pack.cases if case.case_id == row["case_id"]
        )
        assert "expected" not in row["prompt"]
        assert row["case_payload"]["expected"]
        assert row["environment_digest"] == pack.digest

    source = files[f"src/{bundle.module_name}/taskset.py"].decode("utf-8")
    scorer = files[f"src/{bundle.module_name}/scorer.py"]
    runtime_lock = json.loads(
        files[f"src/{bundle.module_name}/bundle.lock.json"],
    )
    compile(source, f"{bundle.module_name}/taskset.py", "exec")
    compile(scorer, f"{bundle.module_name}/scorer.py", "exec")
    assert "_SCORE_OUTPUT(" in source
    assert 'prompt=row["prompt"]' in source
    assert "EXPECTED_VERIFIERS_VERSION = \"0.2.0\"" in source
    assert va.VERIFIERS_COMMIT in source
    assert "EXPECTED_RUNTIME_LOCK_SHA256" in source
    assert "Maverick task data integrity verification failed" in source
    assert "Maverick scorer integrity verification failed" in source
    assert source.index("hashlib.sha256(scorer_blob)") < source.index(
        "exec(compile(scorer_blob",
    )
    assert "from maverick" not in source
    assert "import maverick" not in source
    assert runtime_lock["scorer"] == {
        "path": "scorer.py",
        "sha256": hashlib.sha256(scorer).hexdigest(),
        "bytes": len(scorer),
        "contract": va.PORTABLE_SCORER_CONTRACT,
    }
    assert "row_sha256" in source
    assert "subprocess" not in source
    assert "requests" not in source
    assert "OPENAI_API_KEY" not in source
    assert "HF_TOKEN" not in source


def test_portable_scorer_matches_authoritative_reward_for_shipped_cases():
    namespace: dict[str, object] = {}
    source = va._portable_scorer_source()
    exec(compile(source, "locked-maverick-scorer.py", "exec"), namespace)
    portable_score = namespace["score_output"]
    assert callable(portable_score)
    assert namespace["SCORER_CONTRACT"] == va.PORTABLE_SCORER_CONTRACT

    for environment_id in env.list_environment_ids():
        pack = env.load_environment(environment_id)
        for case in pack.cases:
            expected = env.expected_output(case)
            outputs = (
                expected,
                json.dumps(expected, sort_keys=True),
                "{}",
                '{"decision":"allow","decision":"deny"}',
                '{"value":NaN}',
                {**expected, "unexpected": True},
                "not-json",
                "x" * (env.MAX_OUTPUT_BYTES + 1),
            )
            for output in outputs:
                assert portable_score(
                    va._task_payload(case),
                    output,
                ) == env.score_output(case, output).reward


def test_generated_taskset_loads_verified_scorer_and_scores(
    tmp_path,
    monkeypatch,
):
    class FakeTaskData:
        def __init__(self, **values):
            self.__dict__.update(values)

    class FakeTask:
        @classmethod
        def __class_getitem__(cls, _parameters):
            return cls

        def __init__(self, data, config):
            self.data = data
            self.config = config

    class FakeTasksetConfig:
        def __init__(self):
            self.task = object()

    class FakeTaskset:
        @classmethod
        def __class_getitem__(cls, _parameters):
            return cls

        def __init__(self, config):
            self.config = config

    fake_v1 = types.ModuleType("verifiers.v1")
    fake_v1.TaskData = FakeTaskData
    fake_v1.Task = FakeTask
    fake_v1.TasksetConfig = FakeTasksetConfig
    fake_v1.Taskset = FakeTaskset
    fake_v1.Trace = object
    fake_v1.stop = lambda function: function
    fake_v1.reward = lambda **_kwargs: lambda function: function
    fake_verifiers = types.ModuleType("verifiers")
    fake_verifiers.v1 = fake_v1
    monkeypatch.setitem(sys.modules, "verifiers", fake_verifiers)
    monkeypatch.setitem(sys.modules, "verifiers.v1", fake_v1)
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "0.2.0" if name == "verifiers" else "",
    )

    pack = _promotion_ready_pack("privacy_assessment_v1")
    bundle = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    destination = bundle.materialize(tmp_path / "bundle")
    taskset_path = destination / f"src/{bundle.module_name}/taskset.py"
    generated = types.ModuleType(f"{bundle.module_name}.taskset")
    generated.__file__ = str(taskset_path)
    generated.__package__ = bundle.module_name
    exec(
        compile(taskset_path.read_bytes(), str(taskset_path), "exec"),
        generated.__dict__,
    )

    tasks = generated.MaverickTaskset(FakeTasksetConfig()).load()
    assert tasks
    first = tasks[0]
    case = next(
        item for item in pack.cases
        if item.case_id == first.data.case_payload["case_id"]
    )
    reply = json.dumps(env.expected_output(case), sort_keys=True)
    trace = types.SimpleNamespace(last_reply=reply, num_turns=1)
    assert asyncio.run(first.deterministic_reward(trace)) == (
        env.score_output(case, reply).reward
    )


def test_materialized_handoff_requires_exact_files_and_bytes(tmp_path):
    pack = _promotion_ready_pack("article28_clause_v1")
    bundle = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    destination = tmp_path / "bundle"

    assert bundle.materialize(destination) == destination.resolve()
    assert bundle.verify_materialized(destination)
    with pytest.raises(va.VerifiersExportError, match="must not already exist"):
        bundle.materialize(destination)

    (destination / "undeclared.txt").write_text("extra", encoding="utf-8")
    assert bundle.verify_materialized(destination) is False
    (destination / "undeclared.txt").unlink()
    tasks = destination / f"src/{bundle.module_name}/tasks.jsonl"
    tasks.write_bytes(tasks.read_bytes() + b"\n")
    assert bundle.verify_materialized(destination) is False


def test_export_is_pure_optional_dependency_free_and_tenant_namespaced(monkeypatch):
    pack = _promotion_ready_pack("article28_clause_v1")
    monkeypatch.setitem(sys.modules, "verifiers", None)

    alpha = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
    )
    beta = va.build_verifiers_export_bundle(
        pack,
        tenant_id="beta",
    )

    assert alpha.package_id.startswith("lw-t")
    assert alpha.module_name.startswith("lw_t")
    assert alpha.package_id != beta.package_id
    assert alpha.digest != beta.digest
    assert alpha.manifest()["controls"]["execution_performed"] is False


def test_training_and_evaluation_splits_are_structurally_separate():
    pack = env.load_environment("privacy_assessment_v1")

    with pytest.raises(va.VerifiersExportError, match="split='train'"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            split="holdout",
            purpose=va.BundlePurpose.TRAINING,
        )
    with pytest.raises(va.VerifiersExportError, match="evaluation bundles"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            split="train",
            purpose=va.BundlePurpose.EVALUATION,
        )

    evaluation = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
        split="holdout",
        purpose=va.BundlePurpose.EVALUATION,
    )
    assert evaluation.purpose == "evaluation"
    assert evaluation.split == "holdout"
    assert evaluation.verify()


def test_seed_pack_is_eval_only_and_authority_inputs_are_not_public():
    seed = env.load_environment("privacy_assessment_v1")

    with pytest.raises(env.EnvironmentError, match="promotion-ready"):
        va.build_verifiers_export_bundle(seed, tenant_id="alpha")
    evaluation = va.build_verifiers_export_bundle(
        seed,
        tenant_id="alpha",
        split="holdout",
        purpose=va.BundlePurpose.EVALUATION,
    )
    assert evaluation.verify()
    with pytest.raises(TypeError):
        va.build_verifiers_export_bundle(
            seed,
            tenant_id="alpha",
            now=_NOW,
        )
    with pytest.raises(TypeError):
        va.build_verifiers_export_bundle(
            seed,
            tenant_id="alpha",
            trusted_evidence=object(),
        )


def test_hosted_export_needs_explicit_permission_and_closed_target_enum(
    monkeypatch,
):
    pack = _promotion_ready_pack("dsar_routing_v1")
    with pytest.raises(TrainingBackendError, match="explicit boundary permission"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            target="hosted",
        )

    with pytest.raises(TrainingBackendError, match="disabled by"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            target="hosted",
            hosted_boundary_permission=True,
        )

    monkeypatch.setattr(
        backends,
        "_configured_model_improvement",
        lambda: {"enable": True, "allow_hosted": True},
    )
    hosted = va.build_verifiers_export_bundle(
        pack,
        tenant_id="alpha",
        target="hosted",
        hosted_boundary_permission=True,
    )
    assert hosted.target == "hosted"
    assert hosted.manifest()["boundary"]["hosted_permission"] is True

    with pytest.raises(TrainingBackendError, match="cross-tenant"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            target="cross_tenant",
        )
    with pytest.raises(TrainingBackendError, match="unsupported training target"):
        va.build_verifiers_export_bundle(
            pack,
            tenant_id="alpha",
            target="tenant-local-typo",
        )


def test_sealed_answers_are_not_copied_into_an_external_task_bundle():
    pack = _promotion_ready_pack("privacy_assessment_v1")
    sealed_cases = tuple(
        replace(case, answer_visibility="sealed")
        if case.split == "train"
        else case
        for case in pack.cases
    )
    sealed = replace(pack, cases=sealed_cases)

    with pytest.raises(env.EnvironmentError, match="sealed answers"):
        va.build_verifiers_export_bundle(
            sealed,
            tenant_id="alpha",
        )
