import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.learning.detector import CandidateStore
from app.learning.lint import LintState
from app.learning.review import (
    LearningContext, ReviewOutcome, render_review, resolve_candidate, resolve_fact,
    run_review, short_id,
)
from app.llm.client import ChoiceMessage
from app.memory.facts import KnowledgeStore
from app.memory.journal import TaskJournal
from app.monitoring.loop import MonitorConfig, run_tick
from app.monitoring.state import MonitorState


class FakeLLM:
    def __init__(self, groups=None, fail=False):
        self._groups = groups or []
        self._fail = fail

    async def chat(self, messages, tools=None):
        if self._fail:
            raise RuntimeError("llm down")
        return ChoiceMessage(content=json.dumps(self._groups), tool_calls=None)


def _ctx(tmp_path, llm) -> LearningContext:
    tasks_db = str(tmp_path / "tasks.db")
    return LearningContext(
        journal=TaskJournal(tasks_db),
        facts=KnowledgeStore(str(tmp_path / "facts.db")),
        candidates=CandidateStore(tasks_db),
        lint=LintState(tasks_db),
        llm=llm,
    )


def _seed_repeats(ctx, n=3):
    for i in range(n):
        ctx.journal.record(
            task_id=f"t{i}", chat_id="c1", intent="сколько юзеров на инбаунде",
            agents=["remnawave"], tool_seq=["delegate_to", "rw_query"],
            iterations=2, success=True,
        )


def _seed_stale_fact(ctx, days=40):
    ctx.facts.remember("host-a", "ssh_port", "2222", kind="snapshot")
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with ctx.facts._connect() as conn:
        conn.execute("UPDATE facts SET ts = ?", (old,))


# ---------- проход целиком ----------

@pytest.mark.asyncio
async def test_review_finds_both_kinds(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([{"label": "юзеры на инбаунде", "ids": [1, 2, 3]}]))
    _seed_repeats(ctx)
    _seed_stale_fact(ctx)

    outcome = await run_review(ctx)

    assert [c.label for c in outcome.candidates] == ["юзеры на инбаунде"]
    assert [f.key for f in outcome.stale] == ["ssh_port"]
    assert not outcome.is_empty


@pytest.mark.asyncio
async def test_empty_review_when_nothing_to_say(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([]))

    assert (await run_review(ctx)).is_empty


@pytest.mark.asyncio
async def test_detector_failure_does_not_block_lint(tmp_path):
    """Анализы независимы: упавший LLM не должен лишать нас находок lint'а."""
    ctx = _ctx(tmp_path, FakeLLM(fail=True))
    _seed_repeats(ctx)
    _seed_stale_fact(ctx)

    outcome = await run_review(ctx)

    assert outcome.candidates == [] and [f.key for f in outcome.stale] == ["ssh_port"]


@pytest.mark.asyncio
async def test_review_marks_rows_and_candidates_so_second_pass_is_quiet(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([{"label": "юзеры", "ids": [1, 2, 3]}]))
    _seed_repeats(ctx)
    _seed_stale_fact(ctx)

    await run_review(ctx)

    assert (await run_review(ctx)).is_empty


# ---------- рендер ----------

def test_render_omits_empty_section(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM())
    _seed_stale_fact(ctx)
    from app.learning.lint import StaleFact

    text = render_review(ReviewOutcome(
        stale=[StaleFact("host-a", "ssh_port", "2222", "snapshot", 40)]
    ))

    assert "Устаревшие знания" in text and "Повторяющиеся задачи" not in text


def test_render_empty_outcome_is_blank():
    assert render_review(ReviewOutcome()) == ""


# ---------- разрешение коротких id ----------

def test_resolve_candidate_roundtrip(tmp_path):
    store = CandidateStore(str(tmp_path / "tasks.db"))
    from app.learning.detector import Candidate
    cand = Candidate(label="Юзеры  на инбаунде", task_ids=["t1"], repeats=2,
                     median_steps=2, agents=["remnawave"])
    store.mark_proposed([cand])

    assert resolve_candidate(store, short_id(cand.signature)) == cand.signature
    assert resolve_candidate(store, "deadbeef0000") is None


def test_resolve_fact_roundtrip(tmp_path):
    facts = KnowledgeStore(str(tmp_path / "facts.db"))
    facts.remember("host-a", "ssh_port", "2222")

    assert resolve_fact(facts, short_id("host-a", "ssh_port")) == ("host-a", "ssh_port")
    assert resolve_fact(facts, "deadbeef0000") is None


def test_callback_data_fits_telegram_limit():
    """64 байта — жёсткий лимит Telegram: длинный label не должен его пробить."""
    sid = short_id("очень длинное название задачи " * 5)
    assert len(f"lc:{sid}:no".encode()) <= 64


# ---------- тик мониторинга ----------

async def _tick(tmp_path, cfg, learning, tick=0):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    state = MonitorState(str(tmp_path / "mon.db"))
    from app.monitoring import loop as loop_mod
    orig = loop_mod.run_checks

    async def _no_checks(t, c):
        return []

    loop_mod.run_checks = _no_checks
    try:
        await run_tick(FakeLLM(), bot, 1, state, cfg, tick, learning)
    finally:
        loop_mod.run_checks = orig
    return bot


@pytest.mark.asyncio
async def test_learning_is_off_when_every_ticks_is_zero(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([{"label": "юзеры", "ids": [1, 2, 3]}]))
    _seed_repeats(ctx)

    bot = await _tick(tmp_path, MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1,
                                              load_per_cpu=1, learn_every_ticks=0), ctx)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_learning_runs_on_matching_tick(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([{"label": "юзеры", "ids": [1, 2, 3]}]))
    _seed_repeats(ctx)
    cfg = MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1, load_per_cpu=1,
                        learn_every_ticks=2)

    quiet = await _tick(tmp_path, cfg, ctx, tick=1)
    loud = await _tick(tmp_path, cfg, ctx, tick=2)

    quiet.send_message.assert_not_called()
    assert "Повторяющиеся задачи" in loud.send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_silent_when_nothing_found(tmp_path):
    ctx = _ctx(tmp_path, FakeLLM([]))
    cfg = MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1, load_per_cpu=1,
                        learn_every_ticks=1)

    bot = await _tick(tmp_path, cfg, ctx, tick=1)

    bot.send_message.assert_not_called()
