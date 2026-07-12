from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.skills.backup.tools as bt
from app.tools.base import Safety


def _settings(monkeypatch, allowed="shop", keep=7, dir="/var/backups/sysadmin"):
    monkeypatch.setattr(bt, "settings", SimpleNamespace(
        backup_allowed=allowed, backup_dir=dir, backup_keep=keep))


def test_valid_site():
    assert bt._valid_site("shop") is True
    assert bt._valid_site("") is False
    assert bt._valid_site("a/b") is False
    assert bt._valid_site("..") is False


def test_parse_archive():
    assert bt._parse_archive("shop-20260712-030000.tar.gz") == "shop"
    assert bt._parse_archive("shop.tar.gz") is None
    assert bt._parse_archive("../etc/passwd") is None
    assert bt._parse_archive("shop-20260712-030000.txt") is None


def test_dump_command_postgres_and_mysql():
    pg = bt._dump_command({"container": "shop-db-1", "engine": "postgres"})
    assert "pg_dumpall" in pg and "shop-db-1" in pg
    my = bt._dump_command({"container": "shop-db-1", "engine": "mysql"})
    assert "mysqldump" in my


def test_backup_script_includes_tar_dump_and_rotation(monkeypatch):
    _settings(monkeypatch, keep=3)
    script = bt._backup_script("shop", {"container": "shop-db-1", "engine": "postgres"})
    assert "tar czf /var/backups/sysadmin/shop-$ts.tar.gz -C /opt shop" in script
    assert "pg_dumpall" in script
    assert "tail -n +4" in script  # keep=3 → оставить 3, удалить с 4-й


def test_backup_script_files_only_when_no_db(monkeypatch):
    _settings(monkeypatch)
    script = bt._backup_script("shop", None)
    assert "tar czf" in script
    assert "pg_dumpall" not in script and "mysqldump" not in script


async def test_backup_plan_rejects_not_allowed(monkeypatch):
    _settings(monkeypatch, allowed="shop")
    detect = AsyncMock()
    monkeypatch.setattr(bt, "_detect_db", detect)
    res = await bt.backup_plan("other")
    assert "error" in res
    detect.assert_not_called()


async def test_backup_plan_reports_db(monkeypatch):
    _settings(monkeypatch, allowed="shop")
    async def fake_detect(site):
        return {"container": "shop-db-1", "engine": "postgres"}
    monkeypatch.setattr(bt, "_detect_db", fake_detect)
    res = await bt.backup_plan("shop")
    assert res["site"] == "shop"
    assert res["source"] == "/opt/shop"
    assert res["database"]["engine"] == "postgres"


async def test_backup_run_rejects_not_allowed(monkeypatch):
    _settings(monkeypatch, allowed="shop")
    shell = AsyncMock()
    monkeypatch.setattr(bt, "host_shell", shell)
    monkeypatch.setattr(bt, "_detect_db", AsyncMock())
    res = await bt.backup_run("other")
    assert "error" in res
    shell.assert_not_called()


async def test_restore_plan_rejects_bad_archive(monkeypatch):
    _settings(monkeypatch, allowed="shop")
    res = await bt.restore_plan("../evil")
    assert "error" in res


async def test_restore_run_runs_shell_for_allowed(monkeypatch):
    _settings(monkeypatch, allowed="shop")
    async def fake_detect(site):
        return None
    monkeypatch.setattr(bt, "_detect_db", fake_detect)
    shell = AsyncMock(return_value={"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(bt, "host_shell", shell)
    res = await bt.restore_run("shop-20260712-030000.tar.gz")
    assert res["site"] == "shop"
    argv_script = shell.call_args.args[0]
    assert "tar xzf" in argv_script


def test_build_tools_safety():
    by_name = {t.name: t for t in bt.build_tools()}
    assert by_name["backup_list"].safety is Safety.SAFE
    assert by_name["backup_plan"].safety is Safety.SAFE
    assert by_name["backup_run"].safety is Safety.DANGEROUS
    assert by_name["restore_plan"].safety is Safety.SAFE
    assert by_name["restore_run"].safety is Safety.DANGEROUS
