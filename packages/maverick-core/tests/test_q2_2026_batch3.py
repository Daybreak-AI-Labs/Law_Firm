"""Q2 2026 batch 3: HuggingFace TGI provider, Chroma vector store,
Bluesky/Mastodon channel adapters."""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest

# ---------- TGI provider ----------

class _FakeOpenAIClient:
    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.api_key = api_key
        self.base_url = base_url


def _install_fake_openai(monkeypatch):
    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeOpenAIClient
    fake.AsyncOpenAI = _FakeOpenAIClient
    monkeypatch.setitem(sys.modules, "openai", fake)


def test_tgi_provider_default_url(monkeypatch):
    _install_fake_openai(monkeypatch)
    monkeypatch.delenv("TGI_BASE_URL", raising=False)
    monkeypatch.delenv("TGI_API_KEY", raising=False)
    from maverick.providers.tgi_provider import TGIClient
    client = TGIClient()
    assert "8080" in client._sync.base_url
    assert client._sync.base_url.endswith("/v1")


def test_tgi_provider_env_overrides(monkeypatch):
    _install_fake_openai(monkeypatch)
    monkeypatch.setenv("TGI_BASE_URL", "http://my-tgi.example.com:9999")
    monkeypatch.setenv("TGI_API_KEY", "secret-token")
    from maverick.providers.tgi_provider import TGIClient
    client = TGIClient()
    # /v1 suffix appended automatically when missing.
    assert client._sync.base_url == "http://my-tgi.example.com:9999/v1"
    assert client._sync.api_key == "secret-token"


def test_tgi_provider_v1_suffix_idempotent(monkeypatch):
    _install_fake_openai(monkeypatch)
    monkeypatch.setenv("TGI_BASE_URL", "http://my-tgi.example.com/v1")
    from maverick.providers.tgi_provider import TGIClient
    client = TGIClient()
    # Already had /v1; don't double-suffix.
    assert client._sync.base_url == "http://my-tgi.example.com/v1"


def test_tgi_provider_in_registry():
    from maverick.providers import KNOWN_PROVIDERS, _canonical
    assert "tgi" in KNOWN_PROVIDERS
    assert _canonical("hf-tgi") == "tgi"
    assert _canonical("huggingface-tgi") == "tgi"


def test_tgi_provider_dispatches(monkeypatch):
    _install_fake_openai(monkeypatch)
    monkeypatch.delenv("TGI_BASE_URL", raising=False)
    from maverick.providers import get_provider_client
    from maverick.providers.tgi_provider import TGIClient
    client = get_provider_client("tgi")
    assert isinstance(client, TGIClient)
    # Alias also dispatches.
    client2 = get_provider_client("hf-tgi")
    assert isinstance(client2, TGIClient)


# ---------- Chroma vector store ----------

_HAS_CHROMA = importlib.util.find_spec("chromadb") is not None


@pytest.mark.skipif(not _HAS_CHROMA, reason="chromadb not installed")
def test_chroma_store_round_trip(tmp_path):
    from maverick.vector_store import ChromaStore
    store = ChromaStore(collection="t1", path=tmp_path / "vs")
    store.add(
        ["the user prefers dark mode", "morning is best for cold calls"],
        ids=["fact-1", "fact-2"],
        metadatas=[{"topic": "ui"}, {"topic": "sales"}],
    )
    assert store.count() == 2
    hits = store.query("UI preference", top_k=1)
    assert len(hits) == 1
    assert hits[0]["id"] in {"fact-1", "fact-2"}


@pytest.mark.skipif(not _HAS_CHROMA, reason="chromadb not installed")
def test_chroma_store_delete_and_reset(tmp_path):
    from maverick.vector_store import ChromaStore
    store = ChromaStore(collection="t2", path=tmp_path / "vs")
    store.add(["a", "b", "c"], ids=["i1", "i2", "i3"])
    store.delete(["i2"])
    assert store.count() == 2
    store.reset()
    assert store.count() == 0


def test_wizard_catalog_includes_tgi():
    from maverick_installer import models
    assert "tgi" in models.PROVIDERS
    assert models.PROVIDERS["tgi"]["status"] == "ready"


