"""Legal-suite operating discipline at specialist spawn."""
from __future__ import annotations

from maverick import domain_discipline, dreaming, reflexion
from maverick.domain import DomainProfile, _department_memory, lint_profile


class TestDiscipline:
    def test_only_legal_suite_has_a_block(self):
        assert set(domain_discipline.SUITE_DISCIPLINE) == {"legal"}

    def test_legal_pack_gets_universal_plus_legal(self):
        block = domain_discipline.discipline_for("legal_settlement")
        assert "Operating discipline:" in block
        assert "privilege" in block.lower()
        assert "client confidentiality" in block.lower()

    def test_generic_pack_gets_universal_only(self):
        block = domain_discipline.discipline_for("generic")
        assert "Operating discipline:" in block
        assert "Legal discipline" not in block

    def test_augment_appends_and_respects_opt_out(self, monkeypatch):
        monkeypatch.delenv("MAVERICK_DOMAIN_DISCIPLINE", raising=False)
        out = domain_discipline.augment_persona("legal_settlement", "You are X.")
        assert out.startswith("You are X.")
        assert "Legal discipline" in out
        monkeypatch.setenv("MAVERICK_DOMAIN_DISCIPLINE", "0")
        assert domain_discipline.augment_persona(
            "legal_settlement", "You are X.",
        ) == "You are X."


class TestSpawnIntegration:
    def _ctx(self, tmp_path):
        from maverick.blackboard import Blackboard
        from maverick.budget import Budget
        from maverick.sandbox import LocalBackend
        from maverick.swarm import SwarmContext
        from maverick.world_model import WorldModel

        world = WorldModel(tmp_path / "world.db")
        return SwarmContext(
            llm=None,
            world=world,
            budget=Budget(max_dollars=1.0),
            blackboard=Blackboard(),
            sandbox=LocalBackend(workdir=tmp_path),
            goal_id=world.create_goal("g", ""),
            use_skills=False,
        )

    def test_spawned_specialist_carries_legal_discipline(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MAVERICK_DOMAIN_DISCIPLINE", raising=False)
        from maverick.domain import agent_from_profile

        profile = DomainProfile(
            name="legal_settlement",
            persona="You prepare settlement analysis for counsel.",
            allow_tools=["read_file"],
            max_risk="low",
        )
        agent = agent_from_profile(profile, self._ctx(tmp_path), "review the draft")
        assert "prepare settlement analysis" in agent.system
        assert "Legal discipline" in agent.system
        assert "Operating discipline" in agent.system

    def test_memory_block_empty_when_loops_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAVERICK_REFLEXION", "0")
        monkeypatch.setenv("MAVERICK_DREAMING", "0")
        monkeypatch.setattr(
            reflexion,
            "recall",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("disabled Reflexion recall was invoked")
            ),
        )
        monkeypatch.setattr(
            dreaming,
            "recall_insights",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("disabled Dreaming recall was invoked")
            ),
        )
        profile = DomainProfile(name="legal_settlement")
        assert _department_memory(profile, "anything") == ""


class TestLintProfile:
    def test_clean_pack_is_clean(self):
        errors, warnings = lint_profile(
            DomainProfile(
                name="legal_settlement",
                description="Settlement analysis for attorney review",
                persona="x" * 250,
                allow_tools=["read_file"],
                deny_tools=["shell", "write_file"],
                max_risk="low",
                knowledge_sources=["matter"],
            )
        )
        assert errors == [] and warnings == []

    def test_empty_allowlist_and_bad_risk_are_errors(self):
        errors, _ = lint_profile(DomainProfile(name="x", max_risk="extreme"))
        assert any("ALL tools" in e for e in errors)
        assert any("extreme" in e for e in errors)
        errors2, _ = lint_profile(DomainProfile(name="x", allow_tools=["a"]))
        assert any("max_risk is unset" in e for e in errors2)

    def test_quality_gaps_are_warnings(self):
        _, warnings = lint_profile(
            DomainProfile(
                name="x",
                persona="short",
                allow_tools=["a", "b"],
                deny_tools=["b"],
                max_risk="low",
            )
        )
        joined = " ".join(warnings)
        assert "both allowed and denied" in joined
        assert "persona under" in joined
        assert "knowledge_sources" in joined

    def test_all_builtin_packs_pass_error_level(self):
        from maverick.domain import builtin_dir, load_domains

        for name, prof in load_domains(builtin_dir()).items():
            errors, _ = lint_profile(prof)
            assert errors == [], f"{name}: {errors}"
