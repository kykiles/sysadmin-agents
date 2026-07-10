import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.logging import get_logger

log = get_logger("audit")


def outcome(result: str) -> dict:
    """Компактный итог tool-вызова для журнала: returncode (если есть) + превью."""
    rc = None
    try:
        rc = json.loads(result).get("returncode")
    except (ValueError, TypeError, AttributeError):
        pass
    return {"returncode": rc, "preview": result[:1000]}


def _record_sync(event: dict) -> None:
    path = settings.audit_trail_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), **event},
        ensure_ascii=False, default=str,
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def record(**event) -> None:
    """Append одной JSONL-записи. Сбой аудита не должен ронять действие агента."""
    try:
        await asyncio.to_thread(_record_sync, event)
    except OSError as e:
        log.error("audit_write_failed", error=str(e))
