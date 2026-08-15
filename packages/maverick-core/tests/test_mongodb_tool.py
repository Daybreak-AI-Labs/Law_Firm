"""MongoDB tool: find() must not silently drop an unparseable sort spec.

Regression: a malformed sort (e.g. direction "desc" instead of -1) raised
inside the sort-pair comprehension and was swallowed by an except/pass, so
the query ran UNSORTED but still applied .limit() — returning an arbitrary
subset of documents presented as a successful sorted query.
"""
from __future__ import annotations

import sys
import types


def _install_fake_mongo(monkeypatch, docs):
    """Wire a pymongo stub whose cursor records the sort spec it receives."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://x")
    monkeypatch.setenv("MONGODB_DB", "test")
    pymongo = types.ModuleType("pymongo")
    calls: dict = {"sort": None}

    class _Cursor:
        def sort(self, spec):
            calls["sort"] = spec
            return self

        def limit(self, n):
            return iter(docs[:n])

    class _Col:
        def find(self, flt):
            return _Cursor()

    class _DB:
        def __getitem__(self, _name):
            return _Col()

    class _MongoClient:
        def __init__(self, *a, **k):
            pass

        def __getitem__(self, _name):
            return _DB()

        def close(self):
            pass

    pymongo.MongoClient = _MongoClient
    monkeypatch.setitem(sys.modules, "pymongo", pymongo)
    return calls


def test_find_bad_sort_direction_errors(monkeypatch):
    calls = _install_fake_mongo(monkeypatch, [{"_id": 1, "name": "alice"}])
    from maverick.tools.mongodb_tool import mongodb_tool
    out = mongodb_tool().fn({
        "op": "find", "collection": "orders",
        "sort": [["created_at", "desc"]], "limit": 10,
    })
    assert out.startswith("ERROR") and "sort" in out
    assert calls["sort"] is None  # the unsorted query never ran


def test_find_one_element_sort_pair_errors(monkeypatch):
    _install_fake_mongo(monkeypatch, [{"_id": 1}])
    from maverick.tools.mongodb_tool import mongodb_tool
    out = mongodb_tool().fn({
        "op": "find", "collection": "orders", "sort": [["created_at"]],
    })
    assert out.startswith("ERROR") and "sort" in out


def test_find_valid_sort_still_applies(monkeypatch):
    calls = _install_fake_mongo(monkeypatch, [{"_id": 1, "name": "alice"}])
    from maverick.tools.mongodb_tool import mongodb_tool
    out = mongodb_tool().fn({
        "op": "find", "collection": "orders",
        "sort": [["created_at", -1]], "limit": 5,
    })
    assert "alice" in out and "ERROR" not in out
    assert calls["sort"] == [("created_at", -1)]
