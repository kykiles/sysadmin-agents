from types import SimpleNamespace
from unittest.mock import AsyncMock
import app.skills.deploy.tools as dt
from app.tools.base import Safety


def _allow(monkeypatch, value):
    monkeypatch.setattr(dt, "settings", SimpleNamespace(deploy_allowed=value))


def test_choose_method_prefers_script():
    assert dt._choose_method(True, True, True) == ("script", "bash deployments/deploy.sh")


def test_choose_method_make_then_compose():
    assert dt._choose_method(False, True, True) == ("make", "make deploy")
    assert dt._choose_method(False, False, True) == (
        "compose", "git pull --ff-only && docker compose up -d --build")


def test_choose_method_none():
    assert dt._choose_method(False, False, False) == ("none", "")


def test_valid_site():
    assert dt._valid_site("glowshine") is True
    assert dt._valid_site("") is False
    assert dt._valid_site("a/b") is False
    assert dt._valid_site("..") is False


async def test_deploy_plan_rejects_not_allowed(monkeypatch):
    _allow(monkeypatch, "glowshine")
    called = False
    async def _fake_detect(d):
        nonlocal called
        called = True
        return ("script", "x")
    monkeypatch.setattr(dt, "_detect", _fake_detect)
    res = await dt.deploy_plan("remnawave")
    assert "error" in res
    assert called is False


async def test_deploy_plan_returns_method(monkeypatch):
    _allow(monkeypatch, "glowshine")
    async def _fake_detect(d):
        return ("script", "bash deployments/deploy.sh")
    monkeypatch.setattr(dt, "_detect", _fake_detect)
    res = await dt.deploy_plan("glowshine")
    assert res["site"] == "glowshine"
    assert res["dir"] == "/opt/glowshine"
    assert res["method"] == "script"
    assert res["command"] == "bash deployments/deploy.sh"


async def test_deploy_run_builds_nsenter_command(monkeypatch):
    _allow(monkeypatch, "glowshine")
    async def _fake_detect(d):
        return ("script", "bash deployments/deploy.sh")
    monkeypatch.setattr(dt, "_detect", _fake_detect)
    fake_shell = AsyncMock(return_value={
        "command": [], "returncode": 0, "stdout": "done", "stderr": ""})
    monkeypatch.setattr(dt, "shell_exec", fake_shell)
    res = await dt.deploy_run("glowshine")
    argv = fake_shell.call_args.args[0]
    assert argv[:8] == ["nsenter", "-t", "1", "-m", "-p", "--", "bash", "-lc"]
    assert argv[8] == "cd /opt/glowshine && bash deployments/deploy.sh"
    assert res["returncode"] == 0
    assert res["stdout"] == "done"
    assert res["method"] == "script"


async def test_deploy_run_rejects_not_allowed(monkeypatch):
    _allow(monkeypatch, "glowshine")
    fake_shell = AsyncMock()
    monkeypatch.setattr(dt, "shell_exec", fake_shell)
    res = await dt.deploy_run("remnawave")
    assert "error" in res
    fake_shell.assert_not_called()


def test_build_tools_safety():
    by_name = {t.name: t for t in dt.build_tools()}
    assert by_name["deploy_plan"].safety is Safety.SAFE
    assert by_name["deploy_run"].safety is Safety.DANGEROUS
