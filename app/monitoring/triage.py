from app.logging import get_logger
from app.monitoring.checks import CheckResult

log = get_logger("monitoring.triage")

_SYSTEM = (
    "Ты дежурный SRE-инженер. Тебе дают сработавшую проверку здоровья сервера. "
    "Ответь кратко (2-4 строки) на русском: что сломалось, вероятная причина, что проверить. "
    "Не выдумывай фактов сверх данных. Не предлагай команд на выполнение."
)


def _fallback(result: CheckResult) -> str:
    return f"{result.name}: {result.detail}"


async def triage(llm, result: CheckResult) -> str:
    """Один LLM-вызов без tools. При недоступности LLM — сырой детерминированный текст
    (алерт важнее осмысленного триажа)."""
    try:
        msg = await llm.chat([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Проверка: {result.name}\nДетали: {result.detail}"},
        ])
        return (msg.content or "").strip() or _fallback(result)
    except Exception:
        log.exception("triage_failed", check=result.name)
        return _fallback(result)
