"""The /billing dashboard view: per-period accrued charges, period-over-period
trend, and an itemized CSV invoice download."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    w = world_model.WorldModel(tmp_path / "world.db")
    yield w
    w.close()


@pytest.fixture
def client(world, monkeypatch):
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod
    monkeypatch.setattr(app_mod, "_world", lambda: world)
    monkeypatch.setattr(api_mod, "_world", lambda: world)
    return TestClient(app_mod.app, headers={"Origin": "http://testserver"})


def _seed(world, cost=0.42):
    g = world.create_goal("Pay invoices", "seed")
    ep = world.start_episode(g)
    world.end_episode(ep, "done", "success", cost_dollars=cost,
                      input_tokens=1000, output_tokens=500, tool_calls=3)
    return g


def test_billing_empty_renders(client):
    r = client.get("/billing")
    assert r.status_code == 200
    assert "No priced runs" in r.text


def test_billing_shows_accrued_charges(client, world):
    _seed(world, cost=0.42)
    r = client.get("/billing")
    assert r.status_code == 200
    assert "accrued charges" in r.text
    assert "0.42" in r.text
    assert "Download invoice" in r.text


def test_billing_csv_invoice_is_itemized(client, world):
    _seed(world, cost=0.42)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    r = client.get(f"/billing?format=csv&period={period}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("episode_id,goal_id")
    assert len(lines) == 2                       # header + the one priced episode
    assert ",0.420000," in r.text


def test_billing_csv_other_period_is_empty(client, world):
    _seed(world, cost=0.42)
    r = client.get("/billing?format=csv&period=1999-01")
    assert r.status_code == 200
    assert len(r.text.strip().splitlines()) == 1  # header only, no episodes


def test_billing_linked_from_spend(client):
    # /billing left the sidebar (one nav entry per job); the Spend page links
    # to the monthly statement instead.
    r = client.get("/spend")
    assert r.status_code == 200
    assert 'href="/billing"' in r.text
