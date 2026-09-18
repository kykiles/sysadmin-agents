import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from pydantic import ValidationError

from app.tools import docker as dk


async def test_docker_ps_safe():
    fake_container = MagicMock()
    fake_container._container = {"Id": "abc", "Names": ["bot"], "State": "running", "Image": "img", "Status": "Up"}
    fake = MagicMock()
    fake.containers = MagicMock()
    fake.containers.list = AsyncMock(return_value=[fake_container])
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = await dk.docker_ps()
    assert out["containers"][0]["Names"] == ["bot"]
    assert out["containers"][0]["State"] == "running"


async def test_docker_logs():
    c = MagicMock()
    c.log = AsyncMock(return_value=["line1\n", "line2\n"])
    fake = MagicMock()
    fake.containers = MagicMock()
    fake.containers.container = MagicMock(return_value=c)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = await dk.docker_logs(container="bot", tail=2)
    assert out["logs"] == "line1\nline2\n"


async def test_docker_inspect_projects_and_omits_env():
    c = MagicMock()
    c.show = AsyncMock(return_value={
        "Id": "abc",
        "Name": "/bot",
        "RestartCount": 3,
        "State": {"Status": "running", "Running": True, "ExitCode": 0, "Pid": 42},
        "Config": {"Image": "img", "Env": ["SECRET=shh"]},
        "HostConfig": {"RestartPolicy": {"Name": "always"}},
        "NetworkSettings": {"Ports": {"80/tcp": None}},
        "Mounts": [],
    })
    fake = MagicMock()
    fake.containers = MagicMock()
    fake.containers.container = MagicMock(return_value=c)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = await dk.docker_inspect(container="bot")
    insp = out["inspect"]
    assert insp["Image"] == "img"
    assert insp["State"] == {"Status": "running", "Running": True, "ExitCode": 0}
    assert insp["RestartCount"] == 3
    assert "Env" not in json.dumps(insp)


def test_project_stats_compact():
    out = dk._project_stats({
        "memory_stats": {"usage": 100, "limit": 1000, "stats": {"a": 1}},
        "cpu_stats": {"cpu_usage": {"total_usage": 5}, "system_cpu_usage": 50, "online_cpus": 2},
        "pids_stats": {"current": 7},
    })
    assert out == {
        "memory_usage": 100, "memory_limit": 1000,
        "cpu_total_usage": 5, "system_cpu_usage": 50, "online_cpus": 2, "pids": 7,
    }


async def test_docker_restart_dangerous():
    c = MagicMock()
    c.restart = AsyncMock(return_value=None)
    fake = MagicMock()
    fake.containers = MagicMock()
    fake.containers.container = MagicMock(return_value=c)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = await dk.docker_restart(container="bot")
    assert out["restarted"] == "bot"


async def test_compose_ls(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "docker-compose.yml").write_text("x: 1")
    with patch("app.tools.docker.os.listdir", return_value=["web"]):
        with patch("app.tools.docker.os.path.isdir", return_value=True):
            with patch("app.tools.docker.os.path.isfile", return_value=True):
                out = await dk.compose_ls()
    assert out["projects"] == ["web"]


async def test_run_subprocess_timeout_kills():
    proc = MagicMock()

    async def _hang(*a, **k):
        await asyncio.sleep(10)

    proc.communicate = _hang
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    with patch("app.tools.docker.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        out = await dk._run_subprocess(["sleep", "10"], timeout=0.05)
    assert out["timed_out"] is True
    assert out["returncode"] is None
    proc.kill.assert_called_once()


async def test_compose_up_runs_subprocess(monkeypatch):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    with patch("app.tools.docker.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as m:
        with patch("app.tools.docker._project_dir", return_value="/fake/project"):
            out = await dk.compose_up(project="web")
    assert out["returncode"] == 0
    args = m.call_args.args
    assert "up" in args and "-d" in args


def _docker_with_show(show):
    c = MagicMock()
    c.show = show
    fake = MagicMock()
    fake.containers = MagicMock()
    fake.containers.container = MagicMock(return_value=c)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    return fake


async def test_container_missing_reports_404_only():
    from aiodocker import DockerError

    missing = _docker_with_show(AsyncMock(side_effect=DockerError(404, {"message": "No such container"})))
    with patch("app.tools.docker.Docker", return_value=missing):
        assert "не найден" in await dk.container_missing({"container": "remna_pg_1"})
    present = _docker_with_show(AsyncMock(return_value={"Id": "abc"}))
    with patch("app.tools.docker.Docker", return_value=present):
        assert await dk.container_missing({"container": "glowshine_postgres"}) is None
    broken = _docker_with_show(AsyncMock(side_effect=DockerError(500, {"message": "daemon"})))
    with patch("app.tools.docker.Docker", return_value=broken):
        assert await dk.container_missing({"container": "x"}) is None


async def test_compose_up_build_adds_flag_and_long_timeout(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "build_timeout_seconds", 900)
    run = AsyncMock(return_value={"returncode": 0, "stdout": "", "stderr": ""})
    with patch("app.tools.docker._run_subprocess", run), \
            patch("app.tools.docker._project_dir", return_value="/fake/project"):
        await dk.compose_up(project="web", build=True)
        assert run.await_args.args[0][-3:] == ["up", "-d", "--build"]
        assert run.await_args.kwargs["timeout"] == 900
        await dk.compose_up(project="web")
        assert run.await_args.args[0][-2:] == ["up", "-d"]
        assert run.await_args.kwargs["timeout"] is None


async def test_container_guard_refuses_own_container_by_name_or_label():
    own = _docker_with_show(AsyncMock(return_value={"Name": "/sysadmin-agents"}))
    with patch("app.tools.docker.Docker", return_value=own):
        assert "самой системы" in await dk.container_guard({"container": "5d34b580498d"})
    labelled = _docker_with_show(AsyncMock(return_value={
        "Name": "/renamed", "Config": {"Labels": {"com.docker.compose.project": "sysadmin-agents"}}}))
    with patch("app.tools.docker.Docker", return_value=labelled):
        assert "самой системы" in await dk.container_guard({"container": "renamed"})
    other = _docker_with_show(AsyncMock(return_value={
        "Name": "/glowshine_bot", "Config": {"Labels": {"com.docker.compose.project": "remnabot"}}}))
    with patch("app.tools.docker.Docker", return_value=other):
        assert await dk.container_guard({"container": "glowshine_bot"}) is None


async def test_container_guard_still_reports_missing():
    from aiodocker import DockerError

    missing = _docker_with_show(AsyncMock(side_effect=DockerError(404, {"message": "No such container"})))
    with patch("app.tools.docker.Docker", return_value=missing):
        assert "не найден" in await dk.container_guard({"container": "x"})


async def test_project_guard_refuses_own_project():
    assert "самой системы" in await dk.project_guard({"project": "sysadmin-agents"})
    assert await dk.project_guard({"project": "remnabot"}) is None


@pytest.mark.parametrize("project", ["sysadmin-agents/", "../etc", "a/b", "/etc", "..", "", "-p"])
def test_project_name_rejected(project):
    with pytest.raises(ValidationError):
        dk.ComposeUpParams(project=project)
    with pytest.raises(ValidationError):
        dk.ProjectParams(project=project)


@pytest.mark.parametrize("project", ["remnabot", "web", "glowshine_app", "app.v2"])
def test_project_name_accepted(project):
    assert dk.ComposeUpParams(project=project).project == project
