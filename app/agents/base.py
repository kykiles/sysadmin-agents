import asyncio
import copy
import json
import re
import time
import uuid

from pydantic import ValidationError

from app import audit
from app.config import settings
from app.llm.client import ChoiceMessage, LLMClient, Usage
from app.tools.base import Tool, Safety, INTENT_FIELD
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
from app.agents.episode import Episode, error_of
from app.logging import get_logger, redact

log = get_logger("agent")

# LLM часто отдаёт в аргументах сырые \d, \s из regex или \ из путей — JSON такое
# не принимает. Экранируем всё, что не является валидным JSON-escape.
_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu]|u[0-9a-fA-F]{4})')

# Превью безопасного вызова короче: их сотни за день, и для разбора хватает
# аргументов, кода возврата и объёма. У изменяющих превью прежнее — они наперечёт,
# и по ним потом отвечают на вопрос «что именно сделали с сервером».
_SAFE_PREVIEW_CHARS = 300

# Лимит шагов — не повод выбросить собранное: без итогового хода Директор получал от
# агента одну строку «достигнут лимит итераций», а находки всех его вызовов умирали
# вместе с контекстом (аудит 25.09, A2). Ход без вызовов — только сводка.
_WRAP_UP = (
    "Лимит шагов исчерпан, вызовов больше не будет. Подведи итог по тому, что уже "
    "собрано: что выяснил — с конкретикой из вывода инструментов, что сделал, что не "
    "успел и что стоит проверить дальше."
)


def clamp_output(text: str, limit: int) -> str:
    """Режет середину вывода, оставляя начало и хвост.

    Начало нужно, потому что там заголовок и первая ошибка; хвост — потому что
    у логов и длинных команд итог в конце. Середина логов обычно однородна.
    Обрезка касается только того, что уходит в контекст модели: аудит и журнал
    задачи пишут полный вывод до этого места.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n… вырезано {len(text) - limit} символов …\n{text[-tail:]}"


def parse_args(raw: str | None) -> dict:
    raw = raw or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE.sub(r"\\\\", raw))


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: list[Tool],
        llm: LLMClient,
        gateway=None,
        memory=None,
        episode: Episode | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self._llm = llm
        # Шлюз подтверждений: объект с async request(ConfirmationRequest) -> Decision.
        # Без него опасные вызовы отклоняются — агент не может действовать вслепую.
        self._gateway = gateway
        self._memory = memory
        # Что за задачу пошло не так. Директор передаёт сюда свой эпизод при
        # спавне, поэтому проблемы агентов попадают в тот же журнал.
        self._episode = episode or Episode()
        # Чужой эпизод — значит, агент спавнут: его упор в лимит не обрывает задачу.
        self._spawned = episode is not None
        # Имя у двух параллельных спавнов с одинаковыми навыками совпадает — id нет.
        self.agent_id = f"{name}#{uuid.uuid4().hex[:8]}"

    def _find_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def _run_safe(self, run_id: str, tc) -> str:
        started = time.monotonic()
        args: dict = {}
        tool = self._find_tool(tc.function.name)
        if tool is None:
            out = json.dumps({"error": f"unknown tool {tc.function.name}"})
        else:
            try:
                args = parse_args(tc.function.arguments)
            except json.JSONDecodeError as e:
                out = json.dumps({"error": f"invalid tool arguments: {e}"})
            else:
                out = await tool.execute(args)
        self._note(tc.function.name, out)
        await self._audit(run_id, tc.function.name, args, "auto", out, started,
                          limit=_SAFE_PREVIEW_CHARS)
        log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=redact(str(out))[:200])
        return out

    async def _audit(self, run_id: str, tool: str, args: dict, decision: str, out: str,
                     started: float, limit: int = 1000) -> None:
        """Строка следа на каждый вызов — и безопасный тоже.

        Раньше писались только подтверждаемые: за активный день это 3 записи из 413,
        и разбирать, на что агент потратил сотню вызовов, было не по чему — превью в
        docker logs обрезано и живёт до ротации (живой разбор 21.09.2026).
        """
        await audit.record(
            run_id=run_id,
            agent=self.name,
            tool=tool,
            args=args,
            decision=decision,
            ms=round((time.monotonic() - started) * 1000),
            result=audit.outcome(out, limit),
        )

    def _note(self, tool: str, out: str) -> None:
        if (problem := error_of(out)) is not None:
            self._episode.tool_error(tool, problem)
        else:
            self._episode.tool_ok(tool)

    async def _run_dangerous(self, task: Task, tc, tool: Tool, reason: str) -> str:
        started = time.monotonic()
        try:
            raw = parse_args(tc.function.arguments)
        except json.JSONDecodeError as e:
            out = json.dumps({"error": f"invalid tool arguments: {e}"})
            self._note(tool.name, out)
            # Вызова не было, но попытка была: без неё в следе непонятно, почему
            # агент топчется на месте и не доходит до подтверждения.
            await self._audit(task.run_id or task.id, tool.name, {}, "invalid-args", out, started)
            return out
        intent = str(raw.get(INTENT_FIELD, "") or "").strip()
        # Подтверждается и исполняется один снимок: подготовлен до запроса, человеку
        # уходит копия, исполняется оригинал, который из агента не выходил (аудит F04/F05).
        try:
            prepared = tool.prepare(raw)
        except ValidationError as e:
            out = json.dumps({"error": e.errors(include_url=False, include_context=False)},
                             ensure_ascii=False)
            self._note(tool.name, out)
            await self._audit(task.run_id or task.id, tool.name, raw, "invalid-args", out, started)
            return out
        if tool.precheck is not None and (problem := await tool.precheck(prepared)):
            out = json.dumps({"error": problem}, ensure_ascii=False)
            self._note(tool.name, out)
            await self._audit(task.run_id or task.id, tool.name, prepared, "precheck-refused", out, started)
            log.info("tool_call", agent=self.name, tool=tool.name, result_preview=redact(out)[:200])
            return out
        req = ConfirmationRequest(
            run_id=task.run_id or task.id,
            agent_id=self.agent_id,
            tool_call_id=tc.id,
            tool_name=tool.name,
            args=copy.deepcopy(prepared),
            reason=intent or reason,
        )
        log.info("confirmation_required", agent=self.agent_id, tool=tool.name,
                 args=redact(str(prepared)))
        decision = (
            await self._gateway.request(req) if self._gateway is not None
            else Decision.REJECTED
        )
        if decision.approved:
            out = await tool.invoke(prepared)
            self._note(tool.name, out)
        else:
            # Отказ — не ошибка инструмента: он и есть итог, повтор его не исправит.
            self._episode.refusal(req.scope() or tool.name)
            out = json.dumps({"error": (
                "not approved: пользователь отказал, не ответил или запрос не доставлен. "
                "Не повторяй этот вызов — такой же запрос в этой задаче отклоняется без вопроса. "
                "Заверши работу и сообщи, что действие не выполнено."
            )}, ensure_ascii=False)
        await self._audit(task.run_id or task.id, tool.name, prepared, decision.value, out, started)
        log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=redact(str(out))[:200])
        return out

    async def handle(self, task: Task) -> Result:
        history = await asyncio.to_thread(self._memory.load, task.chat_id) if self._memory else []
        messages = [
            {"role": "system", "content": self.system_prompt},
            *history,
            {"role": "user", "content": task.content},
        ]
        # Текст ходов, где модель заодно вызывала инструменты. Она пишет там не только
        # «сейчас посмотрю»: развёрнутый ответ вместе с попутным remember_fact — обычное
        # дело, а следующим ходом идёт «отчёт выше». Выбросить их значит потерять ответ.
        said: list[str] = []
        trace: list[str] = []
        iterations = 0
        usage = Usage()
        for _ in range(settings.agent_max_iterations):
            iterations += 1
            msg = await self._llm.chat(messages, [t.schema() for t in self.tools])
            usage += msg.usage
            if not msg.tool_calls:
                content = redact("\n\n".join([*said, msg.content or ""]).strip())
                if self._memory:
                    await asyncio.to_thread(self._memory.append, task.chat_id, "user", redact(task.content))
                    await asyncio.to_thread(self._memory.append, task.chat_id, "assistant", content)
                return Result(task_id=task.id, content=content,
                              final=redact((msg.content or "").strip()),
                              trace=trace, iterations=iterations, usage=usage,
                              transcript=[*messages, {"role": "assistant", "content": content}])
            if msg.content:
                said.append(msg.content)
            assistant: dict = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            }
            # Без возврата размышлений thinking-модель отвечает 400 на следующий шаг.
            if msg.reasoning_content:
                assistant["reasoning_content"] = msg.reasoning_content
            messages.append(assistant)
            # Безопасные вызовы идут пачкой параллельно (несколько spawn —
            # это несколько агентов одновременно), DANGEROUS — по одному,
            # иначе подтверждения в Telegram столкнутся. Порядок ответов сохраняется.
            outs: list[str] = [""] * len(msg.tool_calls)
            batch: list[int] = []

            async def flush() -> None:
                if not batch:
                    return
                done = await asyncio.gather(
                    *(self._run_safe(task.run_id or task.id, msg.tool_calls[i]) for i in batch),
                    return_exceptions=True,
                )
                for i, out in zip(batch, done):
                    if isinstance(out, BaseException):
                        raise out
                    outs[i] = out
                batch.clear()

            for i, tc in enumerate(msg.tool_calls):
                trace.append(tc.function.name)
                tool = self._find_tool(tc.function.name)
                if tool is not None and tool.safety is Safety.DANGEROUS:
                    await flush()
                    outs[i] = await self._run_dangerous(task, tc, tool, msg.content or "")
                else:
                    batch.append(i)
            await flush()

            for tc, out in zip(msg.tool_calls, outs):
                content = clamp_output(out, settings.tool_output_max_chars)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
        limit = settings.agent_max_iterations
        note = f"достигнут лимит итераций ({limit}), ответ может быть неполным"
        if self._spawned:
            self._episode.gave_up(f"{self.name}: {note}")
        else:
            self._episode.broke(f"{self.name}: {note}")
        summary = ""
        if (wrap := await self._wrap_up(messages)) is not None:
            usage += wrap.usage
            summary = (wrap.content or "").strip()
            messages.append({"role": "user", "content": _WRAP_UP})
        content = redact("\n\n".join([*said, *filter(None, [summary]), note]))
        if self._memory:
            await asyncio.to_thread(self._memory.append, task.chat_id, "user", redact(task.content))
            await asyncio.to_thread(self._memory.append, task.chat_id, "assistant", content)
        return Result(task_id=task.id, content=content, success=False,
                      final=redact(summary) or note, trace=trace, iterations=iterations,
                      usage=usage,
                      transcript=[*messages, {"role": "assistant", "content": content}])

    async def _wrap_up(self, messages: list[dict]) -> ChoiceMessage | None:
        """Итоговый ход без вызовов, когда шаги кончились. Сбой этого хода не должен
        отнять у задачи ответ — тогда остаётся прежняя пометка о лимите."""
        try:
            return await self._llm.chat(
                [*messages, {"role": "user", "content": _WRAP_UP}],
                [t.schema() for t in self.tools], tool_choice="none",
            )
        except Exception:
            log.warning("wrap_up_failed", agent=self.name)
            return None
