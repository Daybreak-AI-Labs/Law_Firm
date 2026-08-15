from __future__ import annotations

import ast
import gzip
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


REPO_ROOT = Path(__file__).resolve().parents[3]


def _normalise_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _requirement_project_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match is not None, f"cannot parse requirement name: {requirement!r}"
    return _normalise_project_name(match.group())


def _ci_constraints() -> dict[str, str]:
    """Parse the reviewed CI lock and require one exact registry pin per line."""

    lines = [
        line.strip()
        for line in (REPO_ROOT / "requirements" / "ci.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    constraints: dict[str, str] = {}
    for line in lines:
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        assert requirement.url is None
        assert not requirement.extras
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version
        name = _normalise_project_name(requirement.name)
        assert name not in constraints, f"duplicate CI constraint: {name}"
        constraints[name] = specifiers[0].version
    return constraints


def _workflow_step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    if marker in workflow:
        start = workflow.index(marker)
    else:
        name_line = f"        name: {name}\n"
        name_position = workflow.index(name_line)
        start = workflow.rfind("\n      - ", 0, name_position) + 1
    end = workflow.find("\n      - ", start + len(marker))
    return workflow[start:] if end < 0 else workflow[start:end]


def _continued_shell_command(step: str, first_line: str) -> str:
    lines = step.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == first_line
    )
    command = [lines[start].strip()]
    while command[-1].endswith("\\"):
        start += 1
        command.append(lines[start].strip())
    return "\n".join(command)


def _release_input_assembler() -> str:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    step = _workflow_step(
        workflow,
        "Validate exact producer cohorts and assemble trusted release inputs",
    )
    marker = "python -I - <<'PY'\n"
    source = step.split(marker, 1)[1].rsplit("\n          PY", 1)[0]
    return textwrap.dedent(source)


def _release_input_fixture(root: Path, *, tag: str = "v1.2.3") -> tuple[Path, Path]:
    input_root = root / "release-inputs"
    output_root = root / "dist"
    expected = {
        "binary-linux": {"maverick-linux-x86_64"},
        "binary-macos": {"maverick-macos-arm64"},
        "binary-windows": {"maverick-windows-x86_64.exe"},
        "sbom": {f"maverick-sbom-{tag}.cdx.json"},
    }
    for producer, names in expected.items():
        producer_root = input_root / producer
        producer_root.mkdir(parents=True)
        for name in names:
            (producer_root / name).write_bytes(f"{producer}:{name}\n".encode())
    output_root.mkdir()
    return input_root, output_root


def _run_release_input_assembler(
    root: Path,
    input_root: Path,
    *,
    tag: str = "v1.2.3",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "RELEASE_INPUT_ROOT": str(input_root),
            "RELEASE_TAG": tag,
        }
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", _release_input_assembler()],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _load_artifact_normalizer():
    path = REPO_ROOT / "scripts" / "normalize_python_artifacts.py"
    spec = importlib.util.spec_from_file_location(
        "normalize_python_artifacts", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_release_cohort_installer():
    path = REPO_ROOT / "scripts" / "install_release_cohort.py"
    spec = importlib.util.spec_from_file_location(
        "install_release_cohort", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_cohort_helper_validates_sources_and_preserves_core_extra():
    installer = _load_release_cohort_installer()
    version, cohort = installer.load_cohort(REPO_ROOT)

    installer.validate_source_metadata(version, cohort, sys.executable)
    installer.validate_core_extra(
        "release-runtime",
        cohort,
        sys.executable,
    )
    requirements = installer._locked_wheel_requirements(
        version,
        cohort,
        "release-runtime",
    ).splitlines()
    assert requirements[0] == (
        f"maverick-agent[release-runtime]=={version}"
    )
    assert len(requirements) == 8
    assert all("==" in requirement for requirement in requirements)


def test_release_cohort_helper_rejects_unknown_extras_and_rootless_subsets():
    installer = _load_release_cohort_installer()
    _version, cohort = installer.load_cohort(REPO_ROOT)

    try:
        installer.validate_core_extra("release-runtmie", cohort, sys.executable)
    except ValueError as exc:
        assert "unknown maverick-agent extra" in str(exc)
    else:
        raise AssertionError("misspelled core extra was accepted")

    try:
        installer.select_packages(cohort, ["maverick-shield"])
    except ValueError as exc:
        assert "must include maverick-agent" in str(exc)
    else:
        raise AssertionError("rootless cohort subset was accepted")


def test_release_cohort_helper_validates_without_site_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(REPO_ROOT / "scripts" / "install_release_cohort.py"),
            "--source-root",
            str(REPO_ROOT),
            "--target-python",
            sys.executable,
            "--validate-only",
            "--core-extra",
            "release-runtime",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "validated 8 Lightwork cohort source projects" in result.stdout


def test_python_release_archive_normalizer_removes_wall_clock_metadata(tmp_path):
    normalizer = _load_artifact_normalizer()
    wheels = [tmp_path / f"build-{index}.whl" for index in (1, 2)]
    sdists = [tmp_path / f"build-{index}.tar.gz" for index in (1, 2)]

    for index, wheel in enumerate(wheels, start=1):
        with zipfile.ZipFile(wheel, "w") as archive:
            info = zipfile.ZipInfo(
                "demo/__init__.py",
                (2026, 7, 29, 12, 0, index * 2),
            )
            info.external_attr = 0o100644 << 16
            archive.writestr(info, b"VALUE = 1\n")
    for index, sdist in enumerate(sdists, start=1):
        with sdist.open("wb") as raw:
            with gzip.GzipFile(
                filename=sdist.name,
                mode="wb",
                fileobj=raw,
                mtime=1_700_000_000 + index,
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    info = tarfile.TarInfo("demo-1.0/demo.py")
                    info.size = len(b"VALUE = 1\n")
                    info.mtime = 1_700_000_000 + index
                    info.uid = index
                    info.uname = f"builder-{index}"
                    archive.addfile(info, io.BytesIO(b"VALUE = 1\n"))

    for artifact in [*wheels, *sdists]:
        normalizer.normalize(artifact, epoch=1_700_000_000)

    assert wheels[0].read_bytes() == wheels[1].read_bytes()
    assert sdists[0].read_bytes() == sdists[1].read_bytes()
    with tarfile.open(sdists[0], "r:gz") as archive:
        member = archive.getmember("demo-1.0/demo.py")
        assert member.mtime == 1_700_000_000
        assert member.uid == member.gid == 0
        assert member.uname == member.gname == ""


def test_pyinstaller_collects_release_runtime_and_metadata():
    spec_path = REPO_ROOT / "build" / "maverick.spec"
    source = spec_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    collected = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "collect_data_files"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert {"maverick", "maverick_dashboard", "maverick_shield"} <= collected
    shield_metadata = tomllib.loads(
        (
            REPO_ROOT / "packages" / "maverick-shield" / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )
    assert shield_metadata["tool"]["setuptools"]["package-data"][
        "maverick_shield"
    ] == ["redteam_corpus.jsonl"]

    submodules = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "collect_submodules"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    metadata_distributions = {
        item.value
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_dist"
        and isinstance(node.iter, ast.Tuple)
        for item in node.iter.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )
    assert {item["runtime_module"] for item in cohort["packages"]} <= submodules
    assert {item["distribution"] for item in cohort["packages"]} <= (
        metadata_distributions
    )
    assert "pywhispercpp" in submodules
    assert "pywhispercpp" in metadata_distributions
    assert "openai" in submodules
    assert "openai" in metadata_distributions
    assert "'_pywhispercpp'" in source


def test_core_wheel_includes_tracked_pricing_evidence():
    core_root = REPO_ROOT / "packages" / "maverick-core"
    metadata = tomllib.loads(
        (core_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_data = metadata["tool"]["setuptools"]["package-data"]["maverick"]
    assert "data/*.json" in package_data

    evidence_path = (
        core_root
        / "maverick"
        / "data"
        / "pricing-rate-card-2026-07-29.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == 1
    assert evidence["rate_card_version"] == "2026-07-29.2"


def _literal_source_version(path: Path) -> str:
    """Return the source-tree fallback without importing package side effects."""
    versions = {
        node.value.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert len(versions) == 1, f"{path} must declare one literal source version"
    return versions.pop()


def test_public_python_release_cohort_is_complete_and_lockstep():
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )
    release_version = cohort["version"]
    packages = cohort["packages"]

    assert cohort["schema_version"] == 1
    assert release_version == "0.1.7"
    assert cohort["repository"] == "Daybreak-AI-Labs/Lightwork"
    assert len(packages) == 8
    assert len({item["distribution"] for item in packages}) == len(packages)
    assert len({item["path"] for item in packages}) == len(packages)

    workspace = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(workspace["tool"]["uv"]["workspace"]["members"]) == {
        item["path"] for item in packages
    }
    assert set(workspace["tool"]["uv"]["sources"]) == {
        item["distribution"] for item in packages
    }
    assert all(
        source == {"workspace": True}
        for source in workspace["tool"]["uv"]["sources"].values()
    )

    cohort_names = {item["distribution"] for item in packages}
    version_command = (
        REPO_ROOT
        / "packages"
        / "maverick-core"
        / "maverick"
        / "cli"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    assert all(f'"{name}"' in version_command for name in cohort_names)
    canonical_homepage = f"https://github.com/{cohort['repository']}"
    for item in packages:
        package_root = REPO_ROOT / item["path"]
        metadata = tomllib.loads(
            (package_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]
        assert project["name"] == item["distribution"]
        assert project["version"] == release_version
        assert project["license"] == "LicenseRef-Proprietary"
        assert project["urls"]["Homepage"] == canonical_homepage
        assert metadata["build-system"]["requires"] == ["setuptools==83.0.0"]

        requirements = list(project.get("dependencies", []))
        for extra_requirements in project.get("optional-dependencies", {}).values():
            requirements.extend(extra_requirements)
        for sibling in cohort_names - {item["distribution"]}:
            matching = [req for req in requirements if req.split("[", 1)[0].startswith(sibling)]
            assert all(f">={release_version}" in req for req in matching), matching

        init_path = package_root / item["runtime_module"] / "__init__.py"
        assert _literal_source_version(init_path) == release_version

    core = tomllib.loads(
        (
            REPO_ROOT / "packages" / "maverick-core" / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )["project"]
    core_extras = core["optional-dependencies"]
    expected_siblings = {
        f"{name}>={release_version}"
        for name in cohort_names - {"maverick-agent"}
    }
    assert expected_siblings <= set(core_extras["all"])
    assert core_extras["evolve"] == [f"maverick-evolve>={release_version}"]
    assert core_extras["knowledge"] == [
        f"maverick-knowledge>={release_version}"
    ]
    assert core_extras["release-runtime"] == ["openai>=1.30"]
    assert core_extras["postgres"] == ["psycopg[binary,pool]>=3.1"]
    assert "tzdata>=2026.3" in core["dependencies"]


def test_ci_constraints_cover_every_release_base_dependency():
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )["packages"]
    cohort_names = {
        _normalise_project_name(item["distribution"]) for item in cohort
    }
    external_requirements: dict[str, list[str]] = {}
    for item in cohort:
        project = tomllib.loads(
            (REPO_ROOT / item["path"] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        for requirement in project.get("dependencies", []):
            name = _requirement_project_name(requirement)
            if name not in cohort_names:
                external_requirements.setdefault(name, []).append(
                    f"{item['distribution']}: {requirement}"
                )

    constraints = _ci_constraints()
    missing = {
        name: requirements
        for name, requirements in external_requirements.items()
        if name not in constraints
    }
    assert not missing, f"unconstrained release base dependencies: {missing}"
    assert constraints["pywhispercpp"] == "1.5.0"
    assert constraints["openai"] == "2.46.0"
    assert constraints["tqdm"] == "4.67.1"

    dashboard = tomllib.loads(
        (
            REPO_ROOT / "packages" / "maverick-dashboard" / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )["project"]
    assert "pywhispercpp>=1.5" in dashboard["dependencies"]


def test_product_release_manifests_share_the_python_cohort_version():
    release_version = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )["version"]

    installer_package = json.loads(
        (REPO_ROOT / "apps" / "installer-desktop" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    installer_tauri = json.loads(
        (
            REPO_ROOT
            / "apps"
            / "installer-desktop"
            / "src-tauri"
            / "tauri.conf.json"
        ).read_text(encoding="utf-8")
    )
    installer_cargo = tomllib.loads(
        (
            REPO_ROOT / "apps" / "installer-desktop" / "src-tauri" / "Cargo.toml"
        ).read_text(encoding="utf-8")
    )
    desktop_tauri = json.loads(
        (REPO_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    desktop_cargo = tomllib.loads(
        (REPO_ROOT / "apps" / "desktop" / "src-tauri" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
    )
    helm_chart = (REPO_ROOT / "deploy" / "helm" / "maverick" / "Chart.yaml").read_text(
        encoding="utf-8"
    )

    assert installer_package["version"] == release_version
    assert installer_tauri["version"] == release_version
    assert installer_cargo["package"]["version"] == release_version
    assert desktop_tauri["version"] == release_version
    assert desktop_cargo["package"]["version"] == release_version
    assert f'appVersion: "{release_version}"' in helm_chart

    # Native packaging must consume the cohort manifest rather than carrying a
    # fourth independent default that silently produces a differently-versioned
    # MSI or macOS bundle after the next release bump.
    msi_workflow = (
        REPO_ROOT / ".github" / "workflows" / "build-msi.yml"
    ).read_text(encoding="utf-8")
    msi_build = (
        REPO_ROOT / "apps" / "installer-msi" / "build.ps1"
    ).read_text(encoding="utf-8")
    msi_wix = (
        REPO_ROOT / "apps" / "installer-msi" / "Package.wxs"
    ).read_text(encoding="utf-8")
    macos_build = (
        REPO_ROOT / "scripts" / "build-macos-app.sh"
    ).read_text(encoding="utf-8")

    assert "release-cohort.toml" in msi_workflow
    assert "steps.cohort.outputs.version" in msi_workflow
    assert "inputs.version" not in msi_workflow
    assert "release-cohort.toml" in msi_build
    assert '[string]$Version = ""' in msi_build
    assert "<?error ProductVersion is required" in msi_wix
    assert '<?define ProductVersion = "' not in msi_wix
    assert "release-cohort.toml" in macos_build
    assert "<string>$VERSION</string>" in macos_build


def test_release_cohort_dependency_environments_are_complete():
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )["packages"]
    paths = [item["path"] for item in cohort]
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    binary_step = _workflow_step(release, "Install Lightwork + deps")
    assert "python scripts/install_release_cohort.py \\" in binary_step
    assert "--core-extra release-runtime" in binary_step
    assert "--no-deps" not in binary_step
    assert " -e " not in binary_step
    assert "python -m pip check" in binary_step
    assert "python scripts/verify_constraint_closure.py" in binary_step
    for build_tool in (
        "pip==26.1.2",
        "setuptools==83.0.0",
        "wheel==0.47.0",
        "packaging==26.2",
    ):
        assert build_tool in binary_step
    assert "verify_release_runtime" in binary_step
    assert "version('pywhispercpp') == '1.5.0'" in binary_step

    smoke_step = _workflow_step(
        release, "Smoke-test the binary covers all subcommands"
    )
    assert "./dist/maverick mcp --help" in smoke_step
    assert "mcp --help || true" not in smoke_step
    assert "./dist/maverick voice status" in smoke_step
    assert 'grep -F "pywhispercpp engine (installed)"' in smoke_step

    sbom_step = _workflow_step(
        release, "Generate the isolated release-runtime SBOM"
    )
    assert "python scripts/install_release_cohort.py \\" in sbom_step
    assert '--target-python "$SBOM_ENV/bin/python"' in sbom_step
    assert "--core-extra release-runtime" in sbom_step
    assert "--no-deps" not in sbom_step
    assert " -e " not in sbom_step
    assert '"$SBOM_ENV/bin/maverick" release-runtime-check' in sbom_step
    assert 'cyclonedx-py environment "$SBOM_ENV/bin/python"' in sbom_step
    assert "--pyproject packages/maverick-core/pyproject.toml" in sbom_step
    assert sbom_step.count("scripts/verify_constraint_closure.py") == 2
    for build_tool in (
        "pip==26.1.2",
        "setuptools==83.0.0",
        "wheel==0.47.0",
        "packaging==26.2",
    ):
        assert sbom_step.count(build_tool) == 2

    agent_workflow = (
        REPO_ROOT / ".github" / "workflows" / "agent-on-pr.yml"
    ).read_text(encoding="utf-8")
    agent_step = _workflow_step(agent_workflow, "Install Lightwork")
    assert agent_step.count('"$trusted_python" -I -m pip install') == 2
    assert all(f'$source_root/{path}' in agent_step for path in paths)
    assert '--constraint "$source_root/requirements/ci.txt"' in agent_step
    assert '"$trusted_python" -I -m pip check' in agent_step
    assert "verify_constraint_closure.py" in agent_step
    assert "--no-deps" not in agent_step

    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "pip install --no-deps" not in ci
    assert ci.count("python -m pip check") >= 3


def test_release_verifier_accepts_canonical_and_historical_signers():
    verifier = (REPO_ROOT / "deploy" / "verify-release.sh").read_text(
        encoding="utf-8"
    )
    identity_assignment = next(
        line for line in verifier.splitlines() if line.startswith("IDENTITY_REGEXP=")
    )
    template = identity_assignment.partition("=")[2]
    assert template.startswith('"') and template.endswith('"')
    pattern = template[1:-1].replace("${ESCAPED_TAG}", re.escape("v1.2.3"))

    assert re.fullmatch(
        pattern,
        "https://github.com/Daybreak-AI-Labs/Lightwork/"
        ".github/workflows/release.yml@refs/tags/v1.2.3",
    )
    assert re.fullmatch(
        pattern,
        "https://github.com/Day-AI-Labs/Lightwork/"
        ".github/workflows/release.yml@refs/tags/v1.2.3",
    )
    assert re.fullmatch(
        pattern,
        "https://github.com/Day-AI-Labs/Maverick/"
        ".github/workflows/release.yml@refs/tags/v1.2.3",
    )
    assert not re.fullmatch(
        pattern,
        "https://github.com/Daybreak-AI-Labs/Maverick/"
        ".github/workflows/release.yml@refs/tags/v1.2.3",
    )
    assert not re.fullmatch(
        pattern,
        "https://github.com/attacker/Lightwork/"
        ".github/workflows/release.yml@refs/tags/v1.2.3",
    )
    assert not re.fullmatch(
        pattern,
        "https://github.com/Daybreak-AI-Labs/Lightwork/"
        ".github/workflows/release.yml@refs/heads/main",
    )


def test_manual_release_signing_requires_the_exact_tag_run_identity():
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    validation = _workflow_step(release, "Validate release tag")

    assert "EVENT_REF: ${{ github.ref }}" in validation
    assert 'if [[ "$EVENT_NAME" == "workflow_dispatch" ]]' in validation
    assert 'expected_ref="refs/tags/$tag"' in validation
    assert '[[ "$EVENT_REF" != "$expected_ref" ]]' in validation
    assert '[[ "$EVENT_SHA" != "$tag_sha" ]]' in validation
    assert "Manual release must run from $expected_ref" in validation
    distribution = (REPO_ROOT / "docs" / "DISTRIBUTION.md").read_text(
        encoding="utf-8"
    )
    assert (
        "gh workflow run release.yml --ref v0.1.7 -f tag=v0.1.7"
        in distribution
    )


def test_pypi_release_builds_are_commit_anchored_and_byte_reproducible():
    publish = (
        REPO_ROOT / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    build_step = _workflow_step(
        publish, "Validate the tag and build the complete release cohort"
    )
    bootstrap_step = _workflow_step(publish, "Install pinned build tools")

    assert 'export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"' in build_step
    assert "export PYTHONHASHSEED=0" in build_step
    assert "lightwork-publish-source-a" in build_step
    assert "lightwork-publish-source-b" in build_step
    assert build_step.count("python -m build --outdir") == 2
    assert "if first != second:" in build_step
    assert "release artifacts are not reproducible from this commit" in build_step
    assert "python -I scripts/verify_constraint_closure.py" in bootstrap_step
    for build_tool in (
        "pip==26.1.2",
        "setuptools==83.0.0",
        "wheel==0.47.0",
        "packaging==26.2",
        "build==1.5.0",
    ):
        assert build_tool in bootstrap_step

    # Resume remains fail-closed: determinism supports the exact digest
    # comparison; it does not replace or weaken it.
    preflight = _workflow_step(
        publish, "Preflight every distribution and existing PyPI release"
    )
    assert (
        "from verify_github_release_assets import "
        "require_digest_matching_subset"
        in preflight
    )
    assert "missing = require_digest_matching_subset(" in preflight
    assert "if existing != expected:" not in preflight
    assert 'label=f"PyPI {name}=={version}"' in preflight

    # The registry workflow is downstream of the complete Release workflow,
    # checks out that successful run's immutable source SHA, and withholds OIDC
    # publication until artifact signing/attachment also succeeds.
    assert 'workflows: ["Release"]' in publish
    assert "types: [completed]" in publish
    assert "ref: ${{ github.event.workflow_run.head_sha || github.sha }}" in publish
    assert "not successful Release SHA $RELEASE_SOURCE_SHA" in publish
    publish_job = publish.split("\n  publish:", maxsplit=1)[1].split(
        "\n  verify-published:",
        maxsplit=1,
    )[0]
    assert "needs: [build, sign]" in publish_job
    assert "github.event_name == 'workflow_run'" in publish_job
    assert "github.event.workflow_run.conclusion == 'success'" in publish_job


def test_native_source_bootstrap_is_not_advertised_as_a_product_installer():
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    desktop = (
        REPO_ROOT / ".github" / "workflows" / "desktop.yml"
    ).read_text(encoding="utf-8")
    distribution = (REPO_ROOT / "docs" / "DISTRIBUTION.md").read_text(
        encoding="utf-8"
    )
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    app = (
        REPO_ROOT / "apps" / "installer-desktop" / "src" / "App.svelte"
    ).read_text(encoding="utf-8")
    bootstrap_readme = (
        REPO_ROOT / "apps" / "installer-desktop" / "README.md"
    ).read_text(encoding="utf-8")
    deployment = (
        REPO_ROOT / "docs" / "deployment.md"
    ).read_text(encoding="utf-8")
    press = (
        REPO_ROOT / "docs" / "press-kit.md"
    ).read_text(encoding="utf-8")
    features = (
        REPO_ROOT / "docs" / "FEATURES.md"
    ).read_text(encoding="utf-8")

    assert "gh workflow run desktop.yml" not in release
    assert "attach-release:" not in desktop
    assert "if-no-files-found: error" in desktop
    assert "without a GitHub login" not in desktop
    assert "authenticated source-bootstrap" in desktop
    assert "Install from authorized source" in app
    assert "private Lightwork repository" in app
    assert "not a self-contained native installer" in bootstrap_readme
    assert "| **Native installer**" not in distribution
    assert "| **Container image**" in distribution
    assert "**Yes** — installed Python remains readable" in distribution
    assert "four reduced standalone SKUs" in distribution
    assert "native installers, GHCR" not in readme
    assert "Tauri-based GUI installer for users" not in deployment
    assert "not attached to product releases" in deployment
    assert "native double-click installers for Windows" not in press
    assert "self-contained, platform-signed native installer is not" in press
    assert "linux/amd64 and linux/arm64" in features
    assert "arm64 + riscv64" not in features


def test_tauri_binary_apps_use_tracked_locks_and_a_pinned_toolchain():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for relative in (
        "apps/desktop/src-tauri/Cargo.lock",
        "apps/installer-desktop/src-tauri/Cargo.lock",
    ):
        lock = REPO_ROOT / relative
        assert lock.is_file()
        assert lock.read_text(encoding="utf-8").startswith(
            "# This file is automatically @generated by Cargo."
        )
        assert f"!{relative}" in gitignore

    desktop = (
        REPO_ROOT / ".github" / "workflows" / "desktop.yml"
    ).read_text(encoding="utf-8")
    assert 'toolchain: "1.97.1"' in desktop
    assert "src-tauri/Cargo.lock" in desktop
    build_step = _workflow_step(desktop, "Build bundle (unsigned)")
    for required_argument in (
        "pnpm tauri build",
        '--target "$BUNDLE_TARGET"',
        '--config "$TAURI_BUILD_CONFIG"',
        "--ci",
        "-- --locked",
    ):
        assert required_argument in build_step


def test_cloud_quickstarts_and_reference_probes_match_the_runtime_contract():
    distribution = (REPO_ROOT / "docs" / "DISTRIBUTION.md").read_text(
        encoding="utf-8"
    )
    mac_builder = (
        REPO_ROOT / "scripts" / "build-macos-app.sh"
    ).read_text(encoding="utf-8")
    architectures = (
        REPO_ROOT / "docs" / "reference-architectures.md"
    ).read_text(encoding="utf-8")
    fly = (
        REPO_ROOT / "deploy" / "reference-architectures" / "flyio" / "fly.toml"
    ).read_text(encoding="utf-8")

    assert "8000" not in distribution
    assert "8000" not in mac_builder
    assert "--target-port 8765" in distribution
    assert "--command maverick" in distribution
    assert "--args dashboard --host 0.0.0.0 --port 8765" in distribution
    assert "--command=maverick" in distribution
    assert "--args=dashboard,--host,0.0.0.0,--port,8765" in distribution
    assert "dashboard --host 127.0.0.1 --port 8765" in mac_builder
    assert "/readyz" in architectures and "/livez" in architectures
    assert "keep one web/control-plane replica" in architectures
    assert 'path = "/readyz"' in fly


def test_release_container_namespace_matches_the_product_cohort():
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )
    repository = cohort["repository"].casefold()
    owner, product_slug = repository.split("/", 1)
    canonical_image = f"ghcr.io/{owner}/{product_slug}"

    release_workflow = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    helm_values = (
        REPO_ROOT / "deploy" / "helm" / "maverick" / "values.yaml"
    ).read_text(encoding="utf-8")
    deployment_docs = (
        REPO_ROOT / "docs" / "deployment.md"
    ).read_text(encoding="utf-8")
    distribution_docs = (
        REPO_ROOT / "docs" / "DISTRIBUTION.md"
    ).read_text(encoding="utf-8")

    workflow_image = (
        'image="ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/'
        f'{product_slug}"'
    )
    assert workflow_image in release_workflow
    assert "GITHUB_REPOSITORY_OWNER,,}/maverick" not in release_workflow
    assert f"repository: {canonical_image}" in helm_values
    assert canonical_image in deployment_docs
    assert canonical_image in distribution_docs


def test_customer_surfaces_do_not_reference_the_retired_repository_owner():
    """Keep public docs and active delivery paths on the canonical repository."""
    public_sources: list[Path] = []
    for raw in (REPO_ROOT / "docs" / "public-docs.txt").read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source_name = line.partition("|")[0]
        if source_name.startswith("@repo/"):
            public_sources.append(
                REPO_ROOT / source_name.removeprefix("@repo/")
            )
        else:
            public_sources.append(REPO_ROOT / "docs" / source_name)

    checked = {
        REPO_ROOT / path
        for path in (
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/workflows/agent-on-pr.yml",
            "ARCHITECTURE.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "MAINTAINERS.md",
            "README.md",
            "SECURITY.md",
            "TRADEMARK.md",
            "packages/maverick-core/maverick/a2a.py",
            "packages/maverick-core/maverick/issue_report.py",
            "packages/maverick-core/maverick/tools/geocode.py",
            "packages/maverick-core/maverick/tools/reddit_tool.py",
            "packages/maverick-core/maverick/tools/wikipedia.py",
            "packages/maverick-mcp/maverick_mcp/publish.py",
                "scripts/train_runpod.sh",
            "web/README.md",
            "web/index.html",
        )
    }
    checked.update(public_sources)
    customer_suffixes = {
        ".el",
        ".html",
        ".json",
        ".md",
        ".ps1",
        ".rb",
        ".rs",
        ".sh",
        ".toml",
        ".wxs",
        ".yaml",
        ".yml",
    }
    generated_directories = {
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
    for root in (REPO_ROOT / "apps", REPO_ROOT / "deploy"):
        for current, directories, filenames in os.walk(root):
            # Customer-surface review must be deterministic in a developer
            # checkout. Generated dependency trees can contain dangling links
            # and are neither shipped source nor part of the repository review.
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in generated_directories
            ]
            checked.update(
                Path(current) / filename
                for filename in filenames
                if Path(filename).suffix.lower() in customer_suffixes
            )
    # The verifier is the sole intentional compatibility boundary: old signed
    # releases must remain verifiable after the organization rename.
    checked.discard(REPO_ROOT / "deploy" / "verify-release.sh")

    cohort = tomllib.loads((REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8"))
    checked.update(
        REPO_ROOT / item["path"] / "pyproject.toml"
        for item in cohort["packages"]
    )
    mcp_publish = (
        REPO_ROOT / "packages" / "maverick-mcp" / "maverick_mcp" / "publish.py"
    ).read_text(encoding="utf-8")
    assert (
        'DEFAULT_REPO_URL = "https://github.com/Daybreak-AI-Labs/Lightwork"'
        in mcp_publish
    )

    legacy_hits = {
        path.relative_to(REPO_ROOT).as_posix(): [
            line_number
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            )
            if "day-ai-labs" in line.casefold()
        ]
        for path in checked
        if "day-ai-labs"
        in path.read_text(encoding="utf-8", errors="replace").casefold()
    }
    assert not legacy_hits, legacy_hits


def test_ci_posture_tools_are_exactly_pinned_and_secret_scan_uses_module():
    constraints = _ci_constraints()
    expected = {
        "bandit": "1.9.4",
        "cyclonedx-bom": "7.3.1",
        "detect-secrets": "1.5.0",
        "pip-audit": "2.10.0",
        "psycopg": "3.3.4",
        "psycopg-pool": "3.3.1",
        "ruff": "0.15.15",
        "vulture": "2.16",
    }
    assert {name: constraints.get(name) for name in expected} == expected

    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for name, version in expected.items():
        assert f"{name}=={version}" in workflow
        assert f"{name}>=" not in workflow
    assert "python -m detect_secrets scan --baseline" in workflow
    assert workflow.count("python scripts/verify_constraint_closure.py") >= 4
    assert "pgvector/pgvector:pg16@sha256:" in workflow
    for live_test in (
        "test_postgres_world.py",
        "test_pgvector_isolation_pg.py",
        "test_postgres_fact_history.py",
        "test_postgres_facts_trust.py",
        "test_postgres_governed_records_capacity.py",
    ):
        assert live_test in workflow


def test_release_tags_require_main_ancestry_and_isolated_concurrency():
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    publish = (
        REPO_ROOT / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    native = (
        REPO_ROOT / ".github" / "workflows" / "native.yml"
    ).read_text(encoding="utf-8")

    for workflow in (release, publish, native):
        assert "git merge-base --is-ancestor" in workflow
        assert "refs/remotes/origin/main" in workflow
    assert "sha: ${{ steps.release-ref.outputs.sha }}" in release
    assert 'echo "sha=$tag_sha" >> "$GITHUB_OUTPUT"' in release
    assert "outputs.ref" not in release
    assert 'echo "ref=refs/tags/$tag"' not in release
    assert release.count(
        "ref: ${{ needs.validate-release-tag.outputs.sha }}"
    ) == 5
    assert (
        "group: lightwork-release-${{ inputs.tag || github.ref_name }}"
        in release
    )
    assert (
        "group: lightwork-release-${{ github.event.workflow_run.head_branch || "
        "github.ref_name }}"
        in publish
    )
    assert (
        native.count(
            "needs: [core, verify-audit, python, wasm, validate-native-tag]"
        )
        == 2
    )
    pypi_job = native.split("  publish-pypi:", 1)[1].split(
        "  publish-npm:",
        1,
    )[0]
    assert "actions/checkout@" not in pypi_job
    assert "id-token: write" in pypi_job
    assert "contents: write" not in pypi_job


def test_release_version_policy_has_one_canonical_cross_channel_spelling():
    module_path = REPO_ROOT / "scripts" / "release_version.py"
    spec = importlib.util.spec_from_file_location("release_version", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    stable = module.validate_tag("v1.2.3")
    prerelease = module.validate_tag("v1.2.3rc1")
    developmental = module.validate_tag("v1.2.3.dev4")
    postrelease = module.validate_tag("v1.2.3.post2")
    assert (stable.version, stable.prerelease) == ("1.2.3", False)
    assert (prerelease.version, prerelease.prerelease) == ("1.2.3rc1", True)
    assert (developmental.version, developmental.prerelease) == (
        "1.2.3.dev4",
        True,
    )
    assert (postrelease.version, postrelease.prerelease) == (
        "1.2.3.post2",
        False,
    )

    for invalid in (
        "1.2.3",
        "v1.2",
        "v01.2.3",
        "v1.2.3-rc1",
        "v1.2.3.rc1",
        "v1.2.3..",
        "v1.2.3+local",
    ):
        try:
            module.validate_tag(invalid)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"accepted noncanonical release tag: {invalid}")


def test_reusable_agent_separates_untrusted_execution_from_pr_write():
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "agent-on-pr.yml"
    ).read_text(encoding="utf-8")
    review_job = workflow.split("  agent-review:", 1)[1].split(
        "  post-review:",
        1,
    )[0]
    post_job = workflow.split("  post-review:", 1)[1]

    assert "lightwork-agent-review-${{ github.repository }}-${{ github.workflow }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "pull-requests: write" not in review_job
    assert "ANTHROPIC_API_KEY" in review_job
    assert '"$trusted_python" -I -m pip install' in review_job
    assert '"$trusted_python" -I -m pip check' in review_job
    assert '"$trusted_python" -I -m maverick.cli overrides load' in review_job
    assert '"$trusted_python" -I -m maverick.cli start "$review_goal"' in review_job
    unisolated_python = []
    for line in review_job.splitlines():
        command = line.strip()
        if command.startswith("#"):
            continue
        if re.match(r"python(?:3(?:\.\d+)*)?\s", command) and not re.match(
            r"python(?:3(?:\.\d+)*)?\s+-I(?:\s|$)",
            command,
        ):
            unisolated_python.append(command)
    assert unisolated_python == []
    assert 'backend = "docker"' in review_job
    assert "require_container = true" in review_job
    assert "allow_network = false" in review_job
    assert "allow_root = false" in review_job
    assert "python:3.12-slim@sha256:" in review_job
    assert "MAVERICK_REQUIRE_CONTAINER_BACKEND: '1'" in review_job
    assert "WORKFLOW_REPOSITORY: ${{ job.workflow_repository }}" in review_job
    assert "WORKFLOW_SHA: ${{ job.workflow_sha }}" in review_job
    assert '"$LIGHTWORK_REF" != "$WORKFLOW_SHA"' in review_job
    assert "ref: ${{ job.workflow_sha }}" in review_job
    assert "ref: ${{ inputs.lightwork_ref }}" not in review_job
    assert "Materialize an isolated PR review workspace" in review_job
    assert "git archive \"$PR_HEAD_SHA\"" in review_job
    assert "git archive \"$PR_BASE_SHA\"" in review_job
    assert 'merge_base="$(git merge-base "$PR_BASE_SHA" "$PR_HEAD_SHA")"' in review_job
    assert '"$merge_base" "$PR_HEAD_SHA"' in review_job
    assert "PR_DIFF.patch" in review_job
    assert "requested.is_absolute()" in review_job
    assert "candidate.is_relative_to(policy_root)" in review_job
    assert "overrides_path escapes the trusted base" in review_job
    assert "read_only_paths = {read_only_paths}" in review_job
    assert "Verify trusted review evidence was not mutated" in review_job
    assert "review evidence integrity OK" in review_job
    assert "sha256:${AGENT_DIFF_SHA256}" in review_job
    assert "| tee /tmp/agent-output.txt" not in review_job
    assert "> /tmp/agent-output.txt 2>&1" in review_job
    assert "Sanitize review output before crossing the job boundary" in review_job
    sanitizer = _workflow_step(
        review_job,
        "Sanitize review output before crossing the job boundary",
    )
    assert '"$pythonLocation/bin/python" -I -S - <<\'PY\'' in sanitizer
    assert "LIGHTWORK_SOURCE_TOKEN" not in sanitizer
    assert "import unicodedata" in sanitizer
    assert 'not in {"Cc", "Cf", "Cs"}' in sanitizer
    assert 're.sub(r"(?m)^::"' in sanitizer
    assert "github_pat_" in sanitizer
    assert "PRIVATE KEY" in sanitizer
    assert "<pre>" not in sanitizer
    assert "```" not in sanitizer
    assert "pull-requests: write" in post_job
    assert "actions/checkout@" not in post_job
    assert "ANTHROPIC_API_KEY" not in post_job
    assert "OPENAI_API_KEY" not in post_job
    assert '.replace(/</g, "&lt;")' in post_job
    assert '.replace(/>/g, "&gt;")' in post_job
    assert '.replace(/@/g, "＠")' in post_job
    assert "escaped.slice(0, max)" in post_job
    assert "const max = 50000" in post_job
    assert "<pre>\\n" in post_job


def test_reusable_agent_diff_excludes_diverged_base_only_changes(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Lightwork test")
    (repo / "common.txt").write_text("common\n", encoding="utf-8")
    git("add", "common.txt")
    git("commit", "-m", "common")
    git("switch", "-c", "feature")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git("add", "feature.txt")
    git("commit", "-m", "feature change")
    head_sha = git("rev-parse", "HEAD")
    git("switch", "main")
    (repo / "base-only.txt").write_text("base only\n", encoding="utf-8")
    git("add", "base-only.txt")
    git("commit", "-m", "base advanced")
    base_sha = git("rev-parse", "HEAD")

    merge_base = git("merge-base", base_sha, head_sha)
    wrong_two_dot = set(
        git("diff", "--name-only", base_sha, head_sha).splitlines()
    )
    reviewed = set(
        git("diff", "--name-only", merge_base, head_sha).splitlines()
    )
    assert wrong_two_dot == {"base-only.txt", "feature.txt"}
    assert reviewed == {"feature.txt"}


def test_native_release_uses_locked_python_tools_and_preserves_wheel_bytes():
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "native.yml"
    ).read_text(encoding="utf-8")

    assert (
        "PIP_CONSTRAINT: ${{ github.workspace }}/requirements/ci.txt"
        in workflow
    )
    assert "'pip==26.1.2'" in workflow
    assert "'pytest==9.1.1'" in workflow
    assert "'certifi==2026.7.22'" in workflow
    assert "'cryptography==50.0.0'" in workflow
    assert "maturin-version: v1.14.1" in workflow
    assert workflow.count('toolchain: "1.97.1"') == 4
    assert "jetli/wasm-pack-action@" not in workflow
    assert "cargo install wasm-pack --version 0.15.0 --locked" in workflow
    assert workflow.count("--locked") >= 6
    assert "ubuntu-latest" not in workflow
    assert "macos-latest" not in workflow
    assert "windows-latest" not in workflow
    assert (
        "os: [ubuntu-24.04, macos-15, macos-15-intel, windows-2022]"
        in workflow
    )
    assert (REPO_ROOT / "rust" / "Cargo.lock").is_file()
    toolchain = tomllib.loads(
        (REPO_ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    assert toolchain["toolchain"]["channel"] == "1.97.1"
    assert "pip install --upgrade pip" not in workflow
    assert '"cryptography>=44.0.1"' not in workflow
    assert "--allow-unconstrained maverick-native" in workflow
    assert "python -I -m venv .native-parity-venv" in workflow
    assert 'parity_python=".native-parity-venv/Scripts/python.exe"' in workflow
    assert 'parity_python=".native-parity-venv/bin/python"' in workflow
    assert workflow.count('"$parity_python" -I') >= 7
    assert "maverick-native-wheel.sha256" in workflow
    assert "wheel mutated during parity testing" in workflow
    assert "Preflight existing native PyPI bytes" in workflow
    assert "PyPI maverick-native=={version} conflicts with this build" in workflow
    assert "Verify final native PyPI bytes" in workflow
    assert "final native PyPI verification OK" in workflow
    assert "native PyPI propagation incomplete after final retry" in workflow
    assert 'epoch="$(git show -s --format=%ct HEAD)"' in workflow
    assert 'RUSTFLAGS=-C link-arg=/Brepro' in workflow
    assert "native wheel reproducibility OK" in workflow
    assert "changed_members={changed}" in workflow
    assert "wasm npm reproducibility OK" in workflow
    assert workflow.count(
        "CARGO_TARGET_DIR: ${{ runner.temp }}/maverick-native-target"
    ) == 3
    assert "Clean native target before the second build" in workflow
    assert (
        "cargo clean --manifest-path rust/mvk-scan-py/Cargo.toml"
        in workflow
    )
    assert "maverick-native-target-first" not in workflow
    assert "maverick-native-target-second" not in workflow
    assert "wasm-publish-target-first" in workflow
    assert "wasm-publish-target-second" in workflow
    assert "npm pack --pack-destination" in workflow
    assert "name: mvk-scan-wasm-npm" in workflow
    assert workflow.count("sha256sum -c SHA256SUMS") == 2
    assert "wasm-pack build --target nodejs --out-dir pkg --release" in workflow
    assert (
        "wasm-pack build --target bundler --out-dir pkg-publish-first --release"
        in workflow
    )
    assert (
        "wasm-pack build --target bundler --out-dir pkg-publish-second --release"
        in workflow
    )
    assert "wasm-pack build rust/mvk-scan-wasm" not in workflow
    publish_npm = workflow.split("\n  publish-npm:", maxsplit=1)[1]
    assert "wasm-pack build" not in publish_npm
    assert "actions/checkout@" not in publish_npm
    assert "name: mvk-scan-wasm-npm" in publish_npm
    assert "vars.MAVERICK_NATIVE_NPM_ENABLED == 'true'" in publish_npm
    assert "Preflight existing npm bytes" in publish_npm
    assert "npm preflight OK: exact" in publish_npm
    assert "conflicts with this build" in publish_npm
    assert 'npm publish "$NPM_PACKAGE" --access public' in publish_npm
    assert "MAVERICK_NATIVE_NPM_ENABLED is true but NPM_TOKEN is unset" in publish_npm
    assert "Verify final npm bytes" in publish_npm
    assert "final npm verification OK" in publish_npm
    assert "npm package propagation incomplete after final retry" in publish_npm


def test_secret_scan_excludes_only_the_rotating_signed_manifest():
    baseline = json.loads(
        (REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8")
    )
    findings = [
        finding
        for path, path_findings in baseline["results"].items()
        for finding in path_findings
    ]
    assert findings
    assert all("\\" not in path for path in baseline["results"])
    assert all(finding.get("is_secret") is False for finding in findings)
    pre_push = (
        REPO_ROOT / "scripts" / "pre-push-lint.sh"
    ).read_text(encoding="utf-8")
    assert 'filename.replace("\\\\", "/")' in pre_push
    assert "python -c 'import detect_secrets'" in pre_push
    assert "python -m detect_secrets scan --baseline" in pre_push
    assert "FAIL detect-secrets scanner error" in pre_push
    assert "mktemp -d" in pre_push
    assert 'SCAN_BASELINE="$scan_baseline"' in pre_push
    assert "trap cleanup_pre_push EXIT" in pre_push
    assert 'rm -rf -- "$ppl_state_dir"' in pre_push
    assert "/tmp/scan.baseline" not in pre_push
    assert "/tmp/detect-secrets.out" not in pre_push
    assert "command -v detect-secrets" not in pre_push
    assert "python3 -" not in pre_push

    regex_filters = [
        item
        for item in baseline["filters_used"]
        if item["path"] == "detect_secrets.filters.regex.should_exclude_file"
    ]
    assert len(regex_filters) == 1
    patterns = regex_filters[0]["pattern"]
    assert len(patterns) == 1
    exclusion = re.compile(patterns[0])

    # Each published suite's rotating manifest is named EXPLICITLY. The
    # exclusion must never become a wildcard over benchmarks/results: a new
    # suite's artifacts would then be able to carry a real secret past the
    # scanner the day they land, before anyone had audited them.
    manifests = (
        "benchmarks/results/governance-frontier-v1/measured-manifest.json",
        "benchmarks/results/harness-overhead-v1/measured-manifest.json",
    )
    for manifest in manifests:
        assert exclusion.search(manifest)
        assert exclusion.search(manifest.replace("/", "\\"))
        assert not exclusion.search(f"{manifest}.bak")
    assert not exclusion.search(
        "benchmarks/results/governance-frontier-v1/trusted-publisher.pub"
    )
    assert not exclusion.search(
        "benchmarks\\results\\governance-frontier-v1\\trusted-publisher.pub"
    )
    assert not exclusion.search(
        "benchmarks/results/harness-overhead-v1/trusted-publisher.pub"
    )
    assert not exclusion.search(
        "benchmarks/results/another-run/measured-manifest.json"
    )


def test_ci_constraints_satisfy_shipped_network_security_floors():
    constraints = (
        REPO_ROOT / "requirements" / "ci.txt"
    ).read_text(encoding="utf-8")
    docs_constraints = (
        REPO_ROOT / "requirements" / "docs.txt"
    ).read_text(encoding="utf-8")
    core_manifest = (
        REPO_ROOT / "packages" / "maverick-core" / "pyproject.toml"
    ).read_text(encoding="utf-8")
    channels_manifest = (
        REPO_ROOT / "packages" / "maverick-channels" / "pyproject.toml"
    ).read_text(encoding="utf-8")

    for pinned in ("requests==2.34.2", "urllib3==2.7.0"):
        assert pinned in constraints
        assert pinned in docs_constraints
    for manifest in (core_manifest, channels_manifest):
        assert "requests>=2.33.0" in manifest
        assert "urllib3>=2.7.0" in manifest


def test_privileged_release_job_downloads_only_named_producer_artifacts():
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    final_release_job = release.split("\n  release:", 1)[1]

    assert "merge-multiple:" not in final_release_job
    assert final_release_job.count("uses: actions/download-artifact@") == 4
    expected_downloads = {
        "Download the Linux binary producer": (
            "maverick-linux-x86_64",
            "binary-linux",
        ),
        "Download the macOS binary producer": (
            "maverick-macos-arm64",
            "binary-macos",
        ),
        "Download the Windows binary producer": (
            "maverick-windows-x86_64.exe",
            "binary-windows",
        ),
        "Download the release-runtime SBOM producer": (
            "release-runtime-sbom",
            "sbom",
        ),
    }
    for step_name, (artifact_name, producer_directory) in expected_downloads.items():
        step = _workflow_step(final_release_job, step_name)
        assert f"          name: {artifact_name}" in step
        assert (
            "          path: ${{ runner.temp }}/release-inputs/"
            f"{producer_directory}"
        ) in step

    assemble_position = final_release_job.index(
        "Validate exact producer cohorts and assemble trusted release inputs"
    )
    sign_position = final_release_job.index(
        "Sign release artifacts (keyless, Sigstore)"
    )
    assert assemble_position < sign_position


def test_release_input_assembler_accepts_only_the_exact_producer_cohort(tmp_path):
    input_root, output_root = _release_input_fixture(tmp_path)

    result = _run_release_input_assembler(tmp_path, input_root)

    assert result.returncode == 0, result.stderr
    assert "assembled 4 allowlisted release inputs" in result.stdout
    assert {path.name for path in output_root.iterdir()} == {
        "maverick-linux-x86_64",
        "maverick-macos-arm64",
        "maverick-windows-x86_64.exe",
        "maverick-sbom-v1.2.3.cdx.json",
    }


def test_release_input_assembler_rejects_an_extra_producer_file_before_copy(
    tmp_path,
):
    input_root, output_root = _release_input_fixture(tmp_path)
    (input_root / "sbom" / "unrequested-evidence.json").write_text(
        "not allowlisted\n",
        encoding="utf-8",
    )

    result = _run_release_input_assembler(tmp_path, input_root)

    assert result.returncode != 0
    assert "release producer 'sbom' filename mismatch" in result.stderr
    assert list(output_root.iterdir()) == []


def test_release_input_assembler_rejects_a_cross_producer_collision_before_copy(
    tmp_path,
):
    input_root, output_root = _release_input_fixture(tmp_path)
    expected_sbom = input_root / "sbom" / "maverick-sbom-v1.2.3.cdx.json"
    expected_sbom.unlink()
    (input_root / "sbom" / "maverick-linux-x86_64").write_text(
        "attempted binary replacement\n",
        encoding="utf-8",
    )

    result = _run_release_input_assembler(tmp_path, input_root)

    assert result.returncode != 0
    assert "release producer 'sbom' filename mismatch" in result.stderr
    assert list(output_root.iterdir()) == []


def test_release_delivery_toolchains_and_artifacts_are_deterministic():
    release = (
        REPO_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    publish = (
        REPO_ROOT / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    msi = (
        REPO_ROOT / ".github" / "workflows" / "build-msi.yml"
    ).read_text(encoding="utf-8")

    for workflow in (release, publish, msi):
        assert "PIP_CONSTRAINT: ${{ github.workspace }}/requirements/ci.txt" in workflow
        checkout_count = workflow.count("uses: actions/checkout@")
        assert workflow.count("persist-credentials: false") == checkout_count
        assert "runs-on: ubuntu-latest" not in workflow
        assert "runs-on: windows-latest" not in workflow

    assert "pyinstaller==6.21.0" in release
    assert "pyinstaller>=" not in release
    constraints = (
        REPO_ROOT / "requirements" / "ci.txt"
    ).read_text(encoding="utf-8")
    for pinned_helper in (
        "altgraph==0.17.5",
        "macholib==1.16.4",
        "pefile==2024.8.26",
        "pyinstaller-hooks-contrib==2026.6",
        "pywin32-ctypes==0.2.3",
    ):
        assert pinned_helper in constraints
    assert "provenance: mode=max" in release
    assert "sbom: true" in release
    assert "PYTHON_DIGEST=@sha256:" in release
    ci_workflow = (
        REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    assert "--build-arg 'PYTHON_DIGEST=@sha256:" in ci_workflow
    assert "Generate a complete release checksum manifest" in release
    assert "sha256sum --check" in release
    assert "fail_on_unmatched_files: true" in release

    assert "dotnet tool install --global wix --version 4.0.6" in msi
    assert "Expected exactly one maverick-agent wheel" in msi
    assert "Generate MSI checksum" in msi
    assert "apps/installer-msi/dist/SHA256SUMS" in msi
    assert "apps/installer-msi/dist/BUILD-METADATA.json" in msi
    assert 'source_revision = "${{ github.sha }}"' in msi

    # The full cohort is validated before any matrix job receives an OIDC
    # token. Safe resume compares already-published filenames and hashes rather
    # than allowing skip-existing to mask unrelated bytes.
    assert "Preflight every distribution and existing PyPI release" in publish
    assert "require_digest_matching_subset" in publish
    assert "(cd dist-all && sha256sum --check SHA256SUMS)" in publish
    assert "scripts/normalize_python_artifacts.py" in publish
    assert "dist-first/*.whl dist-first/*.tar.gz" in publish
    assert "dist-second/*.whl dist-second/*.tar.gz" in publish
    assert "attestations: true" in publish
    assert "Attach signatures to the draft release" in publish
    assert "Verify existing published Python evidence without mutation" in publish
    assert "finalize-release:" in publish
    assert "Verify every signed byte in the exact release state" in publish
    assert (
        "Publish only after every artifact and exact container tag verify"
        in publish
    )
    assert 'gh release edit "$RELEASE_TAG" --draft=false' in publish
    assert (
        "Release $RELEASE_TAG is already published and fully verified; no-op"
        in publish
    )
    assert "Create the signed release as a draft" in release
    assert "draft: true" in release
    assert "verify-published-release:" in release
    assert "Verify the published release without mutating it" in release
    assert "release-manifest.json" in publish
    assert 'gh release upload "$TAG" \\' in publish
    assert "dist/SHA256SUMS" in publish
    assert "dist/release-manifest.json" in publish
    assert "verify-published:" in publish
    assert "needs: [build, sign]" in publish
    assert "needs: [build, publish]" in publish
    assert "Require exact final filenames and SHA-256 digests on PyPI" in publish
    assert "outside the release manifest" in publish
    assert "PyPI cohort incomplete after final retry" in publish

    docker_job = release.split("\n  docker-image:", 1)[1].split(
        "\n  binaries:", 1
    )[0]
    sbom_job = release.split("\n  release-runtime-sbom:", 1)[1].split(
        "\n  verify-published-release:", 1
    )[0]
    final_release_job = release.split("\n  release:", 1)[1]
    finalizer_job = publish.split("\n  finalize-release:", 1)[1]
    assert ":sha-$GITHUB_SHA" in docker_job
    assert ":latest" not in docker_job
    assert "steps.build.outputs.digest" in docker_job
    assert "Generate the isolated release-runtime SBOM" in sbom_job
    assert "cyclonedx-py environment" in sbom_job
    assert "draft: true" in final_release_job
    assert "--draft=false" not in final_release_job
    assert "python -m pip install" not in final_release_job
    assert "cyclonedx-py" not in final_release_job
    assert "Promote and verify" not in final_release_job
    verify_position = finalizer_job.index(
        "Verify every signed byte in the exact release state"
    )
    promote_position = finalizer_job.index(
        "Promote and verify the signed exact-version container tag"
    )
    publish_position = finalizer_job.index(
        "Publish only after every artifact and exact container tag verify"
    )
    assert verify_position < promote_position < publish_position
    assert "scripts/verify_github_release_assets.py" in finalizer_job
    promotion_step = _workflow_step(
        finalizer_job,
        "Promote and verify the signed exact-version container tag",
    )
    assert promotion_step.count("for attempt in $(seq 1 5)") == 2
    assert "confirmed_absent=false" in promotion_step
    assert "manifest unknown" in promotion_step
    assert "Registry inspection failed operationally" in promotion_step
    assert "Unable to determine whether" in promotion_step
    assert 'if [ "$confirmed_absent" = "true" ]; then' in promotion_step
    assert "imagetools inspect \"$CONTAINER_TARGET\" 2>/dev/null" not in promotion_step
    publish_step = _workflow_step(
        finalizer_job,
        "Publish only after every artifact and exact container tag verify",
    )
    remote_verify_position = publish_step.index(
        'assets="$RUNNER_TEMP/lightwork-published-release-assets"'
    )
    assert publish_step.index('gh release edit "$RELEASE_TAG" --draft=false') < (
        remote_verify_position
    )
    assert 'rm -rf "$assets"' in publish_step
    assert 'gh release download "$RELEASE_TAG" --dir "$assets"' in publish_step
    assert "scripts/verify_github_release_assets.py" in publish_step
    assert '"$GITHUB_WORKSPACE/deploy/verify-release.sh"' in publish_step
    assert 'if [ "${#bundles[@]}" -ne 18 ]; then' in publish_step
    assert "cosign verify-blob" in publish_step
    assert (
        "Fresh published release bytes and signatures verify exactly"
        in publish_step
    )
    assert 'target_tags=("$CONTAINER_IMAGE:$version")' in final_release_job
    assert ":latest" not in final_release_job
    assert ":latest" not in finalizer_job
    for required_need in (
        "validate-release-tag",
        "docker-image",
        "binaries",
        "release-runtime-sbom",
    ):
        assert f"        {required_need}," in final_release_job
    assert "cosign sign --yes" in final_release_job
    assert '"$CONTAINER_IMAGE@$CONTAINER_DIGEST"' in final_release_job
    assert "maverick-container-" in final_release_job

    dockerfile = (
        REPO_ROOT / "deploy" / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "COPY requirements/ci.txt ./requirements/ci.txt" in dockerfile
    assert " AS wheel-builder" in dockerfile
    assert " AS runtime" in dockerfile
    assert "pip==26.1.2" in dockerfile
    assert dockerfile.count("--constraint /tmp/requirements/ci.txt") == 1
    assert "--core-extra release-runtime" in dockerfile
    assert "--no-index" in dockerfile
    assert "cohort-requirements.txt" in dockerfile
    assert "maverick release-runtime-check" in dockerfile
    cohort = tomllib.loads(
        (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")
    )
    for item in cohort["packages"]:
        assert f"COPY {item['path']} ./{item['path']}" in dockerfile
        assert f"./{item['path']}" in dockerfile
    assert "python -m pip check" in dockerfile
    assert "HEALTHCHECK" not in dockerfile

    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert dockerignore.splitlines()[0] == "**"
    assert "!requirements/ci.txt" in dockerignore
    for protected_pattern in (
        "**/.git/**",
        "**/.env.*",
        "**/.maverick/**",
        "**/*.key",
        "**/*.pem",
        "**/*.sqlite",
        "**/*.db",
    ):
        assert protected_pattern in dockerignore
    for item in cohort["packages"]:
        assert f"!{item['path']}/" in dockerignore
        assert f"!{item['path']}/**" in dockerignore

    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert '"import maverick_evolve, maverick_knowledge"' in ci


def test_ci_and_docs_workflows_keep_write_tokens_away_from_untrusted_builds():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    docs = (
        REPO_ROOT / ".github" / "workflows" / "docs.yml"
    ).read_text(encoding="utf-8")

    assert re.search(r"(?m)^permissions:\n  contents: read\n", ci)
    assert ci.count("persist-credentials: false") == ci.count(
        "uses: actions/checkout@"
    )

    top_level_docs = docs.split("jobs:", 1)[0]
    assert "pages: write" not in top_level_docs
    assert "id-token: write" not in top_level_docs
    deploy = docs.split("\n  deploy:", 1)[1]
    assert "pages: write" in deploy
    assert "id-token: write" in deploy
    assert docs.count("persist-credentials: false") == docs.count(
        "uses: actions/checkout@"
    )


def test_documentation_dependency_lock_is_exact_and_complete():
    requirements = [
        line.strip()
        for line in (REPO_ROOT / "requirements" / "docs.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    names = [line.partition("==")[0].casefold() for line in requirements]

    assert requirements
    assert all(line.count("==") == 1 for line in requirements)
    assert len(names) == len(set(names))
    assert {"mkdocs", "mkdocs-material", "requests", "urllib3"} <= set(names)

    workflow = (
        REPO_ROOT / ".github" / "workflows" / "docs.yml"
    ).read_text(encoding="utf-8")
    assert "pip install --requirement requirements/docs.txt" in workflow
    assert workflow.count("'requirements/docs.txt'") == 2
