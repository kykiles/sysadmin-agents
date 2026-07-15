import re
from datetime import datetime, timezone
from pathlib import Path


def _slug(title: str) -> str:
    """Заголовок (обычно русский) -> безопасное имя файла."""
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return s[:60] or "report"


def save_report(reports_dir: str, title: str, markdown: str) -> str:
    """Кладёт отчёт в .md и возвращает путь. Внутри файла markdown полноценный:
    он читается вне Telegram, ограничений на таблицы и заголовки там нет."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(reports_dir) / f"{_slug(title)}-{date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = markdown if markdown.lstrip().startswith("#") else f"# {title}\n\n{markdown}"
    path.write_text(body, encoding="utf-8")
    return str(path)
