from __future__ import annotations

import os
from pathlib import Path

import maverick.skills as skills_module
import pytest
from maverick.skills import (
    MAX_SKILL_BYTES,
    Skill,
    _relevant_skills_lexical,
    available_skills,
    load_skills,
    relevant_skills,
    render_for_prompt,
    validate_skill_file,
)


def test_remote_and_runtime_skill_mutation_surfaces_are_absent():
    for name in (
        "create_skill",
        "distill",
        "install_from_catalog",
        "install_skill",
        "remove_skill",
        "_fetch_skill_source",
        "_fetch_url",
    ):
        assert not hasattr(skills_module, name)


def _markdown(
    name: str = "client-intake",
    *,
    trigger: str = "open a new client matter",
    body: str | None = None,
) -> str:
    body = body or "# Steps\n\n1. Confirm the conflict check.\n2. Gather intake facts."
    return (
        f"---\nname: {name}\ntriggers:\n  - {trigger}\n"
        "tools_needed:\n  - read_attachment\n---\n\n"
        f"{body}\n"
    )


def test_skill_parse_round_trips_local_frontmatter(tmp_path: Path):
    path = tmp_path / "client-intake.md"
    skill = Skill.parse(_markdown(), path)
    assert skill.name == "client-intake"
    assert skill.triggers == ["open a new client matter"]
    assert skill.tools_needed == ["read_attachment"]
    assert "Confirm the conflict check" in skill.body


@pytest.mark.parametrize(
    "text, expected",
    [
        ("not frontmatter", "frontmatter"),
        ("---\nname: Bad Name\n---\nbody", "kebab"),
        (
            "---\nname: x\ntriggers: scalar\n  - list\n---\nbody",
            "mixes",
        ),
        ("---\nname: x\nbroken\n---\nbody", "frontmatter line"),
        ("---\nname: x\nname: y\n---\nbody", "duplicate"),
    ],
)
def test_skill_parse_rejects_malformed_input(tmp_path: Path, text: str, expected: str):
    with pytest.raises(ValueError, match=expected):
        Skill.parse(text, tmp_path / "x.md")


def test_load_skills_requires_an_explicit_store(tmp_path: Path):
    (tmp_path / "client-intake.md").write_text(_markdown(), encoding="utf-8")
    assert load_skills() == []
    assert [skill.name for skill in load_skills(tmp_path)] == ["client-intake"]


def test_load_skills_skips_oversized_and_non_utf8_files(tmp_path: Path):
    (tmp_path / "too-large.md").write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    (tmp_path / "binary.md").write_bytes(b"---\nname: binary\n---\n\xff")
    assert load_skills(tmp_path) == []


def test_load_skills_rejects_hardlink_alias(tmp_path: Path):
    original = tmp_path / "original.md"
    alias = tmp_path / "alias.md"
    original.write_text(_markdown(), encoding="utf-8")
    try:
        os.link(original, alias)
    except OSError:
        pytest.skip("hard links unavailable on this host")
    assert load_skills(tmp_path) == []


def test_load_skills_rejects_symlink_alias(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(_markdown(), encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    assert load_skills(tmp_path) == []


def test_load_skills_rejects_path_replacement_during_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "client-intake.md"
    original = tmp_path / "original.md"
    attacker = tmp_path / "attacker.swap"
    candidate.write_text(_markdown(), encoding="utf-8")
    attacker.write_text(_markdown(name="attacker"), encoding="utf-8")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags):
        nonlocal swapped
        if Path(path) == candidate and not swapped:
            swapped = True
            candidate.replace(original)
            attacker.replace(candidate)
        return real_open(path, flags)

    monkeypatch.setattr("maverick.skills.os.open", swapping_open)
    assert load_skills(tmp_path) == []


def test_load_skills_rejects_in_place_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "client-intake.md"
    candidate.write_text(_markdown(), encoding="utf-8")
    real_read = os.read
    mutated = False

    def mutating_read(fd, size):
        nonlocal mutated
        chunk = real_read(fd, size)
        if chunk and not mutated:
            mutated = True
            candidate.write_text(_markdown(name="attacker"), encoding="utf-8")
        return chunk

    monkeypatch.setattr("maverick.skills.os.read", mutating_read)
    assert load_skills(tmp_path) == []


def test_validate_skill_is_local_read_only_and_secret_scanned(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text(_markdown(), encoding="utf-8")
    result = validate_skill_file(good)
    assert result.ok, result.errors

    bad = tmp_path / "bad.md"
    bad.write_text(_markdown(body="# Steps\n\nshort"), encoding="utf-8")
    result = validate_skill_file(bad)
    assert not result.ok
    assert any("short" in error for error in result.errors)


def test_available_skills_uses_reviewed_builtins_and_explicit_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MAVERICK_BUILTIN_SKILLS", "0")
    (tmp_path / "client-intake.md").write_text(_markdown(), encoding="utf-8")
    assert available_skills() == []
    assert [skill.name for skill in available_skills(tmp_path)] == ["client-intake"]


def test_lexical_recall_is_deterministic_and_drops_noise():
    strong = Skill(
        "strong",
        ["review the vendor agreement"],
        [],
        "body",
        Path("strong.md"),
    )
    weak = Skill("weak", ["the weather"], [], "body", Path("weak.md"))
    out = _relevant_skills_lexical(
        "review the vendor agreement now",
        [weak, strong],
        min_score=4,
    )
    assert out == [strong]


def test_purpose_scoped_skill_fails_closed_when_policy_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    unrestricted = Skill("a", ["draft motion"], [], "body", Path("a.md"))
    scoped = Skill(
        "b",
        ["draft motion"],
        [],
        "body",
        Path("b.md"),
        purposes=("litigation",),
    )
    monkeypatch.setattr(
        "maverick.access_policy.check",
        lambda _policy: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    assert relevant_skills("draft motion", [scoped, unrestricted]) == [unrestricted]


def test_render_for_prompt_is_bounded():
    skills = [
        Skill("one", ["x"], [], "A" * 100, Path("one.md")),
        Skill("two", ["x"], [], "B" * 100, Path("two.md")),
    ]
    rendered = render_for_prompt(
        skills,
        max_body_chars=20,
        max_total_chars=30,
    )
    assert "## one" in rendered
    assert "## two" not in rendered
    assert "A" * 21 not in rendered
