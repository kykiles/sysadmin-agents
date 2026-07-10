from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.messages import Task, Result
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.tools.base import Tool, Safety


class DelegateToParams(BaseModel):
    agent_name: str = Field(description="name of the specialist agent to delegate to")
    task: str = Field(description="clear, self-contained task description for the specialist")


def build_director_prompt(available_agents: dict[str, str]) -> str:
    agents = "\n".join(f"- {name}: {desc}" for name, desc in available_agents.items())
    return (
        "Ты — Директор команды системных администраторов. Получаешь задачи от пользователя "
        "через Telegram. Твоя роль: понять задачу, при необходимости разбить её и делегировать "
        "подходящему специалисту через инструмент delegate_to. У тебя НЕТ прямого доступа к Docker. "
        "Получив результат от специалиста, сформулируй понятный итоговый отчёт для пользователя на русском. "
        "Если задача тривиальная и не требует специалиста — ответь сразу.\n\n"
        f"Доступные специалисты:\n{agents}"
    )


class Director(Agent):
    def __init__(self, llm: LLMClient, registry: AgentRegistry, available_agents: dict[str, str], memory=None):
        async def _delegate(agent_name: str, task: str) -> dict:
            result = await registry.request(agent_name, Task(content=task))
            return {"agent": agent_name, "result": result.content, "success": result.success}

        delegate_tool = Tool(
            name="delegate_to",
            description="Delegate a task to a specialist agent and receive its result",
            params_model=DelegateToParams,
            fn=_delegate,
            safety=Safety.SAFE,
        )
        super().__init__(
            name="director",
            system_prompt=build_director_prompt(available_agents),
            tools=[delegate_tool],
            llm=llm,
            registry=registry,
            memory=memory,
        )
