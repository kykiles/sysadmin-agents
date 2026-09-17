"""Перенос памяти из старых баз в общую `memory.db`.

Факты и карантин жили в `dialog.db` рядом с историей диалога, журнал, транскрипты
и `lint_seen` — в `tasks.db`. Пакет `agent_memory` про эти пути не знает, поэтому
перенос — дело адаптера.

Идемпотентно и по таблицам: копируем таблицу, только если в новой базе она пуста,
а в старой есть строки. Старые базы не трогаем — их удаляют руками, убедившись,
что перенос удался.
"""
import sqlite3
from pathlib import Path

from app.logging import get_logger

log = get_logger("memory.migrate")

# Прежний JOURNAL_DB_PATH настройкой больше не задаётся: журнал лежал рядом с
# dialog.db, оттуда и берём.
_LEGACY_JOURNAL = "tasks.db"

_FROM_DIALOG = ("facts", "fact_proposals")
_FROM_JOURNAL = ("tasks", "transcripts", "lint_seen")


def migrate_legacy(memory_db: str, *, dialog_db: str) -> dict[str, int]:
    """Перелить старые базы в `memory_db`. Возвращает {таблица: скопировано строк}.

    Таблицы назначения должны уже существовать — хранилища создаются раньше вызова.
    """
    moved: dict[str, int] = {}
    journal_db = str(Path(dialog_db).with_name(_LEGACY_JOURNAL))
    for old, tables in ((dialog_db, _FROM_DIALOG), (journal_db, _FROM_JOURNAL)):
        if not Path(old).exists():
            continue
        conn = sqlite3.connect(memory_db, isolation_level=None)
        try:
            conn.execute("ATTACH DATABASE ? AS old", (old,))
            for table in tables:
                copied = _copy(conn, table)
                if copied:
                    moved[table] = copied
            conn.execute("DETACH DATABASE old")
        finally:
            conn.close()

    with sqlite3.connect(memory_db) as conn:
        rebuilt = _rebuild_fts(conn)
    if rebuilt:
        moved["tasks_fts"] = rebuilt
    if moved:
        log.info("memory_migrated", **moved)
    return moved


def _columns(conn: sqlite3.Connection, db: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {db}.table_info({table})")]


def _copy(conn: sqlite3.Connection, table: str) -> int:
    """Скопировать таблицу целиком, если в новой базе её ещё нет данных.

    Колонки перечисляем по пересечению: старая база могла не дожить до колонок,
    добавленных `_add_column`, а `SELECT *` завязался бы на их порядок.
    """
    dst = _columns(conn, "main", table)
    src = _columns(conn, "old", table)
    if not dst or not src:
        return 0
    if conn.execute(f"SELECT 1 FROM main.{table} LIMIT 1").fetchone():
        return 0
    cols = ", ".join(c for c in dst if c in src)
    conn.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM old.{table}")
    return conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]


def _rebuild_fts(conn: sqlite3.Connection) -> int:
    """Собрать `tasks_fts` из `tasks`, если поиск по задачам пуст, а задачи есть.

    FTS — производная таблица, и копировать её вместе с остальными незачем.
    Проверка от состояния, а не от факта копирования: прерванный перенос иначе
    оставил бы задачи без поиска навсегда — `recall_experience` молча ничего бы
    не находил.
    """
    if not _columns(conn, "main", "tasks_fts"):
        return 0
    if conn.execute("SELECT 1 FROM tasks_fts LIMIT 1").fetchone():
        return 0
    cur = conn.execute(
        "INSERT INTO tasks_fts (id, intent, summary) "
        "SELECT id, intent, coalesce(summary, '') FROM tasks"
    )
    return cur.rowcount
