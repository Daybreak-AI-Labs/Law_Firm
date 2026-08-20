"""Firm learning stores are authenticated ciphertext, never plaintext memory."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from maverick import (
    dreaming,
    learning_store,
    reflexion,
    self_harness,
    self_harness_eval,
    user_notes,
)
from maverick.matter_context import MatterContext, matter_context_scope
from maverick.skill import distillation_local, distillation_v2

MATTER_ID = 41
OWNER = "attorney@example.test"
KEY_ONE = "11" * 32
KEY_TWO = "22" * 32
DISTINCTIVE = "CLIENT-ALPHA-PRIVILEGED-SETTLEMENT-STRATEGY"


def _secure(monkeypatch, key: str = KEY_ONE) -> None:
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_PER_TENANT", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", key)


def _context(
    matter_id: int = MATTER_ID,
    principal: str = OWNER,
) -> MatterContext:
    return MatterContext(
        matter_id=matter_id,
        client_id=9,
        principal=principal,
        membership_role="attorney",
        domain="legal",
        jurisdiction="Federal-VA",
        purpose="goal-execution",
        source="learning-encryption-test",
        egress_mode="local_only",
    )


class _AllowShield:
    @staticmethod
    def scan_input(_text):
        return SimpleNamespace(allowed=True)


def test_reflexion_is_sealed_and_plaintext_or_wrong_key_is_withheld(
    monkeypatch,
    tmp_path,
):
    _secure(monkeypatch)
    path = tmp_path / "reflexions.ndjson"
    assert reflexion.record(
        DISTINCTIVE,
        "agent_error",
        "private failure detail",
        "private learned lesson",
        matter_id=MATTER_ID,
        owner=OWNER,
        path=path,
    )
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("MVKAR1:")
    assert DISTINCTIVE not in raw
    assert reflexion.list_recent(path=path)[0].goal_text == DISTINCTIVE

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    assert reflexion.list_recent(path=path) == []
    original = path.read_bytes()
    assert not reflexion.record(
        "must not append under wrong key",
        "agent_error",
        "failure",
        "lesson",
        matter_id=MATTER_ID,
        owner=OWNER,
        path=path,
    )
    assert path.read_bytes() == original

    path.write_text(
        json.dumps(
            {
                "ts": 1,
                "goal_text": DISTINCTIVE,
                "failure_class": "agent_error",
                "failure_msg": "plaintext",
                "reflection": "plaintext",
                "tools_used": [],
                "matter_id": MATTER_ID,
                "owner": OWNER,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_ONE)
    assert reflexion.list_recent(path=path) == []


def test_dream_insights_and_rehearsals_are_sealed_and_matter_bound(
    monkeypatch,
    tmp_path,
):
    _secure(monkeypatch)
    insight_path = tmp_path / "insights.ndjson"
    insight = dreaming.DreamInsight(
        ts=1.0,
        kind="failure_pattern",
        domain="legal",
        text=DISTINCTIVE,
        matter_id=MATTER_ID,
    )
    assert dreaming.append_insights([insight], path=insight_path) == 1
    raw = insight_path.read_text(encoding="utf-8")
    assert raw.startswith("MVKAR1:")
    assert DISTINCTIVE not in raw
    assert dreaming.recall_insights(
        DISTINCTIVE,
        domain="legal",
        matter_id=MATTER_ID,
        path=insight_path,
    ) == []
    context = _context()
    with matter_context_scope(context, authority_resolver=lambda: context):
        recalled = dreaming.recall_insights(
            DISTINCTIVE,
            domain="legal",
            matter_id=MATTER_ID,
            path=insight_path,
        )
    assert recalled and recalled[0][1].text == DISTINCTIVE

    rehearsal_path = tmp_path / "rehearsals.ndjson"
    case = {
        "prompt": DISTINCTIVE,
        "scope": "local",
        "matter_id": MATTER_ID,
        "owner": OWNER,
        "domain": "legal",
    }
    assert dreaming.save_rehearsals([case], rehearsal_path) == 1
    rehearsal_raw = rehearsal_path.read_text(encoding="utf-8")
    assert rehearsal_raw.startswith("MVKAR1:")
    assert DISTINCTIVE not in rehearsal_raw
    assert dreaming.load_rehearsals(rehearsal_path)[0]["prompt"] == DISTINCTIVE

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    assert dreaming.load_insights(insight_path) == []
    assert dreaming.load_rehearsals(rehearsal_path) == []
    insight_raw = insight_path.read_bytes()
    rehearsal_raw_bytes = rehearsal_path.read_bytes()
    assert dreaming.append_insights([insight], path=insight_path) == 0
    assert dreaming.save_rehearsals([case], rehearsal_path) == 0
    assert insight_path.read_bytes() == insight_raw
    assert rehearsal_path.read_bytes() == rehearsal_raw_bytes


def test_learned_skill_is_sealed_and_requires_live_exact_owner(
    monkeypatch,
    tmp_path,
):
    _secure(monkeypatch)
    monkeypatch.setattr(distillation_local, "enabled", lambda: True)
    monkeypatch.setattr(distillation_local, "_default_store", lambda: tmp_path)
    skill = {
        "name": "privileged-settlement-workflow",
        "triggers": [DISTINCTIVE.lower()],
        "tools_needed": ["knowledge_search"],
        "summary": DISTINCTIVE,
        "n_examples": 2,
        "source_goal_ids": [7, 8],
    }
    path = distillation_local.save_skill(
        skill,
        tmp_path,
        project_id=MATTER_ID,
        owner=OWNER,
    )
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("MVKAR1:")
    assert DISTINCTIVE not in raw
    assert distillation_local.recall_context(
        DISTINCTIVE,
        project_id=MATTER_ID,
        owner=OWNER,
        shield=_AllowShield(),
    ) == ""

    context = _context()
    with matter_context_scope(context, authority_resolver=lambda: context):
        rendered = distillation_local.recall_context(
            DISTINCTIVE,
            project_id=MATTER_ID,
            owner=OWNER,
            shield=_AllowShield(),
        )
    assert "privileged-settlement-workflow" in rendered

    signatures = distillation_v2.signatures_from_store(
        tmp_path,
        project_id=MATTER_ID,
        owner=OWNER,
    )
    assert signatures

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    with matter_context_scope(context, authority_resolver=lambda: context):
        assert distillation_local.recall_context(
            DISTINCTIVE,
            project_id=MATTER_ID,
            owner=OWNER,
            shield=_AllowShield(),
        ) == ""
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="authentication failed"):
        distillation_local.save_skill(
            skill,
            tmp_path,
            project_id=MATTER_ID,
            owner=OWNER,
        )
    assert path.read_bytes() == original

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_ONE)
    path.write_text(distillation_local.to_skill_markdown(skill), encoding="utf-8")
    with matter_context_scope(context, authority_resolver=lambda: context):
        assert distillation_local.recall_context(
            DISTINCTIVE,
            project_id=MATTER_ID,
            owner=OWNER,
            shield=_AllowShield(),
        ) == ""


def test_global_user_notes_are_disabled_in_firm_mode(monkeypatch, tmp_path):
    _secure(monkeypatch)
    path = tmp_path / "user_notes.ndjson"
    path.write_text(
        json.dumps(
            {"channel": "chat", "user_id": OWNER, "note": DISTINCTIVE}
        )
        + "\n",
        encoding="utf-8",
    )
    assert user_notes.notes_for("chat", OWNER, path) == []
    assert user_notes.consolidate(object(), path=path) == 0
    assert user_notes.erase_notes("chat", OWNER, path, strict=True) == 1
    assert DISTINCTIVE not in path.read_text(encoding="utf-8")


def test_harness_addenda_and_line_meta_are_sealed_and_exact_matter_bound(
    monkeypatch,
    tmp_path,
):
    _secure(monkeypatch)
    monkeypatch.setattr(self_harness, "enabled", lambda: True)
    store = tmp_path / "addenda.json"
    owner_scope = hashlib.sha256(OWNER.encode("utf-8")).hexdigest()[:16]
    scoped_key = self_harness._matter_scoped_key(
        "M",
        matter_id=MATTER_ID,
        owner_scope=owner_scope,
    )
    block = f"Operating guidance learned for this model:\n- {DISTINCTIVE}"
    line_id = self_harness._line_id(scoped_key, DISTINCTIVE)
    self_harness._write_addenda({scoped_key: block}, store)
    self_harness._write_line_meta(
        {
            line_id: {
                "model_id": scoped_key,
                "text": DISTINCTIVE,
                "rationale": "privileged client evidence",
            }
        },
        store,
    )

    raw_addenda = store.read_text(encoding="utf-8")
    raw_meta = self_harness._meta_path(store).read_text(encoding="utf-8")
    assert raw_addenda.startswith("MVKAR1:")
    assert raw_meta.startswith("MVKAR1:")
    assert DISTINCTIVE not in raw_addenda
    assert DISTINCTIVE not in raw_meta

    assert self_harness.recall_addendum("M", store) == ""
    exact = _context()
    with matter_context_scope(exact, authority_resolver=lambda: exact):
        assert DISTINCTIVE in self_harness.recall_addendum("M", store)
    other_matter = _context(matter_id=MATTER_ID + 1)
    with matter_context_scope(
        other_matter,
        authority_resolver=lambda: other_matter,
    ):
        assert self_harness.recall_addendum("M", store) == ""

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    with matter_context_scope(exact, authority_resolver=lambda: exact):
        assert self_harness.recall_addendum("M", store) == ""
    assert self_harness.load_line_meta(store) == {}
    with pytest.raises(ValueError, match="unreadable or malformed"):
        self_harness._load_addenda_strict(store)

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_ONE)
    store.write_text(json.dumps({scoped_key: block}), encoding="utf-8")
    self_harness._meta_path(store).write_text(
        json.dumps({line_id: {"text": DISTINCTIVE}}),
        encoding="utf-8",
    )
    with matter_context_scope(exact, authority_resolver=lambda: exact):
        assert self_harness.recall_addendum("M", store) == ""
    assert self_harness.load_line_meta(store) == {}


@pytest.mark.parametrize("kind", ["live", "pending", "rejected"])
def test_every_harness_corpus_kind_is_sealed_and_rejects_wrong_key_or_plaintext(
    monkeypatch,
    tmp_path,
    kind,
):
    _secure(monkeypatch)
    base = tmp_path / "eval.json"
    path = {
        "live": base,
        "pending": self_harness_eval.pending_corpus_path(base),
        "rejected": self_harness_eval.rejected_corpus_path(base),
    }[kind]
    data = (
        {"M": [DISTINCTIVE]}
        if kind == "rejected"
        else {"M": [{"goal": DISTINCTIVE, "expected": "private outcome"}]}
    )
    self_harness_eval._write_corpus_file(path, data, seal=kind != "live")

    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("MVKAR1:")
    assert DISTINCTIVE not in raw
    assert json.loads(self_harness_eval._read_json_text(path)) == data

    loader = {
        "live": self_harness_eval.load_eval_corpus,
        "pending": self_harness_eval.load_pending,
        "rejected": self_harness_eval.load_rejected,
    }[kind]
    assert loader(base)
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    assert loader(base) == {}
    original = path.read_bytes()
    mutation = {
        "live": lambda: self_harness_eval.merge_candidates(
            base,
            "M",
            [{"goal": "new case", "expected": "new expected"}],
        ),
        "pending": lambda: self_harness_eval.stage_candidates(
            base,
            "M",
            [{"goal": "new case", "expected": "new expected"}],
        ),
        "rejected": lambda: self_harness_eval.stage_candidates(
            base,
            "M",
            [{"goal": "new case", "expected": "new expected"}],
        ),
    }[kind]
    with pytest.raises(ValueError, match="unauthenticated or unreadable"):
        mutation()
    assert path.read_bytes() == original

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_ONE)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert loader(base) == {}
    plaintext = path.read_bytes()
    with pytest.raises(ValueError, match="unauthenticated or unreadable"):
        mutation()
    assert path.read_bytes() == plaintext


def test_world_harness_rows_are_all_sealed_and_legacy_rows_are_withheld(
    monkeypatch,
    tmp_path,
):
    _secure(monkeypatch)
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MAVERICK_WORLD_BACKEND", raising=False)
    scoped_key = self_harness._matter_scoped_key(
        "M",
        matter_id=MATTER_ID,
        owner_scope=hashlib.sha256(OWNER.encode("utf-8")).hexdigest()[:16],
    )
    learning_store.write_addenda_db({scoped_key: DISTINCTIVE})
    learning_store.write_line_meta_db(
        {"line": {"model_id": scoped_key, "text": DISTINCTIVE}}
    )
    corpus = {"M": [{"goal": DISTINCTIVE, "expected": "private"}]}
    for kind in ("live", "pending", "rejected"):
        learning_store.write_corpus_db(kind, corpus)

    db_path = learning_store._sqlite_path()
    with sqlite3.connect(db_path) as conn:
        values = [
            conn.execute("SELECT block FROM harness_addenda").fetchone()[0],
            conn.execute("SELECT record FROM harness_line_meta").fetchone()[0],
            *[
                row[0]
                for row in conn.execute(
                    "SELECT row FROM harness_corpus ORDER BY kind"
                ).fetchall()
            ],
        ]
    assert values
    assert all(value.startswith("MVKAR1:") for value in values)
    assert all(DISTINCTIVE not in value for value in values)

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_TWO)
    assert learning_store.load_addenda_db() == {}
    assert learning_store.load_line_meta_db() == {}
    for kind in ("live", "pending", "rejected"):
        assert learning_store.load_corpus_db(kind) == {}

    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", KEY_ONE)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE harness_addenda SET block = ?",
            (DISTINCTIVE,),
        )
        conn.execute(
            "UPDATE harness_line_meta SET record = ?",
            (json.dumps({"text": DISTINCTIVE}),),
        )
        conn.execute(
            "UPDATE harness_corpus SET row = ?",
            (json.dumps({"goal": DISTINCTIVE}),),
        )
    assert learning_store.load_addenda_db() == {}
    assert learning_store.load_line_meta_db() == {}
    for kind in ("live", "pending", "rejected"):
        assert learning_store.load_corpus_db(kind) == {}
