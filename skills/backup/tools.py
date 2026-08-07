import re
import shlex

from pydantic import BaseModel, Field
from aiodocker import Docker

from app.tools.base import Tool, Safety
from app.tools.docker import host_shell
from app.config import settings


class SiteParams(BaseModel):
    site: str = Field(description="имя каталога сайта под /opt")


class ArchiveParams(BaseModel):
    archive: str = Field(description="имя файла архива в каталоге бэкапов, например shop-20260712-030000.tar.gz")


class NoParams(BaseModel):
    pass


_DB_ENGINES = {"postgres": "postgres", "mysql": "mysql", "mariadb": "mysql"}
_ARCHIVE_RE = re.compile(r"^(?P<site>[A-Za-z0-9_-]+)-(?P<ts>\d{8}-\d{6})\.tar\.gz$")


def _allowed() -> set[str]:
    return {s.strip() for s in settings.backup_allowed.split(",") if s.strip()}


def _valid_site(site: str) -> bool:
    return bool(site) and "/" not in site and ".." not in site


async def _detect_db(site: str) -> dict | None:
    """Найти запущенный контейнер БД compose-проекта сайта (по label + образу)."""
    async with Docker() as docker:
        containers = await docker.containers.list()
        for c in containers:
            info = c._container
            labels = info.get("Labels") or {}
            if labels.get("com.docker.compose.project") != site:
                continue
            image = (info.get("Image") or "").lower()
            for token, engine in _DB_ENGINES.items():
                if token in image:
                    name = (info.get("Names") or ["?"])[0].lstrip("/")
                    return {"container": name, "engine": engine}
    return None


def _dump_command(db: dict) -> str:
    """Команда дампа внутри контейнера БД, берущая креды из его собственного окружения."""
    c = shlex.quote(db["container"])
    if db["engine"] == "postgres":
        inner = 'pg_dumpall -U "${POSTGRES_USER:-postgres}"'
    else:
        inner = 'mysqldump -u root -p"${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD}}" --all-databases'
    return f"docker exec {c} sh -c {shlex.quote(inner)}"


def _backup_script(site: str, db: dict | None) -> str:
    d = shlex.quote(settings.backup_dir)
    s = shlex.quote(site)
    keep = int(settings.backup_keep)
    lines = [
        "set -e",
        f"mkdir -p {d}",
        "ts=$(date +%Y%m%d-%H%M%S)",
        f"tar czf {d}/{s}-$ts.tar.gz -C /opt {s}",
    ]
    if db:
        lines.append(f"{_dump_command(db)} | gzip > {d}/{s}-$ts.{db['engine']}.sql.gz")
    # ротация: оставить последние KEEP архивов и дампов
    for pat in (f"{s}-*.tar.gz", f"{s}-*.sql.gz"):
        lines.append(
            f"ls -1t {d}/{pat} 2>/dev/null | tail -n +{keep + 1} | xargs -r rm -f"
        )
    lines.append(f"ls -1t {d}/{s}-* 2>/dev/null")
    return "\n".join(lines)


async def backup_list() -> dict:
    d = shlex.quote(settings.backup_dir)
    res = await host_shell(f"ls -lh {d} 2>/dev/null || echo '(нет каталога бэкапов)'")
    return {"dir": settings.backup_dir, "listing": (res.get("stdout") or "").strip()}


async def backup_plan(site: str) -> dict:
    if not _valid_site(site):
        return {"error": f"невалидное имя сайта: {site!r}"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (BACKUP_ALLOWED)", "allowed": sorted(_allowed())}
    db = await _detect_db(site)
    return {
        "site": site,
        "source": f"/opt/{site}",
        "dest_dir": settings.backup_dir,
        "archive": f"{site}-<ts>.tar.gz",
        "database": db or "не обнаружена (будут только файлы)",
        "keep": int(settings.backup_keep),
    }


async def backup_run(site: str) -> dict:
    if not _valid_site(site):
        return {"error": f"невалидное имя сайта: {site!r}"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (BACKUP_ALLOWED)", "allowed": sorted(_allowed())}
    db = await _detect_db(site)
    res = await host_shell(_backup_script(site, db))
    return {
        "site": site,
        "database": db,
        "returncode": res.get("returncode"),
        "stdout": res.get("stdout"),
        "stderr": res.get("stderr"),
    }


def _parse_archive(archive: str) -> str | None:
    m = _ARCHIVE_RE.match(archive)
    return m.group("site") if m else None


async def restore_plan(archive: str) -> dict:
    site = _parse_archive(archive)
    if site is None:
        return {"error": f"невалидное имя архива: {archive!r} (ожидается <site>-<YYYYMMDD-HHMMSS>.tar.gz)"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (BACKUP_ALLOWED)", "allowed": sorted(_allowed())}
    db = await _detect_db(site)
    return {
        "archive": archive,
        "site": site,
        "target": f"/opt/{site}",
        "warning": "файлы в /opt/{0} будут перезаписаны".format(site),
        "database_restore": db or "нет запущенного контейнера БД — дамп восстановлен не будет",
    }


def _restore_script(archive: str, site: str, db: dict | None) -> str:
    d = shlex.quote(settings.backup_dir)
    a = shlex.quote(archive)
    s = shlex.quote(site)
    engine = db["engine"] if db else ""
    lines = [
        "set -e",
        f"tar xzf {d}/{a} -C /opt",
    ]
    if db:
        c = shlex.quote(db["container"])
        dump = f"{d}/{s}-$(echo {a} | sed -E 's/^{s}-(.*)\\.tar\\.gz$/\\1/').{engine}.sql.gz"
        if db["engine"] == "postgres":
            inner = 'psql -U "${POSTGRES_USER:-postgres}"'
        else:
            inner = 'mysql -u root -p"${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD}}"'
        lines.append(
            f"[ -f {dump} ] && gunzip -c {dump} | docker exec -i {c} sh -c {shlex.quote(inner)} || "
            f"echo 'дамп БД не найден рядом с архивом — восстановлены только файлы'"
        )
    return "\n".join(lines)


async def restore_run(archive: str) -> dict:
    site = _parse_archive(archive)
    if site is None:
        return {"error": f"невалидное имя архива: {archive!r}"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (BACKUP_ALLOWED)", "allowed": sorted(_allowed())}
    db = await _detect_db(site)
    res = await host_shell(_restore_script(archive, site, db))
    return {
        "archive": archive,
        "site": site,
        "database": db,
        "returncode": res.get("returncode"),
        "stdout": res.get("stdout"),
        "stderr": res.get("stderr"),
    }


def build_tools() -> list[Tool]:
    return [
        Tool("backup_list", "List existing backups in BACKUP_DIR (read-only).", NoParams, backup_list, Safety.SAFE),
        Tool("backup_plan", "Show what a site backup would include: source dir, detected DB container, destination (SAFE, no changes).", SiteParams, backup_plan, Safety.SAFE),
        Tool("backup_run", "Create a backup of a site under /opt (files tar + DB dump if detected) into BACKUP_DIR with rotation (DESTRUCTIVE: writes to host). Requires user confirmation.", SiteParams, backup_run, Safety.DANGEROUS),
        Tool("restore_plan", "Show what restoring an archive would overwrite (SAFE, no changes).", ArchiveParams, restore_plan, Safety.SAFE),
        Tool("restore_run", "Restore a site from an archive: extract files to /opt and restore DB dump (DESTRUCTIVE: overwrites files and database). Requires user confirmation.", ArchiveParams, restore_run, Safety.DANGEROUS),
    ]
