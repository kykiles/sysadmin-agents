import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent_memory.consolidate import propose
from agent_memory.facts import KIND_LABELS
from agent_memory.lint import LintState, StaleFact, find_stale
from app.config import settings
from app.logging import get_logger

log = get_logger("learning.review")


def short_id(*parts: str) -> str:
    """Короткий стабильный идентификатор для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:12]


@dataclass
class LearningContext:
    facts: object
    lint: LintState
    llm: object | None = None
    journal: object | None = None
    # Предложения консолидации ждут кнопки в Telegram и потому живут в процессе,
    # а не в базе: не подтверждённое до перезапуска предложение просто вернётся
    # следующим проходом.
    pending: dict[str, dict] = field(default_factory=dict)


@dataclass
class ReviewOutcome:
    stale: list[StaleFact] = field(default_factory=list)
    tainted: list[dict] = field(default_factory=list)
    suggested: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.stale and not self.tainted and not self.suggested


async def run_review(ctx: LearningContext) -> ReviewOutcome:
    """Один проход самопроверки: что давно не подтверждалось и что пришло из чужого текста."""
    stale: list[StaleFact] = []
    try:
        stale = find_stale(
            ctx.facts, ctx.lint,
            stable_days=settings.lint_stale_stable_days,
            snapshot_days=settings.lint_stale_snapshot_days,
            remind_days=settings.lint_remind_days,
            max_items=settings.lint_max_items,
        )
        ctx.lint.mark_reported(stale, datetime.now(timezone.utc))
    except Exception:
        log.exception("lint_pass_failed")

    suggested: list[dict] = []
    if ctx.llm is not None and ctx.journal is not None:
        try:
            suggested = await propose(
                ctx.llm, ctx.journal, ctx.facts,
                hours=settings.consolidate_hours,
                limit=settings.consolidate_max_items,
            )
        except Exception:
            log.exception("consolidate_failed")
    ctx.pending = {short_id(p["scope"], p["key"]): p for p in suggested}
    return ReviewOutcome(stale=stale, tainted=ctx.facts.proposals()[:settings.lint_max_items],
                         suggested=suggested)


def resolve_fact(facts, sid: str) -> tuple[str, str] | None:
    for f in facts.all_live():
        if short_id(f["scope"], f["key"]) == sid:
            return f["scope"], f["key"]
    return None


def render_review(outcome: ReviewOutcome) -> str:
    """Текст сводки в разметке модели — дальше через render_answer, как везде."""
    blocks: list[str] = []
    if outcome.stale:
        lines = ["**Устаревшие знания**", ""]
        for f in outcome.stale:
            lines.append(f"> `{f.scope}/{f.key}` = {f.value} — не проверялось {f.age_days} дн.")
        blocks.append("\n".join(lines))
    if outcome.tainted:
        lines = ["**Ждёт проверки: записано со слов недоверенного источника**", ""]
        for f in outcome.tainted:
            line = f"> `{f['scope']}/{f['key']}` = {f['value']} (источник: {f['source']}"
            if f["current"] is not None:
                line += f"; заменит: {f['current']}"
            lines.append(line + ")")
        blocks.append("\n".join(lines))
    if outcome.suggested:
        lines = ["**Предлагаю запомнить**", ""]
        for f in outcome.suggested:
            label = KIND_LABELS.get(f.get("kind", ""), "")
            lines.append(f"> `{f['scope']}/{f['key']}` = {f['value']}"
                         + (f" ({label})" if label else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
