import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.logging import get_logger

log = get_logger("learning.lint")


@dataclass
class StaleFact:
    scope: str
    key: str
    value: str
    kind: str
    age_days: int


class LintState:
    """Что уже показывали, чтобы не напоминать об одном и том же каждый прогон.

    Живёт в той же БД, что журнал задач: это оперативные данные обучения,
    а не знания (знания — в facts.db).
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
                "CREATE TABLE IF NOT EXISTS lint_seen ("
                "scope TEXT NOT NULL, "
                "key TEXT NOT NULL, "
                "ts_reported TEXT NOT NULL, "
                "PRIMARY KEY (scope, key))"
            )

    def reported_since(self, since: datetime) -> set[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scope, key FROM lint_seen WHERE ts_reported >= ?",
                (since.isoformat(),),
            ).fetchall()
        return {(s, k) for s, k in rows}

    def mark_reported(self, items: list[StaleFact], now: datetime) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO lint_seen (scope, key, ts_reported) VALUES (?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET ts_reported = excluded.ts_reported",
                [(i.scope, i.key, now.isoformat()) for i in items],
            )


def _age_days(ts: str, now: datetime) -> int | None:
    try:
        stored = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return (now - stored).days


def find_stale(
    store,
    state: LintState,
    *,
    stable_days: int,
    snapshot_days: int,
    remind_days: int,
    max_items: int,
    now: datetime | None = None,
) -> list[StaleFact]:
    """Факты, которые давно не подтверждались. Детерминированно, без LLM.

    Ничего не меняет в facts: находки уходят человеку в Telegram.
    """
    now = now or datetime.now(timezone.utc)
    recently_shown = state.reported_since(now - timedelta(days=remind_days))

    stale: list[StaleFact] = []
    for fact in store.all_with_ts():
        if (fact["scope"], fact["key"]) in recently_shown:
            continue
        age = _age_days(fact["ts"], now)
        if age is None:
            log.warning("bad_fact_ts", scope=fact["scope"], key=fact["key"], ts=fact["ts"])
            continue
        limit = snapshot_days if fact["kind"] == "snapshot" else stable_days
        if age >= limit:
            stale.append(StaleFact(
                scope=fact["scope"], key=fact["key"], value=fact["value"],
                kind=fact["kind"], age_days=age,
            ))

    stale.sort(key=lambda f: f.age_days, reverse=True)
    return stale[:max_items]
