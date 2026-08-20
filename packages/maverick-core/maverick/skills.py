"""Local, read-only skill loading and retrieval for the firm runtime.

The law-firm build has no skill marketplace, remote installer, runtime
capability acquisition, generated tools, or tenant-global outcome ranking.
It loads the reviewed skills shipped in the wheel and, only when a caller
supplies an exact directory, matter-scoped locally distilled skills.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

BUILTIN_SKILLS_DIR = Path(__file__).parent / "skills_builtin"
MAX_SKILL_BYTES = 256 * 1024
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Skill:
    name: str
    triggers: list[str]
    tools_needed: list[str]
    body: str
    path: Path
    purposes: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str, path: Path) -> Skill:
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError("skill exceeds the local size limit")
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if not match:
            raise ValueError("missing YAML frontmatter")
        front, body = match.group(1), match.group(2)
        meta: dict[str, object] = {}
        current_key: str | None = None
        for raw in front.splitlines():
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("  - ") and current_key:
                bucket = meta.setdefault(current_key, [])
                if not isinstance(bucket, list):
                    raise ValueError(
                        f"malformed skill frontmatter: {current_key!r} mixes "
                        "a scalar and list"
                    )
                item = " ".join(line[4:].split())
                if item:
                    bucket.append(item)
                continue
            if ":" not in line:
                raise ValueError("malformed skill frontmatter line")
            key, _, value = line.partition(":")
            key = key.strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError("malformed skill frontmatter key")
            if key in meta:
                raise ValueError(f"duplicate skill frontmatter key: {key}")
            current_key = key
            value = " ".join(value.split())
            meta[key] = value or []

        name = meta.get("name", path.stem)
        if not isinstance(name, str) or not _KEBAB_RE.fullmatch(name):
            raise ValueError("skill name must be bounded kebab-case")
        if len(name) > 96:
            raise ValueError("skill name exceeds 96 characters")
        triggers = meta.get("triggers", [])
        tools = meta.get("tools_needed", [])
        purposes = meta.get("purposes", [])
        if not isinstance(triggers, list) or not isinstance(tools, list):
            raise ValueError("skill triggers and tools_needed must be lists")
        if not isinstance(purposes, list):
            raise ValueError("skill purposes must be a list")
        if any(len(value) > 256 for value in (*triggers, *tools, *purposes)):
            raise ValueError("skill frontmatter item exceeds 256 characters")
        return cls(
            name=name,
            triggers=list(triggers),
            tools_needed=list(tools),
            body=body.strip(),
            path=path,
            purposes=tuple(purposes),
        )


@dataclass
class SkillValidation:
    ok: bool
    errors: list[str]
    warnings: list[str]


def _read_regular_file(path: Path, root: Path) -> str:
    """Read one bounded, single-link file without following aliases."""
    root = root.resolve()
    try:
        visible = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"skill file not found: {path}") from exc
    except OSError as exc:
        raise ValueError(f"skill file cannot be inspected: {path}") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(visible.st_mode)
        or bool(getattr(visible, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(visible.st_mode)
        or visible.st_nlink != 1
        or not visible.st_dev
        or not visible.st_ino
        or visible.st_size > MAX_SKILL_BYTES
    ):
        raise ValueError(f"skill file is not a bounded regular file: {path}")
    try:
        if path.resolve().parent != root:
            raise ValueError(f"skill file escapes its store: {path}")
    except OSError as exc:
        raise ValueError(f"skill file cannot be resolved: {path}") from exc

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not opened.st_dev
            or not opened.st_ino
            or (
                visible.st_dev,
                visible.st_ino,
                visible.st_size,
                visible.st_mtime_ns,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            )
        ):
            raise ValueError(f"skill file identity changed: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65_536, MAX_SKILL_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SKILL_BYTES:
                raise ValueError(f"skill file exceeds {MAX_SKILL_BYTES} bytes")
        after = os.fstat(fd)
        current = path.lstat()
        if (
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            )
            or visible.st_ctime_ns != current.st_ctime_ns
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise ValueError(f"skill file changed while being read: {path}")
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot read skill file {path}: invalid UTF-8") from exc
    # os.read() does not perform the universal-newline translation that the
    # former Path.read_text() implementation provided.  Canonicalize line
    # endings before parsing so reviewed wheels and Windows checkouts have the
    # exact same frontmatter semantics.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_skill_file(path: Path) -> SkillValidation:
    errors: list[str] = []
    warnings: list[str] = []
    candidate = Path(path)
    try:
        text = _read_regular_file(candidate, candidate.parent)
        skill = Skill.parse(text, candidate)
    except (OSError, ValueError) as exc:
        return SkillValidation(False, [str(exc)], [])

    front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not front or not re.search(r"(?m)^name:[ \t]*\S", front.group(1)):
        errors.append("missing 'name:' in frontmatter")
    if not skill.triggers:
        errors.append("at least one trigger is required")
    if not skill.tools_needed:
        warnings.append("no tools_needed entries declared")
    if len(skill.body) < 40:
        errors.append("body is too short to be useful")
    elif "#" not in skill.body:
        warnings.append("body has no section headings")
    try:
        from .safety.secret_detector import redact

        _safe, matches = redact(text)
        if matches:
            errors.append(f"possible hardcoded secrets: {len(matches)}")
    except Exception:
        errors.append("secret scanning is unavailable")
    return SkillValidation(not errors, errors, warnings)


def builtin_skills_dir() -> Path:
    return BUILTIN_SKILLS_DIR


def load_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Load a caller-selected local store; no implicit global user store."""
    if skills_dir is None:
        return []
    root = Path(skills_dir)
    try:
        info = root.lstat()
    except OSError:
        return []
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(info.st_mode)
    ):
        return []
    loaded: list[Skill] = []
    for path in sorted(root.glob("*.md")):
        try:
            loaded.append(Skill.parse(_read_regular_file(path, root), path))
        except (OSError, ValueError):
            continue
    return loaded


def load_builtin_skills() -> list[Skill]:
    return load_skills(BUILTIN_SKILLS_DIR)


def _builtin_skills_enabled() -> bool:
    env = os.environ.get("MAVERICK_BUILTIN_SKILLS")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    try:
        from .config import get_skills

        return get_skills().get("builtin") is not False
    except Exception:
        return True


def available_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Return reviewed built-ins plus an explicitly supplied local store."""
    by_name: dict[str, Skill] = {}
    if _builtin_skills_enabled():
        by_name.update((skill.name, skill) for skill in load_builtin_skills())
    if skills_dir is not None:
        by_name.update((skill.name, skill) for skill in load_skills(skills_dir))
    return list(by_name.values())


def _purpose_allowed(skills: list[Skill]) -> list[Skill]:
    try:
        from .access_policy import AccessPolicy, check

        return [
            skill
            for skill in skills
            if check(AccessPolicy(purposes=skill.purposes or ())).allowed
        ]
    except Exception:
        # An unrestricted reviewed skill remains usable. A purpose-scoped
        # skill cannot be authorized when policy resolution failed.
        return [skill for skill in skills if not skill.purposes]


def _relevant_skills_lexical(
    goal: str,
    all_skills: list[Skill],
    max_n: int = 3,
    min_score: float = 0.0,
) -> list[Skill]:
    words = set(re.findall(r"\w+", goal.lower()))
    scored: list[tuple[float, Skill]] = []
    for skill in all_skills:
        score = 0.0
        for trigger in skill.triggers:
            trigger_lower = trigger.lower()
            score += 2 * len(set(re.findall(r"\w+", trigger_lower)) & words)
            if trigger_lower in goal.lower():
                score += 5
        if score > 0 and score >= min_score:
            scored.append((score, skill))
    scored.sort(key=lambda row: (-row[0], row[1].name))
    return [skill for _score, skill in scored[: max(0, int(max_n))]]


def relevant_skills(
    goal: str,
    all_skills: list[Skill],
    max_n: int = 3,
) -> list[Skill]:
    """Deterministic local recall; no embedding download or global outcomes."""
    try:
        from .config import get_skills

        minimum = float(get_skills().get("lexical_min_relevance", 0.0))
    except (TypeError, ValueError):
        minimum = 0.0
    return _relevant_skills_lexical(
        goal,
        _purpose_allowed(all_skills),
        max_n=max_n,
        min_score=minimum,
    )


def render_for_prompt(
    skills: list[Skill],
    *,
    max_body_chars: int | None = None,
    max_total_chars: int | None = None,
) -> str:
    if not skills:
        return ""
    from ._envparse import env_int

    if max_body_chars is None:
        max_body_chars = env_int("MAVERICK_SKILL_RENDER_MAX_CHARS", 4_000)
    if max_total_chars is None:
        max_total_chars = env_int("MAVERICK_SKILL_RENDER_TOTAL_CHARS", 10_000)
    parts = ["# Relevant reviewed skills", ""]
    total = 0
    for skill in skills:
        body = skill.body or ""
        if max_body_chars > 0 and len(body) > max_body_chars:
            body = body[:max_body_chars].rstrip() + "\n... [truncated]"
        if max_total_chars > 0 and total and total + len(body) > max_total_chars:
            break
        parts.extend((f"## {skill.name}", body, ""))
        total += len(body)
    return "\n".join(parts)


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "MAX_SKILL_BYTES",
    "Skill",
    "SkillValidation",
    "available_skills",
    "builtin_skills_dir",
    "load_builtin_skills",
    "load_skills",
    "relevant_skills",
    "render_for_prompt",
    "validate_skill_file",
]
