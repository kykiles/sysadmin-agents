import logging
import re
import structlog
from app.config import settings

# Токены Remnawave и прочих API утекают в логи через превью аргументов curl.
_SECRET = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"   # JWT
    r"|(?<=Bearer )[A-Za-z0-9._~+/=-]{8,}"                     # Bearer <token>
)


def redact(text: str) -> str:
    return _SECRET.sub("<redacted>", text)


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
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str):
    return structlog.get_logger(name)
