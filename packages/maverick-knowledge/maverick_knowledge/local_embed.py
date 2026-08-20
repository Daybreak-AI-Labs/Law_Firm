"""Pinned, offline-only sentence-transformers embedding for firm knowledge."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_SUFFIXES = frozenset(
    {
        ".json",
        ".md",
        ".model",
        ".safetensors",
        ".tiktoken",
        ".txt",
        ".vocab",
        ".yaml",
        ".yml",
    }
)
_EXECUTABLE_OR_PICKLE_SUFFIXES = frozenset(
    {
        ".bin",
        ".ckpt",
        ".dll",
        ".dylib",
        ".pkl",
        ".pickle",
        ".pt",
        ".pth",
        ".py",
        ".pyc",
        ".so",
    }
)
_OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
}


def _regular_model_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise RuntimeError("knowledge: local embedding model directory is a symlink")
    try:
        root_info = root.stat()
    except OSError as exc:
        raise RuntimeError("knowledge: local embedding model directory is missing") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("knowledge: local embedding model path is not a directory")

    files: list[Path] = []
    for candidate in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if candidate.is_symlink():
            raise RuntimeError("knowledge: model tree may not contain symlinks")
        try:
            info = candidate.stat()
        except OSError as exc:
            raise RuntimeError("knowledge: model artifact could not be inspected") from exc
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("knowledge: model tree contains a non-regular artifact")
        suffix = candidate.suffix.lower()
        if suffix in _EXECUTABLE_OR_PICKLE_SUFFIXES:
            raise RuntimeError(
                f"knowledge: unsafe model artifact type {suffix!r} is forbidden"
            )
        if suffix not in _ALLOWED_SUFFIXES:
            raise RuntimeError(
                f"knowledge: unapproved model artifact type {suffix or '<none>'!r}"
            )
        files.append(candidate)
    if not files:
        raise RuntimeError("knowledge: local embedding model directory is empty")
    if not any(path.suffix.lower() == ".safetensors" for path in files):
        raise RuntimeError("knowledge: local embedding model requires safetensors weights")
    return files


def _reject_custom_code_metadata(root: Path, files: list[Path]) -> None:
    for path in files:
        if path.suffix.lower() != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("knowledge: model JSON metadata is invalid") from exc
        serialized = json.dumps(payload, sort_keys=True)
        if '"auto_map"' in serialized or '"custom_pipelines"' in serialized:
            raise RuntimeError("knowledge: custom/remote model code metadata is forbidden")
        if path.relative_to(root).as_posix() == "modules.json":
            if not isinstance(payload, list):
                raise RuntimeError("knowledge: sentence-transformers modules.json is invalid")
            for module in payload:
                if not isinstance(module, dict):
                    raise RuntimeError(
                        "knowledge: sentence-transformers modules.json is invalid"
                    )
                module_type = str(module.get("type") or "")
                if not module_type.startswith("sentence_transformers.models."):
                    raise RuntimeError(
                        "knowledge: custom sentence-transformers modules are forbidden"
                    )


def model_tree_digest(model_dir: str | Path) -> str:
    """Canonical content commitment over every approved model artifact."""
    root = Path(model_dir).expanduser()
    if not root.is_absolute():
        raise RuntimeError("knowledge: local embedding model path must be absolute")
    if root.is_symlink():
        raise RuntimeError("knowledge: local embedding model directory is a symlink")
    root = root.resolve(strict=True)
    files = _regular_model_files(root)
    _reject_custom_code_metadata(root, files)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(hashlib.sha256(content).digest())
    return "sha256:" + digest.hexdigest()


@contextmanager
def _offline_environment():
    prior = {key: os.environ.get(key) for key in _OFFLINE_ENV}
    os.environ.update(_OFFLINE_ENV)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LocalEmbedder:
    """Load one operator-provisioned, digest-pinned safetensors model on-box."""

    def __init__(self, model: str, expected_digest: str):
        model_path = Path(str(model or "")).expanduser()
        if not model_path.is_absolute():
            raise RuntimeError("knowledge: local embedding model path must be absolute")
        if model_path.is_symlink():
            raise RuntimeError("knowledge: local embedding model directory is a symlink")
        try:
            self.model_path = model_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("knowledge: local embedding model directory is missing") from exc
        expected = str(expected_digest or "").strip().lower()
        if not _DIGEST_RE.fullmatch(expected):
            raise RuntimeError(
                "knowledge: local embedding model requires model_digest='sha256:<64 hex>'"
            )
        actual = model_tree_digest(self.model_path)
        if actual != expected:
            raise RuntimeError("knowledge: local embedding model digest mismatch")
        self.expected_digest = expected
        self.model_name = str(self.model_path)
        self._model = None
        self.dim = 384

    def _ensure_model(self):
        if model_tree_digest(self.model_path) != self.expected_digest:
            raise RuntimeError("knowledge: local embedding model changed after admission")
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer

        signature = inspect.signature(SentenceTransformer)
        required = {"local_files_only", "trust_remote_code"}
        if not required.issubset(signature.parameters):
            raise RuntimeError(
                "knowledge: installed sentence-transformers cannot enforce offline "
                "and trust_remote_code=False"
            )
        with _offline_environment():
            self._model = SentenceTransformer(
                str(self.model_path),
                local_files_only=True,
                trust_remote_code=False,
                model_kwargs={
                    "local_files_only": True,
                    "trust_remote_code": False,
                },
                tokenizer_kwargs={
                    "local_files_only": True,
                    "trust_remote_code": False,
                },
                config_kwargs={
                    "local_files_only": True,
                    "trust_remote_code": False,
                },
            )
        reported = self._model.get_sentence_embedding_dimension()
        if reported:
            self.dim = int(reported)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in vector] for vector in vectors]


__all__ = ["LocalEmbedder", "model_tree_digest"]
