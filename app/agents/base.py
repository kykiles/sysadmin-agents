import json
from app.config import settings
from app.llm.client import LLMClient
from app.tools.base import Tool, Safety
from app.agents.messages import Task, Result, ConfirmationRequest
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
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self._llm = llm
        self._registry = registry

    def _find_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def handle(self, task: Task) -> Result:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task.content},
        ]
        for _ in range(settings.agent_max_iterations):
            msg = await self._llm.chat(messages, [t.schema() for t in self.tools])
            if not msg.tool_calls:
                return Result(task_id=task.id, content=msg.content or "")
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                tool = self._find_tool(tc.function.name)
                if tool is None:
                    out = json.dumps({"error": f"unknown tool {tc.function.name}"})
                else:
                    args = json.loads(tc.function.arguments or "{}")
                    if tool.safety is Safety.DANGEROUS:
                        req = ConfirmationRequest(
                            task_id=task.id,
                            tool_name=tool.name,
                            args=args,
                            description=f"{tool.name} {args}",
                        )
                        log.info("confirmation_required", agent=self.name, tool=tool.name, args=args)
                        ok = await self._registry.confirm(req)
                        if not ok:
                            out = json.dumps({"error": "rejected by user"})
                        else:
                            out = await tool.execute(args)
                    else:
                        out = await tool.execute(args)
                log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=str(out)[:200])
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        return Result(task_id=task.id, content="max iterations reached", success=False)
