import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class TaskJournal:
    """Журнал завершённых задач Директора: что просили, кто делал, какой ценой.

    Отдельно от audit.log: тот пишет только опасные действия, а поводом
    закристаллизовать метод чаще служат безопасные многошаговые чтения.
    """

    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "id TEXT PRIMARY KEY, "
                "ts TEXT NOT NULL, "
                "chat_id TEXT, "
                "intent TEXT NOT NULL, "
                "agent TEXT, "
                "tool_seq TEXT, "
                "iterations INTEGER, "
                "success INTEGER, "
                "reviewed INTEGER DEFAULT 0)"
            )

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
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, ts, chat_id, intent, agent, tool_seq, iterations, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, ts, chat_id, intent, ",".join(agents),
                 json.dumps(tool_seq, ensure_ascii=False), iterations, int(success)),
            )

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
