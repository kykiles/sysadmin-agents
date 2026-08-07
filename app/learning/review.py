import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.learning.lint import LintState, StaleFact, find_stale
from app.logging import get_logger

log = get_logger("learning.review")


def short_id(*parts: str) -> str:
    """Короткий стабильный идентификатор для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:12]


@dataclass
class LearningContext:
    facts: object
    lint: LintState


@dataclass
class ReviewOutcome:
    stale: list[StaleFact] = field(default_factory=list)
    tainted: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.stale and not self.tainted


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
    return ReviewOutcome(stale=stale, tainted=ctx.facts.tainted()[:settings.lint_max_items])


def resolve_fact(facts, sid: str) -> tuple[str, str] | None:
    for f in facts.all_with_ts():
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
        lines = ["**Записано со слов недоверенного источника**", ""]
        for f in outcome.tainted:
            lines.append(f"> `{f['scope']}/{f['key']}` = {f['value']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
