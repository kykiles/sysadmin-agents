"""Общий скелет SQLite-хранилищ.

Шесть хранилищ проекта отличаются таблицами и запросами, а начинались одинаково:
путь, mkdir, соединение, CREATE TABLE IF NOT EXISTS и ручная догонка схемы.
Здесь это один раз; наследник объявляет только SCHEMA и, если нужно, _migrate.
"""
import sqlite3
from pathlib import Path


class SqliteStore:
    SCHEMA: tuple[str, ...] = ()

    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for stmt in self.SCHEMA:
                conn.execute(stmt)
            self._migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Догнать схему в базе, созданной прошлой версией. По умолчанию нечего."""

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
