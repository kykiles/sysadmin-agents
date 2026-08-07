from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.learning.lint import LintState, StaleFact
from app.learning.review import (
    LearningContext, ReviewOutcome, render_review, resolve_fact, run_review, short_id,
)
from app.memory.facts import KnowledgeStore
from app.monitoring.loop import MonitorConfig, run_tick
from app.monitoring.state import MonitorState


def _ctx(tmp_path) -> LearningContext:
    return LearningContext(
        facts=KnowledgeStore(str(tmp_path / "facts.db")),
        lint=LintState(str(tmp_path / "tasks.db")),
    )


def _seed_stale_fact(ctx, days=40):
    ctx.facts.remember("host-a", "ssh_port", "2222", kind="snapshot")
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with ctx.facts._connect() as conn:
        conn.execute("UPDATE facts SET ts = ?", (old,))


# ---------- проход целиком ----------

@pytest.mark.asyncio
async def test_review_finds_stale_facts(tmp_path):
    ctx = _ctx(tmp_path)
    _seed_stale_fact(ctx)

    outcome = await run_review(ctx)

    assert [f.key for f in outcome.stale] == ["ssh_port"]
    assert not outcome.is_empty


@pytest.mark.asyncio
async def test_empty_review_when_nothing_to_say(tmp_path):
    assert (await run_review(_ctx(tmp_path))).is_empty


@pytest.mark.asyncio
async def test_second_pass_is_quiet(tmp_path):
    ctx = _ctx(tmp_path)
    _seed_stale_fact(ctx)

    await run_review(ctx)

    assert (await run_review(ctx)).is_empty


# ---------- рендер ----------

def test_render_stale_section():
    text = render_review(ReviewOutcome(
        stale=[StaleFact("host-a", "ssh_port", "2222", "snapshot", 40)]
    ))

    assert "Устаревшие знания" in text and "ssh_port" in text


def test_render_empty_outcome_is_blank():
    assert render_review(ReviewOutcome()) == ""


# ---------- разрешение коротких id ----------

def test_resolve_fact_roundtrip(tmp_path):
    facts = KnowledgeStore(str(tmp_path / "facts.db"))
    facts.remember("host-a", "ssh_port", "2222")

    assert resolve_fact(facts, short_id("host-a", "ssh_port")) == ("host-a", "ssh_port")
    assert resolve_fact(facts, "deadbeef0000") is None


def test_callback_data_fits_telegram_limit():
    """64 байта — жёсткий лимит Telegram: длинный ключ не должен его пробить."""
    sid = short_id("очень длинное название области знаний " * 5)
    assert len(f"lf:{sid}:del".encode()) <= 64


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
        await run_tick(MagicMock(), bot, 1, state, cfg, tick, learning)
    finally:
        loop_mod.run_checks = orig
    return bot


@pytest.mark.asyncio
async def test_learning_is_off_when_every_ticks_is_zero(tmp_path):
    ctx = _ctx(tmp_path)
    _seed_stale_fact(ctx)

    bot = await _tick(tmp_path, MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1,
                                              load_per_cpu=1, learn_every_ticks=0), ctx)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_learning_runs_on_matching_tick(tmp_path):
    ctx = _ctx(tmp_path)
    _seed_stale_fact(ctx)
    cfg = MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1, load_per_cpu=1,
                        learn_every_ticks=2)

    quiet = await _tick(tmp_path, cfg, ctx, tick=1)
    loud = await _tick(tmp_path, cfg, ctx, tick=2)

    quiet.send_message.assert_not_called()
    assert "Устаревшие знания" in loud.send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_silent_when_nothing_found(tmp_path):
    cfg = MonitorConfig(interval=300, disk_pct=90, mem_min_mb=1, load_per_cpu=1,
                        learn_every_ticks=1)

    bot = await _tick(tmp_path, cfg, _ctx(tmp_path), tick=1)

    bot.send_message.assert_not_called()
