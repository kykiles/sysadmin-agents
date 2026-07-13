import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MonitorState:
    """Хранит последнее ok/fail каждой проверки, чтобы алертить только на смену
    состояния (edge-triggered) и не спамить после рестарта бота."""

    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS check_state ("
                "name TEXT PRIMARY KEY, "
                "ok INTEGER NOT NULL, "
                "ts TEXT NOT NULL)"
            )

    def load_prev(self) -> dict[str, bool]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name, ok FROM check_state").fetchall()
        return {name: bool(ok) for name, ok in rows}

    def save(self, results: list) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for r in results:
                conn.execute(
                    "INSERT INTO check_state (name, ok, ts) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET ok=excluded.ok, ts=excluded.ts",
                    (r.name, 1 if r.ok else 0, now),
                )
