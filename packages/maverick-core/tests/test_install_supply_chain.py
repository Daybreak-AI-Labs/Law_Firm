"""Regression tests for first-party package installation trust boundaries.

The Lightwork distribution names are intentionally treated as untrusted on
public indexes until the organization has reserved and protected them. CI and
installer entry points must therefore use pinned local source or an explicit
private index, never an implicit public lookup.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository_files(
    *,
    filename: str | None = None,
    suffix: str | None = None,
) -> Iterator[Path]:
    """Yield tracked-source candidates without traversing generated trees."""
    if (filename is None) == (suffix is None):
        raise ValueError("provide exactly one of filename or suffix")

    for directory, subdirectories, filenames in os.walk(REPO_ROOT):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in GENERATED_DIRECTORY_NAMES
        )
        for candidate in sorted(filenames):
            if candidate == filename or (suffix is not None and candidate.endswith(suffix)):
                yield Path(directory) / candidate


def test_composite_action_installs_first_party_packages_from_its_checkout():
    text = _read("deploy/github-action/action.yml")

    assert "GITHUB_ACTION_PATH" in text
    assert "scripts/install_release_cohort.py" in text
    assert "--source-root" in text
    assert "--core-extra release-runtime" in text
    assert "release-cohort.toml" in text

    assert not re.search(
        r"python\s+-m\s+pip\s+install\s+['\"]?maverick-agent",
        text,
    )


def test_reusable_pr_workflow_requires_immutable_runtime_source():
    text = _read(".github/workflows/agent-on-pr.yml")

    assert "lightwork_ref:" in text
    assert "Full 40-character commit SHA" in text
    assert "repository: Daybreak-AI-Labs/Lightwork" in text
    assert "persist-credentials: false" in text
    assert '"$source_root/packages/maverick-core[all]"' in text
    assert "pip install 'maverick-agent[all]'" not in text


def test_gitlab_template_has_no_implicit_public_index_fallback():
    text = _read("deploy/gitlab-ci/maverick.gitlab-ci.yml")

    assert "MAVERICK_SOURCE_DIR" in text
    assert "MAVERICK_SOURCE_REF" in text
    assert "MAVERICK_PACKAGE_INDEX_URL" in text
    assert "python -m pip --isolated install" in text
    assert "Public PyPI is blocked" in text
    assert "status --porcelain --untracked-files=all" in text
    assert 'pip install "maverick-agent"' not in text


def test_desktop_installers_require_a_pinned_source_ref():
    shell = _read("deploy/desktop/install.sh")
    powershell = _read("deploy/desktop/install.ps1")

    for text in (shell, powershell):
        assert "MAVERICK_REF is required" in text
        assert "public-index fallback is disabled" in text
        assert "MAVERICK_ALLOW_UNPINNED" not in text
        assert "maverick-agent[installer]" not in text
        assert "status --porcelain --untracked-files=all" in text

    assert "rev-parse HEAD" in shell
    assert "rev-parse HEAD" in powershell


def test_vps_installer_requires_a_clean_immutable_checkout():
    text = _read("deploy/vps/install.sh")

    assert "MAVERICK_REF is required" in text
    assert "^[0-9a-f]{40}$" in text
    assert "rev-parse HEAD" in text
    assert "status --porcelain --untracked-files=all" in text
    assert "maverick.stage." in text
    assert "MAVERICK_VERSION" not in text
    assert "Lightwork/main/deploy/vps/install.sh" not in text


def test_training_bootstrap_installs_only_from_verified_checkout():
    text = _read("scripts/train_runpod.sh")

    assert "MAVERICK_SOURCE_DIR" in text
    assert "MAVERICK_SOURCE_REF" in text
    assert "status --porcelain --untracked-files=all" in text
    assert '"$MAVERICK_SOURCE_DIR/packages/maverick-core[training]"' in text
    assert "pip install --quiet 'maverick-agent[training]'" not in text


def test_homebrew_formula_bootstraps_python_312_without_venv_pip():
    formula = _read("deploy/homebrew/maverick.rb")

    assert 'python = Formula["python@3.12"].opt_bin/"python3.12"' in formula
    assert "virtualenv_create(libexec, python)" in formula
    assert 'system python, "-m", "pip", "--python=#{libexec}/bin/python"' in formula
    assert formula.count('"--require-hashes"') == 2
    assert formula.count('"--only-binary=:all:"') == 2
    assert formula.count('"--no-deps"') == 3
    assert '"--no-deps", "--no-build-isolation", buildpath' in formula
    assert '"--python=#{libexec}/bin/python", "check"' in formula
    assert 'libexec/"bin/pip"' not in formula
    assert "LIGHTWORK_BUILD_REQUIREMENTS_BEGIN" in formula
    assert "LIGHTWORK_BUILD_REQUIREMENTS_END" in formula
    assert "LIGHTWORK_RUNTIME_REQUIREMENTS_BEGIN" in formula
    assert "LIGHTWORK_RUNTIME_REQUIREMENTS_END" in formula
    assert ".fetch(1)" not in formula
    assert 'odie "generated build requirements are missing"' in formula
    assert "That tap is not deployed" in formula
    assert "brew install Daybreak-AI-Labs/tap/maverick" in formula
    assert "brew install cdayAI/tap/maverick" not in formula


def test_cross_ecosystem_osv_gate_is_pinned_complete_and_expiring():
    ci = _read(".github/workflows/ci.yml")
    step = ci.split(
        "- name: OSV Scanner across every reviewed dependency surface",
        1,
    )[1].split(
        "- name: Upload cross-ecosystem vulnerability report",
        1,
    )[0]
    assert "releases/download/v2.3.8/osv-scanner_linux_amd64" in step
    assert "bc98e15319ed0d515e3f9235287ba53cdc5535d576d24fd573978ecfe9ab92dc" in step  # pragma: allowlist secret
    assert "--config osv-scanner.toml" in step
    assert "|| true" not in step
    for dependency_surface in (
        "requirements/ci.txt",
        "rust/Cargo.lock",
        "apps/desktop/src-tauri/Cargo.lock",
        "go/model-proxy/go.mod",
    ):
        assert dependency_surface in step

    policy = tomllib.loads(_read("osv-scanner.toml"))
    ignored = policy["IgnoredVulns"]
    expected = {
        "GHSA-wrw7-89jp-8q8g",
        "RUSTSEC-2024-0370",
        *(f"RUSTSEC-2024-{number:04d}" for number in range(411, 421)),
        "RUSTSEC-2025-0075",
        "RUSTSEC-2025-0080",
        "RUSTSEC-2025-0081",
        "RUSTSEC-2025-0098",
        "RUSTSEC-2025-0100",
    }
    assert {item["id"] for item in ignored} == expected
    assert all(item["ignoreUntil"] == date(2026, 10, 31) for item in ignored)
    assert all("Tauri" in item["reason"] for item in ignored)


def test_go_model_proxy_security_floor_is_explicit():
    model_proxy = _read("go/model-proxy/go.mod")
    assert "\ngo 1.26.6\n" in model_proxy


def test_ci_integration_docs_do_not_call_workdir_a_security_boundary():
    action = _read("deploy/github-action/action.yml")
    action_docs = _read("deploy/github-action/README.md")
    gitlab = _read("deploy/gitlab-ci/maverick.gitlab-ci.yml")
    gitlab_docs = _read("deploy/gitlab-ci/README.md")

    for text in (action, action_docs, gitlab, gitlab_docs):
        assert "trusted disposable" in text
        assert "CI runner is already an ephemeral VM" not in text
    assert "working directory is not a filesystem or network" in (gitlab_docs)
    assert "confines the starting directory, not the" in action_docs


def test_external_github_actions_are_pinned_to_full_commits():
    files = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
    files.append(REPO_ROOT / "deploy" / "github-action" / "action.yml")

    mutable: list[str] = []
    for path in files:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match is None:
                continue
            target = match.group(1)
            if target.startswith(("./", "docker://")):
                continue
            if re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", target) is None:
                mutable.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {target}")

    assert mutable == [], "mutable external actions:\n" + "\n".join(mutable)


def test_markdown_workflow_examples_do_not_recommend_mutable_action_refs():
    mutable: list[str] = []
    for path in _repository_files(suffix=".md"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match is None:
                continue
            target = match.group(1)
            ref = target.rpartition("@")[2]
            if not ref or "full-40-character-commit-sha" in ref:
                continue
            if re.fullmatch(r"[0-9a-f]{40}", ref) is None:
                mutable.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {target}")

    assert mutable == [], "mutable Markdown action refs:\n" + "\n".join(mutable)


def test_active_runtime_guidance_never_resolves_first_party_from_public_index():
    active_files = (
        "packages/maverick-core/maverick/cli/__init__.py",
        "packages/maverick-core/maverick/health.py",
        "packages/maverick-core/maverick/deployment.py",
        "packages/maverick-core/maverick/queue_dispatcher.py",
    )
    unsafe = re.compile(
        r"pip(?:3|x)?\s+(?:install|inject)[^\n]*(?:maverick-agent|"
        r"maverick-dashboard|maverick-mcp-server|maverick-channels|"
        r"maverick-shield|maverick-installer)",
        re.IGNORECASE,
    )
    for path in active_files:
        assert unsafe.search(_read(path)) is None, path


def test_localized_getting_started_pages_use_reviewed_source_not_public_names():
    for path in sorted((REPO_ROOT / "docs" / "i18n").glob("*/getting-started.md")):
        text = path.read_text(encoding="utf-8")
        assert "pipx install 'maverick-agent[installer]'" not in text
        assert "git checkout --detach <reviewed-full-40-character-commit-sha>" in text


def _metadata(relative_path: str) -> dict:
    return tomllib.loads(_read(relative_path))


def _assert_floor(requirements: list[str], package: str, floor: str) -> None:
    matches = [
        Requirement(item)
        for item in requirements
        if Requirement(item).name.casefold() == package.casefold()
    ]
    assert matches, f"{package} is not constrained"
    lower_bounds = [
        Version(spec.version)
        for requirement in matches
        for spec in requirement.specifier
        if spec.operator == ">="
    ]
    exact_versions = [
        Version(spec.version)
        for requirement in matches
        for spec in requirement.specifier
        if spec.operator in {"==", "==="} and "*" not in spec.version
    ]
    has_safe_floor = bool(lower_bounds) and max(lower_bounds) >= Version(floor)
    has_safe_exact_pin = bool(exact_versions) and min(exact_versions) >= Version(floor)
    assert has_safe_floor or has_safe_exact_pin, (
        f"{package} must be constrained to >= {floor}: {matches}"
    )


def test_security_dependency_floors_cover_current_fixed_releases():
    core = _metadata("packages/maverick-core/pyproject.toml")
    core_deps = core["project"]["dependencies"]
    core_extras = core["project"]["optional-dependencies"]
    _assert_floor(core_deps, "click", "8.3.3")
    _assert_floor(core_deps, "cryptography", "48.0.1")
    _assert_floor(core_extras["langchain"], "langchain-core", "1.3.3")
    _assert_floor(core_extras["langchain"], "langsmith", "0.8.18")
    _assert_floor(core_extras["computer-use"], "pillow", "12.3.0")
    _assert_floor(core_extras["pdf"], "pypdf", "6.14.2")
    _assert_floor(core_extras["dev"], "python-multipart", "0.0.31")
    _assert_floor(core_extras["dev"], "starlette", "1.3.1")

    dashboard = _metadata("packages/maverick-dashboard/pyproject.toml")
    _assert_floor(dashboard["project"]["dependencies"], "python-multipart", "0.0.31")
    _assert_floor(dashboard["project"]["dependencies"], "starlette", "1.3.1")

    channels = _metadata("packages/maverick-channels/pyproject.toml")
    channel_extras = channels["project"]["optional-dependencies"]
    for extra in ("discord", "matrix", "all"):
        _assert_floor(channel_extras[extra], "aiohttp", "3.14.1")
    for extra in ("whatsapp", "sms", "all"):
        _assert_floor(channel_extras[extra], "python-multipart", "0.0.31")
        _assert_floor(channel_extras[extra], "starlette", "1.3.1")

    knowledge = _metadata("packages/maverick-knowledge/pyproject.toml")
    knowledge_extras = knowledge["project"]["optional-dependencies"]
    _assert_floor(knowledge_extras["parsers"], "pypdf", "6.14.2")
    _assert_floor(knowledge_extras["vision"], "pillow", "12.3.0")


def test_all_python_builds_require_patched_setuptools_floor():
    for path in _repository_files(filename="pyproject.toml"):
        metadata = tomllib.loads(path.read_text(encoding="utf-8"))
        build = metadata.get("build-system")
        if build is None:
            continue
        requirements = build.get("requires", [])
        if any(Requirement(item).name.casefold() == "setuptools" for item in requirements):
            _assert_floor(requirements, "setuptools", "83.0.0")
