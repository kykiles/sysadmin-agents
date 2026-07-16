"""Проверка read-only для команд, обёрнутых в `sh -c`/`bash -c`.

Модель часто пишет осмотр как `sh -c '... | grep ... && for d in .../*; do openssl ...; done'`,
потому что ей нужны пайпы, globs и циклы. Наивный классификатор видит `command[0] == "sh"`,
не находит его в белом списке и уводит команду в DANGEROUS-путь с подтверждением. Здесь мы
разбираем вложенный скрипт на простые команды и подтверждаем read-only, только если КАЖДАЯ
простая команда проходит ту же per-binary проверку скила (плюс безопасные shell-встроенные
и текстовые фильтры), а записывающих перенаправлений в реальные файлы нет.

Любая неоднозначность (синтаксис, подстановка команд, незнакомая конструкция) → False:
команда просто уходит в путь с подтверждением, что безопасно.
"""
import shlex
from typing import Callable

_WRAPPERS = {"sh", "bash", "dash", "ash"}

# Shell-встроенные, безопасные в позиции команды (не меняют состояние ФС/системы).
_SAFE_BUILTINS = {"echo", "printf", "true", "false", "test", "[", "[[", ":"}

# Чистые текстовые фильтры: читают stdin/файлы, пишут только в stdout. Безопасны в пайпах.
# Намеренно НЕ включаем sed/awk/xargs/tee/sort (умеют писать в файлы или исполнять команды).
_SAFE_FILTERS = {
    "grep", "egrep", "fgrep", "head", "tail", "wc",
    "cut", "tr", "rev", "nl", "column", "uniq", "cat",
}

# Токены-разделители простых команд (после токенизации с punctuation_chars).
_SEPARATORS = {"|", "||", "&&", ";", "&", "\n"}

# Ключевые слова управляющих конструкций: разделяют команды, но сами командами не являются.
# `for`/`select` обрабатываются отдельно (у них заголовок `VAR in LIST` — не команда).
_DELIMS = {
    "do", "done", "then", "else", "elif", "fi", "if", "while", "until",
    "case", "esac", "in", "{", "}", "(", ")", "!",
}

_REDIR_SAFE_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


def unwrap_shell(command: list[str]) -> str | None:
    """Если команда — `sh -c <script>` (sh/bash/dash/ash, флаг вида -c/-lc), вернуть
    внутренний скрипт, иначе None."""
    if len(command) < 3 or command[0] not in _WRAPPERS:
        return None
    for i, tok in enumerate(command[1:-1], start=1):
        if tok.startswith("-") and "c" in tok:
            return command[i + 1]
    return None


def _safe_redir_target(target: str) -> bool:
    return target in _REDIR_SAFE_TARGETS or target.startswith("&")


def _is_redir(tok: str) -> bool:
    return bool(tok) and all(ch in "<>&" for ch in tok) and (">" in tok or "<" in tok)


def split_simple_commands(script: str) -> list[list[str]] | None:
    """Разобрать shell-скрипт на список argv простых команд.

    None — если структура небезопасна или неразбираема: синтаксическая ошибка,
    подстановка команд (`$(...)`, backticks, `<(...)`/`>(...)`), запись в не-null файл.
    Управляющие ключевые слова и заголовки циклов отбрасываются; команды в теле
    и условиях остаются.
    """
    if "`" in script or "$(" in script or "<(" in script or ">(" in script:
        return None
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None

    commands: list[list[str]] = []
    current: list[str] = []
    skip_header = False  # внутри заголовка `for VAR in LIST` до `do`
    expect: str | None = None  # 'file' | 'fdnum' | 'source' — ждём цель редиректа

    def flush() -> None:
        if current:
            commands.append(list(current))
            current.clear()

    for tok in tokens:
        if skip_header:
            if tok == "do":
                skip_header = False
            continue
        if expect == "file":
            if not _safe_redir_target(tok):
                return None
            expect = None
            continue
        if expect in ("fdnum", "source"):
            expect = None
            continue
        if tok in ("for", "select"):
            flush()
            skip_header = True
            continue
        if tok in _SEPARATORS or tok in _DELIMS:
            flush()
            continue
        if _is_redir(tok):
            if current and current[-1].isdigit():
                current.pop()  # ведущий дескриптор (2>...) — часть редиректа, не аргумент
            if "<" in tok and ">" not in tok:
                expect = "source"  # чтение — безвредно, пропускаем источник
            elif tok.endswith("&"):
                expect = "fdnum"  # дублирование дескриптора (2>&1) — цель это номер, безопасно
            else:
                expect = "file"  # запись в файл — цель обязана быть безопасной
            continue
        current.append(tok)
    flush()
    if expect is not None:
        return None
    return commands


def check_wrapped_readonly(
    command: list[str],
    is_simple_readonly: Callable[[list[str]], bool],
) -> bool | None:
    """Если `command` — обёртка `sh -c <script>`, вернуть, является ли весь скрипт
    read-only (каждая простая команда проходит `is_simple_readonly`; builtin и текстовые
    фильтры проходят автоматически). None — если это не обёртка (вызывающий проверяет argv сам).
    """
    script = unwrap_shell(command)
    if script is None:
        return None
    cmds = split_simple_commands(script)
    if cmds is None:
        return False
    for c in cmds:
        if not c:
            continue
        if c[0] in _SAFE_BUILTINS or c[0] in _SAFE_FILTERS:
            continue
        if not is_simple_readonly(c):
            return False
    return True
