import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_public_release_machinery_is_not_shipped() -> None:
    """The firm distributes reviewed source/local builds, not public releases."""
    retired = (
        ".github/actionlint.yaml",
        ".github/workflows/release.yml",
        "deploy/verify-release.sh",
        "scripts/github_release_state.py",
        "scripts/normalize_python_artifacts.py",
        "scripts/release_version.py",
        "scripts/verify_github_release_assets.py",
    )
    assert not [path for path in retired if (REPO_ROOT / path).exists()]


def test_native_workflow_is_ci_only() -> None:
    workflow = (REPO_ROOT / ".github/workflows/native.yml").read_text(encoding="utf-8")

    assert 'tags: ["native-v*"]' not in workflow
    assert "does not publish release assets or write to a package registry" in workflow
    assert "actions/upload-artifact" in workflow
    for publishing_step in (
        "docker/login-action",
        "docker/build-push-action",
        "gh release",
        "pypa/gh-action-pypi-publish",
        "sigstore/cosign-installer",
        "softprops/action-gh-release",
    ):
        assert publishing_step not in workflow


def test_retained_workflows_have_no_publication_authority() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    ).lower()

    for permission in ("contents: write", "id-token: write", "packages: write"):
        assert permission not in workflows


def test_operator_docs_describe_only_private_builds() -> None:
    deployment = (REPO_ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs/operations.md").read_text(encoding="utf-8")
    navigation = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    cohort = (REPO_ROOT / "release-cohort.toml").read_text(encoding="utf-8")

    assert "docker build -f deploy/docker/Dockerfile" in deployment
    assert '--build-arg "PYTHON_DIGEST=${PYTHON_DIGEST}"' in deployment
    assert "docker image inspect --format '{{.Id}}'" in deployment
    assert "ghcr.io/daybreak-ai-labs/maverick" not in deployment
    assert "rate(maverick_goals_total" not in operations
    assert "increase(maverick_goals_total" not in operations
    assert "reference-architectures" not in navigation
    assert not (REPO_ROOT / "docs/reference-architectures.md").exists()
    assert "release workflow" not in cohort.lower()


def test_retired_desktop_frontend_and_sidecar_are_absent() -> None:
    assert not (REPO_ROOT / "apps/desktop").exists()
    assert not (
        REPO_ROOT / "apps/installer-cli/maverick_installer/bridge.py"
    ).exists()
    assert importlib.util.find_spec("maverick_installer.bridge") is None

    wizard = (
        REPO_ROOT / "apps/installer-cli/maverick_installer/wizard.py"
    ).read_text(encoding="utf-8")
    for retired_choice in ('"desktop ', '"phone '):
        assert retired_choice not in wizard


def test_retired_webhook_protocol_is_not_shipped_or_configurable() -> None:
    assert importlib.util.find_spec("maverick.webhooks") is None
    assert importlib.util.find_spec("maverick.flow.approvals") is None

    wizard = (
        REPO_ROOT / "apps/installer-cli/maverick_installer/wizard.py"
    ).read_text(encoding="utf-8")
    for retired_surface in (
        "MAVERICK_WEBHOOK_SECRET",
        "handoff_webhook",
        "pick_webhooks",
        'lines += _cfg_table("webhooks"',
    ):
        assert retired_surface not in wizard
