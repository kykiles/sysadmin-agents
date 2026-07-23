import asyncio
from typing import Protocol
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
from app.logging import get_logger

log = get_logger("registry")


class ConfirmationGateway(Protocol):
    async def request(self, req: ConfirmationRequest) -> Decision: ...


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, object] = {}
        self._inboxes: dict[str, asyncio.Queue[Task]] = {}
        self._pending: dict[str, asyncio.Future[Result]] = {}
        self._gateway: ConfirmationGateway | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def register(self, agent) -> None:
        self._agents[agent.name] = agent
        self._inboxes.setdefault(agent.name, asyncio.Queue())
        # Агента можно зарегистрировать и на ходу (/reload): если реестр уже работает,
        # поднимаем ему обработчик сразу. Перерегистрация имени обработчик не трогает —
        # _consume берёт агента из реестра на каждой задаче.
        if self._running and agent.name not in self._tasks:
            self._tasks[agent.name] = asyncio.create_task(self._consume(agent.name))

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

    async def confirm(self, req: ConfirmationRequest) -> Decision:
        if self._gateway is None:
            return Decision.REJECTED
        return await self._gateway.request(req)

    async def _consume(self, name: str) -> None:
        inbox = self._inboxes[name]
        while True:
            task = await inbox.get()
            agent = self._agents[name]
            try:
                result = await agent.handle(task)
            except Exception as e:
                log.error("agent_failed", agent=name, error=str(e))
                result = Result(task_id=task.id, content=f"error: {e}", success=False)
            finally:
                if self._gateway is not None and hasattr(self._gateway, "release"):
                    self._gateway.release(task.id)
            await self.respond(result)

    async def run_forever(self) -> None:
        self._running = True
        for name in self._agents:
            self._tasks.setdefault(name, asyncio.create_task(self._consume(name)))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks.values():
            t.cancel()
        for t in self._tasks.values():
            try:
                await t
            except asyncio.CancelledError:
                pass
