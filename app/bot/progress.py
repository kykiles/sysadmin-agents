"""Живой TODO-лист задачи в Telegram: одно сообщение, которое правится на месте.

Пункты пишет Директор инструментом plan, отметки ставит код: spawn с номером пункта
отмечает его начатым и завершённым, шлюз подтверждений — ждущим решения человека.
Модели отмечать прогресс не нужно — забытая отметка не соврёт о состоянии задачи.
"""
import asyncio
import html
from dataclasses import dataclass, field

from app.logging import get_logger

log = get_logger("progress")


@dataclass
class _Step:
    text: str
    active: int = 0
    waiting: int = 0
    done: bool = False
    failed: bool = False

    def render(self) -> str:
        if self.waiting:
            return f"⏳ {html.escape(self.text)}\n      <i>ждёт вашего подтверждения</i>"
        mark = "⏳" if self.active else "❌" if self.failed else "✅" if self.done else "⬜"
        return f"{mark} {html.escape(self.text)}"


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
    # agent_id -> (run_id, индекс пункта): шлюз знает агента, но не пункт плана
    _agents: dict[str, tuple[str, int]] = field(default_factory=dict)
    # агенты, которым отказали в подтверждении: их пункт не выполнен, как бы они ни завершились
    _refused: set[str] = field(default_factory=set)
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

    def has_step(self, run_id: str, step: int) -> bool:
        board = self._boards.get(run_id)
        return board is not None and 1 <= step <= len(board.steps)

    async def started(self, run_id: str, step: int, agent_id: str) -> None:
        if not self.has_step(run_id, step):
            return
        self._agents[agent_id] = (run_id, step - 1)
        self._boards[run_id].steps[step - 1].active += 1
        await self._show(self._boards[run_id])

    async def finished(self, agent_id: str, success: bool) -> None:
        found = self._find(agent_id)
        if found is None:
            return
        board, s = found
        del self._agents[agent_id]
        success = success and agent_id not in self._refused
        self._refused.discard(agent_id)
        # max: plan мог заменить пункт, пока агент работал
        s.active = max(0, s.active - 1)
        # повторный агент на том же пункте исправляет прошлую неудачу
        s.done, s.failed = (True, False) if success else (s.done, True)
        await self._show(board)

    def refused(self, agent_id: str) -> None:
        if agent_id in self._agents:
            self._refused.add(agent_id)

    async def waiting(self, agent_id: str, on: bool) -> None:
        found = self._find(agent_id)
        if found is None:
            return
        board, s = found
        s.waiting = max(0, s.waiting + (1 if on else -1))
        await self._show(board)

    async def finish(self, run_id: str) -> None:
        """Задача закончилась. Пункт, оставшийся «в работе» (задача упала или её
        отменили), помечается неудачным: часики навсегда вводили бы в заблуждение."""
        board = self._boards.pop(run_id, None)
        self._refused -= {a for a, v in self._agents.items() if v[0] == run_id}
        self._agents = {a: v for a, v in self._agents.items() if v[0] != run_id}
        if board is None:
            return
        for s in board.steps:
            if s.active or s.waiting:
                s.active = s.waiting = 0
                s.failed = True
        await self._show(board)

    def _find(self, agent_id: str) -> tuple[_Board, _Step] | None:
        run_id, index = self._agents.get(agent_id, ("", -1))
        board = self._boards.get(run_id)
        if board is None or index >= len(board.steps):
            return None
        return board, board.steps[index]

    async def _show(self, board: _Board) -> None:
        async with self._lock:
            # текст — под замком: иначе медленная правка затёрла бы более свежую
            text = f"<b>{html.escape(board.title)}</b>\n\n" + "\n".join(s.render() for s in board.steps)
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
