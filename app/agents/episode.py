"""Эпизод задачи: что пробовали и что не вышло.

Собирает код по фактам выполнения, а не модель по своей памяти: отказы в
подтверждении, ошибки инструментов, невыполненные пункты плана, упор в лимит
итераций и падение. Самоотчётов «всё получилось» здесь нет — на живом журнале
признак `success` оказался единицей в 89 случаях из 90 (ADR 0006).

Один эпизод живёт одну задачу Директора и общий у него со спавнутыми агентами:
для журнала важно, что пошло не так за задачу, а не в каком из агентов.
"""
import json

# Строки уходят в контекст Директора при recall_experience, поэтому эпизод —
# короткая сводка, а не лог: подробности лежат в транскрипте задачи.
MAX_PROBLEMS = 8
_MAX_ERROR_CHARS = 150


def error_of(out: str) -> str | None:
    """Текст ошибки, если результат вызова — это `{"error": ...}`; иначе None."""
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and "error" in data:
        return str(data["error"])[:_MAX_ERROR_CHARS]
    return None


class Episode:
    def __init__(self) -> None:
        self._problems: list[str] = []
        # Ошибка инструмента — ещё не проблема задачи: модель часто правит
        # аргументы и повторяет вызов удачно. Держим её в стороне и выбрасываем,
        # как только тот же инструмент отработал без ошибки.
        self._errors: dict[str, str] = {}
        self._fatal = False

    def _add(self, line: str) -> None:
        if line not in self._problems:
            self._problems.append(line)

    def refusal(self, target: str) -> None:
        self._add(f"отказ в подтверждении: {target}")

    def tool_error(self, tool: str, message: str) -> None:
        self._errors.setdefault(tool, f"ошибка {tool}: {message}")

    def tool_ok(self, tool: str) -> None:
        self._errors.pop(tool, None)

    def plan_left(self, lines: list[str]) -> None:
        for line in lines:
            self._add(line)

    def broke(self, reason: str) -> None:
        """Задача оборвалась: исключение или лимит итераций."""
        self._fatal = True
        self._add(reason)

    def problems(self) -> list[str]:
        return [*self._problems, *self._errors.values()][:MAX_PROBLEMS]

    def outcome(self) -> str:
        if self._fatal:
            return "failed"
        return "partial" if self._problems or self._errors else "ok"
