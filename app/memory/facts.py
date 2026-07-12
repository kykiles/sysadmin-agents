import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class KnowledgeStore:
    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS facts ("
                "scope TEXT NOT NULL, "
                "key TEXT NOT NULL, "
                "value TEXT NOT NULL, "
                "ts TEXT NOT NULL, "
                "PRIMARY KEY (scope, key))"
            )

    def remember(self, scope: str, key: str, value: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO facts (scope, key, value, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
                (scope, key, value, ts),
            )

    def recall(self, scope: str | None = None, query: str | None = None) -> list[dict]:
        sql = "SELECT scope, key, value FROM facts"
        conds: list[str] = []
        params: list[str] = []
        if scope is not None:
            conds.append("scope = ?")
            params.append(scope)
        if query is not None:
            conds.append("(key LIKE ? OR value LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY scope, key"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"scope": s, "key": k, "value": v} for s, k, v in rows]

    def forget(self, scope: str, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM facts WHERE scope = ? AND key = ?", (scope, key))


_store: KnowledgeStore | None = None


def init_store(db_path: str) -> None:
    global _store
    _store = KnowledgeStore(db_path)


def get_store() -> KnowledgeStore:
    if _store is None:
        raise RuntimeError("KnowledgeStore не инициализирован — вызови init_store()")
    return _store
