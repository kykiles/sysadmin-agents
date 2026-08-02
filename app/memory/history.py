import sqlite3
from datetime import datetime, timedelta, timezone

from app.store import SqliteStore


class DialogHistory(SqliteStore):
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id TEXT NOT NULL DEFAULT '', "
        "role TEXT NOT NULL, "
        "content TEXT NOT NULL, "
        "ts TEXT NOT NULL)",
    )

    def __init__(self, db_path: str, limit: int, token_budget: int = 4000, retention_days: int = 90):
        self._limit = limit
        self._token_budget = token_budget
        self._retention_days = retention_days
        super().__init__(db_path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "messages", "chat_id", "TEXT NOT NULL DEFAULT ''")

    def append(self, chat_id: str, role: str, content: str) -> None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now.isoformat()),
            )
            if self._retention_days > 0:
                cutoff = (now - timedelta(days=self._retention_days)).isoformat()
                conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))

    def load(self, chat_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, self._limit),
            ).fetchall()
        # rows идут от новых к старым; набираем, пока укладываемся в бюджет токенов
        selected: list[dict] = []
        used = 0
        for role, content in rows:
            used += len(content) // 4 + 4
            if selected and used > self._token_budget:
                break
            selected.append({"role": role, "content": content})
        return list(reversed(selected))

    def clear(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
