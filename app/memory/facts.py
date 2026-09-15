import sqlite3
from datetime import datetime, timezone

from app.store import SqliteStore


class KnowledgeStore(SqliteStore):
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS facts ("
        "scope TEXT NOT NULL, "
        "key TEXT NOT NULL, "
        "value TEXT NOT NULL, "
        "ts TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'stable', "
        # «Когда это пригодится» — по описанию факт попадает в задачу, формулировка
        # которой с ключом не совпадает. Пустое описание легально: старые факты
        # показываются одним ключом, как раньше.
        "description TEXT NOT NULL DEFAULT '', "
        # Сила факта: сколько раз он попадал в адресный recall и когда в последний.
        # По ней сортируется оглавление — неиспользуемое уезжает вниз и вытесняется
        # из бюджета, но остаётся в базе и достаётся точечным запросом.
        "hits INTEGER NOT NULL DEFAULT 0, "
        "last_used TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (scope, key))",
        # Карантин: факт, записанный в задаче, где работал скил с недоверенным выводом
        # (веб, чужой API). Внедрённая в такой текст инструкция из активной памяти
        # попала бы в каждый следующий промпт — поэтому до одобрения владельцем он
        # лежит здесь и в оглавление, recall и консолидацию не попадает (аудит F09).
        # id — версия предложения: новое значение под тем же ключом получает новый id,
        # и старая кнопка его не одобрит. AUTOINCREMENT не даёт переиспользовать id
        # удалённой последней строки.
        "CREATE TABLE IF NOT EXISTS fact_proposals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope TEXT NOT NULL, "
        "key TEXT NOT NULL, "
        "value TEXT NOT NULL, "
        "kind TEXT NOT NULL, "
        "description TEXT NOT NULL, "
        # происхождение: задача, инструмент записи и недоверенный источник
        "run_id TEXT NOT NULL, "
        "tool TEXT NOT NULL, "
        "source TEXT NOT NULL, "
        "ts TEXT NOT NULL, "
        "UNIQUE (scope, key))",
    )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT 'stable'")
        self._add_column(conn, "facts", "description", "TEXT NOT NULL DEFAULT ''")
        self._add_column(conn, "facts", "hits", "INTEGER NOT NULL DEFAULT 0")
        self._add_column(conn, "facts", "last_used", "TEXT NOT NULL DEFAULT ''")
        # До карантина недоверенные факты жили в facts с флагом tainted. Переносим
        # их в предложения без потери текста и убираем колонку — второй запуск её
        # не найдёт, так что повторная миграция ничего не делает.
        if "tainted" in {r[1] for r in conn.execute("PRAGMA table_info(facts)")}:
            conn.execute(
                "INSERT INTO fact_proposals "
                "(scope, key, value, kind, description, run_id, tool, source, ts) "
                "SELECT scope, key, value, kind, description, '', 'remember_fact', "
                "'помечен до карантина', ts FROM facts WHERE tainted = 1"
            )
            conn.execute("DELETE FROM facts WHERE tainted = 1")
            conn.execute("ALTER TABLE facts DROP COLUMN tainted")

    @staticmethod
    def _upsert(conn: sqlite3.Connection, scope: str, key: str, value: str, kind: str,
                description: str) -> None:
        conn.execute(
            "INSERT INTO facts (scope, key, value, ts, kind, description) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, key) DO UPDATE SET "
            "value = excluded.value, ts = excluded.ts, kind = excluded.kind, "
            "description = excluded.description",
            (scope, key, value, datetime.now(timezone.utc).isoformat(), kind, description),
        )

    def remember(self, scope: str, key: str, value: str, kind: str = "stable",
                 description: str = "") -> None:
        with self._connect() as conn:
            self._upsert(conn, scope, key, value, kind, description)

    def propose(self, scope: str, key: str, value: str, *, run_id: str, tool: str,
                source: str, kind: str = "stable", description: str = "") -> int:
        """Положить непроверенный факт в карантин. Активный факт под тем же ключом
        не трогается до одобрения; прежнее предложение заменяется новой версией."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM fact_proposals WHERE scope = ? AND key = ?", (scope, key))
            cur = conn.execute(
                "INSERT INTO fact_proposals "
                "(scope, key, value, kind, description, run_id, tool, source, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, key, value, kind, description, run_id, tool, source, ts),
            )
            return cur.lastrowid

    def proposals(self) -> list[dict]:
        """Карантин на глаза владельцу; `current` — что одобрение заменит."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.id, p.scope, p.key, p.value, p.kind, p.description, p.run_id, "
                "p.tool, p.source, p.ts, f.value FROM fact_proposals p "
                "LEFT JOIN facts f ON f.scope = p.scope AND f.key = p.key ORDER BY p.id"
            ).fetchall()
        cols = ("id", "scope", "key", "value", "kind", "description", "run_id",
                "tool", "source", "ts", "current")
        return [dict(zip(cols, r)) for r in rows]

    def approve(self, proposal_id: int) -> dict | None:
        """Сделать активной ровно эту версию. Одноразово: предложение гасится в той
        же транзакции, повторное или устаревшее одобрение вернёт None."""
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM fact_proposals WHERE id = ? "
                "RETURNING scope, key, value, kind, description", (proposal_id,)
            ).fetchone()
            if row is None:
                return None
            self._upsert(conn, *row)
        return dict(zip(("scope", "key", "value", "kind", "description"), row))

    def reject(self, proposal_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM fact_proposals WHERE id = ?", (proposal_id,))
            return cur.rowcount > 0

    def recall(self, scope: str | None = None, query: str | None = None) -> list[dict]:
        sql = "SELECT scope, key, value, kind, description FROM facts"
        conds: list[str] = []
        params: list[str] = []
        if scope is not None:
            conds.append("scope = ?")
            params.append(scope)
        if query is not None:
            conds.append("(key LIKE ? OR value LIKE ? OR description LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY scope, key"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        facts = [{"scope": s, "key": k, "value": v, "kind": kind, "description": d}
                 for s, k, v, kind, d in rows]
        # Хит засчитываем только адресному запросу: дамп всей памяти одним вызовом
        # поднял бы силу всем фактам разом и стёр разницу между ними.
        if conds:
            self._touch([(f["scope"], f["key"]) for f in facts])
        return facts

    def _touch(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE facts SET hits = hits + 1, last_used = ? WHERE scope = ? AND key = ?",
                [(now, s, k) for s, k in keys],
            )

    def similar(self, scope: str, key: str, text: str, limit: int = 3) -> list[dict]:
        """Факты, похожие на записываемый, кроме него самого.

        `PRIMARY KEY (scope, key)` ловит только буквальный дубль: тот же факт под
        другим именем ключа мирно сосуществует со старым, и дальше непонятно, какой
        верен. Ищем по словам значения и описания — грубо, зато без индекса.
        """
        words = sorted({w for w in text.lower().split() if len(w) >= 5}, key=len, reverse=True)
        if not words:
            return []
        conds = " OR ".join(["(lower(value) LIKE ? OR lower(description) LIKE ?)"] * len(words[:5]))
        params: list[str] = []
        for w in words[:5]:
            params.extend([f"%{w}%"] * 2)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT scope, key, value FROM facts WHERE ({conds}) "
                "AND NOT (scope = ? AND key = ?) ORDER BY hits DESC LIMIT ?",
                (*params, scope, key, limit),
            ).fetchall()
        return [{"scope": s, "key": k, "value": v} for s, k, v in rows]

    def index(self) -> list[dict]:
        """Оглавление памяти: области, а в них факты с описанием, сильные первыми.

        По ключам Директор решает, что уже известно и куда углубляться, не вычитывая
        значения; описание подсказывает, когда факт пригодится, если формулировка
        задачи с ключом не совпадает. Порядок задаёт силу: что не используется,
        уезжает в хвост и первым вылетает за бюджет промпта.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, key, description FROM facts "
                "ORDER BY scope, hits DESC, last_used DESC, ts DESC"
            ).fetchall()
        index: dict[str, list[dict]] = {}
        for scope, key, description in rows:
            index.setdefault(scope, []).append({"key": key, "description": description})
        return [{"scope": s, "facts": f} for s, f in index.items()]

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
