"""Q2 2026 batch 4: theme presets and the Semantic Scholar tool."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------- semantic_scholar tool ----------

def test_semantic_scholar_requires_op():
    from maverick.tools.semantic_scholar import semantic_scholar
    out = semantic_scholar().fn({})
    assert "ERROR" in out
    assert "op is required" in out


def test_semantic_scholar_unknown_op():
    from maverick.tools.semantic_scholar import semantic_scholar
    out = semantic_scholar().fn({"op": "garbage"})
    assert "unknown op" in out


def test_semantic_scholar_search_requires_query():
    from maverick.tools.semantic_scholar import semantic_scholar
    out = semantic_scholar().fn({"op": "search", "query": ""})
    assert "requires query" in out


def test_semantic_scholar_paper_requires_id():
    from maverick.tools.semantic_scholar import semantic_scholar
    out = semantic_scholar().fn({"op": "paper", "paper_id": ""})
    assert "requires paper_id" in out


def test_semantic_scholar_search_hits_api(monkeypatch):
    from maverick.tools.semantic_scholar import semantic_scholar
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "data": [
            {
                "title": "Attention Is All You Need",
                "year": 2017,
                "authors": [{"name": "Vaswani et al."}],
                "citationCount": 100000,
                "abstract": "We propose the Transformer...",
                "url": "https://www.semanticscholar.org/paper/abc",
            },
        ],
    })
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        out = semantic_scholar().fn({"op": "search", "query": "transformer"})
    assert "Attention Is All You Need" in out
    assert "100000" in out
    # API URL hit, fields requested.
    call = mock_get.call_args
    assert "/paper/search" in call.args[0]
    assert "fields" in call.kwargs["params"]


def test_semantic_scholar_paper_by_doi(monkeypatch):
    from maverick.tools.semantic_scholar import semantic_scholar
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "title": "T",
        "year": 2024,
        "authors": [{"name": "A"}],
        "citationCount": 1,
        "abstract": "x" * 500,
        "url": "u",
    })
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        out = semantic_scholar().fn({"op": "paper", "paper_id": "10.1234/abc"})
    call = mock_get.call_args
    # DOI without explicit prefix gets prefixed automatically.
    assert "DOI:10.1234/abc" in call.args[0]
    assert "T" in out


def test_semantic_scholar_legacy_arxiv_id(monkeypatch):
    """Old-style arXiv ids contain a slash but are NOT DOIs -- they must be
    looked up as arXiv:<id>, not DOI:<id> (which never resolves)."""
    from maverick.tools.semantic_scholar import semantic_scholar
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "title": "T", "year": 2003, "authors": [{"name": "A"}],
        "citationCount": 1, "abstract": "x", "url": "u",
    })
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        out = semantic_scholar().fn({"op": "paper", "paper_id": "math/0211159"})
    call = mock_get.call_args
    assert "arXiv:math/0211159" in call.args[0]
    assert "DOI:math/0211159" not in call.args[0]
    assert "T" in out


def test_semantic_scholar_404_actionable():
    from maverick.tools.semantic_scholar import semantic_scholar
    fake_resp = MagicMock()
    fake_resp.status_code = 404
    fake_resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_resp):
        out = semantic_scholar().fn({
            "op": "paper", "paper_id": "arxiv:0000.0000",
        })
    assert "no paper found" in out


def test_semantic_scholar_in_registry():
    from maverick.tools import base_registry

    class _FakeSandbox:
        workdir = "."

    class _FakeWorld:
        pass

    reg = base_registry(world=_FakeWorld(), sandbox=_FakeSandbox())
    names = {t.name for t in reg.all()}
    assert "semantic_scholar" in names


# ---------- dashboard themes ----------

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick.world_model import WorldModel
    w = WorldModel(tmp_path / "world.db")
    w.create_goal("hello", "")
    from maverick_dashboard import app as app_mod
    monkeypatch.setattr(app_mod, "_world", lambda: w)
    yield TestClient(app_mod.app)
    w.close()


def test_dashboard_default_theme_is_graphite(dashboard_client):
    resp = dashboard_client.get("/")
    assert resp.status_code == 200
    assert 'class="theme-graphite' in resp.text


@pytest.mark.parametrize(
    "theme", ["graphite", "dove", "dark", "light", "solarized", "hicontrast"]
)
def test_dashboard_theme_query_param(dashboard_client, theme):
    resp = dashboard_client.get(f"/?theme={theme}")
    assert resp.status_code == 200
    assert f'class="theme-{theme}' in resp.text


def test_dashboard_invalid_theme_falls_back(dashboard_client):
    resp = dashboard_client.get("/?theme=garbage-not-a-theme")
    assert resp.status_code == 200
    # Falls back to graphite (the default).
    assert 'class="theme-graphite' in resp.text


def test_dashboard_theme_cookie_persists(dashboard_client):
    # First visit with ?theme=light should set the cookie.
    resp = dashboard_client.get("/?theme=light")
    assert resp.status_code == 200
    cookies = resp.cookies
    assert cookies.get("mvk_theme") == "light"
    # The client cookie jar should carry the server-set value into the next
    # request; passing per-request cookies is deprecated and bypasses that
    # persistence behavior.
    resp2 = dashboard_client.get("/")
    assert 'class="theme-light' in resp2.text


def test_dashboard_theme_switcher_options_in_header(dashboard_client):
    """Council UX pass replaced the 4-coloured-dots switcher with a single
    ``<select>`` element (accessibility + cleaner UI). All four themes
    must still be present as options."""
    resp = dashboard_client.get("/")
    body = resp.text
    for theme in ("dark", "light", "solarized", "hicontrast"):
        # Either as the new <option value="X"> or, transitionally, as
        # a hand-coded link still in the header.
        assert (f'value="{theme}"' in body) or (f"?theme={theme}" in body), (
            f"theme {theme} not exposed via the theme switcher"
        )
