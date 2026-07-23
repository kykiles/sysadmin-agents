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
                "kind TEXT NOT NULL DEFAULT 'stable', "
                "PRIMARY KEY (scope, key))"
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
            if "kind" not in cols:
                conn.execute("ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'stable'")

    def remember(self, scope: str, key: str, value: str, kind: str = "stable") -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO facts (scope, key, value, ts, kind) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET "
                "value = excluded.value, ts = excluded.ts, kind = excluded.kind",
                (scope, key, value, ts, kind),
            )

    def recall(self, scope: str | None = None, query: str | None = None) -> list[dict]:
        sql = "SELECT scope, key, value, kind FROM facts"
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
        return [{"scope": s, "key": k, "value": v, "kind": kind} for s, k, v, kind in rows]

    def scopes(self) -> list[dict]:
        """Оглавление памяти: области и сколько в каждой фактов. «Верхушка айсберга» —
        по ней Директор решает, куда углубляться, не вычитывая факты целиком."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, COUNT(*) FROM facts GROUP BY scope ORDER BY scope"
            ).fetchall()
        return [{"scope": s, "facts": n} for s, n in rows]

    def all_with_ts(self) -> list[dict]:
        """Все факты вместе с меткой времени — для lint'а. Инструментам памяти `ts`
        не отдаём: агенту он не нужен, а токены стоит беречь."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, key, value, kind, ts FROM facts ORDER BY ts"
            ).fetchall()
        return [
            {"scope": s, "key": k, "value": v, "kind": kind, "ts": ts}
            for s, k, v, kind, ts in rows
        ]

    def forget(self, scope: str, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM facts WHERE scope = ? AND key = ?", (scope, key))

    def forget_scope(self, scope: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM facts WHERE scope = ?", (scope,))
            return cur.rowcount


_store: KnowledgeStore | None = None


def init_store(db_path: str) -> None:
    global _store
    _store = KnowledgeStore(db_path)


def get_store() -> KnowledgeStore:
    if _store is None:
        raise RuntimeError("KnowledgeStore не инициализирован — вызови init_store()")
    return _store
