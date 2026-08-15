"""The Consequence Engine: record real outcomes, resolve them, and prefer
reality over the proxy -- only when enabled (OFF by default)."""
from __future__ import annotations

from maverick import consequence as cq


def test_record_resolve_roundtrip(tmp_path):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    assert store.resolve(1, 7) is None
    store.record(1, 7, 1.0, kind="invoice_paid")
    assert store.resolve(1, 7) == 1.0


def test_latest_event_wins(tmp_path):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    store.record(1, 7, 1.0, kind="renewed", ts=10.0)
    store.record(1, 7, 0.0, kind="churned", ts=20.0)   # reality changed; newest wins
    assert store.resolve(1, 7) == 0.0


def test_value_is_clamped(tmp_path):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    store.record(1, 1, 5.0)
    store.record(1, 2, -3.0)
    assert store.resolve(1, 1) == 1.0 and store.resolve(1, 2) == 0.0


def test_persists_across_reload(tmp_path):
    p = tmp_path / "c.ndjson"
    cq.ConsequenceStore(path=p).record(2, 3, 0.7, kind="graded")
    assert cq.ConsequenceStore(path=p).resolve(2, 3) == 0.7


def test_grounded_outcome_prefers_reality_only_when_enabled(tmp_path, monkeypatch):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    store.record(1, 7, 1.0)  # reality says success...

    # disabled (default): keep the proxy
    monkeypatch.setattr("maverick.config.get_consequence", lambda: {"enable": False})
    monkeypatch.delenv("MAVERICK_CONSEQUENCE", raising=False)
    assert cq.grounded_outcome(1, 7, proxy=0.4, store=store) == 0.4

    # enabled: prefer reality where it has landed, else fall back to the proxy
    monkeypatch.setenv("MAVERICK_CONSEQUENCE", "1")
    assert cq.grounded_outcome(1, 7, proxy=0.4, store=store) == 1.0
    assert cq.grounded_outcome(9, 9, proxy=0.4, store=store) == 0.4   # no real outcome yet


def test_record_outcome_public_entry(tmp_path):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    assert cq.record_outcome(5, 5, 0.9, store=store) is True
    assert cq.resolve(5, 5, store=store) == 0.9


# ---- record_self_outcome (first-party grounding) ---------------------------


class _FakeEpisode:
    def __init__(self, id):
        self.id = id


class _FakeWorld:
    def __init__(self, episodes):
        self._episodes = episodes

    def list_episodes(self, goal_id=None, limit=50):
        return self._episodes[:limit]


def test_record_self_outcome_gated_off_by_default(tmp_path, monkeypatch):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    monkeypatch.setattr("maverick.config.get_consequence", lambda: {"enable": False})
    monkeypatch.delenv("MAVERICK_CONSEQUENCE", raising=False)
    w = _FakeWorld([_FakeEpisode(42)])
    # disabled: no-op returning False, nothing stored
    assert cq.record_self_outcome(w, 7, 0.0, kind="failed", store=store) is False
    assert store.resolve(7, 42) is None


def test_record_self_outcome_grounds_latest_episode_when_enabled(tmp_path, monkeypatch):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    monkeypatch.setenv("MAVERICK_CONSEQUENCE", "1")
    w = _FakeWorld([_FakeEpisode(99)])
    assert cq.record_self_outcome(w, 7, 0.0, kind="failed", store=store) is True
    assert store.resolve(7, 99) == 0.0


def test_record_self_outcome_no_episode_is_false(tmp_path, monkeypatch):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    monkeypatch.setenv("MAVERICK_CONSEQUENCE", "1")
    assert cq.record_self_outcome(_FakeWorld([]), 7, 1.0, kind="x", store=store) is False


def test_record_self_outcome_never_raises(tmp_path, monkeypatch):
    store = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    monkeypatch.setenv("MAVERICK_CONSEQUENCE", "1")

    class _Boom:
        def list_episodes(self, **_):
            raise RuntimeError("db down")

    assert cq.record_self_outcome(_Boom(), 1, 0.0, kind="x", store=store) is False


# ---- outcome correlation (external business key -> episode) -----------------


def test_correlation_link_and_resolve_roundtrip(tmp_path):
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    assert corr.resolve("invoice:INV-1") is None
    assert corr.link("invoice:INV-1", 7, 42) is True
    assert corr.resolve("invoice:INV-1") == (7, 42)


def test_correlation_latest_link_wins(tmp_path):
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    corr.link("ticket:9", 1, 1, ts=10.0)
    corr.link("ticket:9", 2, 2, ts=20.0)   # a later run re-touched the entity
    assert corr.resolve("ticket:9") == (2, 2)


def test_correlation_empty_key_rejected(tmp_path):
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    assert corr.link("   ", 1, 1) is False
    assert corr.count() == 0


def test_correlation_persists_across_reload(tmp_path):
    p = tmp_path / "links.ndjson"
    cq.CorrelationStore(path=p).link("deal:ACME", 3, 5)
    assert cq.CorrelationStore(path=p).resolve("deal:ACME") == (3, 5)


def test_record_outcome_for_key_grounds_when_linked(tmp_path):
    cstore = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    corr.link("invoice:INV-2", 8, 3)
    # reality reports back with only the business key
    assert cq.record_outcome_for_key("invoice:INV-2", 1.0, kind="paid",
                                     store=cstore, corr=corr) is True
    assert cstore.resolve(8, 3) == 1.0


def test_record_outcome_for_key_unmatched_is_false(tmp_path):
    cstore = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    # no run ever linked this key -> nothing grounded
    assert cq.record_outcome_for_key("invoice:UNKNOWN", 1.0, store=cstore, corr=corr) is False


def test_record_outcome_for_key_respects_authorize(tmp_path):
    cstore = cq.ConsequenceStore(path=tmp_path / "c.ndjson")
    corr = cq.CorrelationStore(path=tmp_path / "links.ndjson")
    corr.link("invoice:INV-3", 8, 3)
    # authorize denies -> not recorded
    assert cq.record_outcome_for_key("invoice:INV-3", 1.0, store=cstore, corr=corr,
                                     authorize=lambda g, e: False) is False
    assert cstore.resolve(8, 3) is None
    # authorize allows the resolved target -> recorded
    assert cq.record_outcome_for_key("invoice:INV-3", 1.0, store=cstore, corr=corr,
                                     authorize=lambda g, e: (g, e) == (8, 3)) is True
    assert cstore.resolve(8, 3) == 1.0


def test_reset_shared_reresolves_correlation_path(tmp_path, monkeypatch):
    # reset_shared clears the cached singleton so a later call re-resolves the
    # (tenant/HOME-scoped) path -- the isolation seam tests rely on. The NDJSON
    # itself persists on disk, like the ConsequenceStore.
    first = tmp_path / "a"
    monkeypatch.setattr("maverick.paths.data_dir", lambda *p, **k: first.joinpath(*p))
    cq.reset_shared()
    cq.shared_correlation().link("k", 1, 1)
    assert cq.shared_correlation().resolve("k") == (1, 1)
    # a fresh home + reset -> a distinct, empty store
    second = tmp_path / "b"
    monkeypatch.setattr("maverick.paths.data_dir", lambda *p, **k: second.joinpath(*p))
    cq.reset_shared()
    assert cq.shared_correlation().resolve("k") is None
