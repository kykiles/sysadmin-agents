import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from app.store import SqliteStore

# Слова запроса подаём в FTS как строковые литералы через OR: так пользовательский
# текст не может оказаться синтаксисом FTS (NEAR, ^, "), а bm25 ранжирует по числу
# совпавших слов. AND отсекал бы почти всё — формулировки одной задачи редко совпадают.
_WORD = re.compile(r"\w+", re.UNICODE)


def _stem(word: str) -> str:
    """Отрезать окончание, чтобы «инбаунде» нашло «инбаунда».

    Токенизатор FTS5 морфологии не знает, а по-русски одну задачу дважды одинаково
    не формулируют. Режем два последних символа (но не короче четырёх) и ищем по
    префиксу.
    ponytail: наивное отсечение, даёт ложные совпадения на коротких словах;
    snowballstemmer, если точность станет мешать.
    """
    return word if len(word) < 5 else word[: max(4, len(word) - 2)]


def _match_query(query: str) -> str:
    words = _WORD.findall(query.lower())
    return " OR ".join(
        f'"{_stem(w)}"*' if len(w) >= 5 else f'"{w}"' for w in words
    )


class TaskJournal(SqliteStore):
    """Журнал завершённых задач Директора: что просили, чем кончилось, какой ценой.

    Отдельно от audit.log: тот пишет только опасные действия, а поводом
    закристаллизовать метод чаще служат безопасные многошаговые чтения.
    """

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id TEXT PRIMARY KEY, "
        "ts TEXT NOT NULL, "
        "chat_id TEXT, "
        "intent TEXT NOT NULL, "
        "agent TEXT, "
        "tool_seq TEXT, "
        "iterations INTEGER, "
        "success INTEGER, "
        "summary TEXT, "
        "reviewed INTEGER DEFAULT 0)",
        # Эпизодический поиск: FTS5 с встроенным bm25() вместо внешней библиотеки
        # и плоского файла, который пришлось бы переиндексировать целиком.
        "CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5("
        "id UNINDEXED, intent, summary)",
    )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "tasks", "summary", "TEXT")

    def record(
        self,
        *,
        task_id: str,
        chat_id: str,
        intent: str,
        agents: list[str],
        tool_seq: list[str],
        iterations: int,
        success: bool,
        summary: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, ts, chat_id, intent, agent, tool_seq, iterations, success, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, ts, chat_id, intent, ",".join(agents),
                 json.dumps(tool_seq, ensure_ascii=False), iterations, int(success), summary),
            )
            conn.execute("DELETE FROM tasks_fts WHERE id = ?", (task_id,))
            conn.execute(
                "INSERT INTO tasks_fts (id, intent, summary) VALUES (?, ?, ?)",
                (task_id, intent, summary),
            )

    def search(self, query: str, limit: int = 3) -> list[dict]:
        """Похожие задачи из прошлого, лучшие по bm25.

        Возвращаем интент, итог одной фразой и навыки. Признак успеха не отдаём:
        на живом журнале он оказался единицей в 89 случаях из 90 — ставится по
        «Директор дошёл до ответа», а не «получилось», и как сигнал бесполезен.
        Трасса инструментов остаётся в журнале, Директору она шум.
        """
        match = _match_query(query)
        if not match:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.id, f.intent, f.summary, t.agent, t.success "
                "FROM tasks_fts f JOIN tasks t ON t.id = f.id "
                "WHERE tasks_fts MATCH ? ORDER BY bm25(tasks_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
        return [
            {"intent": intent, "summary": summary or "",
             "skills": sorted({s for a in (agent or "").split(",") if a
                               for s in a.removeprefix("spawned:").split("+")})}
            for _i, intent, summary, agent, _ok in rows
        ]

    def recent(self, hours: int) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, intent, agent, tool_seq, iterations, success FROM tasks "
                "WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [
            {"id": i, "intent": intent, "agents": [a for a in (agent or "").split(",") if a],
             "tool_seq": json.loads(seq), "iterations": it, "success": bool(ok)}
            for i, intent, agent, seq, it, ok in rows
        ]
