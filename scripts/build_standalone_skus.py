#!/usr/bin/env python3
"""Build deterministic GitHub Release source archives for standalone SKUs.

These demos are source distributions, not Python/PyPI distributions.  Every
archive is assembled from an explicit allowlist, carries provenance plus
per-file hashes, and uses normalized ZIP metadata so two builds from the same
version, revision, and source bytes are identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from release_version import validate_version  # noqa: E402

SKU_DIRS = (
    Path("demo/grc-concierge"),
    Path("demo/platform-threat-hunter"),
    Path("demo/environment-threat-hunter"),
    Path("demo/model-risk-ai-assurance-officer"),
)
TEXT_SUFFIXES = {".html", ".json", ".md", ".py", ".sh", ".txt", ".toml", ".yaml", ".yml"}
TEXT_FILENAMES = {"LICENSE"}
ARTIFACT_RE = re.compile(r"^lightwork-[a-z0-9]+(?:-[a-z0-9]+)*$")
REVISION_RE = re.compile(r"^(?:[0-9a-f]{7,64}|unknown)$")
DEPENDENCY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _source_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.casefold() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
        # Git checkout line-ending settings must not change release bytes.
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode()
    return data


def _safe_file(root: Path, relative: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or not declared.parts or ".." in declared.parts:
        raise ValueError(f"release manifest path must stay relative: {relative!r}")
    source = root / declared
    if source.is_symlink():
        raise ValueError(f"release manifest path must not be a symlink: {relative!r}")
    candidate = source.resolve()
    if candidate.parent != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError(f"release manifest path escapes its root: {relative!r}")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"release manifest path is not a regular file: {relative!r}")
    return candidate


def _read_manifest(sku_dir: Path) -> dict[str, Any]:
    path = sku_dir / "release-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError(f"{path}: unsupported schema_version")
    artifact_name = manifest.get("artifact_name")
    if not isinstance(artifact_name, str) or not ARTIFACT_RE.fullmatch(artifact_name):
        raise ValueError(f"{path}: invalid artifact_name")
    for key in ("description", "display_name", "entrypoint"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"{path}: {key} must be a non-empty string")
    for key in ("files", "shared_files"):
        values = manifest.get(key)
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{path}: {key} must be a non-empty string list")
        if len(values) != len(set(values)):
            raise ValueError(f"{path}: {key} contains duplicate paths")
    if manifest["entrypoint"] not in manifest["files"]:
        raise ValueError(f"{path}: entrypoint must be included in files")
    return manifest


def _declared_requirements(payload: bytes) -> list[dict[str, str]]:
    requirements: list[dict[str, str]] = []
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = DEPENDENCY_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"unsupported requirement declaration: {line!r}")
        name, constraint = match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        requirements.append(
            {
                "bom_ref": f"pkg:pypi/{normalized}",
                "constraint": constraint.strip(),
                "name": name,
                "requirement": line,
            }
        )
    return requirements


def _archive_payloads(sku_dir: Path, manifest: dict[str, Any]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {
        "release-manifest.json": _source_bytes(sku_dir / "release-manifest.json")
    }
    for relative in manifest["files"]:
        if relative in payloads:
            raise ValueError(f"duplicate archive destination: {relative!r}")
        payloads[relative] = _source_bytes(_safe_file(sku_dir, relative))
    for relative in manifest["shared_files"]:
        destination = Path(relative).name
        if destination in payloads:
            raise ValueError(f"duplicate archive destination: {destination!r}")
        payloads[destination] = _source_bytes(_safe_file(REPO_ROOT, relative))
    return payloads


def _write_zip(path: Path, root_name: str, payloads: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo(f"{root_name}/{relative}", date_time=ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = ((0o755 if relative.endswith(".sh") else 0o644) & 0xFFFF) << 16
            archive.writestr(info, payload)


def _build_sku(
    sku_relative: Path,
    *,
    version: str,
    source_revision: str,
    output_dir: Path,
) -> dict[str, Any]:
    sku_dir = REPO_ROOT / sku_relative
    manifest = _read_manifest(sku_dir)
    artifact_name = manifest["artifact_name"]
    root_name = f"{artifact_name}-{version}"
    payloads = _archive_payloads(sku_dir, manifest)
    requirement_payload = payloads.get("requirements.txt")
    if requirement_payload is None:
        raise ValueError(f"{sku_dir}: files must include requirements.txt")
    requirements = _declared_requirements(requirement_payload)
    release_metadata = {
        "artifact_name": artifact_name,
        "artifact_type": "standalone-source-archive",
        "authority_boundary": "unsigned local standalone store; no Lightwork governance authority",
        "display_name": manifest["display_name"],
        "entrypoint": manifest["entrypoint"],
        "files": {
            relative: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for relative, payload in sorted(payloads.items())
        },
        "license": "LicenseRef-Proprietary",
        "pypi_distribution": False,
        "requirements": [item["requirement"] for item in requirements],
        "schema_version": 1,
        "source_repository": "https://github.com/Daybreak-AI-Labs/Lightwork",
        "source_revision": source_revision,
        "version": version,
    }
    payloads["RELEASE-METADATA.json"] = _json_bytes(release_metadata)
    archive_path = output_dir / f"{root_name}.zip"
    _write_zip(archive_path, root_name, payloads)
    return {
        "archive": archive_path,
        "artifact_name": artifact_name,
        "description": manifest["description"],
        "display_name": manifest["display_name"],
        "requirements": requirements,
    }


def _cyclonedx_bom(
    builds: list[dict[str, Any]], *, version: str, source_revision: str
) -> dict[str, Any]:
    root_ref = f"urn:lightwork:standalone-skus:{version}"
    dependency_components: dict[str, dict[str, Any]] = {}
    components: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    sku_refs: list[str] = []
    for build in builds:
        sku_ref = f"urn:lightwork:sku:{build['artifact_name']}:{version}"
        sku_refs.append(sku_ref)
        components.append(
            {
                "bom-ref": sku_ref,
                "description": build["description"],
                "licenses": [{"license": {"name": "LicenseRef-Proprietary"}}],
                "name": build["artifact_name"],
                "properties": [
                    {"name": "lightwork:distribution-format", "value": "source-zip"},
                    {"name": "lightwork:pypi-distribution", "value": "false"},
                ],
                "type": "application",
                "version": version,
            }
        )
        dependency_refs: list[str] = []
        for requirement in build["requirements"]:
            dependency_refs.append(requirement["bom_ref"])
            component = dependency_components.setdefault(
                requirement["bom_ref"],
                {
                    "bom-ref": requirement["bom_ref"],
                    "name": requirement["name"],
                    "purl": requirement["bom_ref"],
                    "properties": [],
                    "type": "library",
                },
            )
            prop = {
                "name": f"lightwork:requirement:{build['artifact_name']}",
                "value": requirement["requirement"],
            }
            if prop not in component["properties"]:
                component["properties"].append(prop)
        dependencies.append({"dependsOn": sorted(set(dependency_refs)), "ref": sku_ref})
    components.extend(dependency_components[key] for key in sorted(dependency_components))
    dependencies.insert(0, {"dependsOn": sorted(sku_refs), "ref": root_ref})
    serial_material = f"{version}:{source_revision}"
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "components": components,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "name": "lightwork-standalone-skus",
                "properties": [
                    {"name": "lightwork:source-revision", "value": source_revision},
                    {"name": "lightwork:dependency-resolution", "value": "declared-constraints"},
                    {"name": "lightwork:distribution-format", "value": "source-zip"},
                    {"name": "lightwork:pypi-distribution", "value": "false"},
                ],
                "type": "application",
                "version": version,
            }
        },
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_material)}",
        "specVersion": "1.6",
        "version": 1,
    }


def build(version: str, source_revision: str, output_dir: Path) -> list[Path]:
    version = version.removeprefix("v")
    source_revision = source_revision.casefold()
    try:
        release = validate_version(version)
    except ValueError as exc:
        raise ValueError(
            f"version must be a canonical public release version, got {version!r}"
        ) from exc
    if release.version != version:
        raise ValueError(
            f"version must be a canonical public release version, got {version!r}"
        )
    if not REVISION_RE.fullmatch(source_revision):
        raise ValueError("source revision must be 7-64 hexadecimal characters or 'unknown'")
    output_dir.mkdir(parents=True, exist_ok=True)
    builds = [
        _build_sku(
            sku_dir,
            version=version,
            source_revision=source_revision,
            output_dir=output_dir,
        )
        for sku_dir in SKU_DIRS
    ]
    bom_path = output_dir / f"lightwork-standalone-skus-{version}.cdx.json"
    bom_path.write_bytes(_json_bytes(_cyclonedx_bom(builds, version=version, source_revision=source_revision)))
    artifacts = [build["archive"] for build in builds] + [bom_path]
    checksum_path = output_dir / f"lightwork-standalone-skus-{version}.sha256"
    checksum_path.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(artifacts)
        ),
        encoding="utf-8",
        newline="\n",
    )
    return [*artifacts, checksum_path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release version, with optional v prefix")
    parser.add_argument(
        "--source-revision",
        default="unknown",
        help="immutable source commit (7-64 hex characters; defaults to unknown)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    for artifact in build(args.version, args.source_revision, args.output_dir.resolve()):
        print(artifact)


if __name__ == "__main__":
    main()
