"""Cross-process/in-process locking contract for the dashboard settings
overlay mutators. set_channel/clear_channel do a read-modify-write of the
single dashboard-config.toml file and MUST hold _locked() across it, exactly
like set_provider/clear_provider/set_toggle -- otherwise two concurrent saves
read the same base snapshot and the second _write() clobbers the first,
silently dropping a freshly saved credential (lost update)."""
from __future__ import annotations


def _lock_held_during(monkeypatch, call) -> bool:
    """Report whether ``call`` holds the settings lock for its complete RMW.

    Mutators intentionally use the strict ``_load_overlay_for_update`` reader,
    rather than the forgiving public ``load_overlay`` reader, so an unreadable
    overlay cannot be replaced and lose unrelated settings. Instrument both
    that read and the write: ``_locked`` acquires the non-reentrant
    ``_SETTINGS_LOCK``, so a non-blocking acquire fails at each boundary iff
    the mutator still holds the lock.
    """
    from maverick_dashboard import settings_store

    held: list[bool] = []
    real_load = settings_store._load_overlay_for_update
    real_write = settings_store._write

    def record_lock_state() -> None:
        acquired = settings_store._SETTINGS_LOCK.acquire(blocking=False)
        if acquired:
            settings_store._SETTINGS_LOCK.release()
        held.append(not acquired)

    def probing_load():
        record_lock_state()
        return real_load()

    def probing_write(data):
        record_lock_state()
        return real_write(data)

    monkeypatch.setattr(settings_store, "_load_overlay_for_update", probing_load)
    monkeypatch.setattr(settings_store, "_write", probing_write)
    call(settings_store)
    return len(held) == 2 and all(held)


