import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from agent_memory.store import SqliteStore
from agent_memory.text import stem

# Слова запроса подаём в FTS как строковые литералы через OR: так пользовательский
# текст не может оказаться синтаксисом FTS (NEAR, ^, "), а bm25 ранжирует по числу
# совпавших слов. AND отсекал бы почти всё — формулировки одной задачи редко совпадают.
_WORD = re.compile(r"\w+", re.UNICODE)


def _match_query(query: str) -> str:
    # Токенизатор FTS5 морфологии не знает: основу слова ищем префиксом.
    words = _WORD.findall(query.lower())
    return " OR ".join(
        f'"{stem(w)}"*' if len(w) >= 5 else f'"{w}"' for w in words
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
        # Полный ход последних задач: аргументы вызовов и вывод инструментов
        # целиком, чего в логах нет — там превью в 200 символов.
        "CREATE TABLE IF NOT EXISTS transcripts ("
        "task_id TEXT PRIMARY KEY, ts TEXT NOT NULL, body TEXT NOT NULL)",
    )

    # Цена задачи. Ходы Директора и спавнутых агентов — раздельно: модели у них
    # разные, и без разделения не видно, что дорожает. Старые строки остаются NULL.
    _COST_COLUMNS = (
        ("director_in", "INTEGER"), ("director_out", "INTEGER"),
        ("agents_in", "INTEGER"), ("agents_out", "INTEGER"),
        ("cost", "REAL"), ("llm_calls", "INTEGER"),
        ("tool_calls", "INTEGER"), ("spawns", "INTEGER"),
        ("duration_ms", "INTEGER"),
        # Сколько входа пришло из кэша провайдера — отдельно, у двух моделей он свой.
        ("director_cached", "INTEGER"), ("agents_cached", "INTEGER"),
    )

    # Эпизод задачи: чем кончилась и что по дороге не вышло. Собирает код, а не
    # модель, — поэтому это не второй `success`, который почти всегда единица.
    _EPISODE_COLUMNS = (("outcome", "TEXT"), ("problems", "TEXT"))

    def _migrate(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "tasks", "summary", "TEXT")
        for name, decl in (*self._COST_COLUMNS, *self._EPISODE_COLUMNS):
            self._add_column(conn, "tasks", name, decl)

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
        director_in: int = 0,
        director_out: int = 0,
        agents_in: int = 0,
        agents_out: int = 0,
        cost: float = 0.0,
        llm_calls: int = 0,
        duration_ms: int = 0,
        director_cached: int = 0,
        agents_cached: int = 0,
        outcome: str = "ok",
        problems: list[str] | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, ts, chat_id, intent, agent, tool_seq, iterations, success, summary, "
                "director_in, director_out, agents_in, agents_out, cost, llm_calls, "
                "tool_calls, spawns, duration_ms, director_cached, agents_cached, "
                "outcome, problems) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, ts, chat_id, intent, ",".join(agents),
                 json.dumps(tool_seq, ensure_ascii=False), iterations, int(success), summary,
                 director_in, director_out, agents_in, agents_out, cost, llm_calls,
                 # Число вызовов и спавнов — это длина уже переданных списков,
                 # отдельными аргументами их незачем дублировать.
                 len(tool_seq), len(agents), duration_ms, director_cached, agents_cached,
                 outcome, json.dumps(problems or [], ensure_ascii=False)),
            )
            conn.execute("DELETE FROM tasks_fts WHERE id = ?", (task_id,))
            conn.execute(
                "INSERT INTO tasks_fts (id, intent, summary) VALUES (?, ?, ?)",
                (task_id, intent, summary),
            )

    def search(self, query: str, limit: int = 3) -> list[dict]:
        """Похожие задачи из прошлого, лучшие по bm25.

        Возвращаем интент, итог одной фразой, навыки и эпизод: чем кончилось и что
        по дороге не вышло. Признак `success` не отдаём: на живом журнале он оказался
        единицей в 89 случаях из 90 — ставится по «Директор дошёл до ответа», а не
        «получилось». Задачи старше Ш6 приходят без эпизода — пустых полей не
        придумываем. Трасса инструментов остаётся в журнале, Директору она шум.
        """
        match = _match_query(query)
        if not match:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.intent, f.summary, t.agent, t.outcome, t.problems "
                "FROM tasks_fts f JOIN tasks t ON t.id = f.id "
                "WHERE tasks_fts MATCH ? ORDER BY bm25(tasks_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
        return [
            {"intent": intent, "summary": summary or "",
             "skills": sorted({s for a in (agent or "").split(",") if a
                               for s in a.removeprefix("spawned:").split("+")}),
             **({"outcome": outcome} if outcome else {}),
             **({"problems": found} if (found := json.loads(problems or "[]")) else {})}
            for intent, summary, agent, outcome, problems in rows
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

    def recent_with_summary(self, hours: int) -> list[dict]:
        """Задачи за период в виде «что просили → чем кончилось» плюс эпизод — вход
        консолидации. Без проблем задач она предлагает только факты об инфраструктуре,
        а из повторяющейся неудачи получается урок или запрет. Транскрипт — пустой,
        если задача старше хранимых: из него консолидация берёт отчёты агентов."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.intent, t.summary, t.outcome, t.problems, tr.body FROM tasks t "
                "LEFT JOIN transcripts tr ON tr.task_id = t.id WHERE t.ts >= ? ORDER BY t.ts",
                (since,),
            ).fetchall()
        return [{"intent": intent, "summary": summary or "", "outcome": outcome or "",
                 "problems": json.loads(problems or "[]"), "transcript": body or ""}
                for intent, summary, outcome, problems, body in rows]

    def save_transcript(self, task_id: str, body: str, keep: int) -> None:
        """Сохранить ход задачи, оставив в базе только `keep` последних."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO transcripts (task_id, ts, body) VALUES (?, ?, ?)",
                (task_id, ts, body),
            )
            conn.execute(
                "DELETE FROM transcripts WHERE task_id NOT IN "
                "(SELECT task_id FROM transcripts ORDER BY ts DESC LIMIT ?)",
                (keep,),
            )

    def transcript(self, back: int = 1) -> tuple[str, str] | None:
        """`back`-й с конца транскрипт: (task_id, body). None, если столько нет."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, body FROM transcripts ORDER BY ts DESC LIMIT 1 OFFSET ?",
                (max(back, 1) - 1,),
            ).fetchone()
        return row
