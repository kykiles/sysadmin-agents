import logging
import re
import structlog
from app.config import settings

# Токены Remnawave и прочих API утекают в логи через превью аргументов curl.
_SECRET = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"   # JWT
    r"|(?<=Bearer )[A-Za-z0-9._~+/=-]{8,}"                     # Bearer <token>
)

# Агент собирает строку подключения к БД руками: `PGPASSWORD=... psql` и
# `postgresql://user:pass@host`. И то и другое оседало в audit.jsonl открытым текстом.
_ASSIGN = re.compile(
    r"(?i)\b(\w*(?:password|passwd|secret|token|api_?key)\w*\s*=\s*)([^\s\"'\\,}]+)"
)
_URL_CRED = re.compile(r"(://[^\s:/@\"']+:)([^\s@\"'\\]+)(?=@)")
# Приватный ключ целиком: страховка для того, что прочитано с подтверждением.
# Незакрытый блок (вывод обрезан) — до конца текста.
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
                  re.DOTALL)

# Регулярки выше узнают только знакомые формы (JWT, Bearer, password=). Ключ из
# настроек узнаём по значению — в какой бы форме его ни повторила внешняя команда.
_SECRET_SETTINGS = ("llm_api_key", "telegram_bot_token", "remnawave_api_key")
# Короткие значения похожи на обычные слова: их замена портила бы любой вывод.
_MIN_SECRET = 8
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Запомнить credential, которого нет в настройках (ключ из env в URL MCP)."""
    if len(value) >= _MIN_SECRET:
        _registered.add(value)


def _known_secrets() -> list[str]:
    values = {getattr(settings, name) or "" for name in _SECRET_SETTINGS} | _registered
    # длинные первыми: ключ, содержащий другой ключ, не должен остаться хвостом
    return sorted((v for v in values if len(v) >= _MIN_SECRET), key=len, reverse=True)


def redact(text: str) -> str:
    for secret in _known_secrets():
        text = text.replace(secret, "<redacted>")
    text = _PEM.sub("<redacted>", text)
    text = _SECRET.sub("<redacted>", text)
    text = _ASSIGN.sub(r"\1<redacted>", text)
    return _URL_CRED.sub(r"\1<redacted>", text)


def scrub(value):
    """redact по всем строкам вложенных dict/list — объект целиком, а не превью."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def scrub_event(_logger, _method, event_dict: dict) -> dict:
    """Процессор structlog: ни одно поле лога не уходит с секретом."""
    return scrub(event_dict)


def setup_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler()],
    )
    # httpx логирует URL целиком, а ключи API живут в query-строке (Tavily).
    # Установлен httpx2 — его логгер зовётся так же, как пакет.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    file_handler = logging.FileHandler(settings.audit_log_path)
    file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            scrub_event,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str):
    return structlog.get_logger(name)
