"""Local Whisper (whisper.cpp GGML) model management — the built-in STT path.

Speech-to-text has to work inside customer environments and on desktops:
no provider API key, no Python ML stack, optionally no network at all.
``tools/voice.py`` gets that from the whisper.cpp backend, which needs a
GGML weights file on disk. This module owns where those files live
(``<maverick_home>/models/whisper``), which releases are trusted (pinned
SHA-256), and how they are fetched:

- ``maverick voice setup`` — explicit, operator-driven download (air-gapped
  deployments run it wherever there IS network and copy the file in).
- auto-fetch on first use — opt-in via ``MAVERICK_VOICE_AUTO_FETCH=1`` or
  ``[voice] auto_fetch_model = true`` (the installer wizard offers it).

A download that does not hash to the pinned digest is discarded, never
installed: model weights execute math, not code, but a swapped file still
controls every transcript the platform acts on.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL = "base"

# name -> (filename, sha256, size_bytes). Digests are the Hugging Face LFS
# oids for ggerganov/whisper.cpp; ``.en`` variants are English-only (slightly
# better English accuracy), the rest are multilingual.
KNOWN_MODELS: dict[str, tuple[str, str, int]] = {
    "tiny": (
        "ggml-tiny.bin",
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",  # pragma: allowlist secret
        77_691_713,
    ),
    "tiny.en": (
        "ggml-tiny.en.bin",
        "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",  # pragma: allowlist secret
        77_704_715,
    ),
    "base": (
        "ggml-base.bin",
        "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",  # pragma: allowlist secret
        147_951_465,
    ),
    "base.en": (
        "ggml-base.en.bin",
        "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",  # pragma: allowlist secret
        147_964_211,
    ),
    "small": (
        "ggml-small.bin",
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",  # pragma: allowlist secret
        487_601_967,
    ),
    "small.en": (
        "ggml-small.en.bin",
        "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",  # pragma: allowlist secret
        487_614_201,
    ),
    "medium": (
        "ggml-medium.bin",
        "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",  # pragma: allowlist secret
        1_533_763_059,
    ),
    "medium.en": (
        "ggml-medium.en.bin",
        "cc37e93478338ec7700281a7ac30a10128929eb8f427dda2e865faa8f6da4356",  # pragma: allowlist secret
        1_533_774_781,
    ),
    "large-v3": (
        "ggml-large-v3.bin",
        "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",  # pragma: allowlist secret
        3_095_033_483,
    ),
    "large-v3-turbo": (
        "ggml-large-v3-turbo.bin",
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",  # pragma: allowlist secret
        1_624_555_275,
    ),
}

_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
_CHUNK = 1 << 20  # 1 MiB


def _voice_cfg() -> dict:
    try:
        from .config import load_config
        return dict(load_config().get("voice") or {})
    except Exception:  # config must never break voice paths
        return {}


def models_dir() -> Path:
    """Where GGML weights live. NOT tenant-scoped: weights are shared
    read-only artifacts, not tenant data."""
    from .paths import maverick_home
    return maverick_home() / "models" / "whisper"


def configured_model() -> str:
    """The model size setup/auto-fetch should install:
    ``MAVERICK_WHISPER_MODEL`` env > ``[voice] stt_model`` > ``base``."""
    env = (os.environ.get("MAVERICK_WHISPER_MODEL") or "").strip()
    if env:
        return env
    cfg = str(_voice_cfg().get("stt_model") or "").strip()
    return cfg or DEFAULT_MODEL


def model_path(name: str) -> Path:
    if name not in KNOWN_MODELS:
        raise ValueError(
            f"unknown Whisper model {name!r}; known: "
            + ", ".join(sorted(KNOWN_MODELS))
        )
    return models_dir() / KNOWN_MODELS[name][0]


def installed_models() -> list[Path]:
    """GGML files present on disk (any ``ggml-*.bin``, including ones an
    operator copied in by hand)."""
    d = models_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("ggml-*.bin") if p.is_file())


def verify_model(path: Path) -> bool | None:
    """True/False when ``path`` matches/violates a pinned digest; None when
    the filename isn't one we pin (operator-supplied model — allowed)."""
    pinned = {fn: sha for fn, sha, _ in KNOWN_MODELS.values()}
    sha = pinned.get(path.name)
    if sha is None:
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest() == sha


def auto_fetch_enabled() -> bool:
    """Runtime download of the configured model on first use / warm-up.

    ON by default so the dashboard mic works with zero setup — the dashboard
    warm-up fetches at startup, not mid-request. Egress-locked deployments
    (enterprise boundary / compliance floors) default OFF: a locked box must
    not phone Hugging Face because someone clicked a mic. An explicit
    ``MAVERICK_VOICE_AUTO_FETCH`` env or ``[voice] auto_fetch_model`` config
    value always wins, in either direction."""
    raw = (os.environ.get("MAVERICK_VOICE_AUTO_FETCH") or "").strip().lower()
    if raw:
        return raw not in {"0", "false", "no", "off"}
    cfg = _voice_cfg()
    if "auto_fetch_model" in cfg:
        return bool(cfg["auto_fetch_model"])
    try:
        from .enterprise import enterprise_enabled
        return not enterprise_enabled()
    except Exception:  # posture probe must never break voice paths
        return True


def download_model(
    name: str | None = None,
    *,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 60.0,
) -> Path:
    """Fetch a GGML model into :func:`models_dir`, verifying the pinned
    SHA-256 before install (download to a temp file, hash, atomic rename).

    Raises ``ValueError`` for an unknown name or a digest mismatch, and
    ``OSError``/``URLError`` on network failure. Returns the installed path.
    """
    name = name or configured_model()
    dest = model_path(name)  # raises on unknown name
    filename, want_sha, want_size = KNOWN_MODELS[name]
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _BASE_URL + filename

    # Temp file in the destination dir so the final rename is atomic (same fs).
    fd, tmp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".part",
                                    dir=dest.parent)
    tmp = Path(tmp_name)
    h = hashlib.sha256()
    done = 0
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(  # noqa: S310 -- pinned https URL
            url, timeout=timeout
        ) as resp:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, want_size)
        if h.hexdigest() != want_sha:
            raise ValueError(
                f"checksum mismatch for {filename}: got {h.hexdigest()}, "
                f"expected {want_sha} — refusing to install"
            )
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    log.info("installed whisper model %s (%d bytes)", dest, done)
    return dest


def locate_model() -> Path | None:
    """The GGML file the whisper.cpp backend should load, or None.

    Order: ``MAVERICK_WHISPER_CPP_MODEL`` env path > ``[voice]
    whisper_cpp_model`` path > the configured size if installed > any
    installed GGML file (so a hand-copied model Just Works) > auto-fetch of
    the configured size when enabled.
    """
    for raw in (os.environ.get("MAVERICK_WHISPER_CPP_MODEL"),
                _voice_cfg().get("whisper_cpp_model")):
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_file():
                return p
            log.warning("configured whisper model %s not found", p)
    name = configured_model()
    try:
        preferred = model_path(name)
    except ValueError:
        log.warning("unknown [voice] stt_model %r; known: %s",
                    name, ", ".join(sorted(KNOWN_MODELS)))
        preferred = None
    if preferred is not None and preferred.is_file():
        return preferred
    installed = installed_models()
    if installed:
        return installed[0]
    if preferred is not None and auto_fetch_enabled():
        try:
            return download_model(name)
        except Exception as e:
            log.warning("whisper model auto-fetch failed: %s", e)
    return None
