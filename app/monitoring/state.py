import sqlite3
from datetime import datetime, timezone

from app.store import SqliteStore


class MonitorState(SqliteStore):
    """Хранит объявленное ok/fail каждой проверки и число подряд идущих провалов,
    чтобы алертить только на смену состояния (edge-triggered), не спамить после
    рестарта бота и не реагировать на одиночную аномальную выборку."""

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS check_state ("
        "name TEXT PRIMARY KEY, "
        "ok INTEGER NOT NULL, "
        "ts TEXT NOT NULL)",
    )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "check_state", "fails", "INTEGER NOT NULL DEFAULT 0")

    def load_prev(self) -> dict[str, tuple[bool, int]]:
        """name -> (объявленное состояние, число подряд идущих провалов)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT name, ok, fails FROM check_state").fetchall()
        return {name: (bool(ok), fails) for name, ok, fails in rows}

    def save(self, state: dict[str, tuple[bool, int]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for name, (ok, fails) in state.items():
                conn.execute(
                    "INSERT INTO check_state (name, ok, ts, fails) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "ok=excluded.ok, ts=excluded.ts, fails=excluded.fails",
                    (name, 1 if ok else 0, now, fails),
                )
