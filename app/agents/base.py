import asyncio
import json
from app import audit
from app.config import settings
from app.llm.client import LLMClient
from app.tools.base import Tool, Safety, INTENT_FIELD
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
from app.agents.registry import AgentRegistry
from app.logging import get_logger

log = get_logger("agent")


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: list[Tool],
        llm: LLMClient,
        registry: AgentRegistry,
        memory=None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self._llm = llm
        self._registry = registry
        self._memory = memory

    def _find_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def _run_safe(self, tc) -> str:
        tool = self._find_tool(tc.function.name)
        if tool is None:
            out = json.dumps({"error": f"unknown tool {tc.function.name}"})
        else:
            out = await tool.execute(json.loads(tc.function.arguments or "{}"))
        log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=str(out)[:200])
        return out

    async def _run_dangerous(self, task: Task, tc, tool: Tool, reason: str) -> str:
        args = json.loads(tc.function.arguments or "{}")
        intent = str(args.pop(INTENT_FIELD, "") or "").strip()
        req = ConfirmationRequest(
            task_id=task.id,
            tool_name=tool.name,
            args=args,
            description=f"{tool.name} {args}",
            reason=intent or reason,
        )
        log.info("confirmation_required", agent=self.name, tool=tool.name, args=args)
        decision = await self._registry.confirm(req)
        if decision is Decision.REJECTED:
            out = json.dumps({"error": "rejected by user"})
        else:
            out = await tool.execute(args)
        await audit.record(
            agent=self.name,
            tool=tool.name,
            args=args,
            decision=decision.value,
            result=audit.outcome(out),
        )
        log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=str(out)[:200])
        return out

    async def handle(self, task: Task) -> Result:
        history = await asyncio.to_thread(self._memory.load, task.chat_id) if self._memory else []
        messages = [
            {"role": "system", "content": self.system_prompt},
            *history,
            {"role": "user", "content": task.content},
        ]
        last_content = ""
        trace: list[str] = []
        iterations = 0
        for _ in range(settings.agent_max_iterations):
            iterations += 1
            msg = await self._llm.chat(messages, [t.schema() for t in self.tools])
            if msg.content:
                last_content = msg.content
            if not msg.tool_calls:
                if self._memory:
                    await asyncio.to_thread(self._memory.append, task.chat_id, "user", task.content)
                    await asyncio.to_thread(self._memory.append, task.chat_id, "assistant", msg.content or "")
                return Result(task_id=task.id, content=msg.content or "",
                              trace=trace, iterations=iterations)
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            # Безопасные вызовы идут пачкой параллельно (несколько spawn —
            # это несколько агентов одновременно), DANGEROUS — по одному,
            # иначе подтверждения в Telegram столкнутся. Порядок ответов сохраняется.
            outs: list[str] = [""] * len(msg.tool_calls)
            batch: list[int] = []

            async def flush() -> None:
                if not batch:
                    return
                done = await asyncio.gather(
                    *(self._run_safe(msg.tool_calls[i]) for i in batch),
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
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        limit = settings.agent_max_iterations
        note = f"достигнут лимит итераций ({limit}), ответ может быть неполным"
        content = f"{last_content}\n\n⚠️ {note}" if last_content else note
        if self._memory:
            await asyncio.to_thread(self._memory.append, task.chat_id, "user", task.content)
            await asyncio.to_thread(self._memory.append, task.chat_id, "assistant", content)
        return Result(task_id=task.id, content=content, success=False,
                      trace=trace, iterations=iterations)
