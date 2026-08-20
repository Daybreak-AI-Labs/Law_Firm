"""Named connector credentials stay sealed and exact-principal scoped."""
from __future__ import annotations

import asyncio

import pytest
from maverick import connections


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "maverick.paths.data_dir",
        lambda *parts, **_kwargs: tmp_path.joinpath(*parts),
    )
    monkeypatch.setenv("MAVERICK_CONNECTIONS", "1")
    monkeypatch.setattr(
        "maverick.connections.seal_text_for_tenant",
        lambda _tenant, text: text.encode("utf-8"),
    )
    monkeypatch.setattr(
        "maverick.connections.unseal_text_for_tenant",
        lambda _tenant, payload: payload.decode("utf-8"),
    )


def test_exact_owner_or_explicit_grant_is_required():
    connections.set_connection(
        "court-records",
        connector="court-records",
        token="counsel-secret",
        owner="user:counsel",
        allowed_principals=["user:paralegal"],
    )

    assert connections.resolve(
        "court-records", principal="user:counsel"
    ) == ("", "counsel-secret")
    assert connections.resolve(
        "court-records", principal="user:paralegal"
    ) == ("", "counsel-secret")
    assert connections.resolve("court-records", principal="user:other") is None
    assert connections.resolve("court-records") is None


def test_rotation_invalidates_stale_readiness_receipt():
    connections.set_connection("court-records", token="old", owner="user:counsel")
    original = connections.get_connection("court-records")
    assert original is not None and original["revision"]

    connections.set_connection(
        "court-records",
        token="new",
        owner="user:counsel",
        expected_owner="user:counsel",
    )

    with pytest.raises(connections.ConnectionVersionConflict):
        connections.record_test_result(
            "court-records",
            reachable=True,
            authenticated=True,
            status=200,
            expected_owner="user:counsel",
            expected_revision=original["revision"],
        )
    assert connections.get_connection("court-records")["token"] == "new"


def test_registry_binds_principal_at_credential_use_boundary():
    from maverick.tools import Tool, ToolRegistry

    connections.set_connection("private", token="secret", owner="user:counsel")
    probe = Tool(
        name="probe",
        description="resolve inside the execution context",
        input_schema={"type": "object", "properties": {}},
        fn=lambda _args: str(connections.resolve("private")),
    )
    counsel = ToolRegistry(principal="user:counsel")
    counsel.register(probe)
    stranger = ToolRegistry(principal="user:stranger")
    stranger.register(probe)

    assert "secret" in asyncio.run(counsel.run("probe", {}))
    assert asyncio.run(stranger.run("probe", {})) == "None"


def test_principal_values_are_exact_not_trimmed():
    connections.set_connection("exact", token="plain", owner="user:counsel")
    connections.set_connection("spaced", token="spaced", owner="user:counsel ")

    assert connections.resolve("exact", principal="user:counsel") == ("", "plain")
    assert connections.resolve("spaced", principal="user:counsel ") == (
        "",
        "spaced",
    )
