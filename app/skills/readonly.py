"""Единая проверка «команда только читает» и объявление доступа к хосту.

Раньше эта логика существовала в пяти копиях — по одной у скилов host, observe,
tls, security и ssh, — и копии разошлись: список read-only подкоманд systemctl
насчитывал то десять вариантов, то пять. Здесь она одна.

Скил не переопределяет логику, а объявляет `ACCESS = HostAccess(...)`: какие
бинарники ему вообще доступны и можно ли ему выполнять изменяющие команды.
Классификация аргументов (какие подкоманды `systemctl` читают, а какие меняют)
универсальна — она зависит от команды, а не от того, кто её вызвал.

Классификация — белый список: у каждой утилиты явный контракт argv (флаги, опции
со значением, позиционные, подкоманды). Всё, чего в контракте нет, — отказ. Чёрный
список изменяющих флагов обходился формой `--vacuum-time=1s`, а «безопасных при
любых аргументах» утилит не бывает: `date -s`, `hostname NAME`, `ss -K` (аудит
2026-09-12, F02). Форма без короткого понятного контракта в SAFE не попадает —
для неё есть инструмент с подтверждением.

При спавне доступы выданных скилов объединяются в один инструмент `host_query`,
поэтому имя у него одно и коллизии имён с разным скоупом невозможны.
"""
import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable

from app.skills.network import is_allowed
from app.tools.base import Tool, Safety
from app.tools.docker import ShellParams, host_exec


@dataclass(frozen=True)
class HostAccess:
    """Что скил разрешает делать на хосте."""

    binaries: frozenset[str] = field(default_factory=frozenset)
    # можно ли выдавать инструмент изменяющих команд (с подтверждением)
    exec_allowed: bool = False

    def __or__(self, other: "HostAccess") -> "HostAccess":
        return HostAccess(
            binaries=self.binaries | other.binaries,
            exec_allowed=self.exec_allowed or other.exec_allowed,
        )


# ---------- контракт argv ----------

Check = Callable[[str], bool]


@dataclass(frozen=True)
class Argv:
    """Допустимые аргументы одной утилиты (или одной её подкоманды).

    Опции с необязательным значением (`--color[=when]`, `date -I[fmt]`) сюда не
    вносим: getopt не берёт для них следующий аргумент, а разбор ниже взял бы —
    и позиционный аргумент утилиты (например, новое время для `date`) проскочил
    бы под видом значения опции. `--` не поддерживается по той же причине.
    """

    flags: frozenset[str] = frozenset()
    valued: dict[str, Check] = field(default_factory=dict)
    positional: Callable[[list[str]], bool] = lambda ps: not ps
    # первое позиционное выбирает контракт остальных аргументов
    subcommands: dict[str, "Argv"] | None = None
    # хотя бы одна из этих опций обязана быть (здесь или до подкоманды)
    require: frozenset[str] = frozenset()
    # подкоманду можно опустить, если указана одна из этих опций: она и есть
    # выборка (`systemctl --failed` — то же, что `list-units --failed`).
    # Голый вызов без них остаётся отказом.
    bare_requires: frozenset[str] = frozenset()
    # опции-слова с одним дефисом (`find -name`, `openssl -noout`): не склейка букв
    single_dash: bool = False
    # аргументы не опции, а данные: echo печатает, test сравнивает
    raw: bool = False
    # позиционные — файлы, содержимое которых уходит в вывод (cat, grep)
    reads: bool = False
    # опции, которые задают шаблон grep; без них шаблон — первый позиционный
    pattern_opts: frozenset[str] = frozenset()


def _re(pattern: str) -> Check:
    rx = re.compile(pattern)
    return lambda v: bool(rx.fullmatch(v))


def _word(v: str) -> bool:
    return bool(v) and not v.startswith("-")


def _any(v: str) -> bool:
    return True


_INT = _re(r"\d+")
_SIGNED = _re(r"[+-]?\d+[a-zA-Z]?")
_COUNT = _re(r"[+-]?\d+[a-zA-Z]*")
_NAME = _re(r"[A-Za-z0-9][A-Za-z0-9@._:+-]*")


def _paths(ps: list[str]) -> bool:
    return True


def _upto(n: int, check: Check = _word) -> Callable[[list[str]], bool]:
    return lambda ps: len(ps) <= n and all(check(p) for p in ps)


def _each(check: Check) -> Callable[[list[str]], bool]:
    return lambda ps: all(check(p) for p in ps)


def _opts(*names: str) -> frozenset[str]:
    return frozenset(names)


# Секреты: прочитанное SAFE-инструментом уходит в LLM, в транскрипт /trace и в
# историю диалога, а redact узнаёт лишь знакомые формы (аудит Б2). Проверка по
# имени: симлинк с другим именем её обойдёт — это страховка от случайного чтения
# и от внедрённой инструкции «покажи .env», а не песочница.
_SECRET_NAME = re.compile(
    r"\.env|\.env\..*|.*\.key|privkey.*\.pem|id_(?!.*\.pub$).*|\.pgpass|\.netrc|\.git-credentials"
    r"|g?shadow-?|environ"  # /etc/shadow и бэкап shadow-, /proc/<pid>/environ
)


def _secret(path: str) -> bool:
    parts = posixpath.normpath(path).split("/")
    return ".ssh" in parts or bool(_SECRET_NAME.fullmatch(parts[-1]))


def _valid(spec: Argv, args: list[str], seen: set[str] | None = None,
           files: list[str] | None = None, why: list[str] | None = None) -> bool:
    """`files` собирает позиционные читающих утилит — пути, чьё содержимое
    окажется в выводе (шаблон grep сюда не попадает).

    `why` — причина отказа наружу, одной строкой. Общий текст «команда не входит
    в список read-only» модель читала как запрет на всю форму вызова: получив его
    на `journalctl -i -g 'a|b'` (виноват `-i`, у journalctl это `--file=PATH`),
    агент решил, что блокируется `|` в шаблоне, и перешёл на один вызов вместо
    одного шаблона — 63 вызова на задачу (живой разбор 20.09.2026).
    """
    if spec.raw:
        return True
    seen = set() if seen is None else seen
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("-") and a != "-":
            nxt = _option(spec, a, args, i, seen)
            if nxt is None:
                _why(why, f"не принят аргумент `{a}`")
                return False
            i = nxt
            continue
        if spec.subcommands is not None:
            sub = spec.subcommands.get(a)
            if sub is None:
                _why(why, f"не принята подкоманда `{a}`")
                return False
            return _valid(sub, args[i:], seen, files, why)
        pos.append(a)
    if spec.subcommands is not None and not spec.bare_requires & seen:
        _why(why, "нужна подкоманда")
        return False
    if spec.require and not spec.require & seen:
        _why(why, f"нужна одна из опций: {', '.join(sorted(spec.require))}")
        return False
    if spec.reads and files is not None:
        files.extend(pos if spec.pattern_opts & seen or not spec.pattern_opts else pos[1:])
    if not spec.positional(pos):
        _why(why, f"не приняты позиционные аргументы: {' '.join(pos) or '(их нет)'}")
        return False
    return True


def _why(why: list[str] | None, reason: str) -> None:
    if why is not None and not why:  # первая причина, дальше разбор уже свернулся
        why.append(reason)


def _option(spec: Argv, a: str, args: list[str], i: int, seen: set[str]) -> int | None:
    """Разобрать одну опцию; вернуть индекс следующего аргумента или None при отказе."""
    if spec.single_dash or a.startswith("--"):
        name, eq, value = (a, "", "") if spec.single_dash else a.partition("=")
        if name in spec.flags and not eq:
            seen.add(name)
            return i
        check = spec.valued.get(name)
        if check is None:
            return None
        if not eq:
            if i >= len(args):
                return None
            value, i = args[i], i + 1
        if not check(value):
            return None
        seen.add(name)
        return i
    # склейка коротких: -tlnp, -bn1 (последняя опция со значением забирает хвост)
    for j in range(1, len(a)):
        opt = "-" + a[j]
        if opt in spec.flags:
            seen.add(opt)
            continue
        check = spec.valued.get(opt)
        if check is None:
            return None
        value = a[j + 1:]
        if not value:
            if i >= len(args):
                return None
            value, i = args[i], i + 1
        if not check(value):
            return None
        seen.add(opt)
        return i
    return i


# ---------- контракты утилит ----------

_GREP = Argv(
    flags=_opts(
        "-i", "-v", "-c", "-l", "-L", "-n", "-h", "-H", "-o", "-q", "-s",
        "-w", "-x", "-E", "-F", "-G", "-P", "-a", "-I", "-z", "-Z", "-b",
        "--ignore-case", "--invert-match", "--count", "--files-with-matches",
        "--files-without-match", "--line-number", "--no-filename", "--with-filename",
        "--only-matching", "--quiet", "--silent", "--no-messages", "--word-regexp", "--line-regexp", "--extended-regexp",
        "--fixed-strings", "--perl-regexp", "--text", "--null", "--null-data",
    ),
    valued={
        "-e": _any, "--regexp": _any,
        "-f": lambda v: _word(v) and not _secret(v), "--file": lambda v: _word(v) and not _secret(v),
        "-m": _INT, "--max-count": _INT,
        "-A": _INT, "-B": _INT, "-C": _INT,
        "--after-context": _INT, "--before-context": _INT, "--context": _INT,
        "--include": _any, "--exclude": _any, "--exclude-dir": _any,
    },
    # без -r/-R: рекурсия по каталогу проходит и по .env, путь не проверить
    positional=_paths,
    reads=True,
    pattern_opts=_opts("-e", "--regexp", "-f", "--file"),
)

_HEAD_TAIL = Argv(
    flags=_opts("-q", "-v", "-z", "--quiet", "--silent", "--verbose", "--zero-terminated"),
    valued={"-n": _COUNT, "--lines": _COUNT, "-c": _COUNT, "--bytes": _COUNT},
    positional=_paths,
    reads=True,
)

_IPTABLES = Argv(
    flags=_opts("-L", "--list", "-S", "--list-rules", "-n", "--numeric",
                "-v", "--verbose", "-x", "--exact", "--line-numbers"),
    valued={"-t": _re(r"filter|nat|mangle|raw|security"),
            "--table": _re(r"filter|nat|mangle|raw|security")},
    # -L/-S принимают необязательную цепочку (и номер правила у -S)
    positional=_upto(2, _re(r"[A-Za-z0-9_-]+")),
    require=_opts("-L", "--list", "-S", "--list-rules"),
)

# Environment= держит пароли сервисов; `show` без -p печатает все свойства, с ним и его.
# `systemctl cat` тоже может показать Environment= из юнита — редкость, не закрываем.
_PROPERTY = lambda v: _word(v) and "environment" not in v.lower()  # noqa: E731
_SYSTEMCTL_OPTS = dict(
    flags=_opts("--no-pager", "-l", "--full", "-a", "--all", "--no-legend", "--plain",
                "-q", "--quiet", "--failed", "--value", "--system"),
    valued={"-n": _INT, "--lines": _INT, "-t": _word, "--type": _word,
            "--state": _word, "-p": _PROPERTY, "--property": _PROPERTY,
            "-o": _word, "--output": _word},
)
_UNITS = _each(_re(r"[A-Za-z0-9@._:\\*\[\]-]+"))
_SYSTEMCTL = Argv(
    **_SYSTEMCTL_OPTS,
    subcommands={
        **{sub: Argv(**_SYSTEMCTL_OPTS, positional=_UNITS)
           for sub in (
               "status", "cat", "is-active", "is-enabled", "is-failed",
               "list-units", "list-unit-files", "list-timers", "list-sockets", "get-default",
           )},
        "show": Argv(**_SYSTEMCTL_OPTS, positional=_UNITS, require=_opts("-p", "--property")),
    },
    # `systemctl --failed` — обычная форма «что упало», и это list-units под другим
    # именем. Отбивать её нечем: без подкоманды systemctl только перечисляет юниты.
    bare_requires=_opts("--failed"),
)

# ip: после объекта — только просмотр. `ip netns exec`, `ip -batch`, `ip link set`
# сюда не попадают: объекта netns и опций -b/-n/-a в контракте нет.
_IP_SHOW = frozenset({"show", "list", "lst", "ls"})


def _ip_view(verbs: frozenset[str]) -> Argv:
    return Argv(positional=lambda ps: not ps or ps[0] in verbs)


_IP = Argv(
    flags=_opts("-4", "-6", "-br", "-brief", "-s", "-stats", "-statistics", "-d", "-details",
                "-j", "-json", "-p", "-pretty", "-o", "-oneline", "-c", "-color",
                "-h", "-human"),
    single_dash=True,
    subcommands={
        **{obj: _ip_view(_IP_SHOW) for obj in (
            "address", "addr", "a", "link", "l", "neighbour", "neighbor", "neigh", "n",
            "rule", "ru",
        )},
        **{obj: _ip_view(_IP_SHOW | {"get"}) for obj in ("route", "ro", "r")},
    },
)

_APT_SIMULATE = _opts("-s", "--simulate", "--dry-run", "--just-print", "--no-act", "--recon")
_APT_PKGS = _each(_re(r"[A-Za-z0-9][A-Za-z0-9.+:*_-]*"))
_APT = Argv(
    flags=_APT_SIMULATE | {"-q"},
    subcommands={
        "list": Argv(flags=_opts("--upgradable", "--installed", "--manual-installed",
                                 "-a", "--all-versions", "-q"),
                     positional=_APT_PKGS),
        # установка и обновление — только симуляция; -o/-c в контракте нет, иначе
        # `-o APT::Get::Simulate=false` отменил бы -s
        **{sub: Argv(flags=_APT_SIMULATE | {"-q"}, positional=_APT_PKGS, require=_APT_SIMULATE)
           for sub in ("upgrade", "dist-upgrade", "full-upgrade", "install")},
    },
)

_DOCKER_NAME = _re(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# Без inspect: он отдаёт Config.Env с паролями. Локально есть docker_inspect,
# который Env прячет; на ноде — ssh_exec с подтверждением.
_DOCKER = Argv(subcommands={
    "ps": Argv(flags=_opts("-a", "--all", "-q", "--quiet", "-s", "--size", "--no-trunc",
                           "-l", "--latest"),
               valued={"-n": _INT, "--last": _INT, "-f": _word, "--filter": _word,
                       "--format": _any}),
    "logs": Argv(flags=_opts("-t", "--timestamps", "--details"),
                 valued={"-n": _re(r"\d+|all"), "--tail": _re(r"\d+|all"),
                         "--since": _word, "--until": _word},
                 positional=lambda ps: len(ps) == 1 and _DOCKER_NAME(ps[0])),
    "stats": Argv(flags=_opts("--no-stream", "--no-trunc", "-a", "--all"),
                  valued={"--format": _any}, positional=_each(_DOCKER_NAME)),
    "images": Argv(flags=_opts("-a", "--all", "-q", "--quiet", "--digests", "--no-trunc"),
                   valued={"-f": _word, "--filter": _word, "--format": _any},
                   positional=_upto(1)),
    "version": Argv(valued={"-f": _any, "--format": _any}),
    "info": Argv(valued={"-f": _any, "--format": _any}),
    "top": Argv(positional=lambda ps: 1 <= len(ps) <= 2 and _DOCKER_NAME(ps[0])
                and all(_re(r"[A-Za-z]+")(p) for p in ps[1:])),
    "port": Argv(positional=lambda ps: 1 <= len(ps) <= 2 and _DOCKER_NAME(ps[0])
                 and all(_re(r"\d+(/(tcp|udp))?")(p) for p in ps[1:])),
    "diff": Argv(positional=lambda ps: len(ps) == 1 and _DOCKER_NAME(ps[0])),
    "compose": Argv(
        valued={"-p": _word, "--project-name": _word, "-f": _word, "--file": _word,
                "--project-directory": _word},
        subcommands={"ps": Argv(flags=_opts("-a", "--all", "-q", "--quiet", "--services"),
                                valued={"--format": _any, "--status": _word, "--filter": _word},
                                positional=_each(_DOCKER_NAME))},
    ),
})

_FIND = Argv(
    flags=_opts("-L", "-H", "-P", "-print", "-print0", "-ls", "-prune", "-xdev", "-mount",
                "-follow", "-empty", "-readable", "-writable", "-executable", "-true",
                "-false", "-nouser", "-nogroup", "-not", "-a", "-o", "-and", "-or", "-quit",
                "-daystart", "-depth", "-noleaf"),
    valued={
        **dict.fromkeys(("-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                         "-regex", "-iregex", "-lname", "-ilname", "-printf"), _any),
        **dict.fromkeys(("-type", "-xtype"), _re(r"[bcdpflsD](,[bcdpflsD])*")),
        **dict.fromkeys(("-maxdepth", "-mindepth"), _INT),
        **dict.fromkeys(("-mtime", "-atime", "-ctime", "-mmin", "-amin", "-cmin", "-size",
                         "-links", "-inum", "-uid", "-gid", "-used"), _SIGNED),
        **dict.fromkeys(("-user", "-group", "-newer", "-anewer", "-cnewer", "-samefile",
                         "-fstype", "-regextype"), _word),
        "-perm": _re(r"[-/]?([0-7]{1,4}|[ugoa]*[-+=][rwxXst]*(,[ugoa]*[-+=][rwxXst]*)*)"),
    },
    positional=_paths,  # пути и `!`, `(`, `)`
    single_dash=True,
)

_OPENSSL = Argv(single_dash=True, subcommands={
    # только осмотр сертификата: ни -out, ни подписи, ни -CAcreateserial
    "x509": Argv(
        flags=_opts("-noout", "-subject", "-issuer", "-dates", "-enddate", "-startdate",
                    "-serial", "-fingerprint", "-text", "-hash", "-subject_hash",
                    "-issuer_hash", "-email", "-purpose", "-pubkey", "-modulus",
                    "-ocsp_uri", "-sha1", "-sha256"),
        valued={"-in": _word, "-inform": _re(r"(?i)pem|der"), "-nameopt": _word,
                "-ext": _word, "-checkend": _INT, "-certopt": _word},
        single_dash=True,
    ),
    "version": Argv(flags=_opts("-a", "-b", "-o", "-f", "-p", "-d", "-e", "-m", "-r", "-c", "-v"),
                    single_dash=True),
})

# Git: только метаданные — статус, история, какие файлы менялись. Содержимое диффов
# (`log -p`, `diff`, `show` с патчем) — через shell_exec: в репозитории бывает
# закоммиченный .env. `-c`, `--output`, `--ext-diff` в контракт не входят; `rev:path`
# (двоеточие) не проходит по формату ревизии. Без remote -v: в URL бывает токен.
_GIT_REV = _re(r"[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*")
_GIT_REVS = _each(lambda p: _GIT_REV(p) and not _secret(p))
_GIT_FILES_ONLY = _opts("--stat", "--shortstat", "--name-only", "--name-status")
_GIT = Argv(
    flags=_opts("--no-pager"),
    valued={"-C": _word},
    subcommands={
        "status": Argv(flags=_opts("-s", "--short", "-b", "--branch", "--porcelain")),
        "log": Argv(flags=_opts("--oneline", "--graph", "--all", "--no-merges", "--decorate",
                                "--first-parent", "--reverse") | _GIT_FILES_ONLY,
                    valued={"-n": _INT, "--max-count": _INT, "--since": _any, "--until": _any,
                            "--author": _any, "--format": _any},
                    positional=_GIT_REVS),
        "show": Argv(flags=_opts("--oneline", "-s", "--no-patch") | _GIT_FILES_ONLY,
                     valued={"--format": _any},
                     positional=_GIT_REVS,
                     require=_opts("-s", "--no-patch") | _GIT_FILES_ONLY),
        "diff": Argv(flags=_opts("--cached", "--staged") | _GIT_FILES_ONLY,
                     positional=_GIT_REVS, require=_GIT_FILES_ONLY),
        # без позиционных: `git branch NAME` создаёт ветку
        "branch": Argv(flags=_opts("-a", "--all", "-r", "--remotes", "-v", "--verbose",
                                   "--list", "--show-current")),
        "rev-parse": Argv(flags=_opts("--abbrev-ref", "--short", "--show-toplevel",
                                      "--is-inside-work-tree"),
                          positional=_upto(1, _GIT_REV)),
    },
)

# getent — только базы без секретов: `getent shadow` отдавал хэши паролей в обход
# проверки файлов. hosts и ahosts* — DNS-запрос, то есть выход в сеть: ключи — только
# хосты из NETWORK_ALLOWED (см. network.py); без ключа hosts перечисляет /etc/hosts.
_GETENT_LOCAL = frozenset({"passwd", "group", "services", "protocols"})
_GETENT_DNS = frozenset({"hosts", "ahosts", "ahostsv4", "ahostsv6"})


def _getent(ps: list[str]) -> bool:
    if not ps:
        return False
    return ps[0] in _GETENT_LOCAL or (
        ps[0] in _GETENT_DNS and all(is_allowed(key) for key in ps[1:]))


_SPECS: dict[str, Argv] = {
    # состояние системы
    "df": Argv(flags=_opts("-h", "-H", "-T", "-i", "-a", "-l", "-P", "-k",
                           "--human-readable", "--si", "--print-type", "--inodes", "--all",
                           "--local", "--portability", "--total"),
               valued={"-t": _word, "--type": _word, "-x": _word, "--exclude-type": _word,
                       "-B": _word, "--block-size": _word},
               positional=_paths),
    "du": Argv(flags=_opts("-h", "-s", "-a", "-c", "-x", "-b", "-k", "-m", "-S", "-L", "-l",
                           "--human-readable", "--summarize", "--all", "--total",
                           "--one-file-system", "--apparent-size", "--bytes", "--si",
                           "--separate-dirs", "--dereference", "--count-links"),
               valued={"-d": _INT, "--max-depth": _INT, "-t": _SIGNED, "--threshold": _SIGNED,
                       "--exclude": _any, "-B": _word, "--block-size": _word},
               positional=_paths),
    "free": Argv(flags=_opts("-h", "-m", "-g", "-k", "-b", "-t", "-w", "-l", "-v",
                             "--human", "--mega", "--giga", "--kilo", "--bytes", "--total",
                             "--wide", "--lohi", "--si", "--committed")),
    "uptime": Argv(flags=_opts("-p", "-s", "--pretty", "--since")),
    "uname": Argv(flags=_opts("-a", "-s", "-n", "-r", "-v", "-m", "-p", "-i", "-o",
                              "--all", "--kernel-name", "--nodename", "--kernel-release",
                              "--kernel-version", "--machine", "--processor",
                              "--hardware-platform", "--operating-system")),
    # имя хоста без позиционного — позиционный его меняет
    "hostname": Argv(flags=_opts("-f", "-s", "-i", "-I", "-d", "-A", "--fqdn", "--long",
                                 "--short", "--ip-address", "--all-ip-addresses",
                                 "--domain", "--all-fqdns")),
    # единственный позиционный — формат вывода `+...`; `MMDDhhmm` меняет время
    "date": Argv(flags=_opts("-u", "--utc", "--universal", "-R", "--rfc-email"),
                 valued={"-d": _any, "--date": _any, "-r": _word, "--reference": _word,
                         "--rfc-3339": _re(r"date|seconds|ns")},
                 positional=_upto(1, lambda p: p.startswith("+"))),
    "id": Argv(flags=_opts("-u", "-g", "-G", "-n", "-r", "-a", "-z", "--user", "--group",
                           "--groups", "--name", "--real", "--zero"),
               positional=_upto(1)),
    "nproc": Argv(flags=_opts("--all"), valued={"--ignore": _INT}),
    "echo": Argv(raw=True),
    "lsblk": Argv(flags=_opts("-a", "-b", "-d", "-f", "-l", "-m", "-n", "-p", "-J", "-S", "-t",
                              "-z", "-O", "--all", "--bytes", "--nodeps", "--fs", "--list",
                              "--perms", "--noheadings", "--paths", "--json", "--scsi",
                              "--topology", "--output-all"),
                  valued={"-o": _word, "--output": _word, "-e": _word, "--exclude": _word,
                          "-I": _word, "--include": _word},
                  positional=_paths),
    "lscpu": Argv(flags=_opts("-a", "-b", "-c", "-J", "-y", "-x", "--all", "--online",
                              "--offline", "--json", "--hex", "--physical")),
    "vmstat": Argv(flags=_opts("-a", "-f", "-m", "-n", "-s", "-d", "-D", "-t", "-w",
                               "--active", "--forks", "--slabs", "--one-header", "--stats",
                               "--disk", "--disk-sum", "--timestamp", "--wide"),
                   valued={"-S": _word, "--unit": _word, "-p": _word, "--partition": _word},
                   positional=_upto(2, _INT)),
    "iostat": Argv(flags=_opts("-c", "-d", "-h", "-k", "-m", "-N", "-s", "-t", "-x", "-y", "-z"),
                   valued={"-o": _re(r"JSON")},
                   positional=_each(_re(r"[A-Za-z0-9_-]+"))),
    "mpstat": Argv(flags=_opts("-A", "-u", "-T"),
                   valued={"-P": _word, "-I": _word, "-N": _word, "-o": _re(r"JSON")},
                   positional=_upto(2, _INT)),
    "ps": Argv(flags=_opts("-A", "-a", "-d", "-e", "-f", "-F", "-H", "-j", "-l", "-L", "-M",
                           "-T", "-w", "-x", "-y", "-c", "--forest", "--no-headers",
                           "--headers"),
               valued={"-o": _word, "-O": _word, "--format": _word, "-p": _word,
                       "--pid": _word, "-u": _word, "--user": _word, "-U": _word,
                       "-C": _word, "-g": _word, "-G": _word, "-t": _word, "-q": _word,
                       "--sort": _any},
               # BSD-опции (aux) без `e`: она печатает окружение процессов
               positional=_each(_re(r"[A-DF-Za-df-z]+"))),
    "top": Argv(flags=_opts("-b", "-c", "-H", "-i", "-S", "-1"),
                valued={"-n": _INT, "-d": _re(r"\d+(\.\d+)?"), "-o": _word, "-p": _word,
                        "-u": _word, "-U": _word, "-E": _re(r"[kmgtpe]"),
                        "-e": _re(r"[kmgtp]")}),
    "who": Argv(flags=_opts("-a", "-b", "-d", "-H", "-l", "-m", "-p", "-q", "-r", "-s", "-t",
                            "-T", "-u", "--all", "--boot", "--dead", "--heading", "--login",
                            "--lookup", "--process", "--count", "--runlevel", "--short",
                            "--time", "--users", "--mesg"),
                positional=_upto(2)),
    "w": Argv(flags=_opts("-h", "-s", "-f", "-i", "-o", "-u", "--no-header", "--short",
                          "--from", "--ip-addr", "--old-style", "--no-current"),
              positional=_upto(1)),
    # -D пишет кэш устройств, +m — файл mount supplement: ни того, ни `+`-опций
    "lsof": Argv(flags=_opts("-n", "-P", "-t", "-l", "-w", "-V", "-R", "-i", "-4", "-6",
                             "-U", "-a"),
                 valued={"-p": _word, "-u": _word, "-c": _word},
                 positional=_each(lambda p: not p.startswith("+"))),
    # -K закрывает сокеты, -D пишет дамп в файл, -N меняет netns
    "ss": Argv(flags=_opts("-t", "-u", "-l", "-n", "-p", "-a", "-e", "-m", "-o", "-i", "-s",
                           "-x", "-w", "-4", "-6", "-H", "-r", "-O", "-0",
                           "--tcp", "--udp", "--listening", "--numeric", "--processes",
                           "--all", "--extended", "--memory", "--options", "--info",
                           "--summary", "--unix", "--raw", "--no-header", "--resolve",
                           "--oneline", "--ipv4", "--ipv6", "--packet"),
               valued={"-f": _word, "--family": _word, "-A": _word, "--query": _word,
                       "--socket": _word},
               positional=_paths),  # выражение фильтра: state established '( dport = :443 )'
    "getent": Argv(flags=_opts("-i", "--no-idn"), valued={"-s": _word, "--service": _word},
                   positional=_getent),
    # -C/-S очищают и выставляют записи
    "lastlog": Argv(valued={"-u": _word, "--user": _word, "-b": _INT, "--before": _INT,
                            "-t": _INT, "--time": _INT}),
    # файлы и текст
    "ls": Argv(flags=_opts("-a", "-A", "-l", "-h", "-d", "-R", "-t", "-r", "-S", "-1", "-F",
                           "-i", "-n", "-L", "-Z", "-s", "-X", "-c", "-u", "-g", "-o", "-p",
                           "-Q", "-N", "-v", "-x", "-m", "-H", "-G",
                           "--all", "--almost-all", "--human-readable", "--directory",
                           "--recursive", "--full-time", "--inode", "--numeric-uid-gid",
                           "--dereference", "--classify", "--context",
                           "--group-directories-first", "--si", "--size", "--reverse"),
               valued={"--sort": _word, "--time": _word, "--time-style": _any,
                       "-I": _any, "--ignore": _any, "-w": _INT, "--width": _INT},
               positional=_paths),
    "stat": Argv(flags=_opts("-L", "-f", "-t", "--dereference", "--file-system", "--terse"),
                 valued={"-c": _any, "--format": _any, "--printf": _any},
                 positional=_paths),
    "cat": Argv(flags=_opts("-A", "-b", "-e", "-E", "-n", "-s", "-t", "-T", "-u", "-v",
                            "--number", "--show-all", "--number-nonblank", "--squeeze-blank",
                            "--show-ends", "--show-tabs", "--show-nonprinting"),
                positional=_paths, reads=True),
    "head": _HEAD_TAIL,
    "tail": _HEAD_TAIL,  # без -f/-F: слежение висит до таймаута
    "zcat": Argv(flags=_opts("-f", "-q", "-v", "-l", "-t"), positional=_paths, reads=True),
    "grep": _GREP,
    "egrep": _GREP,
    "wc": Argv(flags=_opts("-l", "-w", "-c", "-m", "-L", "--lines", "--words", "--bytes",
                           "--chars", "--max-line-length"),
               positional=_paths),
    "readlink": Argv(flags=_opts("-f", "-e", "-m", "-n", "-q", "-s", "-v", "-z",
                                 "--canonicalize", "--canonicalize-existing",
                                 "--canonicalize-missing"),
                     positional=_paths),
    "test": Argv(raw=True),
    # утилиты с подкомандами и режимами
    "find": _FIND,
    "openssl": _OPENSSL,
    # -C/-c чистят буфер, -D/-E/-n меняют консоль, -w следит
    "dmesg": Argv(flags=_opts("-T", "--ctime", "-t", "--notime", "-k", "--kernel", "-u",
                              "--userspace", "-x", "--decode", "-r", "--raw", "-d",
                              "--show-delta", "-e", "--reltime", "-L", "-P", "--nopager"),
                  valued={"-l": _re(r"[a-z,+]+"), "--level": _re(r"[a-z,+]+"),
                          "-f": _re(r"[a-z,]+"), "--facility": _re(r"[a-z,]+"),
                          "--since": _any, "--until": _any, "--time-format": _word}),
    "crontab": Argv(flags=_opts("-l"), valued={"-u": _word}, require=_opts("-l")),
    "iptables": _IPTABLES,
    "ip6tables": _IPTABLES,
    "ip": _IP,
    "systemctl": _SYSTEMCTL,
    # --vacuum-*, --rotate, --flush, --sync, --cursor-file (пишет файл), -f (следит) — нет
    "journalctl": Argv(
        flags=_opts("--no-pager", "-r", "--reverse", "-k", "--dmesg", "-b", "--boot", "-x",
                    "--catalog", "-e", "--pager-end", "-q", "--quiet", "-a", "--all", "-l",
                    "--full", "--utc", "--system", "--no-hostname", "--no-tail",
                    "--disk-usage", "--list-boots", "-m", "--merge"),
        valued={"-n": _INT, "--lines": _INT, "-u": _word, "--unit": _word,
                "--since": _any, "--until": _any, "-S": _any, "-U": _any,
                "-p": _re(r"[a-z0-9.]+"), "--priority": _re(r"[a-z0-9.]+"),
                "-o": _re(r"[a-z-]+"), "--output": _re(r"[a-z-]+"),
                "-t": _word, "--identifier": _word, "-g": _any, "--grep": _any},
        positional=_paths,  # совпадения FIELD=value
    ),
    "ufw": Argv(subcommands={
        "status": Argv(positional=_upto(1, lambda p: p in ("verbose", "numbered"))),
    }),
    "fail2ban-client": Argv(subcommands={
        "status": Argv(positional=_upto(1, _NAME)),
        "ping": Argv(),
        "get": Argv(positional=lambda ps: 1 <= len(ps) <= 2 and all(_NAME(p) for p in ps)),
    }),
    # только дамп/проверка конфига: без -T/-t sshd запустил бы демон, -E пишет лог
    "sshd": Argv(flags=_opts("-T", "-t"), valued={"-C": _word, "-f": _word},
                 require=_opts("-T", "-t")),
    "apt": _APT,
    "apt-get": _APT,
    "certbot": Argv(subcommands={
        "certificates": Argv(valued={"--cert-name": _word, "-d": _word, "--domains": _word}),
    }),
    # На ноде docker доступен только через ssh, сокета у нас там нет.
    "docker": _DOCKER,
    # только проверка/дамп конфига и версия: без них nginx запустил бы мастер,
    # -s шлёт сигнал, -g добавляет директивы (load_module), -c/-p подменяют конфиг
    "nginx": Argv(flags=_opts("-t", "-T", "-q", "-v", "-V"),
                  require=_opts("-t", "-T", "-v", "-V")),
    "git": _GIT,
}

# Всё, что вообще может быть признано читающим. Скоуп скила — подмножество отсюда.
KNOWN_BINARIES = frozenset(_SPECS)


def is_read_only(command: list[str], binaries: frozenset[str]) -> bool:
    """Читает ли команда, не меняя состояния, в пределах разрешённых бинарников.

    Принимается только один argv разрешённой утилиты. Оболочки (`sh -c`, `bash -c`)
    в автоматический путь не попадают ни с какими аргументами: разбор shell-программы
    строковым парсером обходился переводом строки и редиректами (аудит 2026-09-12, F01).
    Скрипт с пайпами — только через инструмент с подтверждением.
    """
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary not in binaries:
        return False
    spec = _SPECS.get(binary)
    files: list[str] = []
    return spec is not None and _valid(spec, args, files=files) and not any(map(_secret, files))


def reads_secret(command: list[str]) -> bool:
    spec = _SPECS.get(command[0]) if command else None
    files: list[str] = []
    return spec is not None and _valid(spec, command[1:], files=files) and any(map(_secret, files))


# Куда идти вместо отбитой подкоманды: (утилита, подкоманда) → адрес на этом хосте
# и адрес на ноде. Отказ без адреса модель читает как «сюда нельзя вообще» и
# перебирает формы вызова: `docker inspect` отбивался 8 раз за день у агентов,
# которым `docker_inspect` был выдан (живой разбор 21.09.2026). Для ноды эти
# инструменты не ответ — они ходят в локальный демон, и одноимённый контейнер на
# хосте молча отдал бы чужие данные.
_ELSEWHERE = {
    ("docker", "inspect"): (
        "`inspect` печатает Config.Env с паролями, поэтому здесь его нет. То же самое, "
        "но без Env, отдаёт инструмент `docker_inspect` (скил `docker`); полный вывод — "
        "только через shell_exec с подтверждением.",
        "`inspect` печатает Config.Env с паролями, поэтому здесь его нет. Локальные "
        "инструменты `docker_*` до ноды не достают — полный inspect только через "
        "ssh_exec с подтверждением; состояние, образ и порты видны в `docker ps --no-trunc`.",
    ),
    ("docker", "exec"): (
        "команду внутри контейнера выполняет инструмент `docker_exec` (с подтверждением). "
        "Конфиги обычно примонтированы с хоста — путь виден в Mounts у `docker_inspect`, "
        "и тогда файл читается обычным `cat`.",
        "команда внутри контейнера на ноде — только через ssh_exec с подтверждением. "
        "Конфиги обычно примонтированы с ноды — путь можно взять из её compose-файла "
        "и прочитать обычным `cat`.",
    ),
    **{("getent", db): (
        "резолв имени — DNS-запрос наружу: без подтверждения только для хостов из "
        "NETWORK_ALLOWED. Хост свой — shell_exec с подтверждением, а постоянно — пусть "
        "владелец добавит его в NETWORK_ALLOWED.",
        "резолв имени — DNS-запрос наружу: без подтверждения только для хостов из "
        "NETWORK_ALLOWED. Хост свой — ssh_exec с подтверждением.",
    ) for db in _GETENT_DNS},
    **{("getent", db): (
        "это хэши паролей — только через shell_exec с подтверждением.",
        "это хэши паролей — только через ssh_exec с подтверждением.",
    ) for db in ("shadow", "gshadow")},
}


def _elsewhere(command: list[str], exec_tool: str) -> str:
    sub = next((a for a in command[1:] if not a.startswith("-")), "")
    hint = _ELSEWHERE.get((command[0] if command else "", sub))
    return "" if hint is None else hint[1 if exec_tool == "ssh_exec" else 0]


def refusal(command: list[str], binaries: frozenset[str], exec_tool: str = "shell_exec") -> dict:
    if reads_secret(command):
        return {"command": command,
                "error": f"секретный файл — только через {exec_tool} с подтверждением"}
    binary = command[0] if command else ""
    spec = _SPECS.get(binary)
    if binary in binaries and spec is not None:
        # Утилита разрешена, споткнулись на аргументе — называем именно его, иначе
        # модель чинит не то место и вместо одного вызова делает десять.
        why: list[str] = []
        _valid(spec, command[1:], why=why)
        # Есть адрес — ведём туда; иначе общий совет про лишний аргумент.
        fix = _elsewhere(command, exec_tool) or (
            "Остальная команда в порядке — убери или замени этот аргумент. "
            "Это ограничение на опции, а не на текст: `|` внутри шаблона "
            "(grep -E, journalctl -g) работает, перебирать по одному слову не нужно. "
            f"Для изменяющих операций используй {exec_tool} (с подтверждением)."
        )
        return {
            "command": command,
            "error": f"утилита `{binary}` доступна, но {why[0] if why else 'аргументы не приняты'}. {fix}",
        }
    return {
        "command": command,
        "error": f"утилита `{binary}` недоступна этому агенту "
                 f"(доступны: {', '.join(sorted(binaries))}). "
                 "Принимается одна команда argv без sh -c, пайпов и редиректов — "
                 "независимые проверки делай отдельными вызовами. "
                 f"Для изменяющих операций используй {exec_tool} (с подтверждением).",
    }


def build_host_tools(access: HostAccess) -> list[Tool]:
    """Инструменты доступа к хосту по объединённому доступу выданных скилов."""
    if not access.binaries and not access.exec_allowed:
        return []

    async def host_query(command: list[str]) -> dict:
        if not is_read_only(command, access.binaries):
            return refusal(command, access.binaries)
        return await host_exec(command)

    tools = []
    if access.binaries:
        allowed = ", ".join(sorted(access.binaries))
        tools.append(Tool(
            "host_query",
            "Run ONE READ-ONLY command argv on the HOST via nsenter. Allowed binaries: "
            f"{allowed}. No shell: `sh -c`, pipes, redirects and globs are refused — "
            "make several calls instead (they run in parallel). "
            "Safe, auto-executed. For anything that changes state use shell_exec.",
            ShellParams, host_query, Safety.SAFE,
        ))
    if access.exec_allowed:
        tools.append(Tool(
            "shell_exec",
            "Run a state-changing command on the HOST via nsenter (DESTRUCTIVE): "
            "service restarts, certificate renewal, firewall changes, package installs. "
            "Requires user confirmation.",
            ShellParams, host_exec, Safety.DANGEROUS,
        ))
    return tools
