import asyncio
from dataclasses import dataclass, field

from app.bot.keyboards import review_markup
from app.bot.render import render_answer
from app.config import settings
from app.learning.review import render_review, run_review
from app.logging import get_logger
from app.monitoring.checks import (
    CheckResult, check_disk, check_memory, check_load,
    check_docker, check_rw_nodes, check_tls,
)
from app.monitoring.state import MonitorState
from app.monitoring.triage import triage

log = get_logger("monitoring.loop")


@dataclass
class MonitorConfig:
    interval: int
    disk_pct: float
    mem_min_mb: int
    load_per_cpu: float
    containers: list[str] = field(default_factory=list)
    remnawave_enabled: bool = False
    tls_endpoints: list[str] = field(default_factory=list)
    tls_warn_days: int = 14
    tls_every_ticks: int = 12
    learn_every_ticks: int = 0


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def config_from_settings() -> MonitorConfig:
    return MonitorConfig(
        interval=settings.monitor_interval_seconds,
        disk_pct=settings.monitor_disk_pct,
        mem_min_mb=settings.monitor_mem_min_mb,
        load_per_cpu=settings.monitor_load_per_cpu,
        containers=_csv(settings.monitor_containers),
        remnawave_enabled=bool(settings.remnawave_base_url),
        tls_endpoints=_csv(settings.monitor_tls_endpoints),
        tls_warn_days=settings.monitor_tls_warn_days,
        tls_every_ticks=settings.monitor_tls_every_ticks,
        learn_every_ticks=settings.learn_every_ticks,
    )


async def run_checks(tick: int, cfg: MonitorConfig) -> list[CheckResult]:
    results = [
        await check_disk(cfg.disk_pct),
        await check_memory(cfg.mem_min_mb),
        await check_load(cfg.load_per_cpu),
    ]
    if cfg.containers:
        results.extend(await check_docker(cfg.containers))
    if cfg.remnawave_enabled:
        results.append(await check_rw_nodes())
    if cfg.tls_endpoints and tick % cfg.tls_every_ticks == 0:
        for ep in cfg.tls_endpoints:
            results.append(await check_tls(ep, cfg.tls_warn_days))
    return results


async def _run_learning(bot, chat_id, learning) -> None:
    outcome = await run_review(learning)
    if outcome.is_empty:
        return
    await bot.send_message(
        chat_id, render_answer(render_review(outcome)), reply_markup=review_markup(outcome)
    )


async def run_tick(llm, bot, chat_id, state: MonitorState, cfg: MonitorConfig, tick: int,
                   learning=None) -> None:
    results = await run_checks(tick, cfg)
    prev = state.load_prev()
    for r in results:
        was_ok = prev.get(r.name, True)
        if was_ok and not r.ok:
            text = await triage(llm, r)
            await bot.send_message(chat_id, render_answer(f"🔴 **Сбой: {r.name}**\n\n{text}"))
        elif not was_ok and r.ok:
            await bot.send_message(
                chat_id,
                render_answer(f"✅ **Восстановлено: {r.name}**\n\n> {r.detail}"),
            )
    state.save(results)
    if learning is not None and cfg.learn_every_ticks > 0 and tick % cfg.learn_every_ticks == 0:
        await _run_learning(bot, chat_id, learning)


async def health_loop(llm, bot, chat_id, state: MonitorState, cfg: MonitorConfig,
                      learning=None) -> None:
    log.info("monitor_start", interval=cfg.interval, containers=cfg.containers,
             tls=cfg.tls_endpoints, remnawave=cfg.remnawave_enabled)
    tick = 0
    while True:
        try:
            await run_tick(llm, bot, chat_id, state, cfg, tick, learning)
        except Exception:
            log.exception("monitor_tick_failed", tick=tick)
        tick += 1
        await asyncio.sleep(cfg.interval)
