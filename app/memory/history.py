import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class DialogHistory:
    def __init__(self, db_path: str, limit: int):
        self._path = db_path
        self._limit = limit
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "role TEXT NOT NULL, "
                "content TEXT NOT NULL, "
                "ts TEXT NOT NULL)"
            )

    def append(self, role: str, content: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
                (role, content, ts),
            )

    def load(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?",
                (self._limit,),
            ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages")
