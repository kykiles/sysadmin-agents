import json

import pytest

from app.learning.detector import CandidateStore, detect
from app.llm.client import ChoiceMessage
from app.memory.journal import TaskJournal

DEFAULTS = dict(window_hours=168, min_tasks=3, min_repeats=2, min_steps=2)


class FakeLLM:
    """Отдаёт заготовленный ответ и запоминает, звали ли его вообще."""

    def __init__(self, content):
        self._content = content
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        if isinstance(self._content, Exception):
            raise self._content
        return ChoiceMessage(content=self._content, tool_calls=None)


def _groups(*groups) -> str:
    return json.dumps(groups)


def _setup(tmp_path):
    return TaskJournal(str(tmp_path / "tasks.db")), CandidateStore(str(tmp_path / "tasks.db"))


def _record(journal, task_id, intent, steps=2, success=True, agents=("remnawave",)):
    journal.record(
        task_id=task_id, chat_id="c1", intent=intent, agents=list(agents),
        tool_seq=["delegate_to"] + ["rw_query"] * (steps - 1),
        iterations=steps, success=success,
    )


async def _detect(journal, store, llm, **over):
    return await detect(journal, store, llm, **{**DEFAULTS, **over})


@pytest.mark.asyncio
async def test_repeated_intent_becomes_candidate(tmp_path):
    journal, store = _setup(tmp_path)
    _record(journal, "t1", "сколько юзеров на инбаунде")
    _record(journal, "t2", "посчитай пользователей инбаунда node-3")
    _record(journal, "t3", "перезапусти nginx")
    llm = FakeLLM(_groups({"label": "юзеры на инбаунде", "ids": [1, 2]}))

    result = await _detect(journal, store, llm)

    assert [(c.label, c.repeats, c.median_steps) for c in result.candidates] == [
        ("юзеры на инбаунде", 2, 2)
    ]
    assert result.candidates[0].task_ids == ["t1", "t2"]


@pytest.mark.asyncio
async def test_llm_not_called_below_min_tasks(tmp_path):
    journal, store = _setup(tmp_path)
    _record(journal, "t1", "сколько юзеров")
    _record(journal, "t2", "сколько юзеров")
    llm = FakeLLM(_groups({"label": "x", "ids": [1, 2]}))

    result = await _detect(journal, store, llm)

    assert result.candidates == [] and llm.calls == 0


@pytest.mark.asyncio
async def test_group_below_min_repeats_is_dropped(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "разное")
    llm = FakeLLM(_groups({"label": "одиночка", "ids": [1]}))

    assert (await _detect(journal, store, llm)).candidates == []


@pytest.mark.asyncio
async def test_short_method_is_dropped(tmp_path):
    """Одношаговая задача не стоит кристаллизации — скрипт ничего не сэкономит."""
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "статус контейнеров", steps=1)
    llm = FakeLLM(_groups({"label": "статус", "ids": [1, 2, 3]}))

    assert (await _detect(journal, store, llm)).candidates == []


@pytest.mark.asyncio
async def test_failed_tasks_are_ignored(tmp_path):
    journal, store = _setup(tmp_path)
    _record(journal, "t1", "сколько юзеров")
    _record(journal, "t2", "сколько юзеров", success=False)
    _record(journal, "t3", "сколько юзеров", success=False)
    llm = FakeLLM(_groups({"label": "юзеры", "ids": [1, 2, 3]}))

    result = await _detect(journal, store, llm)

    assert result.candidates == [] and llm.calls == 0


@pytest.mark.asyncio
async def test_already_proposed_candidate_is_not_repeated(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")
    llm = FakeLLM(_groups({"label": "Юзеры  на инбаунде", "ids": [1, 2, 3]}))
    first = await _detect(journal, store, llm)
    store.mark_proposed(first.candidates)

    # тот же смысл, но другой регистр и пробелы — сигнатура нормализована
    llm2 = FakeLLM(_groups({"label": "юзеры на инбаунде", "ids": [1, 2, 3]}))
    assert (await _detect(journal, store, llm2)).candidates == []


@pytest.mark.asyncio
async def test_rejected_candidate_is_not_proposed_again(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")
    llm = FakeLLM(_groups({"label": "юзеры", "ids": [1, 2, 3]}))
    first = await _detect(journal, store, llm)
    store.mark_proposed(first.candidates)
    store.set_status("юзеры", "rejected")

    llm2 = FakeLLM(_groups({"label": "юзеры", "ids": [1, 2, 3]}))
    assert (await _detect(journal, store, llm2)).candidates == []


@pytest.mark.asyncio
async def test_reviewed_rows_are_excluded_on_next_pass(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")
    llm = FakeLLM(_groups({"label": "юзеры", "ids": [1, 2, 3]}))
    result = await _detect(journal, store, llm)
    journal.mark_reviewed(result.considered_ids)

    llm2 = FakeLLM(_groups({"label": "другое", "ids": [1, 2, 3]}))
    assert (await _detect(journal, store, llm2)).candidates == [] and llm2.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["не json", "{}", "```json\nсломано\n```", ""])
async def test_broken_llm_answer_yields_nothing(tmp_path, bad):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")

    assert (await _detect(journal, store, FakeLLM(bad))).candidates == []


@pytest.mark.asyncio
async def test_llm_failure_yields_nothing(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")

    result = await _detect(journal, store, FakeLLM(RuntimeError("llm down")))

    assert result.candidates == [] and result.considered_ids == []


@pytest.mark.asyncio
async def test_fenced_json_is_parsed(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")
    fenced = "```json\n" + _groups({"label": "юзеры", "ids": [1, 2, 3]}) + "\n```"

    assert [c.repeats for c in (await _detect(journal, store, FakeLLM(fenced))).candidates] == [3]


@pytest.mark.asyncio
async def test_out_of_range_ids_are_ignored(tmp_path):
    journal, store = _setup(tmp_path)
    for i in range(3):
        _record(journal, f"t{i}", "сколько юзеров")
    llm = FakeLLM(_groups({"label": "юзеры", "ids": [1, 2, 99]}))

    assert [c.repeats for c in (await _detect(journal, store, llm)).candidates] == [2]
