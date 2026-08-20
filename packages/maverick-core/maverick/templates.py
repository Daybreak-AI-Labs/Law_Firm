"""Operator-authored local goal templates with variable substitution.

A template is a markdown file with optional YAML frontmatter that
captures a reusable goal pattern. Variables like ``{{ topic }}`` are
substituted from ``--param key=value`` on the CLI (or programmatically
from a dict).

Templates are loaded only from the active tenant's private
``~/.maverick/templates`` directory.  The firm wheel contains no bundled goal
catalog: every template is a reviewed local artifact.

File format::

    ---
    title: Review the {{ agreement_type }} agreement
    budget_dollars: 2.0
    budget_wall_seconds: 1200
    params:
      - agreement_type
    ---
    Review the {{ agreement_type }} against the matter playbook and draft a
    deviation memo for attorney review.

The title can also contain ``{{ vars }}``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .paths import data_dir

# ``data_dir`` is tenant-sensitive.  Keep the historical constant as an
# explicit test/embedding override, but never use its import-time value as the
# production runtime path: a long-lived dashboard serves many tenant contexts
# in one process.
_IMPORT_USER_TEMPLATES = data_dir("templates")
USER_TEMPLATES = _IMPORT_USER_TEMPLATES

def user_templates_dir() -> Path:
    """Return the active tenant's user-template directory.

    ``USER_TEMPLATES`` remains a supported explicit override for tests and
    embedders.  When it still has its original import-time object, resolve the
    path again so the current tenant and ``MAVERICK_HOME`` are honored.
    """
    if USER_TEMPLATES is not _IMPORT_USER_TEMPLATES:
        return Path(USER_TEMPLATES)
    return data_dir("templates")


@dataclass
class Template:
    name: str
    title: str
    body: str
    budget_dollars: float = 5.0
    budget_wall_seconds: float = 3600.0
    params: list[str] = field(default_factory=list)
    path: Path | None = None
    owner: str = ""
    generation: int = 0

    @classmethod
    def parse(cls, text: str, name: str, path: Path | None = None) -> Template:
        """Parse a template file. YAML frontmatter is optional."""
        # Normalize CRLF/CR so the LF-anchored frontmatter regex matches files
        # authored on Windows or served over HTTP with CRLF endings -- otherwise
        # their frontmatter (title/params AND budgets) is silently ignored.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if m:
            front, body = m.group(1), m.group(2)
            meta = _parse_frontmatter(front)
        else:
            meta, body = {}, text

        def _num(key: str, default: float) -> float:
            # A user-authored template with a non-numeric budget used to raise a
            # raw ValueError traceback out of float() (user-testing finding);
            # give a clear, catchable message the CLI can surface instead.
            raw = meta.get(key, default)
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"template {name!r}: [{key}] must be a number, got {raw!r}"
                ) from None

        try:
            generation = int(meta.get("generation", 0))
        except (TypeError, ValueError):
            raise ValueError(
                f"template {name!r}: [generation] must be an integer"
            ) from None
        if generation < 0:
            raise ValueError(f"template {name!r}: [generation] cannot be negative")

        return cls(
            name=name,
            title=str(meta.get("title", name)),
            body=body.strip(),
            budget_dollars=_num("budget_dollars", 5.0),
            budget_wall_seconds=_num("budget_wall_seconds", 3600),
            params=meta.get("params", []) if isinstance(meta.get("params"), list) else [],
            path=path,
            owner=meta.get("owner") if isinstance(meta.get("owner"), str) else "",
            generation=generation,
        )

    def render(self, **params: str) -> tuple[str, str]:
        """Return (title, body) with variables substituted.

        Missing required params raise ValueError.
        """
        missing = [p for p in self.params if p not in params]
        if missing:
            raise ValueError(
                f"template {self.name!r} missing required params: {missing}"
            )
        return (
            _substitute(self.title, params),
            _substitute(self.body, params),
        )


def _parse_frontmatter(front: str) -> dict:
    meta: dict = {}
    current_key = None
    for line in front.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("  - ") and current_key:
            meta.setdefault(current_key, []).append(line[4:].strip())
        elif ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            current_key = k
            if v:
                # Try numeric coercion for budget fields. The pattern must
                # match a real number -- the old `^[\d.]+$` accepted things
                # like "1.2.3" / "." / "5." that float() then choked on,
                # raising an uncaught ValueError out of template parse.
                if k.startswith("budget_") and re.match(r"^\d+(\.\d+)?$", v):
                    meta[k] = float(v)
                else:
                    meta[k] = v
            else:
                meta[k] = []
    return meta


_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _substitute(text: str, params: dict[str, str]) -> str:
    return _VAR.sub(lambda m: str(params.get(m.group(1), m.group(0))), text)


def _candidate_dirs() -> list[Path]:
    # De-dup by resolved path: the two bundled candidates (package-relative
    # and cwd-relative) point at the same directory when run from a repo
    # checkout, which otherwise lists it twice -- including in the
    # "not found. Searched: [...]" error.
    out: list[Path] = []
    seen: set[Path] = set()
    for d in [user_templates_dir()]:
        rd = d.resolve()
        if rd in seen:
            continue
        seen.add(rd)
        out.append(d)
    return out


def list_templates() -> list[str]:
    """Return the active tenant's reviewed local template names."""
    seen: set[str] = set()
    out: list[str] = []
    for d in _candidate_dirs():
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            if p.stem == "README":
                continue
            if p.stem in seen:
                continue
            seen.add(p.stem)
            out.append(p.stem)
    return out


def _validate_template_name(name: str) -> None:
    """Only allow safe template IDs such as ``nda-review``."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", name):
        raise ValueError(
            f"invalid template name {name!r}; use only letters, numbers, _ and -"
        )


def load_template(name: str) -> Template:
    """Find ``name.md`` in candidate dirs and parse it."""
    _validate_template_name(name)
    for d in _candidate_dirs():
        p = d / f"{name}.md"
        if p.exists():
            return Template.parse(p.read_text(encoding="utf-8"), name, path=p)
    raise FileNotFoundError(
        f"template {name!r} not found. Searched: {[str(d) for d in _candidate_dirs()]}"
    )


def save_user_template(
    name: str,
    *,
    title: str,
    body: str,
    params: list[str] | None = None,
    budget_dollars: float = 5.0,
    budget_wall_seconds: float = 3600.0,
    overwrite: bool = True,
    owner: str | None = None,
    expected_generation: int | None = None,
    admin_override: bool = False,
) -> Template:
    """Persist a user-authored (or AI-drafted) workflow as a user template.

    Writes ``~/.maverick/templates/<name>.md`` with frontmatter that
    :meth:`Template.parse` round-trips, then returns the parsed Template. This
    is the local write path behind the dashboard's "save workflow" for content
    the operator authored or drafted from their own brief.

    Validates the name, requires a non-empty body, collapses the title to a
    single line (frontmatter is line-oriented), and keeps only identifier-like
    param names. Refuses to clobber an existing file when ``overwrite`` is
    False.
    """
    _validate_template_name(name)
    owner = str(owner or "local").strip()
    if (
        not owner
        or len(owner) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in owner)
    ):
        raise ValueError("template owner must be a bounded printable principal")
    if expected_generation is not None:
        try:
            expected_generation = int(expected_generation)
        except (TypeError, ValueError):
            raise ValueError("expected_generation must be an integer") from None
        if expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")
    title = " ".join((title or name).split()) or name
    body = (body or "").strip()
    if not body:
        raise ValueError("template body must not be empty")
    clean_params: list[str] = []
    for p in (str(p).strip() for p in (params or [])):
        if re.match(r"^[A-Za-z_]\w*$", p) and p not in clean_params:
            clean_params.append(p)
    # Reconcile declared params with the {{placeholders}} the title/body actually
    # use: an undeclared placeholder would render as a literal "{{x}}" in the
    # goal, and the scheduler/trigger UI (which prompts from tpl.params) would
    # never ask the operator to fill it. Fold any used-but-undeclared placeholder
    # into params so every slot is fillable.
    for m in _VAR.finditer(f"{title}\n{body}"):
        placeholder = m.group(1)
        if placeholder not in clean_params:
            clean_params.append(placeholder)
    try:
        bd = float(budget_dollars)
        bw = float(budget_wall_seconds)
    except (TypeError, ValueError):
        raise ValueError("budget_dollars and budget_wall_seconds must be numbers") from None
    # Clamp to the same bounds the REST layer enforces (WorkflowSaveIn / GoalIn):
    # the drafting prompt only suggests 0.5-20, but a hand-edited value must still
    # land in a sane, bounded per-run range rather than 0 (never runs) or an
    # arbitrarily large spend.
    bd = min(max(bd, 0.5), 100.0)
    bw = min(max(bw, 1.0), 86400.0)

    dest = user_templates_dir() / f"{name}.md"
    from .file_lock import (
        atomic_write_text,
        cross_process_lock,
        ensure_private_directory,
    )

    # Make the existence check and publication one serialized operation.
    # ``atomic_write_text`` also avoids following a planted destination symlink
    # and publishes a private, whole-file snapshot to concurrent readers.
    ensure_private_directory(dest.parent)
    with cross_process_lock(dest, strict=True):
        if dest.exists() and not overwrite:
            raise FileExistsError(f"template {name!r} already exists")
        generation = 1
        if dest.exists():
            from .file_lock import atomic_read_text, ensure_private_file

            ensure_private_file(dest)
            existing = Template.parse(
                atomic_read_text(dest, encoding="utf-8"), name, path=dest,
            )
            if not admin_override:
                if existing.owner and existing.owner != owner:
                    raise PermissionError(
                        f"template {name!r} is owned by another principal"
                    )
                if not existing.owner and owner != "local":
                    raise PermissionError(
                        f"legacy template {name!r} requires an administrator migration"
                    )
            if (
                expected_generation is not None
                and existing.generation != expected_generation
            ):
                raise FileExistsError(
                    f"template {name!r} changed since generation "
                    f"{expected_generation}; current generation is {existing.generation}"
                )
            generation = existing.generation + 1
        elif expected_generation not in (None, 0):
            raise FileExistsError(
                f"template {name!r} does not exist at generation "
                f"{expected_generation}"
            )
        front = [
            f"title: {title}",
            f"budget_dollars: {bd}",
            f"budget_wall_seconds: {bw}",
            f"owner: {owner}",
            f"generation: {generation}",
        ]
        if clean_params:
            front.append("params:")
            front.extend(f"  - {p}" for p in clean_params)
        content = "---\n" + "\n".join(front) + "\n---\n" + body + "\n"
        atomic_write_text(dest, content)
    return Template.parse(content, name, path=dest)
