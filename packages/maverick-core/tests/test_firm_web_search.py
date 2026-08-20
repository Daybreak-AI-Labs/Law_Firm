from __future__ import annotations

from maverick.matter_context import MatterContext, matter_context_scope
from maverick.tools import web_search as search_module


def _context(mode: str = "local_only") -> MatterContext:
    return MatterContext(
        matter_id=41,
        client_id=3,
        principal="user:attorney",
        membership_role="responsible_attorney",
        domain="legal_contract_review",
        jurisdiction="Tennessee",
        purpose="goal-execution",
        source="test",
        egress_mode=mode,
    )


def test_secure_search_requires_one_explicit_backend(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.delenv("MAVERICK_SEARCH_BACKEND", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})
    calls: list[str] = []
    monkeypatch.setitem(
        search_module._BACKENDS,
        "ddg",
        lambda _query, _num: calls.append("ddg") or [],
    )

    out = search_module._run_search({"query": "privileged facts"})

    assert "requires one explicit backend" in out
    assert calls == []


def test_secure_search_never_falls_back_after_selected_vendor_failure(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_SEARCH_BACKEND", "tavily")
    calls: list[str] = []
    monkeypatch.setitem(
        search_module._BACKENDS,
        "tavily",
        lambda _query, _num: calls.append("tavily") or None,
    )
    for backend in ("brave", "serpapi", "ddg"):
        monkeypatch.setitem(
            search_module._BACKENDS,
            backend,
            lambda _query, _num, name=backend: calls.append(name) or [],
        )

    out = search_module._run_search({"query": "privileged facts"})

    assert "no fallback was attempted" in out
    assert calls == ["tavily"]


def test_local_only_matter_blocks_selected_backend_before_dispatch(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_SEARCH_BACKEND", "tavily")
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: True)
    calls: list[str] = []
    monkeypatch.setitem(
        search_module._BACKENDS,
        "tavily",
        lambda _query, _num: calls.append("tavily") or [],
    )

    with matter_context_scope(_context()):
        out = search_module._run_search({"query": "client secret"})

    assert "matter egress policy" in out
    assert calls == []


def test_approved_matter_uses_configured_backend_once_with_exact_host(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.delenv("MAVERICK_SEARCH_BACKEND", raising=False)
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {
                "search_backend": "tavily",
                "approved_hosts": ["api.tavily.com"],
            }
        },
    )
    context = _context("approved_services")
    monkeypatch.setattr(
        "maverick.enterprise._refresh_bound_matter_context",
        lambda: context,
    )
    calls: list[tuple[str, int]] = []
    monkeypatch.setitem(
        search_module._BACKENDS,
        "tavily",
        lambda query, num: calls.append((query, num))
        or [{"title": "Authority", "url": "https://court.example", "snippet": "x"}],
    )

    with matter_context_scope(context):
        out = search_module._run_search({"query": "authority", "num_results": 3})

    assert out.startswith("[backend: tavily]")
    assert calls == [("authority", 3)]
