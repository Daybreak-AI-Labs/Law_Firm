"""Encrypted, authenticated backup / transactional restore for one client.

Backups snapshot the active tenant/client data root. SQLite databases use the
online backup API; every payload file is content-hashed; ``manifest.json`` is
authenticated with an operator-custodied HMAC-SHA256 signing key; and the
complete compressed archive is wrapped in streaming AES-256-GCM with a
different operator-custodied encryption key.  The keys are supplied only via
``MAVERICK_BACKUP_SIGNING_KEY`` and ``MAVERICK_BACKUP_ENCRYPTION_KEY`` (each
exactly 32 bytes encoded as hex or base64), never read from or written beneath
the data root.

Unencrypted legacy archives have an intentionally awkward compatibility path:
callers must pass ``allow_legacy_auth_off=True`` to ``read_manifest`` or
``restore_backup``.  The operator CLI never exposes that migration-only
override. ``force=True`` only overrides client/schema compatibility and never
bypasses encryption or signature verification.

Restore scans tar streams without ``getmembers``/``extractall``, enforces hard
member/path/type/per-file/expanded-size limits, writes only into a private
staging tree, and authenticates/verifies the complete payload before touching
live state. Application is journaled: originals plus SQLite sidecars are saved
first, and any failure rolls the entire file set back. An interrupted restore
is recovered from its private journal before the next restore begins.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import stat
import tarfile
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from . import file_lock

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
SCHEMA = 3
_AUTH_ALGORITHM = "HMAC-SHA256"
_SIGNING_KEY_ENV = "MAVERICK_BACKUP_SIGNING_KEY"
_ENCRYPTION_KEY_ENV = "MAVERICK_BACKUP_ENCRYPTION_KEY"
_ENCRYPTED_MAGIC = b"MVK-BACKUP\x00"
_ENVELOPE_VERSION = 1
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_ENVELOPE_HEADER_BYTES = len(_ENCRYPTED_MAGIC) + 1 + _GCM_NONCE_BYTES
_SKIP_SUFFIXES = ("-wal", "-shm", ".tmp")
_RESTORE_TX_ROOT = ".restore-transactions"
_RESTORE_LOCK_TARGET = ".restore-transaction"
_JOURNAL = "journal.json"
_DB_SIDECARS = ("-wal", "-shm", "-journal")

# Generous production ceilings, but finite on every attacker-controlled axis.
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_FILE_BYTES = 8 * 1024**3
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024**3
MAX_ARCHIVE_COMPRESSED_BYTES = 64 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_ARCHIVE_PATH_BYTES = 1024
MAX_COMPONENT_UNITS = 255
_COPY_CHUNK = 1 << 20
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}


class BackupError(RuntimeError):
    """A backup cannot be created or a restore cannot be safely applied."""


def _client_root() -> Path:
    from .paths import data_dir

    return data_dir()


def _client_id() -> str | None:
    from .client import client_id

    return client_id()


def data_backups_dir() -> Path:
    from .paths import data_dir

    return data_dir("backups")


def _is_alias(path: Path, info: os.stat_result) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)
        or bool(callable(is_junction) and is_junction())
    )


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink


def _canonical_body(document: dict[str, Any]) -> bytes:
    body = {k: v for k, v in document.items() if k != "authentication"}
    try:
        return json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BackupError("backup metadata is not canonical JSON") from exc


def _decode_key(raw: str, *, environment: str) -> bytes:
    value = raw.strip()
    if len(value) == 64:
        try:
            decoded = bytes.fromhex(value)
        except ValueError:
            decoded = b""
        if len(decoded) == 32:
            return decoded
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) != 32:
        raise BackupError(
            f"{environment} must be exactly 32 bytes encoded as hex or base64"
        )
    return decoded


def _environment_key(environment: str, *, required: bool) -> bytes | None:
    raw = os.environ.get(environment, "")
    if not raw.strip():
        if required:
            raise BackupError(
                f"secure backup requires {environment}; refusing to create or "
                "open an unprotected archive"
            )
        return None
    return _decode_key(raw, environment=environment)


def _operator_key(*, required: bool) -> bytes | None:
    """Return the manifest signing key (legacy private-name compatibility)."""
    return _environment_key(_SIGNING_KEY_ENV, required=required)


def _encryption_key(*, required: bool) -> bytes | None:
    return _environment_key(_ENCRYPTION_KEY_ENV, required=required)


def _require_independent_keys(signing_key: bytes, encryption_key: bytes) -> None:
    if hmac.compare_digest(signing_key, encryption_key):
        raise BackupError(
            f"{_SIGNING_KEY_ENV} and {_ENCRYPTION_KEY_ENV} must contain "
            "independent key values"
        )


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _authenticate_for_write(
    document: dict[str, Any], key: bytes | None,
) -> dict[str, Any]:
    result = dict(document)
    if key is None:
        result["authentication"] = {"algorithm": "none"}
        return result
    signature = hmac.new(key, _canonical_body(result), hashlib.sha256).hexdigest()
    result["authentication"] = {
        "algorithm": _AUTH_ALGORITHM,
        "key_id": _key_id(key),
        "signature": signature,
    }
    return result


def _verify_authentication(
    document: dict[str, Any], *, allow_unsigned: bool, label: str,
) -> bytes | None:
    auth = document.get("authentication")
    if auth is None:
        if allow_unsigned:
            log.warning("accepting explicitly-authorized unsigned legacy %s", label)
            return None
        raise BackupError(f"{label} is unsigned; authenticity cannot be established")
    if not isinstance(auth, dict):
        raise BackupError(f"{label} authentication metadata is malformed")
    algorithm = auth.get("algorithm")
    if algorithm == "none":
        if set(auth) != {"algorithm"}:
            raise BackupError(f"{label} unsigned metadata is ambiguous")
        if not allow_unsigned:
            raise BackupError(f"{label} is unsigned; authenticity cannot be established")
        log.warning("accepting explicitly-authorized unsigned %s", label)
        return None
    if algorithm != _AUTH_ALGORITHM:
        raise BackupError(f"{label} uses an unsupported authentication algorithm")
    if set(auth) != {"algorithm", "key_id", "signature"}:
        raise BackupError(f"{label} authentication metadata is ambiguous")
    key = _operator_key(required=True)
    assert key is not None
    claimed_key_id = auth.get("key_id")
    signature = auth.get("signature")
    if claimed_key_id != _key_id(key):
        raise BackupError(f"{label} was not signed by the configured operator key")
    if not isinstance(signature, str) or len(signature) != 64:
        raise BackupError(f"{label} signature is malformed")
    expected = hmac.new(key, _canonical_body(document), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise BackupError(f"{label} signature verification failed")
    return key


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise BackupError(f"unsafe {label}: {value!r}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BackupError(f"unsafe {label}: {value!r}") from exc
    if len(encoded) > MAX_ARCHIVE_PATH_BYTES:
        raise BackupError(f"{label} is too long")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(p in {"", ".", ".."} for p in path.parts)
    ):
        raise BackupError(f"unsafe {label}: {value!r}")
    for component in path.parts:
        if (
            component.endswith((".", " "))
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in component)
            or any(c in _WINDOWS_FORBIDDEN for c in component)
        ):
            raise BackupError(f"unsafe {label}: {value!r}")
        basename = component.split(".", 1)[0].rstrip(" ").upper()
        if basename in _WINDOWS_RESERVED:
            raise BackupError(f"unsafe {label}: {value!r}")
        if (
            len(component.encode("utf-8")) > MAX_COMPONENT_UNITS
            or len(component.encode("utf-16-le")) // 2 > MAX_COMPONENT_UNITS
        ):
            raise BackupError(f"{label} component is too long")
    return path


def _is_reserved_restore_path(relative: PurePosixPath) -> bool:
    parts = relative.parts
    return (
        parts[0] in {"backups", "keys", _RESTORE_TX_ROOT}
        or parts[:2] == ("audit", "keys")
        or relative.as_posix() == f"{_RESTORE_LOCK_TARGET}.lock"
    )


class _ArchiveBudget:
    def __init__(self) -> None:
        self.members = 0
        self.expanded = 0
        self.names: set[str] = set()
        self.portable_names: dict[str, str] = {}

    def observe(self, member: tarfile.TarInfo) -> str | None:
        self.members += 1
        if self.members > MAX_ARCHIVE_MEMBERS:
            raise BackupError("backup exceeds the archive member-count limit")
        name = member.name
        if not isinstance(name, str) or name in self.names:
            raise BackupError(f"duplicate or malformed archive member: {name!r}")
        self.names.add(name)
        folded = unicodedata.normalize("NFC", name).casefold()
        prior = self.portable_names.get(folded)
        if prior is not None and prior != name:
            raise BackupError(
                f"archive members collide on a case-insensitive host: {prior!r}, {name!r}"
            )
        self.portable_names[folded] = name

        if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
            raise BackupError(f"unsupported archive member type: {name!r}")

        if name == MANIFEST:
            if not member.isfile() or member.issym() or member.islnk():
                raise BackupError("manifest.json must be one regular file")
            rel: str | None = None
        elif name == "data":
            if not member.isdir():
                raise BackupError("data archive root must be a directory")
            rel = None
        elif name.startswith("data/"):
            rel = name[len("data/"):]
            safe_rel = _safe_relative_path(rel, label="archive member path")
            if _is_reserved_restore_path(safe_rel):
                raise BackupError(f"archive member targets reserved restore state: {name!r}")
            if not (member.isfile() or member.isdir()) or member.issym() or member.islnk():
                raise BackupError(f"unsupported archive member type: {name!r}")
        else:
            raise BackupError(f"unexpected archive member: {name!r}")

        if member.isdir():
            if member.size not in {0, None}:
                raise BackupError(f"directory member has a payload: {name!r}")
            return rel
        if not isinstance(member.size, int) or member.size < 0:
            raise BackupError(f"archive member has an invalid size: {name!r}")
        if member.size > MAX_ARCHIVE_FILE_BYTES:
            raise BackupError(f"archive member exceeds the per-file limit: {name!r}")
        if name == MANIFEST and member.size > MAX_MANIFEST_BYTES:
            raise BackupError("manifest.json exceeds the manifest-size limit")
        self.expanded += member.size
        if self.expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise BackupError("backup exceeds the total expanded-size limit")
        return rel


@contextmanager
def _verified_archive(path: str | Path):
    archive = Path(path)
    try:
        before = archive.lstat()
    except OSError as exc:
        raise BackupError(f"unreadable backup {str(path)!r}: {exc}") from exc
    if (
        _is_alias(archive, before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size
        > MAX_ARCHIVE_COMPRESSED_BYTES + _ENVELOPE_HEADER_BYTES + _GCM_TAG_BYTES
    ):
        raise BackupError("backup path must be one bounded, single-link regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(archive), flags)
    except OSError as exc:
        raise BackupError(f"backup could not be opened safely: {exc}") from exc
    try:
        opened = os.fstat(fd)
        after = archive.lstat()
        if (
            _is_alias(archive, after)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or after.st_nlink != 1
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
        ):
            raise BackupError("backup path identity changed while opening")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            yield stream
    finally:
        if fd >= 0:
            os.close(fd)


def _read_exact(member_stream: BinaryIO, size: int, *, limit: int) -> bytes:
    if size > limit:
        raise BackupError("archive member exceeds its read limit")
    data = member_stream.read(size + 1)
    if not isinstance(data, bytes) or len(data) != size:
        raise BackupError("archive member is truncated or overlong")
    return data


def _parse_manifest(data: bytes) -> dict[str, Any]:
    def _reject_constant(value: str):
        raise ValueError(f"non-finite JSON constant {value}")

    try:
        manifest = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BackupError("manifest.json is not valid bounded JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupError("manifest.json must contain an object")
    return manifest


def _read_manifest_stream(
    stream: BinaryIO, *, allow_unsigned: bool,
) -> tuple[dict[str, Any], bytes | None]:
    manifest: dict[str, Any] | None = None
    budget = _ArchiveBudget()
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                budget.observe(member)
                if member.name != MANIFEST:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup has no readable manifest.json")
                manifest = _parse_manifest(
                    _read_exact(extracted, member.size, limit=MAX_MANIFEST_BYTES)
                )
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"unreadable backup archive: {exc}") from exc
    if manifest is None:
        raise BackupError("backup has no manifest.json")
    key = _verify_authentication(
        manifest,
        allow_unsigned=allow_unsigned,
        label="backup manifest",
    )
    _manifest_files(manifest, allow_legacy=allow_unsigned)
    return manifest, key


def _manifest_files(
    manifest: dict[str, Any], *, allow_legacy: bool,
) -> dict[str, tuple[str, int | None]]:
    schema = manifest.get("schema")
    if schema != SCHEMA and not (allow_legacy and schema in {1, 2}):
        raise BackupError(f"unsupported backup manifest schema: {schema!r}")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or len(raw_files) > MAX_ARCHIVE_MEMBERS:
        raise BackupError("backup manifest files map is missing or oversized")
    if not raw_files:
        raise BackupError("backup manifest contains no payload files")
    expected: dict[str, tuple[str, int | None]] = {}
    portable: dict[str, str] = {}
    total = 0
    for raw_rel, record in raw_files.items():
        rel = _safe_relative_path(raw_rel, label="manifest file path").as_posix()
        if _is_reserved_restore_path(PurePosixPath(rel)):
            raise BackupError(f"manifest targets reserved restore state: {rel}")
        folded = unicodedata.normalize("NFC", rel).casefold()
        if folded in portable and portable[folded] != rel:
            raise BackupError("manifest file paths collide on a portable host")
        portable[folded] = rel
        if isinstance(record, str) and allow_legacy and schema == 1:
            digest, size = record, None
        elif isinstance(record, dict):
            digest, size = record.get("sha256"), record.get("size")
            if not isinstance(size, int) or size < 0 or size > MAX_ARCHIVE_FILE_BYTES:
                raise BackupError(f"invalid manifest size for {rel}")
            total += size
        else:
            raise BackupError(f"invalid manifest record for {rel}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise BackupError(f"invalid manifest SHA-256 for {rel}")
        expected[rel] = (digest, size)
    if total > MAX_ARCHIVE_EXPANDED_BYTES:
        raise BackupError("manifest declares too much expanded data")
    return expected


def read_manifest(
    tarball: str | Path, *, allow_legacy_auth_off: bool = False,
) -> dict[str, Any]:
    """Return a bounded, decrypted and authenticated manifest.

    ``allow_legacy_auth_off=True`` is a migration-only compatibility path for
    old unencrypted archives. It is intentionally unavailable from the CLI.
    """
    with _plaintext_archive(
        tarball,
        allow_legacy_auth_off=allow_legacy_auth_off,
    ) as (stream, legacy, encryption_key):
        manifest, signing_key = _read_manifest_stream(
            stream,
            allow_unsigned=legacy,
        )
        _manifest_files(manifest, allow_legacy=legacy)
        if signing_key is not None and encryption_key is not None:
            _require_independent_keys(signing_key, encryption_key)
        return manifest


def _new_private_empty(path: Path) -> None:
    try:
        file_lock.atomic_create_bytes(path, b"")
    except FileExistsError as exc:
        raise BackupError(f"refusing an existing or planted path: {path}") from exc
    except OSError as exc:
        raise BackupError(f"could not create private staging file: {path}") from exc


def _open_private_writer(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BackupError(f"private staging path disappeared: {path}") from exc
    if (
        _is_alias(path, before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not file_lock.private_path_is_restricted(path)
    ):
        raise BackupError(f"staging path is not a private regular file: {path}")
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags)
    try:
        opened = os.fstat(fd)
        after = path.lstat()
        if (
            _is_alias(path, after)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or after.st_nlink != 1
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
        ):
            raise BackupError(f"staging path identity changed: {path}")
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _encrypt_archive_file(
    source: Path,
    destination: Path,
    *,
    key: bytes,
    destination_precreated: bool = False,
) -> None:
    """Stream one bounded compressed tar through AES-256-GCM."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - cryptography is a core dep
        raise BackupError("backup encryption requires the cryptography package") from exc

    if len(key) != 32:
        raise BackupError("backup encryption requires an AES-256 key")
    if not destination_precreated:
        file_lock.ensure_private_directory(destination.parent)
        _new_private_empty(destination)
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
    header = _ENCRYPTED_MAGIC + bytes([_ENVELOPE_VERSION]) + nonce
    fd = -1
    try:
        with _verified_archive(source) as reader:
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            fd, _ = _open_private_writer(destination)
            with os.fdopen(fd, "wb") as output:
                fd = -1
                output.write(header)
                for chunk in iter(lambda: reader.read(_COPY_CHUNK), b""):
                    if not isinstance(chunk, bytes):
                        raise BackupError("backup archive source returned non-bytes data")
                    ciphertext = encryptor.update(chunk)
                    if ciphertext:
                        output.write(ciphertext)
                tail = encryptor.finalize()
                if tail:
                    output.write(tail)
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
        if not file_lock.private_path_is_restricted(destination):
            raise BackupError("encrypted backup did not retain private permissions")
    except BaseException:
        if fd >= 0:
            os.close(fd)
        destination.unlink(missing_ok=True)
        raise


def _decrypt_archive_to_private(
    source: BinaryIO,
    destination: Path,
    *,
    key: bytes,
) -> None:
    """Authenticate and stream-decrypt an envelope before exposing plaintext."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - cryptography is a core dep
        raise BackupError("backup decryption requires the cryptography package") from exc

    if len(key) != 32:
        raise BackupError("backup decryption requires an AES-256 key")
    try:
        total_size = os.fstat(source.fileno()).st_size
    except (AttributeError, OSError) as exc:
        raise BackupError("backup source has no stable file size") from exc
    minimum_size = _ENVELOPE_HEADER_BYTES + _GCM_TAG_BYTES + 1
    maximum_size = (
        MAX_ARCHIVE_COMPRESSED_BYTES
        + _ENVELOPE_HEADER_BYTES
        + _GCM_TAG_BYTES
    )
    if total_size < minimum_size or total_size > maximum_size:
        raise BackupError("encrypted backup envelope has an invalid bounded size")

    source.seek(0)
    header = source.read(_ENVELOPE_HEADER_BYTES)
    if (
        not isinstance(header, bytes)
        or len(header) != _ENVELOPE_HEADER_BYTES
        or not header.startswith(_ENCRYPTED_MAGIC)
    ):
        raise BackupError("backup encryption envelope header is malformed")
    version_offset = len(_ENCRYPTED_MAGIC)
    if header[version_offset] != _ENVELOPE_VERSION:
        raise BackupError("backup encryption envelope version is unsupported")
    nonce = header[-_GCM_NONCE_BYTES:]
    source.seek(total_size - _GCM_TAG_BYTES)
    tag = source.read(_GCM_TAG_BYTES)
    if not isinstance(tag, bytes) or len(tag) != _GCM_TAG_BYTES:
        raise BackupError("backup encryption envelope tag is truncated")
    ciphertext_bytes = total_size - _ENVELOPE_HEADER_BYTES - _GCM_TAG_BYTES
    source.seek(_ENVELOPE_HEADER_BYTES)

    file_lock.ensure_private_directory(destination.parent)
    _new_private_empty(destination)
    fd = -1
    try:
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        fd, _ = _open_private_writer(destination)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            remaining = ciphertext_bytes
            while remaining:
                chunk = source.read(min(_COPY_CHUNK, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise BackupError("encrypted backup ciphertext is truncated")
                remaining -= len(chunk)
                plaintext = decryptor.update(chunk)
                if plaintext:
                    output.write(plaintext)
            try:
                tail = decryptor.finalize()
            except InvalidTag as exc:
                raise BackupError(
                    "backup decryption authentication failed (wrong key or tampering)"
                ) from exc
            if tail:
                output.write(tail)
            output.flush()
            os.fsync(output.fileno())
        if destination.stat().st_size > MAX_ARCHIVE_COMPRESSED_BYTES:
            raise BackupError("decrypted backup exceeds the compressed-size limit")
        if not file_lock.private_path_is_restricted(destination):
            raise BackupError("decrypted backup did not retain private permissions")
    except BaseException:
        if fd >= 0:
            os.close(fd)
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def _plaintext_archive(
    path: str | Path,
    *,
    allow_legacy_auth_off: bool,
):
    """Yield a fully authenticated plaintext tar stream and its key posture."""
    with _verified_archive(path) as source:
        prefix = source.read(len(_ENCRYPTED_MAGIC))
        source.seek(0)
        if prefix != _ENCRYPTED_MAGIC:
            if not allow_legacy_auth_off:
                raise BackupError(
                    "backup is not encrypted; legacy unencrypted restore is "
                    "available only through the explicit auth-off migration path"
                )
            log.warning(
                "accepting explicitly-authorized auth-off legacy unencrypted backup"
            )
            yield source, True, None
            return

        encryption_key = _encryption_key(required=True)
        assert encryption_key is not None
        with tempfile.TemporaryDirectory(prefix="mvk-backup-decrypt-") as temporary:
            private = file_lock.ensure_private_directory(temporary)
            plaintext = private / "archive.tgz"
            _decrypt_archive_to_private(source, plaintext, key=encryption_key)
            # GCM finalize above authenticates every byte before this reader is
            # exposed to manifest parsing or restore staging.
            with _verified_archive(plaintext) as decrypted:
                yield decrypted, False, encryption_key


def _stream_to_new_private(
    reader: BinaryIO,
    destination: Path,
    *,
    expected_size: int,
    precreated: bool = False,
) -> tuple[str, int]:
    if expected_size < 0 or expected_size > MAX_ARCHIVE_FILE_BYTES:
        raise BackupError("file exceeds the backup per-file limit")
    file_lock.ensure_private_directory(destination.parent)
    if not precreated:
        _new_private_empty(destination)
    digest = hashlib.sha256()
    remaining = expected_size
    fd = -1
    try:
        fd, _ = _open_private_writer(destination)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            while remaining:
                chunk = reader.read(min(_COPY_CHUNK, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise BackupError("source file is truncated during streaming copy")
                if len(chunk) > remaining:
                    raise BackupError("source file exceeded its declared size")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if reader.read(1) not in {b"", None}:
                raise BackupError("source file grew during streaming copy")
            output.flush()
            os.fsync(output.fileno())
        if not file_lock.private_path_is_restricted(destination):
            raise BackupError("streamed file did not retain private permissions")
        return digest.hexdigest(), expected_size
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    destination_precreated: bool = False,
) -> tuple[str, int]:
    try:
        before = source.lstat()
    except OSError as exc:
        raise BackupError(f"source file could not be inspected: {source}") from exc
    if _is_alias(source, before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise BackupError(f"source is not a single-link regular file: {source}")
    if before.st_size > MAX_ARCHIVE_FILE_BYTES:
        raise BackupError(f"source file exceeds the per-file limit: {source}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(source), flags)
    try:
        opened = os.fstat(fd)
        after_open = source.lstat()
        if (
            _is_alias(source, after_open)
            or opened.st_nlink != 1
            or after_open.st_nlink != 1
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after_open)
        ):
            raise BackupError(f"source file identity changed: {source}")
        with os.fdopen(fd, "rb") as reader:
            fd = -1
            result = _stream_to_new_private(
                reader,
                destination,
                expected_size=opened.st_size,
                precreated=destination_precreated,
            )
        final = source.lstat()
        if (
            _identity(after_open) != _identity(final)
            or final.st_size != opened.st_size
            or getattr(final, "st_mtime_ns", None) != getattr(after_open, "st_mtime_ns", None)
        ):
            destination.unlink(missing_ok=True)
            raise BackupError(f"source file changed during backup: {source}")
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _consistent_db_copy(source: Path, destination: Path) -> tuple[str, int]:
    file_lock.ensure_private_directory(destination.parent)
    _new_private_empty(destination)
    try:
        src = sqlite3.connect(str(source))
        try:
            dst = sqlite3.connect(str(destination))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        file_lock.ensure_private_file(destination)
        size = destination.stat().st_size
        if size > MAX_ARCHIVE_FILE_BYTES:
            raise BackupError(f"database exceeds the per-file limit: {source}")
        return _sha256(destination), size
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(_COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _world_schema_version(root: Path) -> int | None:
    db = root / "world.db"
    if not db.exists():
        return None
    try:
        connection = sqlite3.connect(str(db))
        try:
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _excluded_from_snapshot(relative: Path) -> bool:
    parts = relative.parts
    return (
        not parts
        or parts[0] in {"backups", "keys", _RESTORE_TX_ROOT}
        or parts[:2] == ("audit", "keys")
        or relative.name == f"{_RESTORE_LOCK_TARGET}.lock"
        or relative.name.endswith(_SKIP_SUFFIXES)
    )


def _stage(root: Path, stage: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    total = 0
    count = 0
    for source in sorted(root.rglob("*")):
        relative = source.relative_to(root)
        if _excluded_from_snapshot(relative):
            continue
        try:
            info = source.lstat()
        except OSError as exc:
            raise BackupError(f"backup source could not be inspected: {source}") from exc
        if _is_alias(source, info):
            raise BackupError(f"backup source contains an unsupported alias: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError(f"backup source is not a regular file: {relative}")
        file_lock.ensure_private_file(source)
        rel = _safe_relative_path(relative.as_posix(), label="backup source path").as_posix()
        destination = stage / Path(*PurePosixPath(rel).parts)
        if source.suffix == ".db":
            digest, size = _consistent_db_copy(source, destination)
        else:
            digest, size = _copy_verified_file(source, destination)
        count += 1
        total += size
        if count > MAX_ARCHIVE_MEMBERS or total > MAX_ARCHIVE_EXPANDED_BYTES:
            raise BackupError("live data exceeds configured backup archive limits")
        files[rel] = {"sha256": digest, "size": size}
    directory_count = sum(1 for path in stage.rglob("*") if path.is_dir())
    if count + directory_count + 2 > MAX_ARCHIVE_MEMBERS:
        raise BackupError("staged backup exceeds the archive member-count limit")
    return files


def _unique_private_empty(parent: Path, prefix: str) -> Path:
    for _ in range(100):
        candidate = parent / f".{prefix}-{secrets.token_hex(16)}.tmp"
        try:
            file_lock.atomic_create_bytes(candidate, b"")
            return candidate
        except FileExistsError:
            continue
    raise BackupError("could not allocate a unique private staging path")


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def create_backup(
    out: str | Path | None = None,
) -> Path:
    """Create one private, encrypted and atomically-published backup archive.

    Creation requires independent signing and encryption keys. Existing
    destination paths are refused rather than overwritten. There is no API or
    CLI switch for creating a new plaintext/unsigned backup.
    """
    root = _client_root()
    if not root.exists():
        raise BackupError(f"no data root to back up at {root}")
    file_lock.ensure_private_directory(root)
    signing_key = _operator_key(required=True)
    encryption_key = _encryption_key(required=True)
    assert signing_key is not None and encryption_key is not None
    _require_independent_keys(signing_key, encryption_key)
    client_identifier = _client_id() or "unbound"
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    default_destination = out is None
    if default_destination:
        out = data_backups_dir() / (
            f"maverick-{client_identifier}-{timestamp}-{secrets.token_hex(4)}.mvkb"
        )
    destination = Path(out).expanduser()
    try:
        if default_destination:
            file_lock.ensure_private_directory(destination.parent)
        else:
            # A caller-selected parent may contain unrelated files. Verify its
            # ACL instead of silently revoking collaborators' access to it.
            file_lock.prepare_private_directory(destination.parent)
    except OSError as exc:
        raise BackupError("backup destination directory must be private") from exc
    if os.path.lexists(destination):
        raise BackupError(f"refusing existing backup destination: {destination}")

    with tempfile.TemporaryDirectory(prefix="mvk-backup-") as temporary:
        temp_root = file_lock.ensure_private_directory(temporary)
        stage = file_lock.ensure_private_directory(temp_root / "data")
        schema_version = _world_schema_version(root)
        files = _stage(root, stage)
        if not files:
            raise BackupError(f"no data files to back up at {root}")
        manifest = _authenticate_for_write({
            "schema": SCHEMA,
            "client_id": _client_id(),
            "created_at": time.time(),
            "world_schema_version": schema_version,
            "files": files,
        }, signing_key)
        manifest_path = temp_root / MANIFEST
        file_lock.atomic_create_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
        )

        plaintext_archive = temp_root / "archive.tgz"
        _new_private_empty(plaintext_archive)
        plaintext_fd, _ = _open_private_writer(plaintext_archive)
        with os.fdopen(plaintext_fd, "wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                archive.add(manifest_path, arcname=MANIFEST, recursive=False)
                archive.add(stage, arcname="data")
            output.flush()
            os.fsync(output.fileno())

        # Validate the signed plaintext while it is still confined to the
        # process-private temporary tree.
        with _verified_archive(plaintext_archive) as stream:
            _read_manifest_stream(stream, allow_unsigned=False)

        staged_archive = _unique_private_empty(destination.parent, destination.name)
        try:
            _encrypt_archive_file(
                plaintext_archive,
                staged_archive,
                key=encryption_key,
                destination_precreated=True,
            )

            # Validate the generated encrypted artifact through the same
            # decrypt/authenticate path used by restore before publication.
            with _plaintext_archive(
                staged_archive,
                allow_legacy_auth_off=False,
            ) as (stream, legacy, verified_encryption_key):
                verified_manifest, verified_signing_key = _read_manifest_stream(
                    stream,
                    allow_unsigned=legacy,
                )
                _manifest_files(verified_manifest, allow_legacy=False)
                assert verified_signing_key is not None
                assert verified_encryption_key is not None
                _require_independent_keys(
                    verified_signing_key,
                    verified_encryption_key,
                )
            if os.path.lexists(destination):
                raise BackupError(f"backup destination was planted: {destination}")
            os.replace(staged_archive, destination)
            _fsync_directory(destination.parent)
            file_lock.ensure_private_file(destination)
        except BaseException:
            staged_archive.unlink(missing_ok=True)
            raise

    log.info(
        "encrypted backup written: %s (%d files, client=%s)",
        destination,
        len(files),
        client_identifier,
    )
    return destination


def _extract_payload_stream(
    stream: BinaryIO,
    stage: Path,
    *,
    expected: dict[str, tuple[str, int | None]],
) -> None:
    budget = _ArchiveBudget()
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                rel = budget.observe(member)
                if rel is None or member.isdir():
                    continue
                record = expected.get(rel)
                if record is None:
                    raise BackupError(
                        f"backup payload {rel} is not in the manifest "
                        "(backup is corrupt or tampered)"
                    )
                if rel in seen:
                    raise BackupError(f"duplicate backup payload path: {rel}")
                seen.add(rel)
                expected_digest, expected_size = record
                if expected_size is not None and member.size != expected_size:
                    raise BackupError(
                        f"backup integrity check failed for {rel} (size mismatch)"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError(f"backup payload is unreadable: {rel}")
                destination = stage / Path(*PurePosixPath(rel).parts)
                digest, size = _stream_to_new_private(
                    extracted,
                    destination,
                    expected_size=member.size,
                )
                if digest != expected_digest or (
                    expected_size is not None and size != expected_size
                ):
                    raise BackupError(
                        f"backup integrity check failed for {rel} "
                        "(SHA-256 mismatch — backup is corrupt or truncated)"
                    )
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"unreadable backup payload: {exc}") from exc
    missing = set(expected) - seen
    if missing:
        sample = sorted(missing)[0]
        raise BackupError(f"manifest payload is missing from archive: {sample}")


def _live_path(root: Path, rel: str) -> Path:
    safe = _safe_relative_path(rel, label="restore path")
    current = root
    for component in safe.parts[:-1]:
        current = current / component
        if os.path.lexists(current):
            info = current.lstat()
            if _is_alias(current, info) or not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"unsafe restore parent: {current}")
        file_lock.ensure_private_directory(current)
    return root / Path(*safe.parts)


def _validate_optional_live_file(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    info = path.lstat()
    if _is_alias(path, info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError(f"refusing planted or aliased live path: {path}")
    file_lock.ensure_private_file(path)
    return True


def _journal_document(body: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    return _authenticate_for_write(body, key)


def _write_journal(tx: Path, body: dict[str, Any], key: bytes | None) -> None:
    document = _journal_document(body, key)
    file_lock.atomic_write_text(
        tx / _JOURNAL,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )


def _load_journal(
    tx: Path, *, allow_unsigned: bool,
) -> tuple[dict[str, Any], bytes | None]:
    path = tx / _JOURNAL
    try:
        file_lock.ensure_private_file(path)
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise BackupError("restore journal is oversized")
        document = _parse_manifest(file_lock.atomic_read_bytes(path))
    except OSError as exc:
        raise BackupError(f"restore journal could not be read: {tx}") from exc
    key = _verify_authentication(
        document,
        allow_unsigned=allow_unsigned,
        label="restore journal",
    )
    return document, key


def _remove_private_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if _is_alias(path, info):
        path.unlink()
        return
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove_private_tree(child)
        path.rmdir()
    else:
        path.unlink()


def _capture_original(
    root: Path,
    tx: Path,
    rel: str,
) -> dict[str, Any]:
    destination = _live_path(root, rel)
    existed = _validate_optional_live_file(destination)
    if existed:
        original = tx / "originals" / Path(*PurePosixPath(rel).parts)
        _copy_verified_file(destination, original)
    sidecars: list[str] = []
    if destination.suffix == ".db":
        for suffix in _DB_SIDECARS:
            sidecar = Path(str(destination) + suffix)
            if _validate_optional_live_file(sidecar):
                side_rel = rel + suffix
                original_sidecar = tx / "originals" / Path(*PurePosixPath(side_rel).parts)
                _copy_verified_file(sidecar, original_sidecar)
                sidecars.append(suffix)
    return {"rel": rel, "existed": existed, "sidecars": sidecars}


def _publish_staged_file(source: Path, destination: Path) -> None:
    file_lock.ensure_private_directory(destination.parent)
    if os.path.lexists(destination):
        _validate_optional_live_file(destination)
    temporary = _unique_private_empty(destination.parent, destination.name + ".restore")
    try:
        _copy_verified_file(
            source,
            temporary,
            destination_precreated=True,
        )
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        file_lock.ensure_private_file(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_journal_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("journal_schema") != 1:
        raise BackupError("restore journal schema is unsupported")
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ARCHIVE_MEMBERS:
        raise BackupError("restore journal entries are malformed")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("restore journal entry is malformed")
        rel = _safe_relative_path(entry.get("rel"), label="journal path").as_posix()
        if rel in seen or not isinstance(entry.get("existed"), bool):
            raise BackupError("restore journal contains duplicate/malformed state")
        seen.add(rel)
        sidecars = entry.get("sidecars")
        if (
            not isinstance(sidecars, list)
            or len(sidecars) > len(_DB_SIDECARS)
            or any(s not in _DB_SIDECARS for s in sidecars)
            or len(set(sidecars)) != len(sidecars)
        ):
            raise BackupError("restore journal sidecar state is malformed")
        validated.append({"rel": rel, "existed": entry["existed"], "sidecars": sidecars})
    return validated


def _rollback(root: Path, tx: Path, document: dict[str, Any]) -> None:
    entries = _validate_journal_entries(document)
    for entry in reversed(entries):
        rel = entry["rel"]
        destination = _live_path(root, rel)
        if destination.suffix == ".db":
            for suffix in _DB_SIDECARS:
                sidecar = Path(str(destination) + suffix)
                if os.path.lexists(sidecar):
                    _validate_optional_live_file(sidecar)
                    sidecar.unlink()
        if entry["existed"]:
            original = tx / "originals" / Path(*PurePosixPath(rel).parts)
            _validate_optional_live_file(original)
            _publish_staged_file(original, destination)
        elif os.path.lexists(destination):
            _validate_optional_live_file(destination)
            destination.unlink()
        for suffix in entry["sidecars"]:
            side_rel = rel + suffix
            original_sidecar = tx / "originals" / Path(*PurePosixPath(side_rel).parts)
            _validate_optional_live_file(original_sidecar)
            _publish_staged_file(
                original_sidecar,
                Path(str(destination) + suffix),
            )


def _recover_transactions(root: Path, *, allow_unsigned: bool) -> None:
    tx_root = root / _RESTORE_TX_ROOT
    if not os.path.lexists(tx_root):
        return
    file_lock.ensure_private_directory(tx_root)
    transactions = sorted(tx_root.iterdir())
    if len(transactions) > 32:
        raise BackupError("too many incomplete restore transactions")
    for tx in transactions:
        info = tx.lstat()
        if _is_alias(tx, info) or not stat.S_ISDIR(info.st_mode):
            raise BackupError(f"invalid restore transaction path: {tx}")
        file_lock.ensure_private_directory(tx)
        journal = tx / _JOURNAL
        if not journal.exists():
            _remove_private_tree(tx)
            continue
        document, _ = _load_journal(tx, allow_unsigned=allow_unsigned)
        state = document.get("state")
        if state in {"prepared", "applying"}:
            _rollback(root, tx, document)
        elif state not in {"preparing", "committed"}:
            raise BackupError(f"unknown restore journal state: {state!r}")
        _remove_private_tree(tx)
    if tx_root.exists() and not any(tx_root.iterdir()):
        tx_root.rmdir()


def _apply_transaction(
    root: Path,
    stage: Path,
    expected: dict[str, tuple[str, int | None]],
    *,
    key: bytes | None,
) -> None:
    tx_root = file_lock.ensure_private_directory(root / _RESTORE_TX_ROOT)
    tx = file_lock.ensure_private_directory(tx_root / secrets.token_hex(16))
    body: dict[str, Any] = {
        "journal_schema": 1,
        "state": "preparing",
        "entries": [],
    }
    _write_journal(tx, body, key)
    committed = False
    try:
        entries = [_capture_original(root, tx, rel) for rel in sorted(expected)]
        body["entries"] = entries
        body["state"] = "prepared"
        _write_journal(tx, body, key)
        body["state"] = "applying"
        _write_journal(tx, body, key)
        for entry in entries:
            rel = entry["rel"]
            source = stage / Path(*PurePosixPath(rel).parts)
            destination = _live_path(root, rel)
            if destination.suffix == ".db":
                for suffix in _DB_SIDECARS:
                    sidecar = Path(str(destination) + suffix)
                    if os.path.lexists(sidecar):
                        _validate_optional_live_file(sidecar)
                        sidecar.unlink()
            _publish_staged_file(source, destination)
        committed_body = dict(body)
        committed_body["state"] = "committed"
        _write_journal(tx, committed_body, key)
        body = committed_body
        committed = True
    except BaseException as original:
        try:
            if body.get("state") == "preparing":
                _remove_private_tree(tx)
            elif not committed and body.get("state") in {"prepared", "applying", "committed"}:
                _rollback(root, tx, body)
                _remove_private_tree(tx)
        except BaseException as rollback_error:
            raise BackupError(
                f"restore failed and rollback is incomplete; journal retained at {tx}: "
                f"{rollback_error}"
            ) from original
        raise
    else:
        _remove_private_tree(tx)
    finally:
        if tx_root.exists() and not any(tx_root.iterdir()):
            tx_root.rmdir()


def restore_backup(
    tarball: str | Path,
    *,
    force: bool = False,
    allow_legacy_auth_off: bool = False,
) -> Path:
    """Decrypt, authenticate, verify and transactionally restore one backup.

    ``force`` does not bypass encryption or signature verification.
    ``allow_legacy_auth_off`` is a migration-only API path for old unencrypted
    archives; it is deliberately not exposed by the operator CLI.
    """
    with _plaintext_archive(
        tarball,
        allow_legacy_auth_off=allow_legacy_auth_off,
    ) as (stream, legacy, encryption_key):
        manifest, signing_key = _read_manifest_stream(
            stream,
            allow_unsigned=legacy,
        )
        if signing_key is not None and encryption_key is not None:
            _require_independent_keys(signing_key, encryption_key)
        backup_client = manifest.get("client_id")
        live_client = _client_id()
        if not force and backup_client != live_client:
            raise BackupError(
                f"backup is for client {backup_client!r} but this deployment is "
                f"{live_client!r}; refusing (pass force=True to override)"
            )
        from .world_model import SCHEMA_VERSION

        backup_schema = manifest.get("world_schema_version")
        if (
            not force
            and isinstance(backup_schema, int)
            and backup_schema > SCHEMA_VERSION
        ):
            raise BackupError(
                f"backup world schema v{backup_schema} is newer than this "
                f"binary's v{SCHEMA_VERSION}; upgrade Maverick first, or pass "
                "force=True to override"
            )
        expected = _manifest_files(
            manifest,
            allow_legacy=legacy,
        )
        root = _client_root()
        file_lock.ensure_private_directory(root)
        with tempfile.TemporaryDirectory(prefix="mvk-restore-") as temporary:
            stage = file_lock.ensure_private_directory(Path(temporary) / "data")
            stream.seek(0)
            _extract_payload_stream(stream, stage, expected=expected)
            lock_target = root / _RESTORE_LOCK_TARGET
            with file_lock.cross_process_lock(lock_target, strict=True):
                _recover_transactions(root, allow_unsigned=legacy)
                _apply_transaction(
                    root,
                    stage,
                    expected,
                    key=signing_key,
                )
    log.info(
        "restore complete into %s (from client=%s, %d files verified, encrypted=%s)",
        root,
        backup_client,
        len(expected),
        not legacy,
    )
    return root


__all__ = [
    "BackupError",
    "create_backup",
    "restore_backup",
    "read_manifest",
    "data_backups_dir",
]
