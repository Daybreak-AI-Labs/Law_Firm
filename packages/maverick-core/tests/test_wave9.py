"""World-model resource lifecycle regressions retained from the old wave suite."""
from __future__ import annotations

import sqlite3

import pytest


class TestWorldModelClose:
    def test_close_closes_connection(self, tmp_path):
        from maverick.world_model import WorldModel

        wm = WorldModel(tmp_path / "w.db")
        wm.close()
        with pytest.raises(sqlite3.ProgrammingError):
            wm.conn.execute("SELECT 1")

    def test_context_manager_closes(self, tmp_path):
        from maverick.world_model import WorldModel

        with WorldModel(tmp_path / "w.db") as wm:
            wm.create_goal("x", "")
        with pytest.raises(sqlite3.ProgrammingError):
            wm.conn.execute("SELECT 1")
