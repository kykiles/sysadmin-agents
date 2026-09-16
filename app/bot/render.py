import html
import json
import re
import shlex

from app.agents.messages import ConfirmationRequest
from app.logging import redact

# Разметка модели -> HTML Telegram. Экранируем ПЕРВЫМ делом, поэтому маркеры ищем
# уже в экранированном тексте ('>' к этому моменту стал '&gt;').
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_QUOTE_LINE = re.compile(r"^&gt;\s?(.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def _inline(text: str) -> str:
    """`код` и **жирный**. Внутри code-спанов разметка не разбирается."""
    parts = _CODE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{part}</code>")
        else:
            out.append(_BOLD.sub(r"<b>\1</b>", part))
    return "".join(out)


def render_answer(text: str) -> str:
    """Разметку модели -> валидный HTML Telegram.

    Модели нельзя доверять генерацию HTML: незакрытый тег — это не «криво»,
    а TelegramBadRequest, и сообщение не уходит вовсе. Поэтому экранируем всё
    и собираем теги сами — что бы модель ни прислала, на выходе валидный HTML.
    """
    lines = html.escape(text or "").split("\n")
    blocks: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            body = "\n".join(_inline(q) for q in quote)
            blocks.append(f"<blockquote>{body}</blockquote>")
            quote.clear()

    for line in lines:
        m = _QUOTE_LINE.match(line)
        if m:
            quote.append(m.group(1))
            continue
        flush_quote()
        h = _HEADING.match(line)
        # заголовков в Telegram нет — ближайшее по смыслу это жирная строка
        blocks.append(f"<b>{_inline(h.group(1))}</b>" if h else _inline(line))
    flush_quote()
    return "\n".join(blocks).strip()


# '"' -> '&quot;' — худший случай раздувания при экранировании
_MAX_ESCAPE_GROWTH = 6


def _hard_wrap(line: str, limit: int) -> list[str]:
    """Строка, которая не влезает сама по себе, режется по символам."""
    step = max(1, limit // _MAX_ESCAPE_GROWTH)
    return [line[i:i + step] for i in range(0, len(line), step)] or [""]


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Режет ДО рендера, по границам строк: разорвать готовый HTML-тег нельзя.

    Меряем длину ОТРЕНДЕРЕННОГО текста, а не сырого: экранирование раздувает
    его до шести раз, и запас «на глазок» тут не работает.
    """
    if len(render_answer(text)) <= limit:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    for raw_line in text.split("\n"):
        for line in _hard_wrap(raw_line, limit):
            if cur and len(render_answer("\n".join(cur + [line]))) > limit:
                parts.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    return parts


TELEGRAM_LIMIT = 4096

# Цель — первыми строками: без неё подтверждения для разных серверов совпадали
# побайтно (аудит 2026-09-12, F05).
_TARGET_KEYS = ("host", "container", "project")
# Пояснение модели — не объект согласования, его можно укоротить.
_REASON_LIMIT = 500
# Превью, когда полный текст ушёл файлом: с экранированием заведомо влезает.
_PREVIEW_LIMIT = 400


def confirmation_details(req: ConfirmationRequest) -> str:
    """Что будет исполнено: инструмент, цель и каждый параметр — детерминированно,
    из валидированных аргументов, а не из пересказа модели. Команда — через
    shlex.join: `["sh", "-c", "a b"]` и `["sh", "-c", "a", "b"]` различимы.
    Секреты заменены: человеку их показывать незачем."""
    args = req.args
    keys = [k for k in _TARGET_KEYS if k in args] + sorted(k for k in args if k not in _TARGET_KEYS)
    lines = [f"инструмент: {req.tool_name}"]
    for k in keys:
        value = args[k]
        if k == "command" and isinstance(value, list) and all(isinstance(a, str) for a in value):
            shown = shlex.join(value)
        else:
            shown = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        lines.append(f"{k}: {shown}")
    return redact("\n".join(lines))


_RW_ACTIONS = {
    "user-extend": "продлена подписка пользователя",
    "user-enable": "включён пользователь",
    "user-disable": "приостановлен доступ пользователя",
    "user-revoke": "отозвана подписка (новые ключи и ссылка) пользователя",
    "user-reset-traffic": "обнулён трафик пользователя",
    "hwid-reset": "сброшены устройства пользователя",
}
_HTTP_ACTIONS = {"POST": "создание или действие", "PATCH": "изменение", "PUT": "изменение",
                 "DELETE": "удаление"}


def _rw_action(a: dict) -> str:
    args = a.get("args") or []
    what = _RW_ACTIONS.get(a.get("script"), f"выполнено действие «{a.get('script')}» для пользователя")
    days = f" на {args[1]} дн." if a.get("script") == "user-extend" and len(args) > 1 else ""
    return f"Сейчас в панели Remnawave будет {what} «{args[0] if args else '?'}»{days}."


# Фраза пишется кодом по аргументам, а не моделью: в живом прогоне модель назвала
# «пересборкой» вызов, который образ не пересобирал. Второй элемент — исполняет ли
# инструмент произвольную команду: тогда рядом показываем пояснение агента, помеченное
# как его слова, — из аргументов смысл такой команды не пересказать.
_ACTIONS: dict[str, tuple] = {
    "docker_restart": (lambda a: f"Сейчас будет перезапущен Docker-контейнер «{a['container']}».", False),
    "docker_stop": (lambda a: f"Сейчас будет остановлен Docker-контейнер «{a['container']}».", False),
    "docker_start": (lambda a: f"Сейчас будет запущен Docker-контейнер «{a['container']}».", False),
    "compose_up": (lambda a: (
        f"Сейчас будет пересобран образ и перезапущено приложение «{a['project']}»."
        if a.get("build") else
        f"Сейчас будет запущено приложение «{a['project']}» на текущем образе, без пересборки."
    ), False),
    "compose_down": (lambda a: f"Сейчас будет остановлено приложение «{a['project']}»: "
                               "контейнеры удалятся, данные в томах останутся.", False),
    "deploy_run": (lambda a: f"Сейчас будет выполнен деплой сайта «{a['site']}».", False),
    "rw_action": (_rw_action, False),
    "rw_curl_write": (lambda a: "Сейчас в панели Remnawave будет "
                                f"{_HTTP_ACTIONS.get(str(a.get('method')).upper(), 'изменение')} данных.", True),
    "write_skill": (lambda a: f"Сейчас будет сохранён новый навык «{a['name']}»: {a.get('description', '')}", False),
    "docker_exec": (lambda a: f"Сейчас внутри Docker-контейнера «{a['container']}» будет выполнена команда.", True),
    "docker_query": (lambda a: f"Сейчас будет выполнен запрос к базе данных в контейнере «{a['container']}».", True),
    "shell_exec": (lambda a: "Сейчас на этом сервере будет выполнена команда.", True),
    "ssh_exec": (lambda a: f"Сейчас на сервере «{a['host']}» будет выполнена команда.", True),
    "run_skill_script": (lambda a: f"Сейчас будет запущен скрипт навыка «{a['skill']}».", True),
}


def plain_action(req: ConfirmationRequest) -> str:
    """Что произойдёт — обычным языком, для человека без знания команд."""
    phrase, opaque = _ACTIONS.get(req.tool_name, (None, True))
    try:
        text = phrase(req.args) if phrase else f"Сейчас будет выполнено действие «{req.tool_name}»."
    except (KeyError, IndexError, TypeError):
        text = f"Сейчас будет выполнено действие «{req.tool_name}»."
    reason = (req.reason or "").strip()
    if opaque and reason:
        if len(reason) > _REASON_LIMIT:
            reason = reason[:_REASON_LIMIT] + " …"
        text += f"\nПояснение агента: {reason}"
    return redact(text)


def format_confirmation(req: ConfirmationRequest, request_id: str, details: str,
                        attached: bool = False) -> str:
    """Сверху — фраза обычным языком. Команда и аргументы — в свёрнутом блоке: исполнено
    будет ровно то, что в нём, поэтому убрать его совсем нельзя (аудит F05)."""
    lines = [html.escape(plain_action(req)), "Требуется ваше подтверждение."]
    tech = f"Подробности для проверки\n\n{details}" if not attached else (
        f"Подробности для проверки\n\n{details[:_PREVIEW_LIMIT]} …\n\n"
        f"Полный текст ({len(details)} символов) — в файле выше; выполнено будет ровно оно."
    )
    if scope := req.scope():
        tech += f"\n«Да, для всех таких» разрешит: {redact(scope)}"
    tech += f"\nзапрос: {request_id}"
    lines.append(f"<blockquote expandable>{html.escape(tech)}</blockquote>")
    if scope:
        lines.append("«Да, для всех таких» — такие же действия до конца этой задачи "
                     "выполнятся без вопроса.")
    return "\n\n".join(lines)
