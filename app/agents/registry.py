import asyncio
from typing import Protocol
from app.agents.messages import Task, Result, ConfirmationRequest
from app.logging import get_logger

log = get_logger("registry")


class ConfirmationGateway(Protocol):
    async def request(self, req: ConfirmationRequest) -> bool: ...


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, object] = {}
        self._inboxes: dict[str, asyncio.Queue[Task]] = {}
        self._pending: dict[str, asyncio.Future[Result]] = {}
        self._gateway: ConfirmationGateway | None = None
        self._tasks: list[asyncio.Task] = []

    def register(self, agent) -> None:
        self._agents[agent.name] = agent
        self._inboxes[agent.name] = asyncio.Queue()

    def set_confirmation_gateway(self, gw: ConfirmationGateway) -> None:
        self._gateway = gw

    def get_agent(self, name: str):
        return self._agents[name]

    def available_agents(self) -> list[str]:
        return list(self._agents.keys())

    async def request(self, agent_name: str, task: Task) -> Result:
        fut = asyncio.get_running_loop().create_future()
        self._pending[task.id] = fut
        await self._inboxes[agent_name].put(task)
        return await fut

    async def respond(self, result: Result) -> None:
        fut = self._pending.pop(result.task_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    async def confirm(self, req: ConfirmationRequest) -> bool:
        if self._gateway is None:
            return False
        return await self._gateway.request(req)

    async def _consume(self, name: str) -> None:
        agent = self._agents[name]
        inbox = self._inboxes[name]
        while True:
            task = await inbox.get()
            try:
                result = await agent.handle(task)
            except Exception as e:
                log.error("agent_failed", agent=name, error=str(e))
                result = Result(task_id=task.id, content=f"error: {e}", success=False)
            await self.respond(result)

    async def run_forever(self) -> None:
        for name in self._agents:
            self._tasks.append(asyncio.create_task(self._consume(name)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
