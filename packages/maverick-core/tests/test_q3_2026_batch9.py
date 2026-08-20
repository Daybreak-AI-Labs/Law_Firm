"""Q3 2026 LLM-cache tests."""
from __future__ import annotations

import time

# ---------- LLM cache ----------

def test_llm_cache_key_stable():
    from maverick.cache.llm import cache_key
    a = cache_key(provider="anthropic", model="m", system="s",
                  messages=[{"role": "user", "content": "hi"}],
                  tools=[], max_tokens=100)
    b = cache_key(provider="anthropic", model="m", system="s",
                  messages=[{"role": "user", "content": "hi"}],
                  tools=[], max_tokens=100)
    c = cache_key(provider="anthropic", model="m", system="s",
                  messages=[{"role": "user", "content": "bye"}],
                  tools=[], max_tokens=100)
    assert a == b
    assert a != c


def test_llm_cache_lookup_miss_then_hit(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db")
    key = "abc"
    assert cache.lookup(key) is None
    cache.store(key, provider="anthropic", model="m", text="hello there",
                stop_reason="end_turn")
    hit = cache.lookup(key)
    assert hit is not None
    assert hit.text == "hello there"
    assert hit.stop_reason == "end_turn"
    assert hit.hit_count == 1
    # Bump count
    hit2 = cache.lookup(key)
    assert hit2.hit_count == 2


def test_llm_cache_ttl_expires(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db", ttl_seconds=1)
    cache.store("k", provider="p", model="m", text="x")
    assert cache.lookup("k") is not None
    # Lookup with a forced future timestamp drops the row.
    later = time.time() + 60
    assert cache.lookup("k", now=later) is None


def test_llm_cache_stats(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db")
    cache.store("a", provider="p", model="m", text="1")
    cache.store("b", provider="p", model="m", text="2")
    cache.lookup("a")
    s = cache.stats()
    assert s["entries"] == 2
    assert s["hits"] >= 1


def test_llm_cache_purge_expired(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db", ttl_seconds=10)
    cache.store("k", provider="p", model="m", text="x")
    # Force expiration window.
    deleted = cache.purge_expired(now=time.time() + 999)
    assert deleted == 1
    assert cache.lookup("k") is None


def test_llm_cache_clear(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db")
    cache.store("a", provider="p", model="m", text="x")
    cache.clear()
    assert cache.lookup("a") is None


def test_llm_cache_enabled_via_env(monkeypatch):
    from maverick.cache import llm as llm_cache
    monkeypatch.setenv("MAVERICK_LLM_CACHE", "1")
    assert llm_cache.enabled() is True
    monkeypatch.setenv("MAVERICK_LLM_CACHE", "0")
    assert llm_cache.enabled() is False
