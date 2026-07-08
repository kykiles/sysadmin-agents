import json
from unittest.mock import AsyncMock, patch, MagicMock
from app.tools import docker as dk
from app.tools.base import Safety


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


async def test_build_sysadmin_tools_classifications():
    tools = dk.build_sysadmin_tools()
    by_name = {t.name: t for t in tools}
    assert by_name["docker_ps"].safety is Safety.SAFE
    assert by_name["docker_logs"].safety is Safety.SAFE
    assert by_name["docker_restart"].safety is Safety.DANGEROUS
    assert by_name["compose_up"].safety is Safety.DANGEROUS
    assert by_name["docker_exec"].safety is Safety.DANGEROUS
    assert "shell_exec" not in by_name


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
