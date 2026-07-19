import json
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.logging import get_logger

log = get_logger("learning.detector")

_SYSTEM = (
    "Ты анализируешь журнал задач сисадминского бота. Тебе дают пронумерованный список "
    "запросов пользователя. Сгруппируй их по СМЫСЛУ задачи, а не по совпадению слов: "
    "«сколько юзеров на инбаунде» и «посчитай пользователей инбаунда node-3» — одна группа. "
    "Ответь ТОЛЬКО JSON-массивом вида "
    '[{"label": "краткое название задачи", "ids": [1, 2]}], без пояснений. '
    "Запросы, которые ни с чем не группируются, в ответ не включай."
)


@dataclass
class Candidate:
    label: str
    task_ids: list[str]
    repeats: int
    median_steps: int
    agents: list[str]

    @property
    def signature(self) -> str:
        return re.sub(r"\s+", " ", self.label.strip().lower())


@dataclass
class DetectResult:
    candidates: list[Candidate] = field(default_factory=list)
    considered_ids: list[str] = field(default_factory=list)


class CandidateStore:
    """Что уже предлагали и что человек отклонил. Рядом с журналом задач."""

    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS candidates ("
                "signature TEXT PRIMARY KEY, "
                "label TEXT NOT NULL, "
                "status TEXT NOT NULL, "  # proposed | rejected | done
                "ts TEXT NOT NULL)"
            )

    def known_signatures(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT signature FROM candidates").fetchall()
        return {s for (s,) in rows}

    def mark_proposed(self, candidates: list[Candidate]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO candidates (signature, label, status, ts) "
                "VALUES (?, ?, 'proposed', ?)",
                [(c.signature, c.label, ts) for c in candidates],
            )

    def set_status(self, signature: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE candidates SET status = ? WHERE signature = ?", (status, signature)
            )


def _parse_groups(content: str | None) -> list[dict]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|```$", "", text).strip()
    groups = json.loads(text)
    if not isinstance(groups, list):
        raise ValueError("expected a JSON array")
    return groups


async def detect(
    journal,
    store: CandidateStore,
    llm,
    *,
    window_hours: int,
    min_tasks: int,
    min_repeats: int,
    min_steps: int,
) -> DetectResult:
    """Ищет повторяющиеся методы в журнале. Один LLM-вызов без tools.

    При любом сбое возвращает пусто: промолчать лучше, чем спамить (инвариант 4).
    """
    rows = [r for r in journal.recent(window_hours, unreviewed_only=True) if r["success"]]
    if len(rows) < min_tasks:
        return DetectResult()

    listing = "\n".join(f"{n}. {r['intent']}" for n, r in enumerate(rows, start=1))
    try:
        msg = await llm.chat([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": listing},
        ])
        groups = _parse_groups(msg.content)
    except Exception:
        log.exception("detect_failed", tasks=len(rows))
        return DetectResult()

    known = store.known_signatures()
    candidates: list[Candidate] = []
    for group in groups:
        members = [rows[n - 1] for n in group.get("ids", []) if 1 <= n <= len(rows)]
        if len(members) < min_repeats:
            continue
        median_steps = int(statistics.median(len(m["tool_seq"]) for m in members))
        if median_steps < min_steps:
            continue
        cand = Candidate(
            label=str(group.get("label", "")).strip() or "без названия",
            task_ids=[m["id"] for m in members],
            repeats=len(members),
            median_steps=median_steps,
            agents=sorted({a for m in members for a in m["agents"]}),
        )
        if cand.signature in known:
            continue
        candidates.append(cand)

    return DetectResult(candidates=candidates, considered_ids=[r["id"] for r in rows])
