import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.learning.detector import Candidate, CandidateStore, detect
from app.learning.lint import LintState, StaleFact, find_stale
from app.logging import get_logger

log = get_logger("learning.review")


def short_id(*parts: str) -> str:
    """Короткий стабильный идентификатор для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:12]


@dataclass
class LearningContext:
    journal: object
    facts: object
    candidates: CandidateStore
    lint: LintState
    llm: object


@dataclass
class ReviewOutcome:
    candidates: list[Candidate] = field(default_factory=list)
    stale: list[StaleFact] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.candidates and not self.stale


async def run_review(ctx: LearningContext) -> ReviewOutcome:
    """Один проход самопроверки: повторы в журнале + протухшие знания.

    Анализы независимы: падение одного не должно лишать нас второго.
    """
    candidates: list[Candidate] = []
    try:
        result = await detect(
            ctx.journal, ctx.candidates, ctx.llm,
            window_hours=settings.learn_window_hours,
            min_tasks=settings.learn_min_tasks,
            min_repeats=settings.learn_min_repeats,
            min_steps=settings.learn_min_steps,
        )
        candidates = result.candidates
        ctx.candidates.mark_proposed(candidates)
        ctx.journal.mark_reviewed(result.considered_ids)
    except Exception:
        log.exception("detect_pass_failed")

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

    return ReviewOutcome(candidates=candidates, stale=stale)


def resolve_candidate(store: CandidateStore, sid: str) -> str | None:
    """Короткий id -> сигнатура кандидата (в callback_data она не влезает)."""
    return next((s for s in store.known_signatures() if short_id(s) == sid), None)


def resolve_fact(facts, sid: str) -> tuple[str, str] | None:
    for f in facts.all_with_ts():
        if short_id(f["scope"], f["key"]) == sid:
            return f["scope"], f["key"]
    return None


def render_review(outcome: ReviewOutcome) -> str:
    """Текст сводки в разметке модели — дальше через render_answer, как везде."""
    blocks: list[str] = []
    if outcome.candidates:
        lines = ["**Повторяющиеся задачи**", ""]
        for c in outcome.candidates:
            agents = ", ".join(c.agents) or "—"
            lines.append(
                f"> {c.label} — {c.repeats} раз, обычно {c.median_steps} шагов ({agents})"
            )
        blocks.append("\n".join(lines))
    if outcome.stale:
        lines = ["**Устаревшие знания**", ""]
        for f in outcome.stale:
            lines.append(f"> `{f.scope}/{f.key}` = {f.value} — не проверялось {f.age_days} дн.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
