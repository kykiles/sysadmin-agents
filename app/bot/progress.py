"""Живой TODO-лист задачи в Telegram: одно сообщение, которое правится на месте.

Пункты пишет Директор инструментом plan и раздаёт их агентам в spawn(steps=...).
Выполнение отмечает сам агент (mark_step): он знает, что сделал, а код видит только
«агент закончил» — автоматическая отметка «выполнен» по этому признаку врала после
отказа в подтверждении. Код ставит то, что знает точно: пункт в работе, ждёт
подтверждения, пользователь отказал. В конце задачи неотмеченный пункт — «пропущен».

Вид без эмодзи: пункты пронумерованы, выполненный зачёркнут, остальные состояния —
подписью курсивом после пункта.
"""
import asyncio
import html
from dataclasses import dataclass, field

from app.logging import get_logger

log = get_logger("progress")

_NOTES = {"failed": "не выполнено", "skipped": "пропущен"}


@dataclass
class _Step:
    text: str
    # pending | done | failed | skipped
    status: str = "pending"
    active: int = 0
    waiting: int = 0

    def render(self, number: int) -> str:
        line = f"{number}. {html.escape(self.text)}"
        if self.waiting:
            note = "ждёт вашего подтверждения"
        elif self.status == "done":
            return f"<s>{line}</s>"
        else:
            note = _NOTES.get(self.status) or ("в работе" if self.active else "")
        return f"{line} — <i>{note}</i>" if note else line


@dataclass
class _Board:
    title: str
    steps: list[_Step]
    message_id: int | None = None
    shown: str = ""


@dataclass
class TelegramProgress:
    bot: object
    chat_id: int
    _boards: dict[str, _Board] = field(default_factory=dict)
    # agent_id -> (run_id, индексы его пунктов): шлюз знает агента, но не пункт плана
    _agents: dict[str, tuple[str, list[int]]] = field(default_factory=dict)
    # (agent_id, индекс пункта), где агенту отказали: выполненным этот пункт он
    # отметить не может — действие не выполнено, что бы он ни написал
    _refused: set[tuple[str, int]] = field(default_factory=set)
    # параллельные агенты правят одно сообщение: без замка второй send_message
    # создал бы дубль списка
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def plan(self, run_id: str, title: str, steps: list[str]) -> None:
        """Показать план. Повторный вызов обновляет список; отметки сохраняются
        у пунктов, чей текст на том же месте не изменился."""
        board = self._boards.get(run_id)
        old = board.steps if board else []
        new = [old[i] if i < len(old) and old[i].text == text else _Step(text)
               for i, text in enumerate(steps)]
        if board is None:
            board = self._boards[run_id] = _Board(title, new)
        else:
            board.title, board.steps = title, new
        await self._show(board)

    def steps(self, run_id: str) -> list[str] | None:
        """Тексты пунктов плана; None — плана у задачи нет."""
        board = self._boards.get(run_id)
        return [s.text for s in board.steps] if board else None

    def unmarked(self, agent_id: str) -> list[int]:
        """Номера (с 1) пунктов агента, которые он так и не отметил."""
        run_id, indices = self._agents.get(agent_id, ("", []))
        board = self._boards.get(run_id)
        if board is None:
            return []
        return [i + 1 for i in indices
                if i < len(board.steps) and board.steps[i].status == "pending"]

    async def started(self, run_id: str, steps: list[int], agent_id: str) -> None:
        """steps — номера пунктов с 1."""
        board = self._boards.get(run_id)
        if board is None:
            return
        indices = [n - 1 for n in steps if 1 <= n <= len(board.steps)]
        self._agents[agent_id] = (run_id, indices)
        for i in indices:
            board.steps[i].active += 1
        await self._show(board)

    async def finished(self, agent_id: str) -> None:
        run_id, indices = self._agents.pop(agent_id, ("", []))
        # отметить пункт завершившийся агент уже не может — запрет ему больше не нужен
        self._refused = {(a, i) for a, i in self._refused if a != agent_id}
        board = self._boards.get(run_id)
        if board is None:
            return
        for i in indices:
            if i < len(board.steps):
                # max: plan мог заменить пункт, пока агент работал
                board.steps[i].active = max(0, board.steps[i].active - 1)
        await self._show(board)

    async def mark(self, agent_id: str, step: int, status: str) -> dict:
        """Отметка агента. Ответ уходит агенту результатом mark_step."""
        run_id, indices = self._agents.get(agent_id, ("", []))
        board = self._boards.get(run_id)
        if board is None or step - 1 not in indices or step > len(board.steps):
            return {"error": f"пункт {step} не твой — твои: {[i + 1 for i in indices]}"}
        if status == "done" and (agent_id, step - 1) in self._refused:
            return {"error": "по этому пункту пользователь отказал в подтверждении — "
                             "он не выполнен, отметь failed"}
        board.steps[step - 1].status = status
        await self._show(board)
        return {"marked": step, "status": status}

    async def refused(self, agent_id: str) -> None:
        """Отказ в подтверждении — факт, а не мнение агента: его текущий пункт не выполнен."""
        found = self._current(agent_id)
        if found is None:
            return
        board, i = found
        self._refused.add((agent_id, i))
        board.steps[i].status = "failed"
        await self._show(board)

    async def waiting(self, agent_id: str, on: bool) -> None:
        found = self._current(agent_id)
        if found is None:
            return
        board, i = found
        step = board.steps[i]
        step.waiting = max(0, step.waiting + (1 if on else -1))
        await self._show(board)

    async def finish(self, run_id: str) -> None:
        """Задача закончилась. Неотмеченный пункт: «не выполнено», если на нём остался
        работающий агент (задача упала или её отменили), иначе «пропущен» — его никто
        не выполнил."""
        board = self._boards.pop(run_id, None)
        mine = {a for a, (r, _) in self._agents.items() if r == run_id}
        self._agents = {a: v for a, v in self._agents.items() if a not in mine}
        self._refused = {(a, i) for a, i in self._refused if a not in mine}
        if board is None:
            return
        for s in board.steps:
            if s.status == "pending":
                s.status = "failed" if s.active or s.waiting else "skipped"
            s.active = s.waiting = 0
        await self._show(board)

    def _current(self, agent_id: str) -> tuple[_Board, int] | None:
        """Пункт, над которым агент сейчас работает: пункты он ведёт по порядку,
        значит это первый неотмеченный из его пунктов, а если отмечены все — последний."""
        run_id, indices = self._agents.get(agent_id, ("", []))
        board = self._boards.get(run_id)
        if board is None:
            return None
        indices = [i for i in indices if i < len(board.steps)]
        if not indices:
            return None
        pending = [i for i in indices if board.steps[i].status == "pending"]
        return board, pending[0] if pending else indices[-1]

    async def _show(self, board: _Board) -> None:
        async with self._lock:
            # текст — под замком: иначе медленная правка затёрла бы более свежую
            text = f"<b>{html.escape(board.title)}</b>\n\n" + "\n".join(
                s.render(n) for n, s in enumerate(board.steps, 1))
            if text == board.shown:
                return
            # Список — подсказка, а не часть задачи: сбой Telegram её не роняет.
            try:
                if board.message_id is None:
                    msg = await self.bot.send_message(self.chat_id, text)
                    board.message_id = msg.message_id
                else:
                    await self.bot.edit_message_text(text, chat_id=self.chat_id,
                                                     message_id=board.message_id)
                board.shown = text
            except Exception:
                log.warning("progress_not_shown", message_id=board.message_id)
