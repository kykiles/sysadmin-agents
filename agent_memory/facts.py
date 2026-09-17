import sqlite3
from datetime import datetime, timezone

from agent_memory.store import SqliteStore

# Колонки facts отдельно от CREATE: тем же списком пересобирается таблица старой
# схемы в _migrate — SQLite не умеет менять первичный ключ на месте.
_FACTS_COLUMNS = (
    # id — версия факта. Значение под ключом меняется не затиранием, а закрытием
    # прежней версии: история «было → стало» и есть реконсолидация из ADR 0006.
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "scope TEXT NOT NULL, "
    "key TEXT NOT NULL, "
    "value TEXT NOT NULL, "
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
    # Период действия. valid_until IS NULL — действующая версия, ровно одна на
    # ключ (частичный индекс ниже).
    "valid_from TEXT NOT NULL, "
    "valid_until TEXT, "
    # Сколько раз то же значение записали заново и когда в последний раз. Отсюда
    # lint считает возраст: подтверждённое вчера знание не устарело, даже если
    # записано полгода назад.
    "confirmed INTEGER NOT NULL DEFAULT 0, "
    "confirmed_at TEXT NOT NULL, "
    # Канал записи: director / consolidation / quarantine / legacy. Происхождение
    # по смыслу («сказал пользователь», «вывод инструмента») не храним — это было
    # бы слово модели о самой себе; за смыслом идут в транскрипт задачи.
    "origin TEXT NOT NULL DEFAULT '', "
    "task_id TEXT NOT NULL DEFAULT ''"
)

_FACTS_LIVE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS facts_live ON facts (scope, key) "
    "WHERE valid_until IS NULL"
)


def _norm(value: str) -> str:
    """Значение для сравнения «то же самое или другое»: различие в пробелах —
    не смена факта, а переформулировка модели."""
    return " ".join(value.split())


class KnowledgeStore(SqliteStore):
    SCHEMA = (
        f"CREATE TABLE IF NOT EXISTS facts ({_FACTS_COLUMNS})",
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
        # До периодов действия у факта была одна метка ts и первичный ключ
        # (scope, key). Ключ меняется только пересборкой таблицы; существующие
        # факты становятся действующими версиями, записанными «когда-то».
        if "valid_from" not in {r[1] for r in conn.execute("PRAGMA table_info(facts)")}:
            conn.execute("ALTER TABLE facts RENAME TO facts_old")
            conn.execute(f"CREATE TABLE facts ({_FACTS_COLUMNS})")
            conn.execute(
                "INSERT INTO facts (scope, key, value, kind, description, hits, last_used, "
                "valid_from, confirmed_at, origin) "
                "SELECT scope, key, value, kind, description, hits, last_used, ts, ts, "
                "'legacy' FROM facts_old"
            )
            conn.execute("DROP TABLE facts_old")
        # Индекс не в SCHEMA: пересборка выше уносит индексы вместе со старой таблицей.
        conn.execute(_FACTS_LIVE_INDEX)

    @staticmethod
    def _write(conn: sqlite3.Connection, scope: str, key: str, value: str, kind: str,
               description: str, origin: str, task_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            "SELECT id, value, hits, last_used FROM facts "
            "WHERE scope = ? AND key = ? AND valid_until IS NULL", (scope, key)
        ).fetchone()
        if row is not None and _norm(row[1]) == _norm(value):
            # То же значение записали заново — это подтверждение, а не новая версия:
            # иначе история ключа состояла бы из повторов одного и того же.
            conn.execute(
                "UPDATE facts SET confirmed = confirmed + 1, confirmed_at = ?, "
                "kind = ?, description = ? WHERE id = ?",
                (now, kind, description, row[0]),
            )
            return
        # Сила принадлежит ключу, а не значению: новая версия наследует хиты, иначе
        # самый востребованный факт после уточнения уезжал бы в хвост оглавления.
        hits, last_used = (row[2], row[3]) if row is not None else (0, "")
        if row is not None:
            conn.execute("UPDATE facts SET valid_until = ? WHERE id = ?", (now, row[0]))
        conn.execute(
            "INSERT INTO facts (scope, key, value, kind, description, hits, last_used, "
            "valid_from, confirmed_at, origin, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, key, value, kind, description, hits, last_used, now, now, origin, task_id),
        )

    def remember(self, scope: str, key: str, value: str, kind: str = "stable",
                 description: str = "", *, origin: str = "director",
                 task_id: str = "") -> None:
        with self._connect() as conn:
            self._write(conn, scope, key, value, kind, description, origin, task_id)

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
                "LEFT JOIN facts f ON f.scope = p.scope AND f.key = p.key "
                "AND f.valid_until IS NULL ORDER BY p.id"
            ).fetchall()
        cols = ("id", "scope", "key", "value", "kind", "description", "run_id",
                "tool", "source", "ts", "current")
        return [dict(zip(cols, r)) for r in rows]

    def approve(self, proposal_id: int) -> dict | None:
        """Сделать действующей ровно эту версию. Одноразово: предложение гасится в
        той же транзакции, повторное или устаревшее одобрение вернёт None."""
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM fact_proposals WHERE id = ? "
                "RETURNING scope, key, value, kind, description, run_id", (proposal_id,)
            ).fetchone()
            if row is None:
                return None
            self._write(conn, *row[:5], "quarantine", row[5])
        return dict(zip(("scope", "key", "value", "kind", "description"), row))

    def reject(self, proposal_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM fact_proposals WHERE id = ?", (proposal_id,))
            return cur.rowcount > 0

    def recall(self, scope: str | None = None, query: str | None = None,
               history: bool = False) -> list[dict]:
        sql = ("SELECT scope, key, value, kind, description FROM facts "
               "WHERE valid_until IS NULL")
        conds: list[str] = []
        params: list[str] = []
        if scope is not None:
            conds.append("scope = ?")
            params.append(scope)
        if query is not None:
            conds.append("(key LIKE ? OR value LIKE ? OR description LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if conds:
            sql += " AND " + " AND ".join(conds)
        sql += " ORDER BY scope, key"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        facts = [{"scope": s, "key": k, "value": v, "kind": kind, "description": d}
                 for s, k, v, kind, d in rows]
        # Хит засчитываем только адресному запросу: дамп всей памяти одним вызовом
        # поднял бы силу всем фактам разом и стёр разницу между ними.
        if conds:
            self._touch([(f["scope"], f["key"]) for f in facts])
        if history:
            self._attach_history(facts)
        return facts

    def _attach_history(self, facts: list[dict]) -> None:
        """Прошлые значения ключей с датами — по запросу, а не всегда: в обычном
        recall они были бы шумом, а при разборе «почему так» ими и объясняется,
        что знание сменилось, а не было выдумано."""
        if not facts:
            return
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, key, value, valid_from, valid_until FROM facts "
                "WHERE valid_until IS NOT NULL ORDER BY valid_from"
            ).fetchall()
        past: dict[tuple[str, str], list[dict]] = {}
        for s, k, v, since, until in rows:
            past.setdefault((s, k), []).append({"value": v, "from": since, "until": until})
        for fact in facts:
            versions = past.get((fact["scope"], fact["key"]))
            if versions:
                fact["history"] = versions

    def _touch(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE facts SET hits = hits + 1, last_used = ? "
                "WHERE scope = ? AND key = ? AND valid_until IS NULL",
                [(now, s, k) for s, k in keys],
            )

    def similar(self, scope: str, key: str, text: str, limit: int = 3) -> list[dict]:
        """Факты, похожие на записываемый, кроме него самого.

        Ключ ловит только буквальный дубль: тот же факт под другим именем ключа
        мирно сосуществует со старым, и дальше непонятно, какой верен. Ищем по
        словам значения и описания — грубо, зато без индекса.
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
                f"SELECT scope, key, value FROM facts WHERE valid_until IS NULL AND ({conds}) "
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
                "SELECT scope, key, description FROM facts WHERE valid_until IS NULL "
                "ORDER BY scope, hits DESC, last_used DESC, valid_from DESC"
            ).fetchall()
        index: dict[str, list[dict]] = {}
        for scope, key, description in rows:
            index.setdefault(scope, []).append({"key": key, "description": description})
        return [{"scope": s, "facts": f} for s, f in index.items()]

    def all_live(self) -> list[dict]:
        """Действующие факты вместе с датой последнего подтверждения — для lint'а.
        Инструментам памяти даты не отдаём: агенту они не нужны, а токены стоит
        беречь."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, key, value, kind, confirmed_at FROM facts "
                "WHERE valid_until IS NULL ORDER BY confirmed_at"
            ).fetchall()
        return [
            {"scope": s, "key": k, "value": v, "kind": kind, "confirmed_at": at}
            for s, k, v, kind, at in rows
        ]

    def forget(self, scope: str, key: str) -> None:
        """Забыть ключ целиком, вместе с историей: человек просит убрать знание,
        а не закрыть его текущую версию."""
        with self._connect() as conn:
            conn.execute("DELETE FROM facts WHERE scope = ? AND key = ?", (scope, key))

    def forget_scope(self, scope: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM facts WHERE scope = ?", (scope,))
            return cur.rowcount
